from __future__ import annotations

import pytest

from poker_deliberation.providers import OpenAIAgentsProvider, ProviderControl, ProviderStatus
from poker_deliberation.providers import openai_agents as openai_agents_module
from poker_deliberation.schemas import AgentAssignment, AgentContext


@pytest.mark.parametrize(
    ("package_present", "key_present"),
    [(False, False), (False, True), (True, False), (True, True)],
)
def test_openai_provider_is_disabled_for_every_sdk_key_combination(
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
        monkeypatch.setenv("OPENAI_API_KEY", "synthetic-test-value")
    monkeypatch.setattr(OpenAIAgentsProvider, "_version", staticmethod(lambda: "test-version"))

    availability = OpenAIAgentsProvider().availability()

    assert availability.status is ProviderStatus.DISABLED
    assert availability.available is False
    assert "not implemented" in availability.reason
    assert ("present" if package_present else "absent") in availability.reason
    assert ("configured" if key_present else "not configured") in availability.reason
    assert availability.version == ("test-version" if package_present else None)


def test_openai_provider_analyze_is_explicitly_not_implemented() -> None:
    provider = OpenAIAgentsProvider()
    context = AgentContext(kind="claim", objective="test")
    assignment = AgentAssignment(agent_role="skeptic", task="test")

    with pytest.raises(NotImplementedError, match="no user data was sent"):
        provider.analyze(context, assignment, ProviderControl(timeout_seconds=1))
