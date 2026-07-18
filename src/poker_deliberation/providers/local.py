"""Non-generative provider that never fabricates specialist findings."""

from __future__ import annotations

from poker_deliberation.providers.base import ProviderAvailability, ProviderControl
from poker_deliberation.schemas import AgentAssignment, AgentContext, AgentReport, ConfidenceGrade


class LocalProvider:
    def availability(self) -> ProviderAvailability:
        return ProviderAvailability(
            available=True,
            provider="local",
            reason="deterministic local validation and calculators are available",
            version="1.0.0",
        )

    def analyze(
        self,
        context: AgentContext,
        assignment: AgentAssignment,
        control: ProviderControl,
    ) -> AgentReport:
        control.raise_if_cancelled()
        return AgentReport(
            agent_role=assignment.agent_role,
            task=assignment.task,
            conclusions=[],
            uncertainties=[
                "外部モデルは使用していません。文章的な専門分析は生成せず、検証済みローカル計算だけを採用します。"
            ],
            confidence=ConfidenceGrade.C,
            unresolved_questions=(
                ["モデル支援の独立分析が必要な場合はProviderとAPIキーを設定してください。"]
                if context.kind in {"hand", "strategy", "claim"}
                else []
            ),
        )
