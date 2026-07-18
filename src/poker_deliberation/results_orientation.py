"""Deterministic detection of result-oriented decision rationales."""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ResultsOrientationFinding:
    rule_id: str
    correction: str


_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "ja-outcome-proves-decision",
        re.compile(
            r"(?:勝(?:った|てた|てたので)|儲かった|利益が出た).{0,30}"
            r"(?:から|ので).{0,30}(?:正しかった|正解|良い(?:判断|プレイ))|"
            r"(?:負けた|損した).{0,30}(?:から|ので).{0,30}"
            r"(?:間違い|不正解|悪い(?:判断|プレイ))"
        ),
    ),
    (
        "en-outcome-proves-decision",
        re.compile(
            r"(?:won|made\s+money).{0,40}(?:therefore|so|means).{0,20}"
            r"(?:correct|right|good\s+(?:call|play|decision))|"
            r"(?:lost|lost\s+money).{0,40}(?:therefore|so|means).{0,20}"
            r"(?:wrong|bad\s+(?:call|play|decision))",
            re.IGNORECASE,
        ),
    ),
)


def detect_results_orientation(text: str) -> list[ResultsOrientationFinding]:
    """Flag outcome-as-proof reasoning without deciding the underlying action."""

    findings: list[ResultsOrientationFinding] = []
    for rule_id, pattern in _RULES:
        if pattern.search(text):
            findings.append(
                ResultsOrientationFinding(
                    rule_id=rule_id,
                    correction=(
                        "実現結果を根拠とする部分だけを棄却し、意思決定時点のポットオッズ、"
                        "レンジ仮定、EV、ICM等で再評価してください。アクション自体の正誤は"
                        "この検出だけでは決まりません。"
                    ),
                )
            )
    return findings
