"""Run-ID authority tests for strict confirmed intake paths."""

from __future__ import annotations

from pathlib import Path
from threading import Event, Thread

import pytest

from poker_deliberation.config import AppConfig
from poker_deliberation.orchestrator import Orchestrator
from poker_deliberation.range_equity import admit_versioned_range_river_equity
from poker_deliberation.storage.terminal_models import (
    ProductRunError,
    ProductRunFailureCode,
)
from tests.bounded_natural_language_support import bounded_admission
from tests.confirmed_review_support import confirmed_admission
from tests.range_support import versioned_river_equity_case


def _config(root: Path) -> AppConfig:
    return AppConfig(
        runs_dir=root / "legacy",
        revision_runs_dir=root / "product",
        durable_budget_runs_dir=root / "budget",
    )


def test_bounded_preflight_and_reservation_share_run_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    bounded = Orchestrator(config)
    bridge = Orchestrator(config)
    run_id = "run-bounded-authority"
    request = bounded_admission(run_id=run_id)
    bridge_request = admit_versioned_range_river_equity(versioned_river_equity_case())
    checked_absent = Event()
    release_bounded = Event()
    original_namespace = bounded._namespace_kind
    bounded_results: list[BaseException | object] = []

    def pausing_namespace(candidate_run_id: str) -> str | None:
        observed = original_namespace(candidate_run_id)
        if candidate_run_id == run_id and not checked_absent.is_set():
            assert observed is None
            checked_absent.set()
            assert release_bounded.wait(timeout=30)
        return observed

    monkeypatch.setattr(bounded, "_namespace_kind", pausing_namespace)
    bridge_tool_calls: list[str] = []
    original_execute = bridge.registry.execute

    def counted_execute(tool_name: str, *args: object, **kwargs: object):  # type: ignore[no-untyped-def]
        bridge_tool_calls.append(tool_name)
        return original_execute(tool_name, *args, **kwargs)

    monkeypatch.setattr(bridge.registry, "execute", counted_execute)

    def run_bounded() -> None:
        try:
            bounded_results.append(bounded.run_bounded_natural_language_review(request))
        except BaseException as exc:  # pragma: no cover - asserted below
            bounded_results.append(exc)

    worker = Thread(target=run_bounded)
    worker.start()
    assert checked_absent.wait(timeout=30)
    try:
        with pytest.raises(ProductRunError) as collision:
            bridge.run_versioned_range_river_equity(bridge_request, run_id=run_id)
        assert collision.value.failure.code is ProductRunFailureCode.RUN_LOCKED
        assert bridge_tool_calls == []
    finally:
        release_bounded.set()
        worker.join(timeout=30)

    assert not worker.is_alive()
    assert len(bounded_results) == 1
    assert not isinstance(bounded_results[0], BaseException)


def test_confirmed_review_cannot_enter_preflight_while_run_authority_is_held(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    bridge = Orchestrator(config)
    confirmed = Orchestrator(config)
    run_id = "run-confirmed-authority"
    bridge_request = admit_versioned_range_river_equity(versioned_river_equity_case())
    request = confirmed_admission(run_id=run_id)
    authority_held = Event()
    release_bridge = Event()
    original_bridge_namespace = bridge._namespace_kind
    bridge_results: list[BaseException | object] = []

    def pausing_bridge_namespace(candidate_run_id: str) -> str | None:
        observed = original_bridge_namespace(candidate_run_id)
        if candidate_run_id == run_id and not authority_held.is_set():
            assert observed is None
            authority_held.set()
            assert release_bridge.wait(timeout=30)
        return observed

    monkeypatch.setattr(bridge, "_namespace_kind", pausing_bridge_namespace)
    confirmed_namespace_calls = 0
    original_confirmed_namespace = confirmed._namespace_kind

    def counted_namespace(candidate_run_id: str) -> str | None:
        nonlocal confirmed_namespace_calls
        confirmed_namespace_calls += 1
        return original_confirmed_namespace(candidate_run_id)

    monkeypatch.setattr(confirmed, "_namespace_kind", counted_namespace)

    def run_bridge() -> None:
        try:
            bridge_results.append(
                bridge.run_versioned_range_river_equity(bridge_request, run_id=run_id)
            )
        except BaseException as exc:  # pragma: no cover - asserted below
            bridge_results.append(exc)

    worker = Thread(target=run_bridge)
    worker.start()
    assert authority_held.wait(timeout=30)
    try:
        with pytest.raises(ProductRunError) as collision:
            confirmed.run_confirmed_review(request)
        assert collision.value.failure.code is ProductRunFailureCode.RUN_LOCKED
        assert confirmed_namespace_calls == 0
    finally:
        release_bridge.set()
        worker.join(timeout=30)

    assert not worker.is_alive()
    assert len(bridge_results) == 1
    assert not isinstance(bridge_results[0], BaseException)
