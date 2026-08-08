from __future__ import annotations

from pathlib import Path

import pytest

from poker_deliberation.codex_bridge import evaluation
from tests.codex_bridge_support import REPOSITORY_ROOT

FIXTURE = REPOSITORY_ROOT / "tests" / "fixtures" / "codex_bridge" / "v1" / "cases.json"


def test_deterministic_bridge_evaluation_passes_without_live_transport(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        evaluation,
        "verify_bounded_river_call_ev_evaluation_module_origins",
        lambda _root: None,
    )
    monkeypatch.setattr(
        evaluation,
        "verify_bounded_river_call_ev_evaluation_checkout",
        lambda _root, **_kwargs: None,
    )
    fixture = evaluation.load_bounded_codex_bridge_evaluation_fixture(FIXTURE)
    result = evaluation.run_bounded_codex_bridge_evaluation(
        fixture,
        repository_root=REPOSITORY_ROOT,
        work_root=tmp_path,
        source_commit_id="1" * 40,
        source_tree_id="2" * 40,
    )

    assert result.passed is True, result.case_results
    assert result.overall_score == "1.0"
    assert all(item.passed for item in result.case_results)
    assert sum(item.declared_checks for item in result.metrics) == 29
    durable = next(
        item for item in result.case_results if item.case_id == "durable-effect-recovery"
    )
    assert durable.observed_evidence[-3:-1] == (
        "partial-thread-terminal-publication-and-replay",
        "partial-thread-claim-collision-and-corruption-refused",
    )
    assert result.transport_qualification == "deterministic_fixture_only"
    assert result.live_qualification_sha256 is None
