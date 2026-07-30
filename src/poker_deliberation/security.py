"""Best-effort redaction for artifacts and user-visible reports."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from typing import Any

from pydantic import BaseModel

from poker_deliberation.schemas import CaseInput, SecurityEvent

_SENSITIVE_KEY = re.compile(
    r"(?:api[.\s_-]*key|authorization|bearer|cookie|password|passwd|secret|"
    r"session[.\s_-]*token|access[.\s_-]*token|client[.\s_-]*secret)",
    re.IGNORECASE,
)
_SECRET_PATTERNS = (
    re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b"),
    re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{8,}\b", re.IGNORECASE),
    re.compile(r"\b(?:authorization|cookie)\s*[:=]\s*[^\r\n]+", re.IGNORECASE),
    re.compile(
        r"(?<![A-Za-z0-9])(?:[A-Za-z][A-Za-z0-9]*_)*"
        r"(?:API_KEY|SECRET(?:_ACCESS_KEY)?|ACCESS_TOKEN|TOKEN|PASSWORD)"
        r"\s*[:=]\s*[^\s,;]+",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:api[.\s_-]*key|password|secret|token|access[.\s_-]*token|"
        r"client[.\s_-]*secret)\s*[:=]\s*[^\s,;]+",
        re.IGNORECASE,
    ),
)
_SECURITY_IGNORABLES = re.compile(
    "[\u0300-\u036f\u1ab0-\u1aff\u1dc0-\u1dff\u200b-\u200f"
    "\u202a-\u202e\u2060-\u206f\u20d0-\u20ff\ufe00-\ufe0f"
    "\ufe20-\ufe2f\ufeff\U000e0100-\U000e01ef]"
)
_SECURITY_DASH_TRANSLATION = str.maketrans(
    {
        "\u2010": "-",
        "\u2011": "-",
        "\u2012": "-",
        "\u2013": "-",
        "\u2014": "-",
        "\u2015": "-",
        "\u2212": "-",
    }
)

_REAL_TIME_ASSISTANCE = re.compile(
    r"(?:(?:ただいま|いま|今|現在)(?:は)?.{0,24}"
    r"(?:オンライン(?:ポーカー|卓)?|ポーカー|ハンド|ゲーム|卓).{0,20}"
    r"(?:中|プレイ中|参加中|参加して(?:い)?(?:る|ます)|"
    r"打って(?:い)?(?:る|ます)|着席して(?:い)?(?:る|ます))|"
    r"今(?:まさに)?プレイ中|リアルタイム(?:で|の).{0,20}(?:指示|助言|教えて)|"
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
_LIVE_PLAY_CONTEXT = re.compile(
    r"(?:(?:オンライン\s*(?:ポーカー|卓|MTT|SNG|PKO|キャッシュ(?:ゲーム)?|"
    r"スピン\s*(?:&|アンド)\s*ゴー)|"
    r"(?:ポーカー|トーナメント|MTT|SNG|PKO|キャッシュ(?:ゲーム)?|"
    r"シット[- ]?アンド[- ]?ゴー|スピン\s*(?:&|アンド)\s*ゴー)(?:卓)?)"
    r"(?:に|で|を)?(?:参加して(?:おり|い)ます|出場して(?:おり|い)ます|"
    r"プレイして(?:おり|い)ます|打って(?:おり|い)ます|"
    r"着席して(?:おり|い)ます|参加中|出場中|プレイ中)|"
    r"\bi(?:['\u2019]m| am)\s+(?:currently\s+)?(?:"
    r"playing\s+(?:in\s+)?(?:an?\s+)?(?:online\s+)?"
    r"(?:(?:poker|cash)\s+(?:game|tournament|hand)|"
    r"poker|game|tournament|mtt|sng|pko|sit[- ]and[- ]go|"
    r"spin\s*(?:&|and)\s*go)|"
    r"in\s+(?:an?\s+)?(?:online\s+)?"
    r"(?:(?:poker|cash)\s+(?:game|tournament|hand)|"
    r"game|tournament|mtt|sng|pko|sit[- ]and[- ]go|"
    r"spin\s*(?:&|and)\s*go)|"
    r"at\s+(?:an?|the)?\s*(?:online\s+)?(?:poker|tournament|cash)\s+table|"
    r"seated\s+at\s+(?:an?|the)?\s*(?:online\s+)?"
    r"(?:poker|tournament|cash)\s+table|"
    r"multi[- ]tabling(?:\s+online)?|"
    r"on\s+(?:pokerstars|ggpoker|acr|wsop|888poker|partypoker|"
    r"ignition|bovada)(?:\s+(?:right\s+)?now)?|"
    r"playing\s+(?:zoom|rush|blitz|snap|fast[- ]?forward))|"
    r"オンラインで(?:\d+|[一二三四五六七八九十]+)面打ちして(?:おり|い)ます)",
    re.IGNORECASE,
)
_PARTIAL_LIVE_CONTEXT = re.compile(
    r"\bi(?:['\u2019]m| am)\s+(?:currently\s+)?(?:"
    r"playing\b|in\s+(?:an?\s+)?(?:online\b)?|"
    r"at\s+(?:an?|the)?\s*(?:online\b)?|on\b|seated\b)|"
    r"\bi(?:['\u2019]m| am)\s*$",
    re.IGNORECASE,
)
_DECISION_REQUEST = re.compile(
    r"(?:\b(?:call|fold|check|bet|raise|shove|all[- ]?in)\b|"
    r"\bshould\s+i\b(?!\s+have\b)|what\s+should\s+i\s+do|"
    r"コール|フォールド|チェック|ベット|レイズ|オールイン|"
    r"アクション|どちら|どうすべき|教えて|すべき)",
    re.IGNORECASE,
)
_ARCHIVED_QUOTATION = re.compile(
    r"(?:(?:(?:archived|historical|recorded)\b[^:\r\n]{0,96}"
    r"(?::|\breads?\s+)|"
    r"(?:アーカイブ|過去|履歴)(?:の)?(?:引用|記録|メモ)[^\uFF1A\r\n]{0,48}"
    r"[\uFF1A:])\s*"
    r"(?:[\"“][^\"”]*[\"”]|'[^']*'|[「『][^」』]*[」』]))",
    re.IGNORECASE,
)
_RETROSPECTIVE_LIVE_SUFFIX = re.compile(
    r"\s+(?:(?:(?:video\s+)?(?:replay|recording|review|simulation|trainer|viewer)"
    r"|archive(?:\s+review)?|hand[- ]?history(?:\s+viewer)?)\b"
    r"(?=[^.\r\n]{0,160}\b(?:yesterday(?:'s)?|completed|finished|historical|"
    r"archived|recorded|last\s+(?:week|month|monday|tuesday|wednesday|thursday|"
    r"friday|saturday|sunday)|(?:one|two|three|four|five|six|seven|\d+)\s+"
    r"days?\s+ago|session\s+(?:has\s+)?ended|saved\s+last)\b)|"
    r"from\s+(?:yesterday|last\s+(?:week|month|monday|tuesday|wednesday|thursday|"
    r"friday|saturday|sunday)|(?:one|two|three|four|five|six|seven|\d+)\s+"
    r"days?\s+ago)\b|of\s+(?:an?|the)\s+(?:completed|finished)\b)",
    re.IGNORECASE,
)
_RETROSPECTIVE_REVERSAL = re.compile(
    r"\b(?:but|however|yet|although|nevertheless)\b|"
    r"(?:ですが|しかし|ただし|けれども|にもかかわらず)",
    re.IGNORECASE,
)
_LIVE_CONTRADICTION = re.compile(
    r"\b(?:actual(?:ly)?\s+live|live\s+(?:game|tournament|table|hand)|right\s+now|"
    r"i(?:['\u2019]m| am)\s+(?:still\s+)?playing(?:\s+(?:it|this\s+"
    r"(?:game|tournament|hand)))?\s+now|"
    r"i(?:['\u2019]m| am)\s+still\s+playing\s+(?:this\s+)?"
    r"(?:game|tournament|hand)|"
    r"this\s+is\s+my\s+current\s+(?:game|tournament|hand)|"
    r"(?:this|the)\s+(?:game|tournament|hand)\s+is\s+"
    r"(?:(?:currently\s+)?(?:in\s+progress|underway)|happening\s+now))\b",
    re.IGNORECASE,
)
_ACTIVE_LIVE_STATUS = re.compile(
    r"(?:\b(?:(?:this|the)\s+(?:mtt|sng|event|game|tournament|hand|session|play)"
    r"\s+(?:(?:is|remains)\s+(?:currently\s+)?(?:in\s+progress|underway|ongoing|"
    r"live|active|running)|has(?:n't|n\u2019t| not)\s+(?:ended|finished|completed|"
    r"concluded)(?:\s+yet)?)|"
    r"(?:i(?:['\u2019]m| am)|we(?:['\u2019]re| are))\s+still\s+"
    r"(?:(?:playing|competing|participating)(?:\s+(?:in\s+)?(?:it|this\s+"
    r"(?:mtt|sng|event|game|tournament|hand)))?|in\s+(?:it|this\s+"
    r"(?:mtt|sng|event|game|tournament|hand))|on\s+the\s+bubble)|"
    r"cards\s+are\s+still\s+being\s+dealt|play\s+is\s+(?:still\s+)?ongoing)\b|"
    r"(?:この|その)?(?:大会|トーナメント|MTT|SNG|イベント|ハンド|ゲーム|プレイ)"
    r".{0,16}(?:現在進行中|進行中|継続中|まだ終わっていません|終了していません|"
    r"まだ参加中|まだ出場中)|"
    r"(?:私|自分)?(?:は)?(?:まだ|現在).{0,12}(?:参加|出場|プレイ)して(?:い|おり)ます)",
    re.IGNORECASE,
)
_NEGATED_REAL_TIME_CONTEXT = re.compile(
    r"(?:(?:ただいま|いま|今|現在)(?:は)?.{0,24}"
    r"(?:オンライン(?:ポーカー|卓)?|ポーカー|ハンド|ゲーム|卓).{0,20}"
    r"(?:中ではありません|中ではない|プレイしていません|参加していません|"
    r"打っていません)|"
    r"\b(?:i(?:['\u2019]m| am)\s+)?not\s+(?:currently\s+)?"
    r"(?:playing(?:\s+poker)?|at\s+(?:a|the)\s+poker\s+table|"
    r"in\s+(?:an?\s+)?(?:online\s+)?(?:poker\s+|cash\s+)?game|"
    r"seated\s+at\s+(?:a|the)\s+(?:poker|tournament)\s+table)\s+"
    r"(?:right\s+)?now\b|"
    r"今(?:まさに)?プレイ中"
    r"(?:ではありません|じゃありません|ではない|じゃない|ではなくて?|じゃなくて?)"
    r"(?!\s*(?:です)?(?:[か\uFF1F?]|わけでは?ない)|\s*は?ない))",
    re.IGNORECASE,
)

_BLOCKED_RULE_GUIDANCE = {
    "scope-field-unspecified": (
        '`analysis_scope="unspecified"`は安全側で拒否しました。'
        '事後検討であることを明示して`analysis_scope="retrospective"`を指定してください。'
    ),
    "scope-field-real-time": (
        '`analysis_scope="real_time"`は対応範囲外です。プレイ終了後の記録を使い、'
        '`analysis_scope="retrospective"`として依頼してください。'
    ),
    "scope-real-time": (
        "入力から現在進行中のプレイ支援を検出しました。プレイ終了後の事後検討として"
        "依頼し直してください。"
    ),
    "private-cards": "相手の非公開カードの取得・推測支援には対応しません。",
    "collusion": "共謀やホールカード共有を伴う依頼には対応しません。",
    "automated-play": "自動プレイや意思決定ボットの運用支援には対応しません。",
    "detection-evasion": "検出・アンチチート回避の支援には対応しません。",
}

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


def _security_probe(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value)
    without_ignorables = _SECURITY_IGNORABLES.sub("", normalized)
    return without_ignorables.translate(_SECURITY_DASH_TRANSLATION).casefold()


def _redact_text(value: str) -> str:
    redacted = value
    for pattern in _SECRET_PATTERNS:
        redacted = pattern.sub("[REDACTED]", redacted)
    if redacted == value:
        security_probe = _security_probe(value)
        if any(pattern.search(security_probe) for pattern in _SECRET_PATTERNS):
            return "[REDACTED]"
    return redacted


def _collision_free_key(candidate: str, occupied: dict[str, Any]) -> str:
    if candidate not in occupied:
        return candidate
    ordinal = 2
    while True:
        alternative = f"{candidate} [collision {ordinal}]"
        if alternative not in occupied:
            return alternative
        ordinal += 1


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
            safe_key = _collision_free_key(_redact_text(raw_key), redacted_mapping)
            redacted_mapping[safe_key] = (
                "[REDACTED]"
                if _SENSITIVE_KEY.search(_security_probe(raw_key))
                else redact_sensitive(item, enabled=True)
            )
        return redacted_mapping
    if isinstance(value, (list, tuple)):
        return [redact_sensitive(item, enabled=True) for item in value]
    if isinstance(value, str):
        return _redact_text(value)
    return value


def _isolate_prompt_injection_text(value: str) -> str:
    if any(pattern.search(_security_probe(value)) for _, pattern in _INJECTION_RULES):
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


def _has_unqualified_partial_live_context(cleaned: str) -> bool:
    retrospective_live_spans = tuple(
        match.span()
        for match in _LIVE_PLAY_CONTEXT.finditer(cleaned)
        if _RETROSPECTIVE_LIVE_SUFFIX.match(cleaned, match.end()) is not None
    )
    span_index = 0
    for partial in _PARTIAL_LIVE_CONTEXT.finditer(cleaned):
        while (
            span_index < len(retrospective_live_spans)
            and retrospective_live_spans[span_index][1] < partial.end()
        ):
            span_index += 1
        if span_index == len(retrospective_live_spans):
            return True
        live_start, live_end = retrospective_live_spans[span_index]
        if live_start > partial.start() or live_end < partial.end():
            return True
    return False


def _has_retrospective_reversal(cleaned: str) -> bool:
    for live_match in _LIVE_PLAY_CONTEXT.finditer(cleaned):
        suffix = _RETROSPECTIVE_LIVE_SUFFIX.match(cleaned, live_match.end())
        if suffix is None:
            continue
        if _RETROSPECTIVE_REVERSAL.search(
            cleaned,
            suffix.end(),
            min(len(cleaned), suffix.end() + 512),
        ):
            return True
    return False


def real_time_assistance_signals(value: Any) -> tuple[bool, bool, bool]:
    """Return live-context, decision-request, and explicit-assistance signals."""

    cleaned_texts: list[str] = []
    partial_live_context_present = False
    for text in _walk_strings(value):
        cleaned = _security_probe(text)
        cleaned = _ARCHIVED_QUOTATION.sub("", cleaned)
        cleaned = _NEGATED_REAL_TIME_CONTEXT.sub("", cleaned)
        cleaned_texts.append(cleaned)
        partial_live_context_present = (
            partial_live_context_present or _has_unqualified_partial_live_context(cleaned)
        )
    combined = "\n".join(cleaned_texts)
    decision_request_present = _DECISION_REQUEST.search(combined) is not None
    explicit_assistance_present = _REAL_TIME_ASSISTANCE.search(combined) is not None
    live_context_present = partial_live_context_present or any(
        _RETROSPECTIVE_LIVE_SUFFIX.match(combined, match.end()) is None
        for match in _LIVE_PLAY_CONTEXT.finditer(combined)
    )
    if (
        _LIVE_CONTRADICTION.search(combined) is not None
        or _ACTIVE_LIVE_STATUS.search(combined) is not None
        or _has_retrospective_reversal(combined)
    ):
        live_context_present = True
    return (
        live_context_present,
        decision_request_present,
        explicit_assistance_present,
    )


def _contains_real_time_assistance(value: Any) -> bool:
    live_context, decision_request, explicit_assistance = real_time_assistance_signals(value)
    return explicit_assistance or (live_context and decision_request)


def contains_real_time_assistance(value: Any) -> bool:
    """Return whether strong live context and decision language coexist in one envelope."""

    return _contains_real_time_assistance(value)


def blocked_security_guidance(events: list[SecurityEvent]) -> list[str]:
    """Return deterministic user guidance for each distinct blocked security rule."""

    guidance: list[str] = []
    seen: set[str] = set()
    for event in events:
        if not event.blocked or event.rule_id in seen:
            continue
        seen.add(event.rule_id)
        guidance.append(
            _BLOCKED_RULE_GUIDANCE.get(
                event.rule_id,
                f"安全規則`{event.rule_id}`により事後検討を開始できませんでした。",
            )
        )
    return guidance


def screen_case(case: CaseInput) -> list[SecurityEvent]:
    """Detect prohibited live assistance and record inert injection attempts."""

    dumped = case.model_dump(mode="json")
    serialized = json.dumps(dumped, ensure_ascii=False, sort_keys=True, allow_nan=False)
    security_probe = _security_probe(serialized)
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
        if pattern.search(security_probe):
            add(category, rule_id, blocked=True)
    for rule_id, pattern in _INJECTION_RULES:
        if pattern.search(security_probe):
            add("prompt_injection", rule_id, blocked=False)
    return events
