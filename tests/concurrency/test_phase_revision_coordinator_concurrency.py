"""P2-010B phase revision coordinator concurrency tests."""

from __future__ import annotations

from collections import Counter
from collections.abc import Generator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Barrier

import pytest

from poker_deliberation.phases.revision_coordinator import (
    PhaseRevisionFailureV1,
    PhaseTransitionApplyResultV1,
    PhaseTransitionAuthorizationV1,
)
from poker_deliberation.state_machine import RunState
from tests.integration.test_phase_revision_coordinator import (
    build_valid_scenario,
)

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def short_tmp() -> Generator[Path, None, None]:
    parent = ROOT / "tmp"
    parent.mkdir(exist_ok=True)
    with TemporaryDirectory(prefix="p2cc-", dir=parent) as directory:
        yield Path(directory)


def test_concurrent_exact_apply_creates_one_terminal_event(
    short_tmp: Path,
) -> None:
    orchestrator, machine, coordinator, bundle = build_valid_scenario(short_tmp)
    authorization = coordinator.publish(bundle)
    assert isinstance(authorization, PhaseTransitionAuthorizationV1)
    barrier = Barrier(8)

    def apply() -> PhaseTransitionApplyResultV1 | PhaseRevisionFailureV1:
        barrier.wait()
        return orchestrator.apply_revision_transition(
            machine,
            coordinator=coordinator,
            bundle=bundle,
            authorization=authorization,
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = tuple(pool.map(lambda _index: apply(), range(8)))

    kinds = Counter(
        result.outcome_kind
        for result in results
        if isinstance(result, PhaseTransitionApplyResultV1)
    )
    assert kinds == {"applied": 1, "already_applied": 7}
    assert machine.state is RunState.COMPLETED
    assert len(machine.events) == 1
    assert machine.events[0].source is RunState.FINAL_SYNTHESIS
    assert machine.events[0].target is RunState.COMPLETED


def test_concurrent_exact_publish_never_creates_a_successor_revision(
    short_tmp: Path,
) -> None:
    _orchestrator, machine, coordinator, bundle = build_valid_scenario(short_tmp)
    barrier = Barrier(2)

    def publish():  # type: ignore[no-untyped-def]
        barrier.wait()
        return coordinator.publish(bundle)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = tuple(pool.map(lambda _index: publish(), range(2)))

    assert any(isinstance(result, PhaseTransitionAuthorizationV1) for result in results)
    assert all(
        isinstance(
            result,
            (PhaseTransitionAuthorizationV1, PhaseRevisionFailureV1),
        )
        for result in results
    )
    assert coordinator.store.read_current(bundle.request.run_id).current_revision == 1
    assert machine.state is RunState.FINAL_SYNTHESIS
    assert machine.events == []
