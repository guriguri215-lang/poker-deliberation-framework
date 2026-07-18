"""Best-effort redaction for artifacts and user-visible reports."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from pydantic import BaseModel

from poker_deliberation.schemas import CaseInput, SecurityEvent

_SENSITIVE_KEY = re.compile(
    r"(?:api[_-]?key|authorization|bearer|cookie|password|passwd|secret|session[_-]?token|access[_-]?token)",
    re.IGNORECASE,
)
_SECRET_PATTERNS = (
    re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b"),
    re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{8,}\b", re.IGNORECASE),
    re.compile(
        r"\b(?:api[_-]?key|password|secret|token)\s*[:=]\s*[^\s,;]+",
        re.IGNORECASE,
    ),
)

_REAL_TIME_ASSISTANCE = re.compile(
    r"(?:今(?:まさに)?プレイ中|リアルタイム(?:で|の).{0,20}(?:指示|助言|教えて)|"
    r"(?:i(?:['\u2019]m| am)\s+)?playing\s+(?:poker\s+)?(?:right\s+)?now\b|"
    r"(?:i(?:['\u2019]m| am)\s+)?at\s+(?:a|the)\s+poker\s+table\s+(?:right\s+)?now\b|"
    r"(?:i(?:['\u2019]m| am)\s+)?in\s+(?:an?\s+)?(?:online\s+)?"
    r"(?:poker\s+|cash\s+)?game\s+"
    r"(?:right\s+)?now\b|"
    r"(?:i(?:['\u2019]m| am)\s+)?seated\s+at\s+(?:a|the)\s+"
    r"(?:poker|tournament)\s+table\s+(?:right\s+)?now\b|"
    r"今(?:キャッシュゲーム|トーナメント)(?:を)?(?:打って|プレイして)います|"
    r"(?:currently\s+playing|in\s+(?:a|the)\s+live\s+hand).{0,40}"
    r"(?:call|fold|check|bet|raise|shove|all[- ]?in|what\s+should\s+i\s+do)|"
    r"(?:give|provide|need|want|use).{0,20}real[- ]?time.{0,20}(?:advice|assistance)|"
    r"real[- ]?time.{0,20}(?:advice|assistance).{0,20}while\s+i\s+play)",
    re.IGNORECASE,
)
_NEGATED_REAL_TIME_CONTEXT = re.compile(
    r"(?:\b(?:i(?:['\u2019]m| am)\s+)?not\s+(?:currently\s+)?"
    r"(?:playing(?:\s+poker)?|at\s+(?:a|the)\s+poker\s+table|"
    r"in\s+(?:an?\s+)?(?:online\s+)?(?:poker\s+|cash\s+)?game|"
    r"seated\s+at\s+(?:a|the)\s+(?:poker|tournament)\s+table)\s+"
    r"(?:right\s+)?now\b|"
    r"今(?:まさに)?プレイ中(?:では|じゃ)?ない)",
    re.IGNORECASE,
)

_BLOCKING_RULES: tuple[tuple[str, str, re.Pattern[str]], ...] = (
    (
        "private_cards",
        "private-cards",
        re.compile(
            r"(?:相手の(?:非公開|ホール)カード.{0,20}(?:教えて|取得|見る)|"
            r"(?:reveal|get|show).{0,20}opponent(?:'s)?\s+(?:private|hole)\s+cards)",
            re.IGNORECASE,
        ),
    ),
    (
        "collusion",
        "collusion",
        re.compile(
            r"(?:共謀して|カードを共有して勝|collude\s+with\s+me|share\s+hole\s+cards)",
            re.IGNORECASE,
        ),
    ),
    (
        "automated_play",
        "automated-play",
        re.compile(
            r"(?:自動(?:で)?プレイするボット|ボット.{0,20}自動プレイ|"
            r"(?:build|run).{0,20}(?:poker\s+)?bot.{0,20}(?:auto|play))",
            re.IGNORECASE,
        ),
    ),
    (
        "detection_evasion",
        "detection-evasion",
        re.compile(
            r"(?:検出を回避|BANを回避|アンチチート.{0,10}回避|"
            r"(?:evade|bypass).{0,20}(?:detection|anti[- ]?cheat))",
            re.IGNORECASE,
        ),
    ),
)
_INJECTION_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "prompt-injection-ignore",
        re.compile(
            r"(?:(?:ignore|disregard|forget)\s+(?:all\s+)?(?:previous|prior)?\s*"
            r"(?:rules|instructions)|"
            r"以前の(?:指示|ルール)を無視)",
            re.IGNORECASE,
        ),
    ),
    (
        "prompt-injection-override",
        re.compile(
            r"(?:override|replace|bypass)\s+(?:the\s+)?"
            r"(?:system|developer)\s+(?:prompt|instructions)",
            re.IGNORECASE,
        ),
    ),
    (
        "prompt-injection-tool",
        re.compile(
            r"(?:execute\s+(?:a\s+)?shell|run\s+this\s+command|シェルを実行|コマンドを実行)",
            re.IGNORECASE,
        ),
    ),
)


def _redact_text(value: str) -> str:
    redacted = value
    for pattern in _SECRET_PATTERNS:
        redacted = pattern.sub("[REDACTED]", redacted)
    return redacted


def redact_sensitive(value: Any, *, enabled: bool = True) -> Any:
    """Return a JSON-compatible copy with common secret shapes removed."""

    if not enabled:
        if isinstance(value, BaseModel):
            return value.model_dump(mode="json")
        return value
    if isinstance(value, BaseModel):
        return redact_sensitive(value.model_dump(mode="json"), enabled=True)
    if isinstance(value, dict):
        redacted_mapping: dict[str, Any] = {}
        for key, item in value.items():
            raw_key = str(key)
            safe_key = _redact_text(raw_key)
            redacted_mapping[safe_key] = (
                "[REDACTED]"
                if _SENSITIVE_KEY.search(raw_key)
                else redact_sensitive(item, enabled=True)
            )
        return redacted_mapping
    if isinstance(value, (list, tuple)):
        return [redact_sensitive(item, enabled=True) for item in value]
    if isinstance(value, str):
        return _redact_text(value)
    return value


def _isolate_prompt_injection_text(value: str) -> str:
    if any(pattern.search(value) for _, pattern in _INJECTION_RULES):
        digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]
        return f"[PROMPT_INJECTION_REMOVED:{digest}]"
    return value


def isolate_prompt_injection(value: Any) -> Any:
    """Replace injection-bearing strings before any provider receives them."""

    if isinstance(value, BaseModel):
        return isolate_prompt_injection(value.model_dump(mode="json"))
    if isinstance(value, dict):
        isolated: dict[str, Any] = {}
        for key, item in value.items():
            safe_key = _isolate_prompt_injection_text(str(key))
            while safe_key in isolated:
                safe_key += "_"
            isolated[safe_key] = isolate_prompt_injection(item)
        return isolated
    if isinstance(value, (list, tuple)):
        return [isolate_prompt_injection(item) for item in value]
    if isinstance(value, str):
        return _isolate_prompt_injection_text(value)
    return value


def _walk_strings(value: Any) -> list[str]:
    strings: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            strings.append(str(key))
            strings.extend(_walk_strings(item))
    elif isinstance(value, (list, tuple)):
        for item in value:
            strings.extend(_walk_strings(item))
    elif isinstance(value, str):
        strings.append(value)
    return strings


def _contains_real_time_assistance(value: Any) -> bool:
    return any(
        _REAL_TIME_ASSISTANCE.search(_NEGATED_REAL_TIME_CONTEXT.sub("", text))
        for text in _walk_strings(value)
    )


def screen_case(case: CaseInput) -> list[SecurityEvent]:
    """Detect prohibited live assistance and record inert injection attempts."""

    dumped = case.model_dump(mode="json")
    serialized = json.dumps(dumped, ensure_ascii=False, sort_keys=True, allow_nan=False)
    digest = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
    events: list[SecurityEvent] = []
    seen: set[tuple[str, str]] = set()

    def add(category: str, rule_id: str, *, blocked: bool) -> None:
        key = (category, rule_id)
        if key in seen:
            return
        seen.add(key)
        events.append(
            SecurityEvent(
                category=category,
                rule_id=rule_id,
                action="refused" if blocked else "recorded",
                blocked=blocked,
                input_sha256=digest,
            )
        )

    if case.analysis_scope == "real_time":
        add("real_time_assistance", "scope-field-real-time", blocked=True)
    if case.analysis_scope == "unspecified":
        add("real_time_assistance", "scope-field-unspecified", blocked=True)
    if _contains_real_time_assistance(dumped):
        add("real_time_assistance", "scope-real-time", blocked=True)
    for category, rule_id, pattern in _BLOCKING_RULES:
        if pattern.search(serialized):
            add(category, rule_id, blocked=True)
    for rule_id, pattern in _INJECTION_RULES:
        if pattern.search(serialized):
            add("prompt_injection", rule_id, blocked=False)
    return events
