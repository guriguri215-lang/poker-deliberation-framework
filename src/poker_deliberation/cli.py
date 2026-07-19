"""Dependency-light command-line interface."""

from __future__ import annotations

import argparse
import importlib.metadata
import importlib.util
import json
import os
import platform
import sys
from pathlib import Path
from typing import Any

import pydantic

from poker_deliberation import __version__
from poker_deliberation.agents import ROLE_CATALOG
from poker_deliberation.capabilities import capability_snapshot
from poker_deliberation.config import AppConfig
from poker_deliberation.normalization import normalize_hand_text
from poker_deliberation.orchestrator import Orchestrator
from poker_deliberation.providers import LocalProvider, OpenAIAgentsProvider
from poker_deliberation.reporting import render_markdown
from poker_deliberation.schemas import CanonicalHand, CaseInput, Claim, EpistemicLabel, FinalReport
from poker_deliberation.security import redact_sensitive
from poker_deliberation.tools import default_registry


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


def _emit(value: Any, format_name: str) -> None:
    if format_name == "json":
        if hasattr(value, "model_dump"):
            value = value.model_dump(mode="json")
        print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False))
    else:
        print(
            value
            if isinstance(value, str)
            else json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False)
        )


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
        "project_files": {path: Path(path).exists() for path in project_files},
        "local_calculators": default_registry().names(),
        "capabilities": capability_snapshot(),
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
    raw_text = source.read_text(encoding="utf-8")
    normalized = normalize_hand_text(raw_text)
    return CaseInput(
        kind="hand",
        raw_text=raw_text,
        hand=normalized.hand,
        analysis_scope="retrospective",
        metadata={"normalization_warnings": list(normalized.warnings)},
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

    for command in ("review-hand", "review-strategy"):
        subparser = subparsers.add_parser(command)
        subparser.add_argument("--file", required=True)
        subparser.add_argument("--format", choices=["json", "markdown"], default="markdown")

    audit = subparsers.add_parser("audit-claim")
    audit.add_argument("claim")
    audit.add_argument("--format", choices=["json", "markdown"], default="markdown")

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
    resume.add_argument("--reason", default="human decision recorded by CLI")
    resume.add_argument("--format", choices=["json", "markdown"], default="markdown")
    show = subparsers.add_parser("show")
    show.add_argument("run_id")
    show.add_argument("--format", choices=["json", "markdown"], default="markdown")
    return parser


def main(argv: list[str] | None = None) -> int:
    _configure_output()
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "doctor":
            _emit(doctor(), args.format)
            return 0
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
        orchestrator = Orchestrator(AppConfig.from_env())
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
            report = orchestrator.resume(
                args.run_id,
                approve_ids=args.approve,
                reject_ids=args.reject,
                reason=args.reason,
            )
        elif args.command == "show":
            report = orchestrator.load_report(args.run_id)
        else:  # pragma: no cover - argparse guarantees the command
            parser.error("unknown command")
            return 2
        _emit(report if args.format == "json" else render_markdown(report), args.format)
        if report.run_status == "approval_required":
            return 3
        return 2 if report.run_status == "failed_with_limitations" else 0
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
