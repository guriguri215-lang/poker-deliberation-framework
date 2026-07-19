from __future__ import annotations

import pytest

from poker_deliberation.reporting import render_markdown
from poker_deliberation.schemas import Exactness, FinalReport, ToolResult, ToolStatus

pytestmark = pytest.mark.golden


def _render(result: ToolResult) -> str:
    return render_markdown(
        FinalReport(
            run_id=f"golden-{result.status.value}-{result.exactness.value}",
            conclusion="fixture",
            tool_results=[result],
            reproduction_steps=["argv-json fixture"],
        )
    )


def test_exact_tool_result_metadata_golden() -> None:
    rendered = _render(
        ToolResult(
            result_id="exact-result",
            tool_name="pot_odds",
            input={"pot_before_bet": 100},
            output={"required_equity": 0.25},
            status=ToolStatus.SUCCESS,
            exactness=Exactness.EXACT,
            assumptions=["one chip unit"],
            warnings=[],
            version="1.2.3",
            duration_seconds=0.125,
            reproduce_command="poker-deliberate calculate pot_odds --input fixture.json",
        )
    )
    assert "- 状態: `success`" in rendered
    assert "- 区分: `exact`" in rendered
    assert '- 仮定: `["one chip unit"]`' in rendered
    assert "- seed: `null`" in rendered
    assert "- samples: `null`" in rendered
    assert "- 信頼区間: `null`" in rendered
    assert "- エラー: `null`" in rendered
    assert "calculate pot_odds" in rendered


def test_approximate_tool_result_metadata_golden() -> None:
    rendered = _render(
        ToolResult(
            result_id="approximate-result",
            tool_name="holdem_equity",
            input={"mode": "monte_carlo"},
            output={"hero_equity": 0.5},
            status=ToolStatus.SUCCESS,
            exactness=Exactness.APPROXIMATE,
            assumptions=["heads-up only"],
            warnings=["sampling error remains"],
            seed=7,
            samples=1000,
            confidence_interval=(0.45, 0.55),
            version="1.0.0",
            duration_seconds=1.5,
            reproduce_command="poker-deliberate calculate holdem_equity --input fixture.json",
        )
    )
    assert "- 区分: `approximate`" in rendered
    assert '- 警告: `["sampling error remains"]`' in rendered
    assert "- seed: `7`" in rendered
    assert "- samples: `1000`" in rendered
    assert "- 信頼区間: `[0.45, 0.55]`" in rendered


def test_failed_tool_result_metadata_golden() -> None:
    rendered = _render(
        ToolResult(
            result_id="failed-result",
            tool_name="pot_odds",
            input={"pot_before_bet": -1},
            status=ToolStatus.FAILED,
            exactness=Exactness.UNAVAILABLE,
            assumptions=["non-negative amounts"],
            error="ValueError: invalid amount",
            reproduce_command="poker-deliberate calculate pot_odds --input invalid.json",
        )
    )
    assert "- 状態: `failed`" in rendered
    assert "- 区分: `unavailable`" in rendered
    assert '- エラー: `"ValueError: invalid amount"`' in rendered
    assert '"output"' not in rendered


def test_unavailable_tool_result_metadata_golden() -> None:
    rendered = _render(
        ToolResult(
            result_id="unavailable-result",
            tool_name="solver_status",
            input={},
            status=ToolStatus.UNAVAILABLE,
            exactness=Exactness.UNAVAILABLE,
            warnings=["no solver configured"],
            error="external solver unavailable",
            reproduce_command="poker-deliberate calculate solver_status --input empty.json",
        )
    )
    assert "- 状態: `unavailable`" in rendered
    assert "- 区分: `unavailable`" in rendered
    assert '"external solver unavailable"' in rendered
    assert '- 警告: `["no solver configured"]`' in rendered
