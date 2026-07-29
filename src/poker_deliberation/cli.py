"""Dependency-light command-line interface."""

from __future__ import annotations

import argparse
import importlib.metadata
import importlib.util
import json
import os
import platform
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pydantic

from poker_deliberation import __version__
from poker_deliberation.agents import ROLE_CATALOG
from poker_deliberation.approval_canonical import (
    parse_canonical_model as parse_approval_model,
)
from poker_deliberation.approval_models import (
    ApprovalDecisionBatch,
    ApprovalDecisionItemV2,
    ApprovalReissueBatchV2,
    DecisionValue,
)
from poker_deliberation.approvals import (
    ApprovalDecisionValidationError,
    LocalCliAuthorityProvider,
    read_approval_state_v2,
)
from poker_deliberation.capabilities import capability_snapshot
from poker_deliberation.config import AppConfig
from poker_deliberation.confirmed_review import (
    admit_confirmed_review,
    create_review_confirmation,
    prepare_review_intake,
    review_confirmed_intake,
)
from poker_deliberation.confirmed_review_models import (
    MAX_CONFIRMED_REVIEW_ARTIFACT_BYTES,
    MAX_CONFIRMED_REVIEW_SOURCE_BYTES,
    ReviewConfirmationAuthorityV1,
    ReviewIntakeConfirmationV1,
    ReviewIntakePreparationResultV1,
)
from poker_deliberation.normalization import (
    MAX_SOURCE_BYTES,
    NormalizationDiagnosticCode,
    normalization_transport,
    normalize_hand_bytes,
)
from poker_deliberation.orchestrator import Orchestrator
from poker_deliberation.providers import LocalProvider, OpenAIAgentsProvider
from poker_deliberation.reporting import render_markdown, render_summary
from poker_deliberation.roadmap import roadmap_summary
from poker_deliberation.schemas import CanonicalHand, CaseInput, Claim, EpistemicLabel, FinalReport
from poker_deliberation.security import redact_sensitive
from poker_deliberation.storage.revision_canonical import (
    canonical_json_bytes,
    parse_canonical_model,
)
from poker_deliberation.tools import default_registry

_DEFAULT_RESUME_REASON = "human decision recorded by CLI"
_REPORT_FORMATS = ("json", "markdown", "summary")


def _configure_output() -> None:
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if reconfigure is not None:
        reconfigure(encoding="utf-8")


def _read_json(path: str) -> dict[str, Any]:
    def reject_non_finite_constant(value: str) -> None:
        raise ValueError(f"non-finite JSON number is not allowed: {value}")

    value = json.loads(
        Path(path).read_text(encoding="utf-8"),
        parse_constant=reject_non_finite_constant,
    )
    if not isinstance(value, dict):
        raise ValueError("JSON input must be an object")
    return value


def _read_limited_json(path: str, limit: int) -> dict[str, Any]:
    data = _read_limited_bytes(path, limit)

    def reject_non_finite_constant(value: str) -> None:
        raise ValueError(f"non-finite JSON number is not allowed: {value}")

    value = json.loads(
        data.decode("utf-8", errors="strict"),
        parse_constant=reject_non_finite_constant,
    )
    if not isinstance(value, dict):
        raise ValueError("JSON input must be an object")
    return value


def _read_limited_bytes(path: str, limit: int) -> bytes:
    source = Path(path)
    if source.stat().st_size > limit:
        raise ValueError("CRI_E_SOURCE_SIZE")
    with source.open("rb") as stream:
        value = stream.read(limit + 1)
    if len(value) > limit:
        raise ValueError("CRI_E_SOURCE_SIZE")
    return value


def _write_canonical_model(path: str, value: Any) -> None:
    Path(path).write_bytes(canonical_json_bytes(value))


def _parse_cli_datetime(value: str | None) -> datetime | None:
    if value is None:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("CRI_E_CONFIRMATION_BINDING")
    return parsed


def _emit(value: Any, format_name: str) -> None:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    if format_name == "json":
        print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False))
    else:
        if isinstance(value, str):
            print(value)
        else:
            serialized = json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False)
            print(f"```json\n{serialized}\n```")


def doctor() -> dict[str, Any]:
    agents_provider = OpenAIAgentsProvider().availability()
    local_provider = LocalProvider().availability()
    project_files = [
        "AGENTS.md",
        ".codex/config.toml",
        "tools/manifest.yaml",
        "pyproject.toml",
    ]
    return {
        "status": "ok",
        "framework_version": __version__,
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "pydantic": pydantic.__version__,
        "openai_api_key_present": bool(os.getenv("OPENAI_API_KEY")),
        "openai_agents": agents_provider.model_dump(mode="json"),
        "providers": {
            "local": local_provider.model_dump(mode="json"),
            "openai_agents": agents_provider.model_dump(mode="json"),
        },
        "pytest_installed": importlib.util.find_spec("pytest") is not None,
        "ruff_installed": importlib.util.find_spec("ruff") is not None,
        "mypy_installed": importlib.util.find_spec("mypy") is not None,
        "project_files_scope": "current_working_directory",
        "project_files": {path: Path(path).exists() for path in project_files},
        "local_calculators": default_registry().names(),
        "capabilities": capability_snapshot(),
        "roadmap": roadmap_summary(),
        "external_solver": "unavailable",
        "notes": [
            "Doctor status 'ok' means diagnostics completed; disabled or unavailable "
            "capabilities remain listed.",
            "Local calculators and schema validation do not require an API key.",
            "LocalProvider does not generate specialist prose.",
            "OpenAIAgentsProvider outbound analyze is not implemented, even when SDK and "
            "key are present.",
            "No user data is sent externally by the MVP.",
        ],
    }


def _case_from_hand_file(path: str) -> CaseInput:
    source = Path(path)
    if source.suffix.lower() == ".json":
        data = _read_json(path)
        if "kind" in data:
            data.setdefault("analysis_scope", "retrospective")
            return CaseInput.model_validate(data)
        return CaseInput(
            kind="hand",
            hand=CanonicalHand.model_validate(data),
            analysis_scope="retrospective",
        )
    if source.stat().st_size > MAX_SOURCE_BYTES:
        raise ValueError(NormalizationDiagnosticCode.SOURCE_TOO_LARGE.value)
    with source.open("rb") as stream:
        source_bytes = stream.read(MAX_SOURCE_BYTES + 1)
    if len(source_bytes) > MAX_SOURCE_BYTES:
        raise ValueError(NormalizationDiagnosticCode.SOURCE_TOO_LARGE.value)
    normalized = normalize_hand_bytes(source_bytes)
    fatal_without_safe_text = {
        NormalizationDiagnosticCode.UTF8,
        NormalizationDiagnosticCode.SECRET_SHAPE,
    }
    fatal = next(
        (
            diagnostic
            for diagnostic in normalized.diagnostics
            if diagnostic.code in fatal_without_safe_text
        ),
        None,
    )
    if fatal is not None:
        raise ValueError(fatal.code.value)
    raw_text = source_bytes.decode("utf-8", errors="strict")
    return CaseInput(
        kind="hand",
        raw_text=raw_text,
        hand=normalized.hand,
        analysis_scope="retrospective",
        metadata=normalization_transport(normalized),
    )


def _case_from_strategy_file(path: str) -> CaseInput:
    source = Path(path)
    if source.suffix.lower() == ".json":
        data = _read_json(path)
        if "kind" in data:
            data.setdefault("analysis_scope", "retrospective")
            return CaseInput.model_validate(data)
        return CaseInput(
            kind="strategy",
            raw_text=json.dumps(data, ensure_ascii=False),
            analysis_scope="retrospective",
        )
    return CaseInput(
        kind="strategy",
        raw_text=source.read_text(encoding="utf-8"),
        analysis_scope="retrospective",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="poker-deliberate")
    parser.add_argument("--version", action="version", version=__version__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    doctor_parser = subparsers.add_parser("doctor")
    doctor_parser.add_argument("--format", choices=["json", "markdown"], default="json")

    prepare_intake = subparsers.add_parser("prepare-review-intake")
    prepare_intake.add_argument("--source", required=True)
    prepare_intake.add_argument("--candidate", required=True)
    prepare_intake.add_argument("--output", required=True)
    prepare_intake.add_argument("--source-id", required=True)
    prepare_intake.add_argument(
        "--source-kind",
        choices=["user_supplied", "repository_fixture"],
        default="user_supplied",
    )
    prepare_intake.add_argument(
        "--license-classification",
        choices=["user_supplied_private_analysis", "repository_owned_mit"],
        default="user_supplied_private_analysis",
    )
    prepare_intake.add_argument(
        "--usage-classification",
        choices=["local_analysis_only", "redistribution_allowed"],
        default="local_analysis_only",
    )
    prepare_intake.add_argument(
        "--classification",
        choices=["internal", "public"],
        default="internal",
    )
    prepare_intake.add_argument("--format", choices=["json", "markdown"], default="json")

    confirm_intake = subparsers.add_parser("confirm-review-intake")
    confirm_intake.add_argument("--preparation", required=True)
    confirm_intake.add_argument("--output", required=True)
    confirm_intake.add_argument("--run-id", required=True)
    confirm_intake.add_argument("--authority-id", required=True)
    confirm_intake.add_argument("--confirmation-id", required=True)
    confirm_intake.add_argument("--idempotency-key", required=True)
    confirm_intake.add_argument("--expected-source-sha256", required=True)
    confirm_intake.add_argument("--expected-candidate-sha256", required=True)
    confirm_intake.add_argument("--confirmed-at")
    confirm_intake.add_argument("--expires-at")
    confirm_intake.add_argument("--format", choices=["json", "markdown"], default="json")

    review_intake = subparsers.add_parser("review-confirmed-intake")
    review_intake.add_argument("--source", required=True)
    review_intake.add_argument("--preparation", required=True)
    review_intake.add_argument("--confirmation", required=True)
    review_intake.add_argument("--format", choices=_REPORT_FORMATS, default="markdown")

    for command in ("review-hand", "review-strategy"):
        subparser = subparsers.add_parser(command)
        subparser.add_argument("--file", required=True)
        subparser.add_argument("--format", choices=_REPORT_FORMATS, default="markdown")

    audit = subparsers.add_parser("audit-claim")
    audit.add_argument("claim")
    audit.add_argument("--format", choices=_REPORT_FORMATS, default="markdown")

    calculate = subparsers.add_parser("calculate")
    calculate.add_argument("tool")
    calculate.add_argument(
        "--analysis-scope",
        choices=["unspecified", "retrospective", "real_time"],
        default="unspecified",
    )
    calculate.add_argument("--input", required=True)
    calculate.add_argument("--format", choices=["json", "markdown"], default="json")

    list_tools = subparsers.add_parser("list-tools")
    list_tools.add_argument("--format", choices=["json", "markdown"], default="json")
    list_agents = subparsers.add_parser("list-agents")
    list_agents.add_argument("--format", choices=["json", "markdown"], default="json")

    resume = subparsers.add_parser("resume")
    resume.add_argument("run_id")
    resume.add_argument("--approve", action="append", default=[])
    resume.add_argument("--reject", action="append", default=[])
    resume.add_argument("--reason")
    resume.add_argument("--decision-file")
    resume.add_argument("--reissue-file")
    resume.add_argument("--actor-id")
    resume.add_argument("--decision-id")
    resume.add_argument("--idempotency-key")
    resume.add_argument("--expected-run-revision", type=int)
    resume.add_argument("--expected-ledger-revision", type=int)
    resume.add_argument("--decision-at")
    resume.add_argument("--format", choices=_REPORT_FORMATS, default="markdown")
    show = subparsers.add_parser("show")
    show.add_argument("run_id")
    show.add_argument("--format", choices=_REPORT_FORMATS, default="markdown")
    return parser


def main(argv: list[str] | None = None) -> int:
    _configure_output()
    parser = build_parser()
    args = parser.parse_args(argv)
    resume_reason = (
        args.reason
        if args.command == "resume" and args.reason is not None
        else _DEFAULT_RESUME_REASON
    )
    try:
        if args.command == "doctor":
            _emit(doctor(), args.format)
            return 0
        if args.command == "prepare-review-intake":
            source_bytes = _read_limited_bytes(
                args.source,
                MAX_CONFIRMED_REVIEW_SOURCE_BYTES,
            )
            preparation = prepare_review_intake(
                source_bytes,
                _read_limited_json(
                    args.candidate,
                    MAX_CONFIRMED_REVIEW_SOURCE_BYTES,
                ),
                source_id=args.source_id,
                source_kind=args.source_kind,
                license_classification=args.license_classification,
                usage_classification=args.usage_classification,
                classification=args.classification,
            )
            _write_canonical_model(args.output, preparation)
            _emit(preparation, args.format)
            return 0 if preparation.status == "ready" else 2
        if args.command == "confirm-review-intake":
            preparation = parse_canonical_model(
                _read_limited_bytes(
                    args.preparation,
                    MAX_CONFIRMED_REVIEW_ARTIFACT_BYTES,
                ),
                ReviewIntakePreparationResultV1,
            )
            if preparation.status != "ready" or preparation.candidate is None:
                raise ValueError("CRI_E_CONFIRMATION_MISSING")
            authority = ReviewConfirmationAuthorityV1(
                authority_id=args.authority_id,
                authority_kind="local_user",
                authentication="self_asserted",
            )
            confirmation = create_review_confirmation(
                preparation.candidate,
                run_id=args.run_id,
                confirmation_id=args.confirmation_id,
                idempotency_key=args.idempotency_key,
                authority=authority,
                expected_source_sha256=args.expected_source_sha256,
                expected_candidate_sha256=args.expected_candidate_sha256,
                confirmed_at=_parse_cli_datetime(args.confirmed_at),
                expires_at=_parse_cli_datetime(args.expires_at),
            )
            _write_canonical_model(args.output, confirmation)
            _emit(confirmation, args.format)
            return 0
        if args.command == "review-confirmed-intake":
            source_bytes = _read_limited_bytes(
                args.source,
                MAX_CONFIRMED_REVIEW_SOURCE_BYTES,
            )
            preparation = parse_canonical_model(
                _read_limited_bytes(
                    args.preparation,
                    MAX_CONFIRMED_REVIEW_ARTIFACT_BYTES,
                ),
                ReviewIntakePreparationResultV1,
            )
            if preparation.status != "ready" or preparation.candidate is None:
                raise ValueError("CRI_E_CONFIRMATION_MISSING")
            confirmation = parse_canonical_model(
                _read_limited_bytes(
                    args.confirmation,
                    MAX_CONFIRMED_REVIEW_ARTIFACT_BYTES,
                ),
                ReviewIntakeConfirmationV1,
            )
            admission = admit_confirmed_review(
                source_bytes,
                preparation.candidate,
                confirmation,
            )
            report = review_confirmed_intake(
                admission,
                config=AppConfig.from_env(),
            )
            if args.format == "json":
                rendered_confirmed_report: object = report
            elif args.format == "summary":
                rendered_confirmed_report = render_summary(report)
            else:
                rendered_confirmed_report = render_markdown(report)
            _emit(rendered_confirmed_report, args.format)
            return 2 if report.run_status == "failed_with_limitations" else 0
        if args.command == "calculate":
            if args.analysis_scope != "retrospective":
                print("error: calculate is retrospective-only", file=sys.stderr)
                return 2
            result = default_registry().execute(args.tool, _read_json(args.input))
            if args.format == "json":
                rendered_result: object = redact_sensitive(result)
            else:
                rendered_result = render_markdown(
                    FinalReport(
                        run_id="standalone-calculation",
                        conclusion="単独の計算結果です。状態・数値区分・仮定・誤差を確認してください。",
                        tool_results=[result],
                        reproduction_steps=[result.reproduce_command or "再現コマンドなし"],
                    )
                )
            _emit(rendered_result, args.format)
            return 0 if result.status.value == "success" else 2
        if args.command == "list-tools":
            _emit(default_registry().describe(), args.format)
            return 0
        if args.command == "list-agents":
            descriptions = [
                {
                    "name": definition.name,
                    "purpose": definition.purpose,
                    "read_only": definition.read_only,
                }
                for definition in ROLE_CATALOG.values()
            ]
            _emit(descriptions, args.format)
            return 0
        decision_batch: ApprovalDecisionBatch | None = None
        reissue_batch: ApprovalReissueBatchV2 | None = None
        if args.command == "resume" and args.reissue_file:
            conflicting = (
                args.decision_file is not None
                or args.approve
                or args.reject
                or args.reason is not None
                or args.actor_id is not None
                or args.decision_id is not None
                or args.idempotency_key is not None
                or args.expected_run_revision is not None
                or args.expected_ledger_revision is not None
                or args.decision_at is not None
            )
            if conflicting:
                parser.error("--reissue-file cannot be combined with decision construction options")
            reissue_batch = parse_approval_model(
                Path(args.reissue_file).read_bytes(),
                ApprovalReissueBatchV2,
            )
            local_actor_id = "local-cli-user"
        elif args.command == "resume" and args.decision_file:
            conflicting = (
                args.approve
                or args.reject
                or args.reason is not None
                or args.actor_id is not None
                or args.decision_id is not None
                or args.idempotency_key is not None
                or args.expected_run_revision is not None
                or args.expected_ledger_revision is not None
                or args.decision_at is not None
            )
            if conflicting:
                parser.error(
                    "--decision-file cannot be combined with decision construction options"
                )
            decision_batch = parse_approval_model(
                Path(args.decision_file).read_bytes(),
                ApprovalDecisionBatch,
            )
            local_actor_id = decision_batch.actor.actor_id
        elif args.command == "resume":
            local_actor_id = args.actor_id or "local-cli-user"
        else:
            local_actor_id = "local-cli-user"
        authority_provider = LocalCliAuthorityProvider(local_actor_id)
        orchestrator = Orchestrator(
            AppConfig.from_env(),
            decision_authority_provider=authority_provider,
        )
        if args.command == "review-hand":
            report = orchestrator.run(_case_from_hand_file(args.file))
        elif args.command == "review-strategy":
            report = orchestrator.run(_case_from_strategy_file(args.file))
        elif args.command == "audit-claim":
            report = orchestrator.run(
                CaseInput(
                    kind="claim",
                    claims=[Claim(text=args.claim, label=EpistemicLabel.USER_CLAIM)],
                    analysis_scope="retrospective",
                )
            )
        elif args.command == "resume":
            if decision_batch is None and (args.approve or args.reject):
                read = orchestrator.product_store.read_current(args.run_id)
                names = {payload.inventory.logical_name for payload in read.payloads}
                if "approval_ledger_v2.json" in names:
                    state = read_approval_state_v2(
                        read.payload_bytes("approval_ledger_v2.json"),
                        read.payload_bytes("approval_decisions_v2.jsonl"),
                        read.payload_bytes("approval_audit_v2.jsonl"),
                        (
                            read.payload_bytes("approval_reissues_v2.jsonl")
                            if "approval_reissues_v2.jsonl" in names
                            else b""
                        ),
                    )
                    requests = {request.request_id: request for request in state.ledger.requests}
                    decision_at = (
                        datetime.fromisoformat(args.decision_at)
                        if args.decision_at is not None
                        else datetime.now(UTC)
                    )
                    actor = authority_provider.resolve_actor(
                        local_actor_id,
                        decision_at=decision_at,
                    ).actor
                    decisions = [
                        *((request_id, "approved") for request_id in args.approve),
                        *((request_id, "rejected") for request_id in args.reject),
                    ]
                    items = []
                    for request_id, decision in decisions:
                        request = requests.get(request_id)
                        items.append(
                            ApprovalDecisionItemV2(
                                request_id=request_id,
                                expected_request_revision=(
                                    1 if request is None else request.request_revision
                                ),
                                action_digest_sha256=(
                                    "0" * 64 if request is None else request.action_digest_sha256
                                ),
                                decision=cast(DecisionValue, decision),
                            )
                        )
                    items.sort(
                        key=lambda item: (
                            item.request_id.encode("utf-8"),
                            item.decision.encode("ascii"),
                        )
                    )
                    decision_batch = ApprovalDecisionBatch(
                        run_id=args.run_id,
                        expected_run_revision=(
                            read.revision
                            if args.expected_run_revision is None
                            else args.expected_run_revision
                        ),
                        expected_ledger_revision=(
                            state.ledger.ledger_revision
                            if args.expected_ledger_revision is None
                            else args.expected_ledger_revision
                        ),
                        actor=actor,
                        decision_id=(
                            args.decision_id or orchestrator.terminal_id_factory("decision")
                        ),
                        idempotency_key=(
                            args.idempotency_key or orchestrator.terminal_id_factory("decision-key")
                        ),
                        items=tuple(items),
                        reason=str(redact_sensitive(resume_reason, enabled=True)),
                        decision_at=decision_at,
                    )
            report = orchestrator.resume(
                args.run_id,
                approve_ids=(args.approve if decision_batch is None else None),
                reject_ids=(args.reject if decision_batch is None else None),
                reason=resume_reason,
                decision_batch=decision_batch,
                reissue_batch=reissue_batch,
            )
        elif args.command == "show":
            report = orchestrator.load_report(args.run_id)
        else:  # pragma: no cover - argparse guarantees the command
            parser.error("unknown command")
            return 2
        rendered_report: object
        if args.format == "json":
            rendered_report = report
        elif args.format == "summary":
            rendered_report = render_summary(report)
        else:
            rendered_report = render_markdown(report)
        _emit(rendered_report, args.format)
        if report.run_status == "approval_required":
            return 3
        return 2 if report.run_status == "failed_with_limitations" else 0
    except ApprovalDecisionValidationError as exc:
        if getattr(args, "format", "markdown") == "json":
            _emit(exc.failure, "json")
        else:
            print(f"error: approval decision failed: {exc.failure.code.value}", file=sys.stderr)
        return 2
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
