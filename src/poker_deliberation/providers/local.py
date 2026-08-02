"""Non-generative provider that never fabricates specialist findings."""

from __future__ import annotations

from poker_deliberation.budgets import ExecutionClass
from poker_deliberation.providers.base import (
    ProviderAvailability,
    ProviderControl,
    ProviderStatus,
)
from poker_deliberation.schemas import AgentAssignment, AgentContext, AgentReport, ConfidenceGrade

LOCAL_PROVIDER_UNCERTAINTY = (
    "外部モデルは使用していません。文章的な専門分析は生成せず、"
    "検証済みローカル計算だけを採用します。"
)
LOCAL_PROVIDER_MODEL_SUPPORT_QUESTION = (
    "モデル支援の独立分析が必要な場合はProviderとAPIキーを設定してください。"
)


class LocalProvider:
    def availability(self) -> ProviderAvailability:
        return ProviderAvailability(
            status=ProviderStatus.AVAILABLE,
            available=True,
            provider="local",
            reason="deterministic local validation and calculators are available",
            version="1.0.0",
            execution_class=ExecutionClass.LOCAL_FREE,
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
            uncertainties=[LOCAL_PROVIDER_UNCERTAINTY],
            confidence=ConfidenceGrade.C,
            unresolved_questions=(
                [LOCAL_PROVIDER_MODEL_SUPPORT_QUESTION]
                if context.kind in {"hand", "strategy", "claim"}
                else []
            ),
        )
