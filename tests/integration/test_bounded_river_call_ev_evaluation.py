from __future__ import annotations

import sys
from pathlib import Path
from types import ModuleType

import pytest

import poker_deliberation.bounded_river_call_ev_evaluation as evaluation_module
import poker_deliberation.tools.registry as registry_module
from poker_deliberation.bounded_river_call_ev_evaluation import (
    EVALUATION_FAMILY_ID,
    REQUIRED_CASE_IDS,
    REQUIRED_METRICS,
    BoundedRiverCallEvEvaluationFixtureV1,
    BoundedRiverCallEvEvaluationResultV1,
    load_bounded_river_call_ev_evaluation_fixture,
    run_bounded_river_call_ev_evaluation,
    verify_bounded_river_call_ev_evaluation_checkout,
    verify_bounded_river_call_ev_evaluation_module_origins,
)
from poker_deliberation.range_equity_models import canonical_domain_sha256

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "tests" / "fixtures" / "bounded_river_call_ev" / "v1" / "scenarios.json"
COMMIT_ID = "1" * 40
TREE_ID = "2" * 40


def _clean_checkout(monkeypatch: pytest.MonkeyPatch) -> dict[tuple[str, ...], str]:
    responses = {
        ("rev-parse", "HEAD"): COMMIT_ID,
        ("rev-parse", "HEAD^{tree}"): TREE_ID,
        ("status", "--porcelain=v1", "--untracked-files=all"): "",
        ("replace", "-l"): "",
        ("ls-files", "-v"): "H tracked.py",
    }

    def git_stdout(_root: Path, *arguments: str) -> str:
        return responses[arguments]

    monkeypatch.setattr(evaluation_module, "_git_stdout", git_stdout)
    monkeypatch.setattr(
        evaluation_module,
        "verify_bounded_river_call_ev_evaluation_module_origins",
        lambda _root: None,
    )
    return responses


def test_bounded_river_call_ev_evaluation_scores_all_metrics(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clean_checkout(monkeypatch)
    result = run_bounded_river_call_ev_evaluation(
        load_bounded_river_call_ev_evaluation_fixture(FIXTURE),
        repository_root=tmp_path,
        work_root=tmp_path / "run",
        source_commit_id=COMMIT_ID,
        source_tree_id=TREE_ID,
    )

    assert result.passed is True, [
        (item.case_id, item.observed_evidence) for item in result.case_results if not item.passed
    ]
    assert result.overall_score == "1.0"
    assert tuple(item.case_id for item in result.case_results) == REQUIRED_CASE_IDS
    assert tuple(item.metric for item in result.metrics) == REQUIRED_METRICS
    assert all(item.score == "1.0" for item in result.metrics)
    assert result.source_commit_id == COMMIT_ID
    assert result.source_tree_id == TREE_ID


def test_evaluation_fixture_and_result_tamper_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clean_checkout(monkeypatch)
    fixture = load_bounded_river_call_ev_evaluation_fixture(FIXTURE)
    fixture_payload = fixture.model_dump(mode="python")
    fixture_payload["cases"] = fixture_payload["cases"][:-1]
    with pytest.raises(ValueError, match="inventory mismatch"):
        BoundedRiverCallEvEvaluationFixtureV1.model_validate(fixture_payload)

    result = run_bounded_river_call_ev_evaluation(
        fixture,
        repository_root=tmp_path,
        work_root=tmp_path / "run",
        source_commit_id=COMMIT_ID,
        source_tree_id=TREE_ID,
    )
    payload = result.model_dump(mode="python")
    payload["case_results"][0]["observed_evidence"] = ()
    payload.pop("result_sha256")
    payload["result_sha256"] = canonical_domain_sha256(EVALUATION_FAMILY_ID, payload)
    with pytest.raises(ValueError, match="case score mismatch"):
        BoundedRiverCallEvEvaluationResultV1.model_validate(payload, strict=True)


def test_evaluation_is_deterministic_and_source_bound(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    responses = _clean_checkout(monkeypatch)
    fixture = load_bounded_river_call_ev_evaluation_fixture(FIXTURE)
    first = run_bounded_river_call_ev_evaluation(
        fixture,
        repository_root=tmp_path,
        work_root=tmp_path / "first",
        source_commit_id=COMMIT_ID,
        source_tree_id=TREE_ID,
    )
    second = run_bounded_river_call_ev_evaluation(
        fixture,
        repository_root=tmp_path,
        work_root=tmp_path / "second",
        source_commit_id=COMMIT_ID,
        source_tree_id=TREE_ID,
    )
    responses[("rev-parse", "HEAD")] = "3" * 40
    rebound = run_bounded_river_call_ev_evaluation(
        fixture,
        repository_root=tmp_path,
        work_root=tmp_path / "rebound",
        source_commit_id="3" * 40,
        source_tree_id=TREE_ID,
    )

    assert first == second
    assert first.result_sha256 != rebound.result_sha256


def test_evaluation_cli_checkout_binding_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    responses = {
        ("rev-parse", "HEAD"): COMMIT_ID,
        ("rev-parse", "HEAD^{tree}"): TREE_ID,
        ("status", "--porcelain=v1", "--untracked-files=all"): "",
        ("replace", "-l"): "",
        ("ls-files", "-v"): "H tracked.py",
    }

    def git_stdout(_root: Path, *arguments: str) -> str:
        return responses[arguments]

    monkeypatch.setattr(evaluation_module, "_git_stdout", git_stdout)
    verify_bounded_river_call_ev_evaluation_checkout(
        tmp_path,
        source_commit_id=COMMIT_ID,
        source_tree_id=TREE_ID,
    )

    responses[("rev-parse", "HEAD")] = "3" * 40
    with pytest.raises(ValueError, match="checkout binding mismatch"):
        verify_bounded_river_call_ev_evaluation_checkout(
            tmp_path,
            source_commit_id=COMMIT_ID,
            source_tree_id=TREE_ID,
        )

    responses[("rev-parse", "HEAD")] = COMMIT_ID
    responses[("status", "--porcelain=v1", "--untracked-files=all")] = " M tracked.py"
    with pytest.raises(ValueError, match="checkout binding mismatch"):
        verify_bounded_river_call_ev_evaluation_checkout(
            tmp_path,
            source_commit_id=COMMIT_ID,
            source_tree_id=TREE_ID,
        )


def test_evaluation_loaded_module_origins_are_checkout_bound(tmp_path: Path) -> None:
    verify_bounded_river_call_ev_evaluation_module_origins(ROOT)
    with pytest.raises(ValueError, match="module origin mismatch"):
        verify_bounded_river_call_ev_evaluation_module_origins(tmp_path)


def test_evaluation_loaded_module_origin_check_covers_tool_dependency(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module_name = "poker_deliberation.tools.hand_pot_ledger"
    foreign_module = ModuleType(module_name)
    foreign_module.__file__ = str(tmp_path / "foreign" / "hand_pot_ledger.py")
    monkeypatch.setitem(sys.modules, module_name, foreign_module)

    with pytest.raises(ValueError, match="module origin mismatch"):
        verify_bounded_river_call_ev_evaluation_module_origins(ROOT)


def test_evaluation_rejects_stale_foreign_registry_callable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    namespace: dict[str, object] = {}
    foreign_path = tmp_path / "foreign" / "hand_pot_ledger.py"
    exec(
        compile(
            "def calculate_hand_pot_ledger(payload):\n    return payload\n",
            str(foreign_path),
            "exec",
        ),
        namespace,
    )
    monkeypatch.setattr(
        registry_module,
        "calculate_hand_pot_ledger",
        namespace["calculate_hand_pot_ledger"],
    )

    with pytest.raises(ValueError, match="callable origin mismatch"):
        verify_bounded_river_call_ev_evaluation_module_origins(ROOT)


def test_evaluation_rechecks_loaded_module_origins_after_handlers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clean_checkout(monkeypatch)
    calls: list[Path] = []

    def record_origin_check(repository_root: Path) -> None:
        calls.append(repository_root)

    monkeypatch.setattr(
        evaluation_module,
        "verify_bounded_river_call_ev_evaluation_module_origins",
        record_origin_check,
    )
    result = run_bounded_river_call_ev_evaluation(
        load_bounded_river_call_ev_evaluation_fixture(FIXTURE),
        repository_root=tmp_path,
        work_root=tmp_path / "run",
        source_commit_id=COMMIT_ID,
        source_tree_id=TREE_ID,
    )

    assert result.passed is True
    assert calls == [tmp_path, tmp_path]
