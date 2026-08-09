"""Deterministic no-network evaluation for the bounded P2-025B bridge."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from fractions import Fraction
from pathlib import Path
from tempfile import mkdtemp
from typing import Final, Literal, NoReturn, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, model_validator

from poker_deliberation.bounded_river_call_ev_evaluation import (
    build_repository_owned_bounded_river_evaluation_admission,
    verify_bounded_river_call_ev_evaluation_checkout,
    verify_bounded_river_call_ev_evaluation_module_origins,
)
from poker_deliberation.codex_bridge.canonical import (
    BridgeCanonicalError,
    canonical_json_bytes,
    domain_sha256,
    parse_canonical_model,
)
from poker_deliberation.codex_bridge.contracts import (
    build_runtime_policy,
    validate_role_response,
)
from poker_deliberation.codex_bridge.controller import (
    BoundedCodexBridgeController,
    canonical_assignment_id,
    role_artifact_name,
)
from poker_deliberation.codex_bridge.models import (
    BRIDGE_ROLE_ORDER,
    BoundedCodexBridgeRequestV1,
    BridgeConfirmationAuthorityV1,
    BridgeEffectState,
    BridgeEpistemicLabel,
    BridgeExecutionAuditV1,
    BridgeRole,
    BridgeRoleOutputV1,
    BridgeRoleResultV1,
    BridgeSourceContextV1,
    RuntimeAuthModeV1,
)
from poker_deliberation.codex_bridge.qualification import (
    build_sanitized_live_qualification_manifest,
)
from poker_deliberation.codex_bridge.replay import replay_bridge
from poker_deliberation.codex_bridge.sdk_transport import OpenAIAPITransport
from poker_deliberation.codex_bridge.source import project_verified_p3_terminal
from poker_deliberation.codex_bridge.storage import (
    BoundedCodexBridgeStore,
    BridgeStorageError,
    VerifiedBridgeRead,
)
from poker_deliberation.codex_bridge.transport import (
    BridgeTransportFailure,
    BridgeTransportResult,
    DeterministicReadOnlyTransport,
)
from poker_deliberation.config import AppConfig
from poker_deliberation.orchestrator import Orchestrator
from poker_deliberation.providers import LocalProvider

EVALUATION_SCHEMA_VERSION: Final[Literal["1.0.0"]] = "1.0.0"
EVALUATION_FAMILY_ID: Final[Literal["poker-bounded-codex-bridge-evaluation-json-v1"]] = (
    "poker-bounded-codex-bridge-evaluation-json-v1"
)
EVALUATION_FIXTURE_ID: Final[Literal["bounded-codex-bridge-cases-v1"]] = (
    "bounded-codex-bridge-cases-v1"
)
_MODE: Final = RuntimeAuthModeV1.CODEX_SUBSCRIPTION

EvaluationMetric: TypeAlias = Literal[
    "exact-authority",
    "runtime-auth-modes",
    "control-and-replay",
    "security-and-effects",
]

REQUIRED_CASE_EVIDENCE: tuple[tuple[str, EvaluationMetric, tuple[str, ...]], ...] = (
    (
        "exact-source-authority",
        "exact-authority",
        (
            "call-fold-tie-fractions-preserved",
            "raw-source-and-report-excluded",
            "seven-tool-results-reference-only",
        ),
    ),
    (
        "runtime-auth-mode-separation",
        "runtime-auth-modes",
        (
            "local-mode-has-no-model-credential-or-network",
            "subscription-mode-uses-saved-chatgpt-reference-without-api-cost",
            "api-mode-is-explicit-cost-capped-and-distinct",
            "api-live-unqualified-cost-authority-fails-closed",
            "unknown-mode-and-cross-mode-confirmation-refused",
            "cross-mode-transport-and-fallback-refused",
            "credential-values-absent-from-canonical-contracts",
            "all-tools-disabled-and-product-retry-zero",
            "retention-and-provider-retry-unknowns-are-mode-specific",
        ),
    ),
    (
        "serial-role-control",
        "control-and-replay",
        (
            "five-fresh-serial-attempts",
            "independent-and-dependent-parent-lineage",
            "p2-025a-read-only-empty-tool-bindings",
            "terminal-adjudication-report-replay",
            "deterministic-fixture-cannot-be-live-qualification",
        ),
    ),
    (
        "strict-schema-policy",
        "security-and-effects",
        (
            "extra-missing-unknown-output-refused",
            "numeric-range-solver-gto-calculated-refused",
            "content-free-sequential-claim-ids-enforced",
            "adjudicator-each-claim-binds-all-parents",
            "hash-model-role-allowlist-mutation-refused",
            "exact-confirmation-and-replay-refused",
        ),
    ),
    (
        "durable-effect-recovery",
        "security-and-effects",
        (
            "admission-is-durable-before-transport",
            "failure-states-terminal-without-retry",
            "effect-unknown-requires-reconciliation",
            "partial-thread-terminal-publication-and-replay",
            "partial-thread-claim-collision-and-corruption-refused",
            "local-source-solver-unavailable-no-gto",
        ),
    ),
)
REQUIRED_CASE_IDS = tuple(item[0] for item in REQUIRED_CASE_EVIDENCE)
REQUIRED_METRICS: tuple[EvaluationMetric, ...] = (
    "exact-authority",
    "runtime-auth-modes",
    "control-and-replay",
    "security-and-effects",
)


class _EvaluationModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        revalidate_instances="always",
    )


class BoundedCodexBridgeEvaluationCaseV1(_EvaluationModel):
    case_id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,95}$")
    metric: EvaluationMetric
    expected_evidence: tuple[str, ...] = Field(min_length=1)


class BoundedCodexBridgeEvaluationFixtureV1(_EvaluationModel):
    schema_version: Literal["1.0.0"] = EVALUATION_SCHEMA_VERSION
    family_id: Literal["poker-bounded-codex-bridge-evaluation-json-v1"] = EVALUATION_FAMILY_ID
    fixture_id: Literal["bounded-codex-bridge-cases-v1"] = EVALUATION_FIXTURE_ID
    source_kind: Literal["repository_fixture"] = "repository_fixture"
    license_classification: Literal["repository_owned_mit"] = "repository_owned_mit"
    usage_classification: Literal["redistribution_allowed"] = "redistribution_allowed"
    content_classification: Literal["public"] = "public"
    model_processing_authorized: Literal[True] = True
    scoring: Literal["exact-evidence-set-v1"] = "exact-evidence-set-v1"
    threshold: Literal["1.0"] = "1.0"
    cases: tuple[BoundedCodexBridgeEvaluationCaseV1, ...]

    @model_validator(mode="after")
    def exact_inventory(self) -> BoundedCodexBridgeEvaluationFixtureV1:
        observed = tuple((item.case_id, item.metric, item.expected_evidence) for item in self.cases)
        if observed != REQUIRED_CASE_EVIDENCE:
            raise ValueError("bounded Codex bridge evaluation case inventory mismatch")
        return self


class BoundedCodexBridgeEvaluationCaseResultV1(_EvaluationModel):
    case_id: str
    metric: EvaluationMetric
    expected_evidence: tuple[str, ...]
    observed_evidence: tuple[str, ...]
    score: Literal["0.0", "1.0"]
    passed: bool
    observation_error_type: str | None = Field(
        default=None,
        pattern=r"^[A-Za-z][A-Za-z0-9_]{0,127}$",
    )

    @model_validator(mode="after")
    def exact_score(self) -> BoundedCodexBridgeEvaluationCaseResultV1:
        matched = self.observed_evidence == self.expected_evidence
        if self.passed is not matched or self.score != ("1.0" if matched else "0.0"):
            raise ValueError("bounded Codex bridge case score mismatch")
        if matched and self.observation_error_type is not None:
            raise ValueError("a passing observation cannot record an observation error")
        return self


class BoundedCodexBridgeEvaluationMetricV1(_EvaluationModel):
    metric: EvaluationMetric
    declared_checks: int = Field(gt=0)
    passed_checks: int = Field(ge=0)
    score: Literal["0.0", "1.0"]


class BoundedCodexBridgeEvaluationResultV1(_EvaluationModel):
    schema_version: Literal["1.0.0"] = EVALUATION_SCHEMA_VERSION
    family_id: Literal["poker-bounded-codex-bridge-evaluation-json-v1"] = EVALUATION_FAMILY_ID
    fixture_id: Literal["bounded-codex-bridge-cases-v1"]
    scoring: Literal["exact-evidence-set-v1"]
    threshold: Literal["1.0"]
    source_commit_id: str = Field(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
    source_tree_id: str = Field(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
    case_results: tuple[BoundedCodexBridgeEvaluationCaseResultV1, ...]
    metrics: tuple[BoundedCodexBridgeEvaluationMetricV1, ...]
    overall_score: Literal["0.0", "1.0"]
    passed: bool
    transport_qualification: Literal["deterministic_fixture_only"] = "deterministic_fixture_only"
    live_qualification_sha256: Literal[None] = None
    result_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def exact_summary(self) -> BoundedCodexBridgeEvaluationResultV1:
        if (
            tuple(item.case_id for item in self.case_results) != REQUIRED_CASE_IDS
            or tuple(item.metric for item in self.metrics) != REQUIRED_METRICS
        ):
            raise ValueError("bounded Codex bridge result inventory mismatch")
        for metric in self.metrics:
            selected = tuple(item for item in self.case_results if item.metric == metric.metric)
            declared = sum(len(item.expected_evidence) for item in selected)
            passed = sum(len(item.expected_evidence) if item.passed else 0 for item in selected)
            if (
                metric.declared_checks != declared
                or metric.passed_checks != passed
                or metric.score != ("1.0" if declared == passed else "0.0")
            ):
                raise ValueError("bounded Codex bridge metric score mismatch")
        passed = all(item.passed for item in self.case_results)
        if self.passed is not passed or self.overall_score != ("1.0" if passed else "0.0"):
            raise ValueError("bounded Codex bridge overall score mismatch")
        payload = self.model_dump(mode="json", exclude={"result_sha256"})
        if self.result_sha256 != domain_sha256(EVALUATION_FAMILY_ID, payload):
            raise ValueError("bounded Codex bridge result hash mismatch")
        return self


class _StepClock:
    def __init__(self) -> None:
        self.current = datetime(2030, 1, 1, tzinfo=UTC)

    def __call__(self) -> datetime:
        value = self.current
        self.current += timedelta(seconds=1)
        return value


class _Context:
    def __init__(
        self,
        root: Path,
        repository_root: Path,
        source_commit_id: str,
        source_tree_id: str,
    ) -> None:
        self.root = root
        self.repository_root = repository_root
        self.source_commit_id = source_commit_id
        self.source_tree_id = source_tree_id
        self.sources: dict[str, BridgeSourceContextV1] = {}
        self.successes: dict[
            str,
            tuple[VerifiedBridgeRead, DeterministicReadOnlyTransport],
        ] = {}

    def source(self, notation: str, token: str) -> BridgeSourceContextV1:
        if token not in self.sources:
            base = self.root / f"s-{token}"
            config = AppConfig(
                runs_dir=base / "l",
                revision_runs_dir=base / "p",
                durable_budget_runs_dir=base / "b",
            )
            orchestrator = Orchestrator(config=config, provider=LocalProvider())
            admission = build_repository_owned_bounded_river_evaluation_admission(
                notation,
                f"bridge-eval-source-{token}",
            )
            report = orchestrator.run_bounded_river_call_ev_review(admission)
            read = orchestrator.product_store.read_current(report.run_id)
            self.sources[token] = project_verified_p3_terminal(
                read,
                source_revision_root=orchestrator.product_store.revision_root,
            )
        return self.sources[token]

    def controller(
        self,
        source: BridgeSourceContextV1,
        token: str,
    ) -> tuple[BoundedCodexBridgeController, _StepClock]:
        clock = _StepClock()
        controller = BoundedCodexBridgeController(
            BoundedCodexBridgeStore(self.root / f"b-{token}"),
            clock=clock,
        )
        controller.prepare_run(
            bridge_run_id=f"bridge-eval-{token}",
            source_context=source,
            repository_root=self.repository_root,
            repository_commit_id=self.source_commit_id,
            repository_tree_id=self.source_tree_id,
            auth_mode=_MODE,
        )
        return controller, clock

    @staticmethod
    def confirm(
        controller: BoundedCodexBridgeController,
        bridge_run_id: str,
        role: BridgeRole,
        token: str,
    ) -> None:
        request = controller.read_role_request(bridge_run_id, role)
        controller.confirm_role(
            bridge_run_id,
            role,
            authority=BridgeConfirmationAuthorityV1(
                authority_id="local-evaluation-user",
                authority_kind="local_user",
                authentication="self_asserted",
            ),
            confirmation_id=f"confirmation-{token}-{role.value}",
            idempotency_key=f"idempotency-{token}-{role.value}",
            expected_request_sha256=request.request_sha256,
            expected_request_bytes_sha256=request.request_bytes_sha256,
            expected_envelope_sha256=request.context.envelope_sha256,
            expected_runtime_policy_sha256=request.context.runtime_policy.policy_sha256,
            expected_auth_mode=_MODE,
            expected_runtime_identity=request.context.runtime_policy.runtime_identity,
            expected_model_provider=request.context.runtime_policy.model_provider,
            expected_model=request.context.runtime_policy.model,
            expected_credential_reference=request.context.runtime_policy.credential_reference,
            expected_remote_retention_policy=(
                request.context.runtime_policy.remote_retention_policy
            ),
        )

    def success(
        self,
        source: BridgeSourceContextV1,
        token: str,
    ) -> tuple[VerifiedBridgeRead, DeterministicReadOnlyTransport]:
        if token not in self.successes:
            controller, clock = self.controller(source, token)
            run_id = f"bridge-eval-{token}"
            transport = DeterministicReadOnlyTransport(auth_mode=_MODE, clock=clock)
            for role in BRIDGE_ROLE_ORDER:
                self.confirm(controller, run_id, role, token)
                read = controller.execute_confirmed_role(
                    run_id,
                    role,
                    auth_mode=_MODE,
                    current_source_terminal_manifest_sha256=(
                        source.source.source_terminal_manifest_sha256
                    ),
                    transport=transport,
                )
            self.successes[token] = read, transport
        return self.successes[token]


def _artifact_models(read: VerifiedBridgeRead) -> dict[str, object]:
    return {item.logical_name: item.model for item in read.decoded_artifacts()}


def _exact_source_authority(context: _Context) -> tuple[str, ...]:
    expected = (
        ("QcJc", "call", Fraction(1), Fraction(38), "call"),
        ("9c9d", "fold", Fraction(0), Fraction(-10), "fold"),
        ("QcJc@0.05,9c9d@0.19", "tie", Fraction(5, 24), Fraction(0), "tie"),
    )
    preserved = True
    excluded = True
    referenced = True
    for notation, token, equity, call_ev, comparison in expected:
        source = context.source(notation, token)
        before = canonical_json_bytes(source.math)
        read, _transport = context.success(source, token)
        models = _artifact_models(read)
        stored = models["source_context.json"]
        assert isinstance(stored, BridgeSourceContextV1)
        preserved = preserved and (
            Fraction(stored.math.equity.numerator, stored.math.equity.denominator) == equity
            and Fraction(
                stored.math.call_ev_units.numerator,
                stored.math.call_ev_units.denominator,
            )
            == call_ev
            and stored.math.action_comparison == comparison
            and canonical_json_bytes(stored.math) == before
        )
        encoded = canonical_json_bytes(stored)
        excluded = excluded and all(
            token_bytes not in encoded
            for token_bytes in (b"raw_text", b"observations", b"final_report")
        )
        referenced = referenced and (
            len(stored.math.tool_support) == 7
            and not any("input" in item.evidence_id for item in stored.math.tool_support)
        )
    evidence: list[str] = []
    if preserved:
        evidence.append("call-fold-tie-fractions-preserved")
    if excluded:
        evidence.append("raw-source-and-report-excluded")
    if referenced:
        evidence.append("seven-tool-results-reference-only")
    return tuple(evidence)


def _runtime_auth_mode_separation(context: _Context) -> tuple[str, ...]:
    local = build_runtime_policy(auth_mode=RuntimeAuthModeV1.LOCAL_ONLY)
    subscription = build_runtime_policy(auth_mode=RuntimeAuthModeV1.CODEX_SUBSCRIPTION)
    api = build_runtime_policy(
        auth_mode=RuntimeAuthModeV1.OPENAI_API,
        api_max_cost_micro_usd=204_000,
    )
    evidence: list[str] = []
    if (
        local.interface == "local_provider"
        and local.model is None
        and local.credential_reference == "none"
        and local.network_allowed is False
        and local.model_processing_authorized is False
        and local.budget.max_turns == 0
    ):
        evidence.append("local-mode-has-no-model-credential-or-network")
    if (
        subscription.interface == "codex_exec_json"
        and subscription.credential_reference == "codex_home:saved_chatgpt_login"
        and subscription.budget.cost_budget_kind == "subscription_usage"
        and subscription.budget.max_cost_micro_usd is None
        and subscription.provider_internal_retry_status == "UNKNOWN"
    ):
        evidence.append("subscription-mode-uses-saved-chatgpt-reference-without-api-cost")
    if (
        api.interface == "codex_sdk_responses"
        and api.credential_reference == "env:OPENAI_API_KEY"
        and api.budget.cost_budget_kind == "api_explicit_cap"
        and api.budget.max_cost_micro_usd == 204_000
        and api.model_provider != subscription.model_provider
    ):
        evidence.append("api-mode-is-explicit-cost-capped-and-distinct")
    api_live_refused = False
    try:
        OpenAIAPITransport._require_api_live_qualification()
    except BridgeTransportFailure as exc:
        api_live_refused = (
            exc.reason_code == "api_live_execution_unqualified_cost_authority"
            and exc.effect_state is BridgeEffectState.NOT_LAUNCHED
            and exc.launched_at is None
        )
    if (
        api_live_refused
        and OpenAIAPITransport.api_live_qualified is False
        and OpenAIAPITransport.price_authority_version is None
        and OpenAIAPITransport.provider_hard_cost_stop is False
        and api.budget.hard_provider_cost_stop is False
    ):
        evidence.append("api-live-unqualified-cost-authority-fails-closed")

    unknown_refused = False
    try:
        RuntimeAuthModeV1("unknown")
    except ValueError:
        unknown_refused = True
    source = context.source("QcJc", "call")
    controller, _clock = context.controller(source, "cross-mode-confirmation")
    request = controller.read_role_request(
        "bridge-eval-cross-mode-confirmation",
        BridgeRole.STRATEGY_ANALYST,
    )
    confirmation_refused = False
    try:
        controller.confirm_role(
            "bridge-eval-cross-mode-confirmation",
            BridgeRole.STRATEGY_ANALYST,
            authority=BridgeConfirmationAuthorityV1(
                authority_id="local-evaluation-user",
                authority_kind="local_user",
                authentication="self_asserted",
            ),
            confirmation_id="confirmation-cross-mode",
            idempotency_key="idempotency-cross-mode",
            expected_request_sha256=request.request_sha256,
            expected_request_bytes_sha256=request.request_bytes_sha256,
            expected_envelope_sha256=request.context.envelope_sha256,
            expected_runtime_policy_sha256=request.context.runtime_policy.policy_sha256,
            expected_auth_mode=RuntimeAuthModeV1.OPENAI_API,
            expected_runtime_identity=request.context.runtime_policy.runtime_identity,
            expected_model_provider=request.context.runtime_policy.model_provider,
            expected_model=request.context.runtime_policy.model,
            expected_credential_reference=request.context.runtime_policy.credential_reference,
            expected_remote_retention_policy=(
                request.context.runtime_policy.remote_retention_policy
            ),
        )
    except Exception:
        confirmation_refused = True
    if unknown_refused and confirmation_refused:
        evidence.append("unknown-mode-and-cross-mode-confirmation-refused")

    transport_refused = False
    try:
        DeterministicReadOnlyTransport(
            auth_mode=RuntimeAuthModeV1.OPENAI_API,
            clock=_StepClock(),
        ).execute(request)
    except BridgeTransportFailure:
        transport_refused = True
    if (
        transport_refused
        and not subscription.provider_fallback_allowed
        and not subscription.model_fallback_allowed
        and not api.provider_fallback_allowed
        and not api.model_fallback_allowed
    ):
        evidence.append("cross-mode-transport-and-fallback-refused")

    policy_bytes = canonical_json_bytes((local, subscription, api))
    if (
        b"sk-synthetic" not in policy_bytes
        and b"access_token" not in policy_bytes
        and b"refresh_token" not in policy_bytes
        and b"auth.json" not in policy_bytes
    ):
        evidence.append("credential-values-absent-from-canonical-contracts")
    if all(
        item.tool_allowlist == ()
        and not item.shell_enabled
        and not item.web_enabled
        and not item.mcp_enabled
        and not item.apps_enabled
        and not item.nested_agents_enabled
        and not item.file_write_enabled
        and not item.automatic_product_retry
        for item in (local, subscription, api)
    ):
        evidence.append("all-tools-disabled-and-product-retry-zero")
    if (
        local.remote_retention_policy == "none_local_only"
        and subscription.remote_retention_policy == "chatgpt_workspace_policy_unknown"
        and api.remote_retention_policy == "openai_api_org_policy_no_zdr_claim"
        and local.provider_internal_retry_status == "not_applicable"
        and subscription.provider_internal_retry_status == "UNKNOWN"
        and api.provider_internal_retry_status == "disabled"
    ):
        evidence.append("retention-and-provider-retry-unknowns-are-mode-specific")
    return tuple(evidence)


def _serial_role_control(context: _Context) -> tuple[str, ...]:
    source = context.source("QcJc", "call")
    read, transport = context.success(source, "call")
    replayed = replay_bridge(read)
    models = _artifact_models(read)
    requests = [models[role_artifact_name(role, "request")] for role in BRIDGE_ROLE_ORDER]
    audits = [models[role_artifact_name(role, "audit")] for role in BRIDGE_ROLE_ORDER]
    results = [models[role_artifact_name(role, "result")] for role in BRIDGE_ROLE_ORDER]
    assert all(isinstance(item, BoundedCodexBridgeRequestV1) for item in requests)
    assert all(isinstance(item, BridgeExecutionAuditV1) for item in audits)
    assert all(isinstance(item, BridgeRoleResultV1) for item in results)
    typed_requests = [item for item in requests if isinstance(item, BoundedCodexBridgeRequestV1)]
    typed_audits = [item for item in audits if isinstance(item, BridgeExecutionAuditV1)]
    typed_results = [item for item in results if isinstance(item, BridgeRoleResultV1)]
    evidence: list[str] = []
    if (
        transport.calls
        == [
            canonical_assignment_id(replayed.bridge_run_id, _MODE, role)
            for role in BRIDGE_ROLE_ORDER
        ]
        and len({item.assignment_id for item in (r.context.assignment for r in typed_requests)})
        == 5
        and len({item.thread_id_sha256 for item in typed_audits}) == 5
        and len({item.turn_id_sha256 for item in typed_audits}) == 5
    ):
        evidence.append("five-fresh-serial-attempts")
    parents = [item.context.assignment.parent_assignment_ids for item in typed_requests]
    if (
        parents[:3] == [(), (), ()]
        and parents[3]
        == tuple(item.context.assignment.assignment_id for item in typed_requests[:3])
        and parents[4] == (typed_requests[3].context.assignment.assignment_id,)
    ):
        evidence.append("independent-and-dependent-parent-lineage")
    if all(
        item.context.assignment.conformance.role_read_only
        and item.context.assignment.conformance.declared_tool_allowlist == ()
        and item.context.runtime_policy.tool_allowlist == ()
        for item in typed_requests
    ):
        evidence.append("p2-025a-read-only-empty-tool-bindings")
    if (
        replayed.completed_roles == BRIDGE_ROLE_ORDER
        and replayed.reconciliation_required is False
        and typed_results[-1].output.role is BridgeRole.REPORT_WRITER
        and any(
            item.evidence_sha256 == typed_results[3].result_sha256
            for item in typed_results[-1].output.evidence_references
        )
    ):
        evidence.append("terminal-adjudication-report-replay")
    try:
        build_sanitized_live_qualification_manifest(
            read,
            repository_root=context.repository_root,
            qualification_id="deterministic-must-not-qualify",
            deterministic_evaluation_sha256="0" * 64,
        )
    except ValueError:
        evidence.append("deterministic-fixture-cannot-be-live-qualification")
    return tuple(evidence)


def _strict_schema_policy(context: _Context) -> tuple[str, ...]:
    source = context.source("QcJc", "call")
    read, _transport = context.success(source, "call")
    models = _artifact_models(read)
    request = models[role_artifact_name(BridgeRole.STRATEGY_ANALYST, "request")]
    result = models[role_artifact_name(BridgeRole.STRATEGY_ANALYST, "result")]
    assert isinstance(request, BoundedCodexBridgeRequestV1)
    assert isinstance(result, BridgeRoleResultV1)
    failures = 0
    output = result.output.model_dump(mode="json")
    for mutation in ("extra", "missing", "unknown"):
        changed = json.loads(json.dumps(output))
        if mutation == "extra":
            changed["extra"] = True
        elif mutation == "missing":
            del changed["attempt_id"]
        else:
            changed["role"] = "unknown-role"
        try:
            parse_canonical_model(canonical_json_bytes(changed), BridgeRoleOutputV1)
        except Exception:
            failures += 1
    claim_failures = 0
    for narrative, label in (
        ("Equity is 99 percent.", BridgeEpistemicLabel.INFERENCE),
        ("Use the AA range.", BridgeEpistemicLabel.INFERENCE),
        ("The solver result proves GTO.", BridgeEpistemicLabel.INFERENCE),
        ("A new fact.", "CALCULATED"),
        ("５割の頻度でコールする。", BridgeEpistemicLabel.INFERENCE),
        ("エースキングスーテッドを含むレンジです。", BridgeEpistemicLabel.INFERENCE),
        ("ソルバー解析による戦略です。", BridgeEpistemicLabel.INFERENCE),
        ("This is CALCULATED.", BridgeEpistemicLabel.INFERENCE),
    ):
        changed = result.output.model_dump(mode="json")
        changed["conclusions"][0]["label"] = (
            label.value if isinstance(label, BridgeEpistemicLabel) else label
        )
        changed["conclusions"][0]["narrative"] = narrative
        try:
            validate_role_response(request, canonical_json_bytes(changed))
        except Exception:
            claim_failures += 1
    claim_id_failures = 0
    for claim_id in ("GTO-call-AKs-99-percent", "claim-02"):
        changed = result.output.model_dump(mode="json")
        changed["conclusions"][0]["claim_id"] = claim_id
        try:
            validate_role_response(request, canonical_json_bytes(changed))
        except Exception:
            claim_id_failures += 1
    adjudicator_request = models[role_artifact_name(BridgeRole.ADJUDICATOR, "request")]
    adjudicator_result = models[role_artifact_name(BridgeRole.ADJUDICATOR, "result")]
    assert isinstance(adjudicator_request, BoundedCodexBridgeRequestV1)
    assert isinstance(adjudicator_result, BridgeRoleResultV1)
    split_parent_binding_refused = False
    changed_adjudication = adjudicator_result.output.model_dump(mode="json")
    parent_ids = tuple(
        item.evidence_id
        for item in adjudicator_result.output.evidence_references
        if item.evidence_kind == "role_result"
    )
    template = changed_adjudication["conclusions"][0]
    changed_adjudication["conclusions"] = [
        {
            **template,
            "claim_id": f"claim-{index:02d}",
            "evidence_ids": [parent_id],
        }
        for index, parent_id in enumerate(parent_ids, start=1)
    ]
    changed_adjudication["uncertainties"] = []
    try:
        validate_role_response(
            adjudicator_request,
            canonical_json_bytes(changed_adjudication),
        )
    except Exception:
        split_parent_binding_refused = True
    mutation_failures = 0
    mutations = (
        ("request_sha256", "0" * 64),
        ("output_schema_sha256", "0" * 64),
    )
    for field, value in mutations:
        changed = request.model_dump(mode="python")
        changed[field] = value
        try:
            BoundedCodexBridgeRequestV1.model_validate(changed, strict=True)
        except Exception:
            mutation_failures += 1
    changed_output = result.output.model_dump(mode="python")
    changed_output["model"] = "gpt-unknown"
    try:
        BridgeRoleOutputV1.model_validate(changed_output, strict=True)
    except Exception:
        mutation_failures += 1
    changed_policy = request.context.runtime_policy.model_dump(mode="python")
    changed_policy["tool_allowlist"] = ("shell",)
    try:
        type(request.context.runtime_policy).model_validate(changed_policy, strict=True)
    except Exception:
        mutation_failures += 1
    controller, _clock = context.controller(source, "confirmation-refusal")
    run_id = "bridge-eval-confirmation-refusal"
    exact = controller.read_role_request(run_id, BridgeRole.STRATEGY_ANALYST)
    exact_refused = False
    try:
        controller.confirm_role(
            run_id,
            BridgeRole.STRATEGY_ANALYST,
            authority=BridgeConfirmationAuthorityV1(
                authority_id="local-evaluation-user",
                authority_kind="local_user",
                authentication="self_asserted",
            ),
            confirmation_id="confirmation-refusal",
            idempotency_key="idempotency-refusal",
            expected_request_sha256="0" * 64,
            expected_request_bytes_sha256=exact.request_bytes_sha256,
            expected_envelope_sha256=exact.context.envelope_sha256,
            expected_runtime_policy_sha256=exact.context.runtime_policy.policy_sha256,
            expected_auth_mode=_MODE,
            expected_runtime_identity=exact.context.runtime_policy.runtime_identity,
            expected_model_provider=exact.context.runtime_policy.model_provider,
            expected_model=exact.context.runtime_policy.model,
            expected_credential_reference=exact.context.runtime_policy.credential_reference,
            expected_remote_retention_policy=(exact.context.runtime_policy.remote_retention_policy),
        )
    except Exception:
        exact_refused = True
    evidence: list[str] = []
    if failures == 3:
        evidence.append("extra-missing-unknown-output-refused")
    if claim_failures == 8:
        evidence.append("numeric-range-solver-gto-calculated-refused")
    if claim_id_failures == 2:
        evidence.append("content-free-sequential-claim-ids-enforced")
    if split_parent_binding_refused:
        evidence.append("adjudicator-each-claim-binds-all-parents")
    if mutation_failures == 4:
        evidence.append("hash-model-role-allowlist-mutation-refused")
    if exact_refused:
        evidence.append("exact-confirmation-and-replay-refused")
    return tuple(evidence)


class _AdmissionCheckingTransport:
    transport_qualification: Literal["deterministic_fixture"] = "deterministic_fixture"

    def __init__(
        self,
        controller: BoundedCodexBridgeController,
        run_id: str,
        clock: _StepClock,
    ) -> None:
        self.controller = controller
        self.run_id = run_id
        self.auth_mode = _MODE
        self.delegate = DeterministicReadOnlyTransport(auth_mode=_MODE, clock=clock)
        self.admission_seen = False

    def execute(self, request: BoundedCodexBridgeRequestV1) -> BridgeTransportResult:
        names = {
            item.logical_name
            for item in self.controller.store.read_current(self.run_id).manifest.inventory
        }
        self.admission_seen = (
            role_artifact_name(request.context.assignment.role, "admission") in names
        )
        return self.delegate.execute(request)


class _FailingTransport:
    transport_qualification: Literal["deterministic_fixture"] = "deterministic_fixture"

    def __init__(self, effect_state: BridgeEffectState) -> None:
        self.auth_mode = _MODE
        self.effect_state = effect_state
        self.calls = 0

    def execute(self, request: BoundedCodexBridgeRequestV1) -> NoReturn:
        self.calls += 1
        launched = self.effect_state is not BridgeEffectState.NOT_LAUNCHED
        raise BridgeTransportFailure(
            f"fixture-{self.effect_state.value}",
            effect_state=self.effect_state,
            launched_at=datetime(2030, 1, 1, tzinfo=UTC) if launched else None,
            completed_at=datetime(2030, 1, 1, 0, 0, 1, tzinfo=UTC),
            duration_ms=1,
            stream_bytes=0,
            thread_id_sha256="a" * 64 if launched else None,
            turn_id_sha256="b" * 64 if launched else None,
        )


class _UnknownCrashTransport:
    auth_mode = _MODE
    transport_qualification: Literal["deterministic_fixture"] = "deterministic_fixture"

    def execute(self, request: BoundedCodexBridgeRequestV1) -> NoReturn:
        raise RuntimeError("synthetic transport crash")


class _ThreadOnlyFailureTransport:
    auth_mode = _MODE
    transport_qualification: Literal["deterministic_fixture"] = "deterministic_fixture"

    def __init__(self, thread_id_sha256: str) -> None:
        self.thread_id_sha256 = thread_id_sha256
        self.calls = 0

    def execute(self, request: BoundedCodexBridgeRequestV1) -> NoReturn:
        self.calls += 1
        raise BridgeTransportFailure(
            "fixture-thread-started-before-turn",
            effect_state=BridgeEffectState.EFFECT_UNKNOWN,
            launched_at=None,
            completed_at=datetime(2030, 1, 1, 0, 0, 1, tzinfo=UTC),
            duration_ms=1,
            stream_bytes=0,
            thread_id_sha256=self.thread_id_sha256,
            turn_id_sha256=None,
        )


def _durable_effect_recovery(context: _Context) -> tuple[str, ...]:
    source = context.source("QcJc", "call")
    controller, clock = context.controller(source, "admission-check")
    run_id = "bridge-eval-admission-check"
    context.confirm(controller, run_id, BridgeRole.STRATEGY_ANALYST, "admission-check")
    checking = _AdmissionCheckingTransport(controller, run_id, clock)
    controller.execute_confirmed_role(
        run_id,
        BridgeRole.STRATEGY_ANALYST,
        auth_mode=_MODE,
        current_source_terminal_manifest_sha256=source.source.source_terminal_manifest_sha256,
        transport=checking,
    )
    statuses: list[str] = []
    reconciliation: list[bool] = []
    no_retry = True
    for ordinal, state in enumerate(
        (
            BridgeEffectState.NOT_LAUNCHED,
            BridgeEffectState.TIMED_OUT,
            BridgeEffectState.CANCELLED,
            BridgeEffectState.CANCEL_UNCONFIRMED,
        )
    ):
        token = f"failure-{ordinal}"
        failed_controller, _failed_clock = context.controller(source, token)
        failed_run_id = f"bridge-eval-{token}"
        context.confirm(
            failed_controller,
            failed_run_id,
            BridgeRole.STRATEGY_ANALYST,
            token,
        )
        transport = _FailingTransport(state)
        failed_read = failed_controller.execute_confirmed_role(
            failed_run_id,
            BridgeRole.STRATEGY_ANALYST,
            auth_mode=_MODE,
            current_source_terminal_manifest_sha256=(source.source.source_terminal_manifest_sha256),
            transport=transport,
        )
        replayed = replay_bridge(failed_read)
        statuses.append(replayed.status)
        reconciliation.append(replayed.reconciliation_required)
        no_retry = no_retry and transport.calls == 1
    crash_controller, _crash_clock = context.controller(source, "crash")
    crash_run_id = "bridge-eval-crash"
    context.confirm(crash_controller, crash_run_id, BridgeRole.STRATEGY_ANALYST, "crash")
    crash_read = crash_controller.execute_confirmed_role(
        crash_run_id,
        BridgeRole.STRATEGY_ANALYST,
        auth_mode=_MODE,
        current_source_terminal_manifest_sha256=source.source.source_terminal_manifest_sha256,
        transport=_UnknownCrashTransport(),
    )
    crash_replay = replay_bridge(crash_read)
    shared_store = BoundedCodexBridgeStore(context.root / "b-partial-thread-shared")
    thread_id_sha256 = "c" * 64

    def execute_partial_thread(
        token: str,
    ) -> tuple[VerifiedBridgeRead, _ThreadOnlyFailureTransport]:
        partial_clock = _StepClock()
        partial_controller = BoundedCodexBridgeController(shared_store, clock=partial_clock)
        partial_run_id = f"bridge-eval-{token}"
        partial_controller.prepare_run(
            bridge_run_id=partial_run_id,
            source_context=source,
            repository_root=context.repository_root,
            repository_commit_id=context.source_commit_id,
            repository_tree_id=context.source_tree_id,
            auth_mode=_MODE,
        )
        context.confirm(
            partial_controller,
            partial_run_id,
            BridgeRole.STRATEGY_ANALYST,
            token,
        )
        partial_transport = _ThreadOnlyFailureTransport(thread_id_sha256)
        partial_read = partial_controller.execute_confirmed_role(
            partial_run_id,
            BridgeRole.STRATEGY_ANALYST,
            auth_mode=_MODE,
            current_source_terminal_manifest_sha256=(source.source.source_terminal_manifest_sha256),
            transport=partial_transport,
        )
        return partial_read, partial_transport

    first_partial, first_partial_transport = execute_partial_thread("partial-thread-first")
    first_partial_replay = replay_bridge(first_partial)
    first_partial_audit = _artifact_models(first_partial)[
        role_artifact_name(BridgeRole.STRATEGY_ANALYST, "audit")
    ]
    assert isinstance(first_partial_audit, BridgeExecutionAuditV1)
    claim_path = shared_store.root / ".i" / f"thread-{thread_id_sha256}.json"
    partial_terminal_replayed = (
        first_partial.pointer.status == "effect_unknown"
        and first_partial.completion_marker is not None
        and first_partial_audit.effect_state is BridgeEffectState.EFFECT_UNKNOWN
        and first_partial_audit.thread_id_sha256 == thread_id_sha256
        and first_partial_audit.turn_id_sha256 is None
        and first_partial_audit.launched_at is None
        and first_partial_audit.failure_reason_code == "fixture-thread-started-before-turn"
        and first_partial_replay.status == "effect_unknown"
        and first_partial_replay.reconciliation_required
        and first_partial_transport.calls == 1
        and claim_path.is_file()
    )
    second_partial, second_partial_transport = execute_partial_thread("partial-thread-second")
    second_partial_replay = replay_bridge(second_partial)
    second_partial_audit = _artifact_models(second_partial)[
        role_artifact_name(BridgeRole.STRATEGY_ANALYST, "audit")
    ]
    assert isinstance(second_partial_audit, BridgeExecutionAuditV1)
    collision_refused = (
        second_partial.pointer.status == "effect_unknown"
        and second_partial_audit.thread_id_sha256 == thread_id_sha256
        and second_partial_audit.turn_id_sha256 is None
        and second_partial_audit.failure_reason_code == "execution_identity_registry_rejected"
        and second_partial_replay.reconciliation_required
        and second_partial_transport.calls == 1
    )
    claim_path.write_bytes(b"{}")
    corruption_refused = False
    try:
        shared_store.read_current(first_partial.pointer.bridge_run_id)
    except BridgeCanonicalError:
        corruption_refused = True
    history_corruption_refused = False
    try:
        shared_store.verify_execution_identity_history()
    except BridgeStorageError:
        history_corruption_refused = True
    evidence: list[str] = []
    if checking.admission_seen:
        evidence.append("admission-is-durable-before-transport")
    if (
        statuses == ["failed", "timed_out", "cancelled", "cancel_unconfirmed"]
        and reconciliation == [False, False, False, True]
        and no_retry
    ):
        evidence.append("failure-states-terminal-without-retry")
    if crash_replay.status == "effect_unknown" and crash_replay.reconciliation_required:
        evidence.append("effect-unknown-requires-reconciliation")
    if partial_terminal_replayed:
        evidence.append("partial-thread-terminal-publication-and-replay")
    if collision_refused and corruption_refused and history_corruption_refused:
        evidence.append("partial-thread-claim-collision-and-corruption-refused")
    if (
        source.math.solver_status == "unavailable"
        and b"gto" not in canonical_json_bytes(source).lower()
    ):
        evidence.append("local-source-solver-unavailable-no-gto")
    return tuple(evidence)


_HANDLERS: dict[str, Callable[[_Context], tuple[str, ...]]] = {
    "exact-source-authority": _exact_source_authority,
    "runtime-auth-mode-separation": _runtime_auth_mode_separation,
    "serial-role-control": _serial_role_control,
    "strict-schema-policy": _strict_schema_policy,
    "durable-effect-recovery": _durable_effect_recovery,
}


def load_bounded_codex_bridge_evaluation_fixture(
    path: Path,
) -> BoundedCodexBridgeEvaluationFixtureV1:
    return BoundedCodexBridgeEvaluationFixtureV1.model_validate_json(
        path.read_bytes(),
        strict=True,
    )


def run_bounded_codex_bridge_evaluation(
    fixture: BoundedCodexBridgeEvaluationFixtureV1,
    *,
    repository_root: Path,
    work_root: Path,
    source_commit_id: str,
    source_tree_id: str,
) -> BoundedCodexBridgeEvaluationResultV1:
    verify_bounded_river_call_ev_evaluation_module_origins(repository_root)
    verify_bounded_river_call_ev_evaluation_checkout(
        repository_root,
        source_commit_id=source_commit_id,
        source_tree_id=source_tree_id,
    )
    root = Path(mkdtemp(prefix="p2-025b-", dir=work_root.resolve()))
    context = _Context(root, repository_root.resolve(), source_commit_id, source_tree_id)
    case_results: list[BoundedCodexBridgeEvaluationCaseResultV1] = []
    for case in fixture.cases:
        observation_error_type: str | None = None
        try:
            observed = _HANDLERS[case.case_id](context)
        except Exception as exc:
            observed = ("evaluation-observation-failed",)
            observation_error_type = type(exc).__name__
        passed = observed == case.expected_evidence
        case_results.append(
            BoundedCodexBridgeEvaluationCaseResultV1(
                case_id=case.case_id,
                metric=case.metric,
                expected_evidence=case.expected_evidence,
                observed_evidence=observed,
                score="1.0" if passed else "0.0",
                passed=passed,
                observation_error_type=observation_error_type,
            )
        )
    metrics: list[BoundedCodexBridgeEvaluationMetricV1] = []
    for metric_name in REQUIRED_METRICS:
        selected = tuple(item for item in case_results if item.metric == metric_name)
        declared = sum(len(item.expected_evidence) for item in selected)
        passed_checks = sum(len(item.expected_evidence) if item.passed else 0 for item in selected)
        metrics.append(
            BoundedCodexBridgeEvaluationMetricV1(
                metric=metric_name,
                declared_checks=declared,
                passed_checks=passed_checks,
                score="1.0" if declared == passed_checks else "0.0",
            )
        )
    passed = all(item.passed for item in case_results)
    payload: dict[str, object] = {
        "schema_version": EVALUATION_SCHEMA_VERSION,
        "family_id": EVALUATION_FAMILY_ID,
        "fixture_id": fixture.fixture_id,
        "scoring": fixture.scoring,
        "threshold": fixture.threshold,
        "source_commit_id": source_commit_id,
        "source_tree_id": source_tree_id,
        "case_results": tuple(case_results),
        "metrics": tuple(metrics),
        "overall_score": "1.0" if passed else "0.0",
        "passed": passed,
        "transport_qualification": "deterministic_fixture_only",
        "live_qualification_sha256": None,
    }
    return BoundedCodexBridgeEvaluationResultV1.model_validate(
        {**payload, "result_sha256": domain_sha256(EVALUATION_FAMILY_ID, payload)},
        strict=True,
    )


__all__ = [
    "EVALUATION_FAMILY_ID",
    "REQUIRED_CASE_EVIDENCE",
    "REQUIRED_CASE_IDS",
    "REQUIRED_METRICS",
    "BoundedCodexBridgeEvaluationFixtureV1",
    "BoundedCodexBridgeEvaluationResultV1",
    "load_bounded_codex_bridge_evaluation_fixture",
    "run_bounded_codex_bridge_evaluation",
]
