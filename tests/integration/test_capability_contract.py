from __future__ import annotations

import re
from pathlib import Path

import pytest

from poker_deliberation.agents import ROLE_CATALOG
from poker_deliberation.capabilities import CAPABILITIES, capability_snapshot
from poker_deliberation.cli import doctor
from poker_deliberation.normalization import (
    NORMALIZATION_PARSER_VERSION,
    NORMALIZATION_SUPPORTED_SITE,
    normalize_hand_text,
)
from poker_deliberation.providers import (
    LocalProvider,
    OpenAIAgentsProvider,
    ProviderControl,
    ProviderStatus,
)
from poker_deliberation.providers import openai_agents as openai_agents_module
from poker_deliberation.schemas import (
    AgentAssignment,
    AgentContext,
    Exactness,
    ToolStatus,
)
from poker_deliberation.tools import default_registry

ROOT = Path(__file__).resolve().parents[2]


def test_capability_matrix_matches_code_and_doctor() -> None:
    text = (ROOT / "docs" / "capabilities.md").read_text(encoding="utf-8")
    documented = dict(
        re.findall(
            r"^\| `([^`]+)` \| \*\*(implemented|disabled|unavailable|planned)\*\* \|",
            text,
            flags=re.MULTILINE,
        )
    )
    expected = {item.capability_id: item.state for item in CAPABILITIES}
    assert documented == expected
    assert doctor()["capabilities"] == capability_snapshot()
    assert set(documented.values()) <= {"implemented", "disabled", "unavailable", "planned"}
    assert {"implemented", "disabled", "unavailable"} <= set(documented.values())


def test_phase_1_and_runtime_surface_capabilities_match_executable_boundaries() -> None:
    states = {item.capability_id: item.state for item in CAPABILITIES}
    descriptions = default_registry().describe()

    assert states["phase_1_hardening"] == "implemented"
    assert len(descriptions) == 21
    assert {item["contract_version"] for item in descriptions} == {"2.0.0"}
    assert all(item["input_schema"] and item["output_schema"] for item in descriptions)
    assert states["runtime_conformance_contract"] == "implemented"
    assert states["codex_python_runtime_bridge"] == "unavailable"
    assert states["immutable_revision_storage_foundation"] == "implemented"
    assert states["product_integrated_durable_run"] == "implemented"
    assert states["local_data_cleanup_executor"] == "implemented"
    assert states["offline_evaluation_harness"] == "implemented"


def test_codex_and_python_are_documented_as_separate_execution_surfaces() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    capabilities = (ROOT / "docs" / "capabilities.md").read_text(encoding="utf-8")
    architecture = (ROOT / "docs" / "architecture.md").read_text(encoding="utf-8")

    assert "別実行面" in readme
    assert "別実行面" in capabilities
    assert "separate execution surfaces" in architecture


def test_documented_tool_and_role_counts_are_computed_contracts() -> None:
    capability_text = (ROOT / "docs" / "capabilities.md").read_text(encoding="utf-8")
    tool_count = len(default_registry().names())
    codex_role_count = len(list((ROOT / ".codex" / "agents").glob("*.toml")))
    python_role_count = len(ROLE_CATALOG)

    assert (tool_count, codex_role_count, python_role_count) == (21, 9, 7)
    assert f"`{tool_count}`個のtool名" in capability_text
    assert f"`{codex_role_count}`定義" in capability_text
    assert f"`{python_role_count}`役" in capability_text


def test_quality_and_public_preflight_commands_are_documented() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    capabilities = (ROOT / "docs" / "capabilities.md").read_text(encoding="utf-8")
    checklist = (ROOT / "docs" / "public-release-checklist.md").read_text(encoding="utf-8")
    for command in ("python -m pytest", "ruff check .", "ruff format --check .", "mypy src"):
        assert command in readme
        assert command in capabilities
    assert "docs/capabilities.md" in readme
    assert "scripts\\public_preflight.py" in checklist
    assert "CPython 3.11-3.13" in capabilities
    assert "UNKNOWN" in capabilities


def test_local_provider_is_available_but_non_generative() -> None:
    provider = LocalProvider()
    availability = provider.availability()
    report = provider.analyze(
        AgentContext(kind="claim", objective="contract probe"),
        AgentAssignment(agent_role="skeptic", task="probe implementation semantics"),
        ProviderControl(timeout_seconds=1),
    )

    assert availability.status is ProviderStatus.AVAILABLE
    assert availability.available is True
    assert report.conclusions == []
    assert report.uncertainties


@pytest.mark.parametrize(
    ("package_present", "key_present"),
    [(False, False), (False, True), (True, False), (True, True)],
)
def test_openai_provider_contract_is_disabled_for_all_local_prerequisites(
    package_present: bool,
    key_present: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        openai_agents_module.importlib.util,
        "find_spec",
        lambda name: object() if name == "agents" and package_present else None,
    )
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    if key_present:
        monkeypatch.setenv("OPENAI_API_KEY", "synthetic-contract-value")
    monkeypatch.setattr(OpenAIAgentsProvider, "_version", staticmethod(lambda: "fixture"))

    availability = OpenAIAgentsProvider().availability()

    assert availability.status is ProviderStatus.DISABLED
    assert availability.available is False


def test_openai_provider_analyze_stops_at_the_local_not_implemented_boundary() -> None:
    with pytest.raises(NotImplementedError, match="no user data was sent"):
        OpenAIAgentsProvider().analyze(
            AgentContext(kind="claim", objective="contract probe"),
            AgentAssignment(agent_role="skeptic", task="do not send"),
            ProviderControl(timeout_seconds=1),
        )


def test_solver_status_is_unavailable_without_strategy_or_numeric_results() -> None:
    result = default_registry().execute("solver_status", {})

    assert result.status is ToolStatus.UNAVAILABLE
    assert result.exactness is Exactness.UNAVAILABLE
    assert result.output["status"] == "unavailable"
    assert result.output["result"] == {}
    assert result.output["capability"]["available"] is False
    assert result.output["capability"]["supported_games"] == []
    assert result.output["capability"]["supported_operations"] == []


def test_equity_contract_executes_heads_up_nlhe_and_rejects_plo_and_multiway() -> None:
    registry = default_registry()
    base_input = {
        "hero_range": "AsAh",
        "villain_range": "KcKd",
        "board": ["2c", "3d", "4h", "5s", "9c"],
        "mode": "exact",
    }

    heads_up = registry.execute("holdem_equity", base_input)
    plo = registry.execute("holdem_equity", {**base_input, "game_type": "PLO"})
    multiway = registry.execute(
        "holdem_equity",
        {**base_input, "opponent_ranges": ["KcKd", "QcQd"]},
    )

    assert heads_up.status is ToolStatus.SUCCESS
    assert heads_up.output["exact"] is True
    assert plo.status is ToolStatus.FAILED
    assert "NLHE only" in str(plo.error)
    assert multiway.status is ToolStatus.FAILED
    assert "exactly one villain" in str(multiway.error)


def test_parser_contract_accepts_documented_grammar_but_not_site_history() -> None:
    documented = normalize_hand_text(
        (ROOT / "examples" / "free_text_hand.txt").read_text(encoding="utf-8")
    )
    site_history = normalize_hand_text(
        "PokerStars Hand #12345\nSeat 1: Hero (100 in chips)\n*** HOLE CARDS ***"
    )

    assert documented.hand is not None
    assert documented.hand.game_type == "NLHE"
    assert NORMALIZATION_PARSER_VERSION == "1.0.0"
    assert NORMALIZATION_SUPPORTED_SITE == "none"
    assert site_history.hand is None
    assert any("NRM_E_MALFORMED_LINE" in item for item in site_history.warnings)
