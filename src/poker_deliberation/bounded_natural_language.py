"""Deterministic bounded-Japanese parser, confirmation, and admission helpers."""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Literal, NoReturn, cast
from uuid import uuid4

from pydantic import BaseModel, ValidationError

from poker_deliberation.bounded_natural_language_models import (
    BOUNDED_NL_BINDINGS_CANONICALIZATION_ID,
    BOUNDED_NL_CANDIDATE_CANONICALIZATION_ID,
    BOUNDED_NL_CONFIRMATION_CANONICALIZATION_ID,
    BOUNDED_NL_CONTRACT_ID,
    BOUNDED_NL_EXTRACTOR_CANONICALIZATION_ID,
    BOUNDED_NL_EXTRACTOR_ID,
    BOUNDED_NL_EXTRACTOR_VERSION,
    BOUNDED_NL_FOCAL_CANONICALIZATION_ID,
    BOUNDED_NL_PROVENANCE_CANONICALIZATION_ID,
    BOUNDED_NL_SOURCE_CANONICALIZATION_ID,
    BOUNDED_NL_TOOL_ALLOWLIST,
    BOUNDED_NL_TOOL_ORDER,
    BOUNDED_NL_TOOL_PLAN_CANONICALIZATION_ID,
    MAX_BOUNDED_NL_ACTIONS,
    MAX_BOUNDED_NL_ARTIFACT_BYTES,
    MAX_BOUNDED_NL_BINDINGS,
    MAX_BOUNDED_NL_PLAYERS,
    MAX_BOUNDED_NL_SOURCE_BYTES,
    BoundedCandidateProjectionV1,
    BoundedConfirmationAuthorityV1,
    BoundedDeclaredPotAssertionsV1,
    BoundedFocalDecisionV1,
    BoundedIntakeCandidateV1,
    BoundedIntakeConfirmationV1,
    BoundedIntakePreparationResultV1,
    BoundedNaturalLanguageDiagnosticCode,
    BoundedNaturalLanguageDiagnosticV1,
    BoundedPartialExtractionV1,
    BoundedPotOddsInputV1,
    BoundedSourceBindingV1,
    BoundedSourceProvenanceV1,
    BoundedToolPlanV1,
)
from poker_deliberation.schemas import (
    CanonicalHand,
    CaseInput,
    Claim,
    ConfidenceGrade,
    EpistemicLabel,
    FocalDecision,
    HandAction,
    PlayerStack,
    Street,
    ToolStatus,
)
from poker_deliberation.security import (
    real_time_assistance_signals,
    redact_sensitive,
)
from poker_deliberation.storage.revision_canonical import (
    CanonicalStorageError,
    canonical_json_bytes,
)
from poker_deliberation.tools import default_registry
from poker_deliberation.tools.hand_pot_ledger import (
    PROFILE_ID,
    PROFILE_SCHEMA_VERSION,
    PROFILE_VERSION,
    SUPPORTED_SITE,
    HandPotLedgerOutputV1,
    HandRuleProfileV1,
)

_AMOUNT = r"(?:0|[1-9][0-9]{0,11})(?:\.[0-9]{1,4})?"
_POSITIVE_AMOUNT = r"(?:[1-9][0-9]{0,11})(?:\.[0-9]{1,4})?|0\.[0-9]{0,3}[1-9]"
_PLAYER = r"[A-Za-z][A-Za-z0-9._-]{0,31}"
_POSITION = r"(?:UTG|HJ|CO|BTN|SB|BB)"
_CARD = r"[2-9TJQKA][cdhs]"
_SPACE = r"[ \u3000]*"
_SEP = r"[、,]"
_STOP = r"(?:。|\.)"
_COPULA = r"(?:です|だ)"
_PAST = r"(?:しました|した)"
_CONTROL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")

_HEADER_RE = re.compile(
    rf"^{_SPACE}これは完了済みの(?P<game>NLHE)(?P<format>キャッシュゲーム){_COPULA}{_STOP}"
    rf"{_SPACE}参加者は(?P<count>[2-6])人{_COPULA}{_STOP}{_SPACE}$"
)
_BLINDS_RE = re.compile(
    rf"^{_SPACE}ブラインドは(?P<sb>{_POSITIVE_AMOUNT})[/\uFF0F](?P<bb>{_POSITIVE_AMOUNT})で"
    rf"{_SEP}アンティは(?P<ante>{_AMOUNT}){_SEP}レーキは(?P<rake>{_AMOUNT}){_COPULA}{_STOP}{_SPACE}$"
)
_PLAYER_RE = re.compile(
    rf"^{_SPACE}(?P<player>{_PLAYER})は(?P<position>{_POSITION})で開始スタック"
    rf"(?P<stack>{_POSITIVE_AMOUNT}){_COPULA}{_STOP}{_SPACE}$"
)
_HERO_CARDS_RE = re.compile(
    rf"^{_SPACE}(?P<hero>Hero)のホールカードは(?P<card1>{_CARD})[ \u3000]+"
    rf"(?P<card2>{_CARD}){_COPULA}{_STOP}{_SPACE}$"
)
_PREFLOP_RE = re.compile(rf"^{_SPACE}(?P<street>プリフロップ)です{_STOP}{_SPACE}$")
_FLOP_RE = re.compile(
    rf"^{_SPACE}(?P<street>フロップ)は(?P<card1>{_CARD})[ \u3000]+"
    rf"(?P<card2>{_CARD})[ \u3000]+(?P<card3>{_CARD}){_COPULA}{_STOP}{_SPACE}$"
)
_TURN_RE = re.compile(rf"^{_SPACE}(?P<street>ターン)は(?P<card>{_CARD}){_COPULA}{_STOP}{_SPACE}$")
_RIVER_RE = re.compile(rf"^{_SPACE}(?P<street>リバー)は(?P<card>{_CARD}){_COPULA}{_STOP}{_SPACE}$")
_POST_RE = re.compile(
    rf"^{_SPACE}(?P<actor>{_PLAYER})が(?P<amount>{_POSITIVE_AMOUNT})を"
    rf"(?P<blind>SB|BB)として(?P<verb>ポスト){_PAST}{_STOP}{_SPACE}$"
)
_CHECK_RE = re.compile(rf"^{_SPACE}(?P<actor>{_PLAYER})が(?P<verb>チェック){_PAST}{_STOP}{_SPACE}$")
_FOLD_RE = re.compile(
    rf"^{_SPACE}(?P<actor>{_PLAYER})が(?P<verb>フォールド|降り){_PAST}{_STOP}{_SPACE}$"
)
_CALL_RE = re.compile(
    rf"^{_SPACE}(?P<actor>{_PLAYER})が(?P<amount>{_POSITIVE_AMOUNT})を"
    rf"(?P<verb>コール){_PAST}{_STOP}{_SPACE}$"
)
_BET_RE = re.compile(
    rf"^{_SPACE}(?P<actor>{_PLAYER})が(?P<amount>{_POSITIVE_AMOUNT})を"
    rf"(?P<verb>ベット){_PAST}{_STOP}{_SPACE}$"
)
_RAISE_RE = re.compile(
    rf"^{_SPACE}(?P<actor>{_PLAYER})が追加で(?P<amount>{_POSITIVE_AMOUNT})を出し"
    rf"{_SEP}合計(?P<to_amount>{_POSITIVE_AMOUNT})まで(?P<verb>レイズ){_PAST}{_STOP}{_SPACE}$"
)
_POT_BEFORE_RE = re.compile(
    rf"^{_SPACE}判断直前のポットは(?P<amount>{_AMOUNT}){_COPULA}{_STOP}{_SPACE}$"
)
_CALL_COST_RE = re.compile(
    rf"^{_SPACE}コール額は(?P<amount>{_POSITIVE_AMOUNT}){_COPULA}{_STOP}{_SPACE}$"
)
_CONTESTABLE_RE = re.compile(
    rf"^{_SPACE}コール後の争点ポットは(?P<amount>{_POSITIVE_AMOUNT}){_COPULA}{_STOP}{_SPACE}$"
)
_FOCAL_BET_RE = re.compile(
    rf"^{_SPACE}検討対象は{_SEP}(?P<street>プリフロップ|フロップ|ターン|リバー)で"
    rf"(?P<actor>{_PLAYER})が(?P<amount>{_POSITIVE_AMOUNT})を(?P<verb>ベット)した直後の"
    rf"(?P<hero>Hero)のコールまたはフォールド判断{_COPULA}{_STOP}{_SPACE}$"
)
_FOCAL_RAISE_RE = re.compile(
    rf"^{_SPACE}検討対象は{_SEP}(?P<street>プリフロップ|フロップ|ターン|リバー)で"
    rf"(?P<actor>{_PLAYER})が合計(?P<amount>{_POSITIVE_AMOUNT})まで(?P<verb>レイズ)した直後の"
    rf"(?P<hero>Hero)のコールまたはフォールド判断{_COPULA}{_STOP}{_SPACE}$"
)

_STREET_VALUE = {
    "プリフロップ": Street.PREFLOP,
    "フロップ": Street.FLOP,
    "ターン": Street.TURN,
    "リバー": Street.RIVER,
}
_STREET_ORDER = {
    Street.PREFLOP: 0,
    Street.FLOP: 1,
    Street.TURN: 2,
    Street.RIVER: 3,
}


class BoundedNaturalLanguageError(ValueError):
    """Stable parser/admission error that never includes source text."""

    def __init__(
        self,
        code: BoundedNaturalLanguageDiagnosticCode,
        field_path: str,
        *,
        start_byte: int | None = None,
        end_byte: int | None = None,
    ) -> None:
        super().__init__(code.value)
        self.code = code
        self.field_path = field_path
        self.start_byte = start_byte
        self.end_byte = end_byte


def _fail(
    code: BoundedNaturalLanguageDiagnosticCode,
    field_path: str,
    *,
    start_byte: int | None = None,
    end_byte: int | None = None,
) -> NoReturn:
    raise BoundedNaturalLanguageError(
        code,
        field_path,
        start_byte=start_byte,
        end_byte=end_byte,
    )


def _validate_control_id(value: object, field_path: str) -> None:
    if not isinstance(value, str) or _CONTROL_ID_RE.fullmatch(value) is None:
        _fail(BoundedNaturalLanguageDiagnosticCode.CONTROL, field_path)
    if redact_sensitive(value) != value:
        _fail(BoundedNaturalLanguageDiagnosticCode.CONTROL_SECRET, field_path)


def _has_exact_declared_model_shape(value: object) -> bool:
    """Reject model-copy extras recursively without inspecting unknown values."""

    if isinstance(value, BaseModel):
        declared = set(type(value).model_fields)
        if set(value.__dict__) != declared or value.__pydantic_extra__:
            return False
        return all(_has_exact_declared_model_shape(value.__dict__[field]) for field in declared)
    if isinstance(value, (list, tuple, set, frozenset)):
        return all(_has_exact_declared_model_shape(item) for item in value)
    if isinstance(value, dict):
        return all(_has_exact_declared_model_shape(item) for item in value.values())
    return True


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _domain_sha256(domain: str, value: Any) -> str:
    return _sha256(domain.encode("ascii") + b"\x00" + canonical_json_bytes(value))


def _bytes_domain_sha256(domain: str, value: bytes) -> str:
    return _sha256(domain.encode("ascii") + b"\x00" + value)


def _without_hash(model: BaseModel, field: str) -> dict[str, Any]:
    payload = model.model_dump(mode="json")
    del payload[field]
    return payload


def bounded_focal_sha256(focal: BoundedFocalDecisionV1) -> str:
    return _domain_sha256(
        BOUNDED_NL_FOCAL_CANONICALIZATION_ID,
        _without_hash(focal, "focal_sha256"),
    )


def bounded_tool_plan_sha256(plan: BoundedToolPlanV1) -> str:
    return _domain_sha256(
        BOUNDED_NL_TOOL_PLAN_CANONICALIZATION_ID,
        _without_hash(plan, "tool_plan_sha256"),
    )


def bounded_candidate_sha256(projection: BoundedCandidateProjectionV1) -> str:
    return _domain_sha256(
        BOUNDED_NL_CANDIDATE_CANONICALIZATION_ID,
        projection.model_dump(mode="json"),
    )


def bounded_authority_snapshot_sha256(authority: BoundedConfirmationAuthorityV1) -> str:
    return _domain_sha256(
        BOUNDED_NL_CONFIRMATION_CANONICALIZATION_ID + ":authority",
        authority.model_dump(mode="json"),
    )


def bounded_confirmation_sha256(confirmation: BoundedIntakeConfirmationV1) -> str:
    return _domain_sha256(
        BOUNDED_NL_CONFIRMATION_CANONICALIZATION_ID,
        _without_hash(confirmation, "confirmation_sha256"),
    )


def bounded_provenance_sha256(provenance: BaseModel) -> str:
    return _domain_sha256(
        BOUNDED_NL_PROVENANCE_CANONICALIZATION_ID,
        _without_hash(provenance, "provenance_sha256"),
    )


@dataclass(frozen=True, slots=True)
class _Line:
    text: str
    char_start: int
    char_end: int


@dataclass(frozen=True, slots=True)
class _SpanRef:
    start_char: int
    end_char: int


@dataclass(frozen=True, slots=True)
class _FocalSelector:
    street: Street
    actor: str
    action: Literal["bet", "raise"]
    amount: Decimal
    line: _Line
    match: re.Match[str]


def _line_span(line: _Line, byte_offsets: list[int]) -> tuple[int, int]:
    stripped_start = len(line.text) - len(line.text.lstrip(" \u3000"))
    stripped_end = len(line.text.rstrip(" \u3000"))
    start_char = line.char_start + stripped_start
    end_char = line.char_start + stripped_end
    if end_char <= start_char:
        end_char = min(line.char_end, start_char + 1)
    return byte_offsets[start_char], byte_offsets[end_char]


def _span_ref(line: _Line, match: re.Match[str], group: str) -> _SpanRef:
    return _SpanRef(
        start_char=line.char_start + match.start(group),
        end_char=line.char_start + match.end(group),
    )


def _binding(
    *,
    field_path: str,
    canonical_value: Any,
    span: _SpanRef,
    source_bytes: bytes,
    source_sha256: str,
    byte_offsets: list[int],
) -> BoundedSourceBindingV1:
    start_byte = byte_offsets[span.start_char]
    end_byte = byte_offsets[span.end_char]
    lexeme = source_bytes[start_byte:end_byte]
    return BoundedSourceBindingV1(
        field_path=field_path,
        source_sha256=source_sha256,
        start_byte=start_byte,
        end_byte=end_byte,
        lexeme_sha256=_bytes_domain_sha256(
            BOUNDED_NL_BINDINGS_CANONICALIZATION_ID + ":lexeme",
            lexeme,
        ),
        canonical_value_sha256=_domain_sha256(
            BOUNDED_NL_BINDINGS_CANONICALIZATION_ID + ":value",
            canonical_value,
        ),
    )


def _partial_extractions(
    bindings: list[BoundedSourceBindingV1],
) -> tuple[BoundedPartialExtractionV1, ...]:
    return tuple(
        BoundedPartialExtractionV1(
            field_path=item.field_path,
            start_byte=item.start_byte,
            end_byte=item.end_byte,
            canonical_value_sha256=item.canonical_value_sha256,
        )
        for item in bindings[:MAX_BOUNDED_NL_BINDINGS]
    )


def _diagnostic(exc: BoundedNaturalLanguageError) -> BoundedNaturalLanguageDiagnosticV1:
    return BoundedNaturalLanguageDiagnosticV1(
        code=exc.code,
        field_path=exc.field_path,
        start_byte=exc.start_byte,
        end_byte=exc.end_byte,
    )


def _source_provenance(
    source_bytes: bytes,
    *,
    source_id: str,
    source_kind: Literal["user_supplied", "repository_fixture"],
    license_classification: Literal[
        "user_supplied_private_analysis",
        "repository_owned_mit",
    ],
    usage_classification: Literal["local_analysis_only", "redistribution_allowed"],
    classification: Literal["internal", "public"],
) -> BoundedSourceProvenanceV1:
    try:
        return BoundedSourceProvenanceV1(
            source_id=source_id,
            source_kind=source_kind,
            license_classification=license_classification,
            usage_classification=usage_classification,
            classification=classification,
            bytes_length=len(source_bytes),
            content_sha256=_bytes_domain_sha256(
                BOUNDED_NL_SOURCE_CANONICALIZATION_ID,
                source_bytes,
            ),
        )
    except ValidationError:
        _fail(BoundedNaturalLanguageDiagnosticCode.SOURCE_RIGHTS, "source.provenance")


def validate_bounded_source(
    source_bytes: bytes,
    *,
    source_id: str,
    source_kind: Literal["user_supplied", "repository_fixture"],
    license_classification: Literal[
        "user_supplied_private_analysis",
        "repository_owned_mit",
    ],
    usage_classification: Literal["local_analysis_only", "redistribution_allowed"],
    classification: Literal["internal", "public"],
) -> tuple[BoundedSourceProvenanceV1, str]:
    _validate_control_id(source_id, "source.source_id")
    if not source_bytes or len(source_bytes) > MAX_BOUNDED_NL_SOURCE_BYTES:
        _fail(BoundedNaturalLanguageDiagnosticCode.SOURCE_SIZE, "source")
    if source_bytes.startswith(b"\xef\xbb\xbf"):
        _fail(BoundedNaturalLanguageDiagnosticCode.SOURCE_BOM, "source")
    if b"\r" in source_bytes:
        _fail(BoundedNaturalLanguageDiagnosticCode.SOURCE_NEWLINE, "source")
    try:
        text = source_bytes.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        _fail(BoundedNaturalLanguageDiagnosticCode.SOURCE_UTF8, "source")
    if unicodedata.normalize("NFC", text) != text:
        _fail(BoundedNaturalLanguageDiagnosticCode.SOURCE_NFC, "source")
    for character in text:
        category = unicodedata.category(character)
        if character not in {"\n", "\t"} and (
            category in {"Cf", "Cc"} or 0x7F <= ord(character) <= 0x9F
        ):
            _fail(BoundedNaturalLanguageDiagnosticCode.SOURCE_CONTROL, "source")
    if redact_sensitive(text) != text:
        _fail(BoundedNaturalLanguageDiagnosticCode.SOURCE_SECRET, "source")
    live, decision, explicit = real_time_assistance_signals([text])
    if explicit or (live and decision):
        _fail(BoundedNaturalLanguageDiagnosticCode.UNSUPPORTED, "source.analysis_scope")
    return (
        _source_provenance(
            source_bytes,
            source_id=source_id,
            source_kind=source_kind,
            license_classification=license_classification,
            usage_classification=usage_classification,
            classification=classification,
        ),
        text,
    )


def _byte_offsets(text: str) -> list[int]:
    offsets = [0]
    current = 0
    for character in text:
        current += len(character.encode("utf-8"))
        offsets.append(current)
    return offsets


def _lines(text: str) -> list[_Line]:
    lines: list[_Line] = []
    cursor = 0
    for item in text.splitlines(keepends=True):
        content = item[:-1] if item.endswith("\n") else item
        lines.append(_Line(text=content, char_start=cursor, char_end=cursor + len(content)))
        cursor += len(item)
    if not lines and text:
        lines.append(_Line(text=text, char_start=0, char_end=len(text)))
    return lines


def _decimal(value: str, *, field_path: str) -> Decimal:
    try:
        parsed = Decimal(value)
    except InvalidOperation:
        _fail(BoundedNaturalLanguageDiagnosticCode.AMOUNT, field_path)
    if not parsed.is_finite() or parsed < 0:
        _fail(BoundedNaturalLanguageDiagnosticCode.AMOUNT, field_path)
    return parsed


def _float(value: Decimal, *, field_path: str) -> float:
    result = float(value)
    round_trip = Decimal(str(result))
    if not round_trip.is_finite() or round_trip != value:
        _fail(BoundedNaturalLanguageDiagnosticCode.AMOUNT, field_path)
    return result


def _chip_unit(values: list[Decimal]) -> Decimal:
    exponents = [value.as_tuple().exponent for value in values]
    scale = max(
        (max(0, -exponent) for exponent in exponents if isinstance(exponent, int)),
        default=0,
    )
    return Decimal(1).scaleb(-scale)


def _chip_unit_text(unit: Decimal) -> str:
    return format(unit, "f")


def _to_units(value: Decimal, unit: Decimal, *, field_path: str) -> int:
    units = value / unit
    integral = units.to_integral_value()
    if units != integral or integral < 0 or integral > 2**63 - 1:
        _fail(BoundedNaturalLanguageDiagnosticCode.AMOUNT, field_path)
    return int(integral)


def _unmatched_code(text: str) -> BoundedNaturalLanguageDiagnosticCode:
    if "レイズ" in text:
        return BoundedNaturalLanguageDiagnosticCode.RAISE_AMBIGUITY
    if any(
        token in text
        for token in (
            "トーナメント",
            "PokerStars",
            "GGPoker",
            "画像",
            "OCR",
            "ショーダウン",
            "オールイン",
            "サイドポット",
            "レンジ",
        )
    ):
        return BoundedNaturalLanguageDiagnosticCode.UNSUPPORTED
    return BoundedNaturalLanguageDiagnosticCode.SYNTAX


def _strict_hand(hand: CanonicalHand) -> CanonicalHand:
    try:
        return CanonicalHand.model_validate_json(canonical_json_bytes(hand), strict=True)
    except (ValidationError, CanonicalStorageError):
        _fail(BoundedNaturalLanguageDiagnosticCode.CONFLICT, "candidate.hand")


_POSITIONS_BY_TABLE_SIZE: dict[int, frozenset[str]] = {
    2: frozenset({"SB", "BB"}),
    3: frozenset({"BTN", "SB", "BB"}),
    4: frozenset({"CO", "BTN", "SB", "BB"}),
    5: frozenset({"HJ", "CO", "BTN", "SB", "BB"}),
    6: frozenset({"UTG", "HJ", "CO", "BTN", "SB", "BB"}),
}
_PREFLOP_POSITION_ORDER = ("UTG", "HJ", "CO", "BTN", "SB", "BB")
_POSTFLOP_POSITION_ORDER = ("SB", "BB", "UTG", "HJ", "CO", "BTN")
_HEADS_UP_POSTFLOP_POSITION_ORDER = ("BB", "SB")


def _validate_action_order(
    players: list[tuple[str, str, Decimal]],
    actions: list[HandAction],
) -> None:
    """Validate the finite grammar's complete betting order without extending the ledger."""

    table_size = len(players)
    position_to_player = {position: player for player, position, _ in players}
    if frozenset(position_to_player) != _POSITIONS_BY_TABLE_SIZE.get(table_size):
        _fail(BoundedNaturalLanguageDiagnosticCode.PLAYER, "hand.players.position")
    if len(actions) < 3:
        _fail(BoundedNaturalLanguageDiagnosticCode.MISSING, "hand.actions")
    small_blind_player = position_to_player["SB"]
    big_blind_player = position_to_player["BB"]
    if tuple((item.actor, item.action, item.street) for item in actions[:2]) != (
        (small_blind_player, "post_blind", Street.PREFLOP),
        (big_blind_player, "post_blind", Street.PREFLOP),
    ):
        _fail(BoundedNaturalLanguageDiagnosticCode.ACTION, "hand.actions.blind_order")

    active = {player for player, _, _ in players}
    current_street = Street.PREFLOP
    street_action_count = 0
    terminal = False

    def ordered_active(street: Street) -> list[str]:
        order = (
            _PREFLOP_POSITION_ORDER
            if street is Street.PREFLOP
            else _HEADS_UP_POSTFLOP_POSITION_ORDER
            if table_size == 2
            else _POSTFLOP_POSITION_ORDER
        )
        return [
            position_to_player[position] for position in order if position in position_to_player
        ]

    pending = [player for player in ordered_active(current_street) if player in active]
    for action in actions[2:]:
        if terminal or action.action == "post_blind":
            _fail(BoundedNaturalLanguageDiagnosticCode.ACTION, "hand.actions.terminal")
        if action.street is not current_street:
            if (
                _STREET_ORDER[action.street] != _STREET_ORDER[current_street] + 1
                or pending
                or street_action_count == 0
                or len(active) < 2
            ):
                _fail(BoundedNaturalLanguageDiagnosticCode.STREET, "hand.actions.street")
            current_street = action.street
            street_action_count = 0
            pending = [player for player in ordered_active(current_street) if player in active]
        if action.actor not in active or not pending or action.actor != pending[0]:
            _fail(BoundedNaturalLanguageDiagnosticCode.ACTION, "hand.actions.actor_order")
        pending.pop(0)
        street_action_count += 1
        if action.action == "fold":
            active.remove(action.actor)
            pending = [player for player in pending if player in active]
            if len(active) == 1:
                terminal = True
                pending = []
        elif action.action in {"bet", "raise"}:
            order = [player for player in ordered_active(current_street) if player in active]
            actor_index = order.index(action.actor)
            pending = order[actor_index + 1 :] + order[:actor_index]

    if not terminal:
        _fail(BoundedNaturalLanguageDiagnosticCode.UNSUPPORTED, "hand.completion")


def _parse_candidate(
    source_bytes: bytes,
    text: str,
    source: BoundedSourceProvenanceV1,
    *,
    intake_id: str,
    bindings: list[BoundedSourceBindingV1],
) -> BoundedIntakeCandidateV1:
    byte_offsets = _byte_offsets(text)
    header_count: int | None = None
    small_blind: Decimal | None = None
    big_blind: Decimal | None = None
    ante: Decimal | None = None
    rake: Decimal | None = None
    players: list[tuple[str, str, Decimal]] = []
    hero_cards: list[str] | None = None
    board: list[str] = []
    actions: list[HandAction] = []
    action_decimals: list[tuple[Decimal, Decimal | None]] = []
    action_blinds: list[str | None] = []
    current_street: Street | None = None
    current_street_span: _SpanRef | None = None
    seen_streets: list[Street] = []
    focal_selectors: list[_FocalSelector] = []
    assertions: dict[str, Decimal] = {}
    amount_values: list[Decimal] = []
    stage = 0

    def add_binding(field_path: str, value: Any, span: _SpanRef) -> None:
        if len(bindings) >= MAX_BOUNDED_NL_BINDINGS:
            _fail(BoundedNaturalLanguageDiagnosticCode.LIMIT, "candidate.source_bindings")
        bindings.append(
            _binding(
                field_path=field_path,
                canonical_value=value,
                span=span,
                source_bytes=source_bytes,
                source_sha256=source.content_sha256,
                byte_offsets=byte_offsets,
            )
        )

    for line in _lines(text):
        if not line.text.strip(" \u3000"):
            continue
        match = _HEADER_RE.fullmatch(line.text)
        if match is not None:
            if header_count is not None:
                _fail(BoundedNaturalLanguageDiagnosticCode.DUPLICATE, "hand.header")
            if stage != 0:
                _fail(BoundedNaturalLanguageDiagnosticCode.CONFLICT, "hand.header")
            header_count = int(match.group("count"))
            add_binding("hand.game_type", "NLHE", _span_ref(line, match, "game"))
            add_binding("hand.format", "cash", _span_ref(line, match, "format"))
            add_binding("hand.table_size", header_count, _span_ref(line, match, "count"))
            stage = 1
            continue
        match = _BLINDS_RE.fullmatch(line.text)
        if match is not None:
            if small_blind is not None:
                _fail(BoundedNaturalLanguageDiagnosticCode.DUPLICATE, "hand.blinds")
            if stage == 0:
                _fail(BoundedNaturalLanguageDiagnosticCode.MISSING, "hand.header")
            if stage != 1:
                _fail(BoundedNaturalLanguageDiagnosticCode.CONFLICT, "hand.blinds")
            small_blind = _decimal(match.group("sb"), field_path="hand.small_blind")
            big_blind = _decimal(match.group("bb"), field_path="hand.big_blind")
            ante = _decimal(match.group("ante"), field_path="hand.ante")
            rake = _decimal(match.group("rake"), field_path="hand.rake")
            if small_blind <= 0 or big_blind <= small_blind:
                _fail(BoundedNaturalLanguageDiagnosticCode.CONFLICT, "hand.blinds")
            if ante != 0 or rake != 0:
                _fail(BoundedNaturalLanguageDiagnosticCode.UNSUPPORTED, "hand.ante_or_rake")
            amount_values.extend((small_blind, big_blind, ante, rake))
            for field, field_value, group in (
                (
                    "hand.small_blind",
                    _float(small_blind, field_path="hand.small_blind"),
                    "sb",
                ),
                (
                    "hand.big_blind",
                    _float(big_blind, field_path="hand.big_blind"),
                    "bb",
                ),
                ("hand.ante", 0.0, "ante"),
                ("hand.rake", 0.0, "rake"),
            ):
                add_binding(field, field_value, _span_ref(line, match, group))
            stage = 2
            continue
        match = _PLAYER_RE.fullmatch(line.text)
        if match is not None:
            if stage not in {2, 3}:
                if stage == 1:
                    _fail(BoundedNaturalLanguageDiagnosticCode.MISSING, "hand.blinds")
                _fail(BoundedNaturalLanguageDiagnosticCode.CONFLICT, "hand.players")
            if len(players) >= MAX_BOUNDED_NL_PLAYERS:
                _fail(BoundedNaturalLanguageDiagnosticCode.LIMIT, "hand.players")
            player = match.group("player")
            position = match.group("position")
            stack = _decimal(match.group("stack"), field_path="hand.players.starting_stack")
            if stack <= 0:
                _fail(BoundedNaturalLanguageDiagnosticCode.AMOUNT, "hand.players.starting_stack")
            if player in {item[0] for item in players} or position in {item[1] for item in players}:
                _fail(BoundedNaturalLanguageDiagnosticCode.DUPLICATE, "hand.players")
            index = len(players)
            players.append((player, position, stack))
            amount_values.append(stack)
            add_binding(
                f"hand.players[{index}].player_id",
                player,
                _span_ref(line, match, "player"),
            )
            add_binding(
                f"hand.players[{index}].position",
                position,
                _span_ref(line, match, "position"),
            )
            add_binding(
                f"hand.players[{index}].starting_stack",
                _float(stack, field_path="hand.players.starting_stack"),
                _span_ref(line, match, "stack"),
            )
            stage = 3
            continue
        match = _HERO_CARDS_RE.fullmatch(line.text)
        if match is not None:
            if hero_cards is not None:
                _fail(BoundedNaturalLanguageDiagnosticCode.DUPLICATE, "hand.hero_cards")
            if header_count is None or len(players) != header_count:
                _fail(BoundedNaturalLanguageDiagnosticCode.MISSING, "hand.players")
            if stage != 3:
                _fail(BoundedNaturalLanguageDiagnosticCode.CONFLICT, "hand.hero_cards")
            hero_cards = [match.group("card1"), match.group("card2")]
            add_binding("hand.hero_player_id", "Hero", _span_ref(line, match, "hero"))
            add_binding(
                "hand.hero_cards[0]",
                hero_cards[0],
                _span_ref(line, match, "card1"),
            )
            add_binding(
                "hand.hero_cards[1]",
                hero_cards[1],
                _span_ref(line, match, "card2"),
            )
            stage = 4
            continue
        match = _PREFLOP_RE.fullmatch(line.text)
        if match is not None:
            if stage != 4 or seen_streets:
                if stage == 3 and hero_cards is None:
                    _fail(BoundedNaturalLanguageDiagnosticCode.MISSING, "hand.hero_cards")
                _fail(BoundedNaturalLanguageDiagnosticCode.STREET, "hand.actions")
            current_street = Street.PREFLOP
            current_street_span = _span_ref(line, match, "street")
            seen_streets.append(current_street)
            stage = 5
            continue
        board_match: re.Match[str] | None = None
        next_street: Street | None = None
        card_groups: tuple[str, ...] = ()
        for pattern, street, groups in (
            (_FLOP_RE, Street.FLOP, ("card1", "card2", "card3")),
            (_TURN_RE, Street.TURN, ("card",)),
            (_RIVER_RE, Street.RIVER, ("card",)),
        ):
            candidate_match = pattern.fullmatch(line.text)
            if candidate_match is not None:
                board_match = candidate_match
                next_street = street
                card_groups = groups
                break
        if board_match is not None and next_street is not None:
            if stage != 5 or current_street is None:
                _fail(BoundedNaturalLanguageDiagnosticCode.STREET, "hand.board")
            if _STREET_ORDER[next_street] != _STREET_ORDER[current_street] + 1:
                _fail(BoundedNaturalLanguageDiagnosticCode.STREET, "hand.board")
            current_street = next_street
            current_street_span = _span_ref(line, board_match, "street")
            seen_streets.append(current_street)
            for group in card_groups:
                card = board_match.group(group)
                board_index = len(board)
                board.append(card)
                add_binding(
                    f"hand.board[{board_index}]",
                    card,
                    _span_ref(line, board_match, group),
                )
            continue
        action_match: re.Match[str] | None = None
        action_kind: Literal["post_blind", "check", "fold", "call", "bet", "raise"] | None = None
        for pattern, kind in (
            (_POST_RE, "post_blind"),
            (_CHECK_RE, "check"),
            (_FOLD_RE, "fold"),
            (_CALL_RE, "call"),
            (_BET_RE, "bet"),
            (_RAISE_RE, "raise"),
        ):
            candidate_match = pattern.fullmatch(line.text)
            if candidate_match is not None:
                action_match = candidate_match
                action_kind = cast(
                    Literal["post_blind", "check", "fold", "call", "bet", "raise"],
                    kind,
                )
                break
        if action_match is not None and action_kind is not None:
            if stage != 5 or current_street is None or current_street_span is None:
                _fail(BoundedNaturalLanguageDiagnosticCode.ACTION, "hand.actions")
            if len(actions) >= MAX_BOUNDED_NL_ACTIONS:
                _fail(BoundedNaturalLanguageDiagnosticCode.LIMIT, "hand.actions")
            actor = action_match.group("actor")
            blind_kind = action_match.group("blind") if action_kind == "post_blind" else None
            amount = (
                _decimal(action_match.group("amount"), field_path="hand.actions.amount")
                if "amount" in action_match.groupdict()
                else Decimal(0)
            )
            to_amount = (
                _decimal(action_match.group("to_amount"), field_path="hand.actions.to_amount")
                if action_kind == "raise"
                else amount
                if action_kind == "bet"
                else None
            )
            if action_kind == "raise" and (to_amount is None or to_amount <= amount):
                _fail(BoundedNaturalLanguageDiagnosticCode.RAISE_AMBIGUITY, "hand.actions")
            action_index = len(actions)
            actions.append(
                HandAction(
                    street=current_street,
                    actor=actor,
                    action=action_kind,
                    amount=_float(
                        amount,
                        field_path=f"hand.actions[{action_index}].amount",
                    ),
                    to_amount=(
                        None
                        if to_amount is None
                        else _float(
                            to_amount,
                            field_path=f"hand.actions[{action_index}].to_amount",
                        )
                    ),
                )
            )
            action_decimals.append((amount, to_amount))
            action_blinds.append(blind_kind)
            amount_values.append(amount)
            if action_kind == "raise" and to_amount is not None:
                amount_values.append(to_amount)
            add_binding(
                f"hand.actions[{action_index}].street",
                current_street.value,
                current_street_span,
            )
            add_binding(
                f"hand.actions[{action_index}].actor",
                actor,
                _span_ref(line, action_match, "actor"),
            )
            add_binding(
                f"hand.actions[{action_index}].action",
                action_kind,
                _span_ref(line, action_match, "verb"),
            )
            if blind_kind is not None:
                add_binding(
                    f"hand.actions[{action_index}].blind",
                    blind_kind,
                    _span_ref(line, action_match, "blind"),
                )
            if "amount" in action_match.groupdict():
                add_binding(
                    f"hand.actions[{action_index}].amount",
                    _float(
                        amount,
                        field_path=f"hand.actions[{action_index}].amount",
                    ),
                    _span_ref(line, action_match, "amount"),
                )
            if action_kind == "raise" and to_amount is not None:
                add_binding(
                    f"hand.actions[{action_index}].to_amount",
                    _float(
                        to_amount,
                        field_path=f"hand.actions[{action_index}].to_amount",
                    ),
                    _span_ref(line, action_match, "to_amount"),
                )
            continue
        assertion_match: re.Match[str] | None = None
        assertion_name: str | None = None
        for pattern, name in (
            (_POT_BEFORE_RE, "pot_before_bet"),
            (_CALL_COST_RE, "call_cost"),
            (_CONTESTABLE_RE, "contestable_pot"),
        ):
            candidate_match = pattern.fullmatch(line.text)
            if candidate_match is not None:
                assertion_match = candidate_match
                assertion_name = name
                break
        if assertion_match is not None and assertion_name is not None:
            if assertion_name in assertions:
                _fail(
                    BoundedNaturalLanguageDiagnosticCode.DUPLICATE, f"assertions.{assertion_name}"
                )
            if stage not in {5, 6}:
                _fail(
                    BoundedNaturalLanguageDiagnosticCode.CONFLICT,
                    f"assertions.{assertion_name}",
                )
            assertion_value = _decimal(
                assertion_match.group("amount"), field_path=f"assertions.{assertion_name}"
            )
            assertions[assertion_name] = assertion_value
            amount_values.append(assertion_value)
            add_binding(
                f"declared_pot_assertions.{assertion_name}",
                _float(
                    assertion_value,
                    field_path=f"declared_pot_assertions.{assertion_name}",
                ),
                _span_ref(line, assertion_match, "amount"),
            )
            stage = 6
            continue
        focal_match = _FOCAL_BET_RE.fullmatch(line.text)
        focal_action: Literal["bet", "raise"] | None = "bet" if focal_match is not None else None
        if focal_match is None:
            focal_match = _FOCAL_RAISE_RE.fullmatch(line.text)
            focal_action = "raise" if focal_match is not None else None
        if focal_match is not None and focal_action is not None:
            if stage not in {5, 6, 7}:
                _fail(BoundedNaturalLanguageDiagnosticCode.FOCAL_MISMATCH, "focal_decision")
            selector = _FocalSelector(
                street=_STREET_VALUE[focal_match.group("street")],
                actor=focal_match.group("actor"),
                action=focal_action,
                amount=_decimal(
                    focal_match.group("amount"), field_path="focal_decision.selector_amount"
                ),
                line=line,
                match=focal_match,
            )
            focal_selectors.append(selector)
            if len(focal_selectors) > 1:
                _fail(BoundedNaturalLanguageDiagnosticCode.FOCAL_MULTIPLE, "focal_decision")
            amount_values.append(selector.amount)
            add_binding(
                "focal_decision.selector_street",
                selector.street.value,
                _span_ref(line, focal_match, "street"),
            )
            add_binding(
                "focal_decision.selector_actor",
                selector.actor,
                _span_ref(line, focal_match, "actor"),
            )
            add_binding(
                "focal_decision.selector_action",
                selector.action,
                _span_ref(line, focal_match, "verb"),
            )
            add_binding(
                "focal_decision.selector_amount",
                _float(selector.amount, field_path="focal_decision.selector_amount"),
                _span_ref(line, focal_match, "amount"),
            )
            stage = 7
            continue
        start_byte, end_byte = _line_span(line, byte_offsets)
        _fail(
            _unmatched_code(line.text),
            "source.syntax",
            start_byte=start_byte,
            end_byte=end_byte,
        )

    if header_count is None:
        _fail(BoundedNaturalLanguageDiagnosticCode.MISSING, "hand.header")
    if small_blind is None or big_blind is None or ante is None or rake is None:
        _fail(BoundedNaturalLanguageDiagnosticCode.MISSING, "hand.blinds")
    if len(players) != header_count:
        _fail(BoundedNaturalLanguageDiagnosticCode.MISSING, "hand.players")
    if "Hero" not in {item[0] for item in players}:
        _fail(BoundedNaturalLanguageDiagnosticCode.MISSING, "hand.hero_player_id")
    if hero_cards is None:
        _fail(BoundedNaturalLanguageDiagnosticCode.MISSING, "hand.hero_cards")
    if current_street is None or not seen_streets or seen_streets[0] is not Street.PREFLOP:
        _fail(BoundedNaturalLanguageDiagnosticCode.MISSING, "hand.actions.street")
    if not actions:
        _fail(BoundedNaturalLanguageDiagnosticCode.MISSING, "hand.actions")
    if actions[-1].action != "fold":
        _fail(BoundedNaturalLanguageDiagnosticCode.UNSUPPORTED, "hand.completion")
    if len(focal_selectors) != 1:
        _fail(BoundedNaturalLanguageDiagnosticCode.FOCAL_MISSING, "focal_decision")
    player_map = {player: (position, stack) for player, position, stack in players}
    if any(action.actor not in player_map for action in actions):
        _fail(BoundedNaturalLanguageDiagnosticCode.PLAYER, "hand.actions.actor")
    if seen_streets[-1] is not actions[-1].street:
        _fail(BoundedNaturalLanguageDiagnosticCode.ACTION, "hand.actions.terminal")
    _validate_action_order(players, actions)
    for action, (amount, _), declared_blind in zip(
        actions,
        action_decimals,
        action_blinds,
        strict=True,
    ):
        if action.action == "post_blind":
            position, _ = player_map[action.actor]
            expected = small_blind if position == "SB" else big_blind if position == "BB" else None
            if expected is None or amount != expected or declared_blind != position:
                _fail(BoundedNaturalLanguageDiagnosticCode.CONFLICT, "hand.actions.post_blind")
    known_cards = [*hero_cards, *board]
    if len(known_cards) != len(set(known_cards)):
        _fail(BoundedNaturalLanguageDiagnosticCode.CARD, "hand.cards")

    hand = _strict_hand(
        CanonicalHand(
            game_type="NLHE",
            format="cash",
            table_size=header_count,
            small_blind=_float(small_blind, field_path="hand.small_blind"),
            big_blind=_float(big_blind, field_path="hand.big_blind"),
            ante=0.0,
            rake=0.0,
            players=[
                PlayerStack(
                    player_id=player,
                    position=position,
                    starting_stack=_float(stack, field_path="hand.players.starting_stack"),
                )
                for player, position, stack in players
            ],
            hero_player_id="Hero",
            hero_cards=hero_cards,
            board=board,
            actions=actions,
            known_ranges=[],
            opponent_observations=[],
            analysis_objective="strategy_review",
        )
    )
    selector = focal_selectors[0]
    matching_indexes = [
        index
        for index, (action, (_, to_amount)) in enumerate(zip(actions, action_decimals, strict=True))
        if action.street is selector.street
        and action.actor == selector.actor
        and action.action == selector.action
        and (
            (selector.action == "bet" and Decimal(str(action.amount)) == selector.amount)
            or (selector.action == "raise" and to_amount == selector.amount)
        )
    ]
    if len(matching_indexes) != 1:
        code = (
            BoundedNaturalLanguageDiagnosticCode.FOCAL_MULTIPLE
            if len(matching_indexes) > 1
            else BoundedNaturalLanguageDiagnosticCode.FOCAL_MISMATCH
        )
        _fail(code, "focal_decision")
    facing_index = matching_indexes[0]
    hero_index = facing_index + 1
    if hero_index >= len(actions):
        _fail(BoundedNaturalLanguageDiagnosticCode.FOCAL_MISMATCH, "focal_decision.hero_action")
    hero_action = actions[hero_index]
    if (
        selector.actor == "Hero"
        or hero_action.actor != "Hero"
        or hero_action.street is not selector.street
        or hero_action.action not in {"call", "fold"}
    ):
        _fail(BoundedNaturalLanguageDiagnosticCode.FOCAL_MISMATCH, "focal_decision.hero_action")

    provisional_focal = BoundedFocalDecisionV1(
        selector_street=cast(Literal["preflop", "flop", "turn", "river"], selector.street.value),
        selector_actor=selector.actor,
        selector_action=selector.action,
        selector_amount=_float(selector.amount, field_path="focal_decision.selector_amount"),
        facing_action_index=facing_index,
        hero_action_index=hero_index,
        hero_response=hero_action.action,
        focal_sha256="0" * 64,
    )
    focal = provisional_focal.model_copy(
        update={"focal_sha256": bounded_focal_sha256(provisional_focal)}
    )
    chip_unit = _chip_unit(amount_values)
    profile = HandRuleProfileV1(
        schema_version=PROFILE_SCHEMA_VERSION,
        profile_id=PROFILE_ID,
        profile_version=PROFILE_VERSION,
        supported_site=SUPPORTED_SITE,
        chip_unit=_chip_unit_text(chip_unit),
    )
    ledger_input = {
        "schema_version": "1.0.0",
        "rule_profile": profile.model_dump(mode="json"),
        "hand": hand.model_dump(mode="json"),
    }
    registry = default_registry()
    ledger_result = registry.execute("hand_pot_ledger", ledger_input)
    if ledger_result.status is not ToolStatus.SUCCESS:
        _fail(BoundedNaturalLanguageDiagnosticCode.LEDGER, "tool_plan.hand_pot_ledger")
    try:
        ledger = HandPotLedgerOutputV1.model_validate(ledger_result.output, strict=True)
    except ValidationError:
        _fail(BoundedNaturalLanguageDiagnosticCode.LEDGER, "tool_plan.hand_pot_ledger")
    if any(remaining == 0 for remaining in ledger.remaining_stacks_units.values()):
        _fail(BoundedNaturalLanguageDiagnosticCode.UNSUPPORTED, "focal_decision.all_in")
    facing_ledger = ledger.ledger_actions[facing_index]
    hero_ledger = ledger.ledger_actions[hero_index]
    if (
        facing_ledger.action not in {"bet", "raise"}
        or hero_ledger.actor != "Hero"
        or hero_ledger.action not in {"call", "fold"}
    ):
        _fail(BoundedNaturalLanguageDiagnosticCode.LEDGER, "focal_decision")
    hero_before_units = hero_ledger.street_contribution_units_after - hero_ledger.committed_units
    call_cost_units = facing_ledger.current_bet_units_after - hero_before_units
    pot_before_units = facing_ledger.pot_units_after - facing_ledger.committed_units
    opponent_bet_units = facing_ledger.committed_units
    if call_cost_units <= 0 or pot_before_units < 0 or opponent_bet_units <= 0:
        _fail(BoundedNaturalLanguageDiagnosticCode.LEDGER, "tool_plan.pot_odds")
    hero_stack_before = hero_ledger.remaining_stack_units_after + hero_ledger.committed_units
    if hero_stack_before <= call_cost_units:
        _fail(BoundedNaturalLanguageDiagnosticCode.UNSUPPORTED, "focal_decision.all_in")
    if hero_action.action == "call" and hero_ledger.committed_units != call_cost_units:
        _fail(BoundedNaturalLanguageDiagnosticCode.LEDGER, "focal_decision.call")
    contestable_units = pot_before_units + opponent_bet_units + call_cost_units

    assertion_values = BoundedDeclaredPotAssertionsV1(
        pot_before_bet=(
            None
            if "pot_before_bet" not in assertions
            else _float(
                assertions["pot_before_bet"],
                field_path="declared_pot_assertions.pot_before_bet",
            )
        ),
        call_cost=(
            None
            if "call_cost" not in assertions
            else _float(
                assertions["call_cost"],
                field_path="declared_pot_assertions.call_cost",
            )
        ),
        contestable_pot=(
            None
            if "contestable_pot" not in assertions
            else _float(
                assertions["contestable_pot"],
                field_path="declared_pot_assertions.contestable_pot",
            )
        ),
    )
    expected_assertions = {
        "pot_before_bet": pot_before_units,
        "call_cost": call_cost_units,
        "contestable_pot": contestable_units,
    }
    for name, assertion_value in assertions.items():
        if (
            _to_units(
                assertion_value,
                chip_unit,
                field_path=f"declared_pot_assertions.{name}",
            )
            != expected_assertions[name]
        ):
            _fail(
                BoundedNaturalLanguageDiagnosticCode.POT_MISMATCH, f"declared_pot_assertions.{name}"
            )

    pot_odds_input = BoundedPotOddsInputV1(
        pot_before_bet=_float(
            chip_unit * pot_before_units,
            field_path="tool_plan.pot_odds.pot_before_bet",
        ),
        opponent_bet=_float(
            chip_unit * opponent_bet_units,
            field_path="tool_plan.pot_odds.opponent_bet",
        ),
        call_cost=_float(
            chip_unit * call_cost_units,
            field_path="tool_plan.pot_odds.call_cost",
        ),
        expected_rake=0.0,
    )
    pot_odds_payload = pot_odds_input.model_dump(mode="json")
    pot_odds_result = registry.execute("pot_odds", pot_odds_payload)
    if (
        pot_odds_result.status is not ToolStatus.SUCCESS
        or pot_odds_result.verification is None
        or not pot_odds_result.verification.passed
    ):
        _fail(BoundedNaturalLanguageDiagnosticCode.TOOL, "tool_plan.pot_odds")
    provisional_plan = BoundedToolPlanV1(
        ordered_tools=BOUNDED_NL_TOOL_ORDER,
        ledger_profile=profile,
        facing_action_index=facing_index,
        hero_action_index=hero_index,
        pot_before_bet_units=pot_before_units,
        opponent_bet_units=opponent_bet_units,
        call_cost_units=call_cost_units,
        contestable_pot_units=contestable_units,
        ledger_input_sha256=_domain_sha256(
            BOUNDED_NL_TOOL_PLAN_CANONICALIZATION_ID + ":ledger-input",
            ledger_input,
        ),
        ledger_output_sha256=_domain_sha256(
            BOUNDED_NL_TOOL_PLAN_CANONICALIZATION_ID + ":ledger-output",
            ledger_result.output,
        ),
        pot_odds_input=pot_odds_input,
        pot_odds_input_sha256=_domain_sha256(
            BOUNDED_NL_TOOL_PLAN_CANONICALIZATION_ID + ":pot-odds-input",
            pot_odds_payload,
        ),
        tool_plan_sha256="0" * 64,
    )
    tool_plan = provisional_plan.model_copy(
        update={"tool_plan_sha256": bounded_tool_plan_sha256(provisional_plan)}
    )
    bindings_tuple = tuple(bindings)
    bindings_sha256 = _domain_sha256(
        BOUNDED_NL_BINDINGS_CANONICALIZATION_ID,
        [binding.model_dump(mode="json") for binding in bindings_tuple],
    )
    extractor_sha256 = _domain_sha256(
        BOUNDED_NL_EXTRACTOR_CANONICALIZATION_ID,
        {
            "contract_id": BOUNDED_NL_CONTRACT_ID,
            "extractor_id": BOUNDED_NL_EXTRACTOR_ID,
            "extractor_version": BOUNDED_NL_EXTRACTOR_VERSION,
        },
    )
    projection = BoundedCandidateProjectionV1(
        intake_id=intake_id,
        source=source,
        hand=hand,
        focal_decision=focal,
        source_bindings=bindings_tuple,
        source_bindings_sha256=bindings_sha256,
        extractor_sha256=extractor_sha256,
        declared_pot_assertions=assertion_values,
        tool_plan=tool_plan,
    )
    return BoundedIntakeCandidateV1(
        projection=projection,
        candidate_sha256=bounded_candidate_sha256(projection),
    )


def prepare_bounded_natural_language_intake(
    source_bytes: bytes,
    *,
    intake_id: str,
    source_id: str,
    source_kind: Literal["user_supplied", "repository_fixture"],
    license_classification: Literal[
        "user_supplied_private_analysis",
        "repository_owned_mit",
    ],
    usage_classification: Literal["local_analysis_only", "redistribution_allowed"],
    classification: Literal["internal", "public"],
) -> BoundedIntakePreparationResultV1:
    """Parse one bounded source into a candidate without creating a run."""

    bindings: list[BoundedSourceBindingV1] = []
    try:
        _validate_control_id(intake_id, "candidate.intake_id")
        source, text = validate_bounded_source(
            source_bytes,
            source_id=source_id,
            source_kind=source_kind,
            license_classification=license_classification,
            usage_classification=usage_classification,
            classification=classification,
        )
    except BoundedNaturalLanguageError as exc:
        return BoundedIntakePreparationResultV1(
            status="blocked",
            diagnostics=(_diagnostic(exc),),
        )
    try:
        candidate = _parse_candidate(
            source_bytes,
            text,
            source,
            intake_id=intake_id,
            bindings=bindings,
        )
        if len(canonical_json_bytes(candidate)) > MAX_BOUNDED_NL_ARTIFACT_BYTES:
            _fail(BoundedNaturalLanguageDiagnosticCode.LIMIT, "candidate.size_bytes")
        candidate = verify_bounded_candidate(candidate)
    except (BoundedNaturalLanguageError, CanonicalStorageError, ValidationError) as exc:
        if isinstance(exc, (CanonicalStorageError, ValidationError)):
            exc = BoundedNaturalLanguageError(
                BoundedNaturalLanguageDiagnosticCode.CONFLICT,
                "candidate",
            )
        return BoundedIntakePreparationResultV1(
            status="blocked",
            source=source,
            partial_extractions=_partial_extractions(bindings),
            diagnostics=(_diagnostic(exc),),
        )
    return BoundedIntakePreparationResultV1(
        status="ready",
        source=source,
        candidate=candidate,
    )


def verify_bounded_candidate(candidate: BoundedIntakeCandidateV1) -> BoundedIntakeCandidateV1:
    if type(candidate) is not BoundedIntakeCandidateV1 or not _has_exact_declared_model_shape(
        candidate
    ):
        _fail(BoundedNaturalLanguageDiagnosticCode.CONFLICT, "candidate")
    try:
        payload = canonical_json_bytes(candidate)
        if len(payload) > MAX_BOUNDED_NL_ARTIFACT_BYTES:
            _fail(BoundedNaturalLanguageDiagnosticCode.LIMIT, "candidate.size_bytes")
        candidate = BoundedIntakeCandidateV1.model_validate_json(payload, strict=True)
    except (ValidationError, CanonicalStorageError):
        _fail(BoundedNaturalLanguageDiagnosticCode.CONFLICT, "candidate")
    projection = candidate.projection
    _validate_control_id(projection.intake_id, "candidate.intake_id")
    _validate_control_id(projection.source.source_id, "source.source_id")
    if candidate.candidate_sha256 != bounded_candidate_sha256(projection):
        _fail(
            BoundedNaturalLanguageDiagnosticCode.CONFIRMATION_BINDING, "candidate.candidate_sha256"
        )
    if projection.focal_decision.focal_sha256 != bounded_focal_sha256(projection.focal_decision):
        _fail(BoundedNaturalLanguageDiagnosticCode.CONFIRMATION_BINDING, "candidate.focal_sha256")
    if projection.tool_plan.tool_plan_sha256 != bounded_tool_plan_sha256(projection.tool_plan):
        _fail(
            BoundedNaturalLanguageDiagnosticCode.CONFIRMATION_BINDING, "candidate.tool_plan_sha256"
        )
    expected_bindings = _domain_sha256(
        BOUNDED_NL_BINDINGS_CANONICALIZATION_ID,
        [binding.model_dump(mode="json") for binding in projection.source_bindings],
    )
    if projection.source_bindings_sha256 != expected_bindings:
        _fail(
            BoundedNaturalLanguageDiagnosticCode.CONFIRMATION_BINDING,
            "candidate.source_bindings_sha256",
        )
    expected_extractor = _domain_sha256(
        BOUNDED_NL_EXTRACTOR_CANONICALIZATION_ID,
        {
            "contract_id": BOUNDED_NL_CONTRACT_ID,
            "extractor_id": BOUNDED_NL_EXTRACTOR_ID,
            "extractor_version": BOUNDED_NL_EXTRACTOR_VERSION,
        },
    )
    if projection.extractor_sha256 != expected_extractor:
        _fail(
            BoundedNaturalLanguageDiagnosticCode.CONFIRMATION_BINDING,
            "candidate.extractor_sha256",
        )
    return candidate


def _strict_bounded_confirmation_authority(
    authority: BoundedConfirmationAuthorityV1,
) -> BoundedConfirmationAuthorityV1:
    if type(authority) is not BoundedConfirmationAuthorityV1 or not _has_exact_declared_model_shape(
        authority
    ):
        _fail(
            BoundedNaturalLanguageDiagnosticCode.CONFIRMATION_AUTHORITY,
            "confirmation.authority",
        )
    try:
        authority_id = authority.authority_id
        authority_kind = authority.authority_kind
        authentication = authority.authentication
        scope = authority.scope
    except AttributeError:
        _fail(
            BoundedNaturalLanguageDiagnosticCode.CONFIRMATION_AUTHORITY,
            "confirmation.authority",
        )
    if (
        not isinstance(authority_id, str)
        or authority_kind not in {"local_user", "verified_application"}
        or authentication not in {"self_asserted", "verified"}
        or scope != "confirm_bounded_natural_language_projection"
    ):
        _fail(
            BoundedNaturalLanguageDiagnosticCode.CONFIRMATION_AUTHORITY,
            "confirmation.authority",
        )
    _validate_control_id(authority_id, "confirmation.authority.authority_id")
    try:
        return BoundedConfirmationAuthorityV1(
            authority_id=authority_id,
            authority_kind=authority_kind,
            authentication=authentication,
            scope=scope,
        )
    except ValidationError:
        _fail(
            BoundedNaturalLanguageDiagnosticCode.CONFIRMATION_AUTHORITY,
            "confirmation.authority",
        )


def create_bounded_confirmation_authority(
    *,
    authority_id: str,
    authority_kind: Literal["local_user", "verified_application"],
    authentication: Literal["self_asserted", "verified"],
) -> BoundedConfirmationAuthorityV1:
    """Validate raw authority controls before Pydantic can echo their values."""

    _validate_control_id(authority_id, "confirmation.authority.authority_id")
    try:
        authority = BoundedConfirmationAuthorityV1(
            authority_id=authority_id,
            authority_kind=authority_kind,
            authentication=authentication,
        )
    except ValidationError:
        _fail(
            BoundedNaturalLanguageDiagnosticCode.CONFIRMATION_AUTHORITY,
            "confirmation.authority",
        )
    return _strict_bounded_confirmation_authority(authority)


def create_bounded_confirmation(
    candidate: BoundedIntakeCandidateV1,
    *,
    run_id: str,
    confirmation_id: str,
    idempotency_key: str,
    authority: BoundedConfirmationAuthorityV1,
    expected_source_sha256: str,
    expected_candidate_sha256: str,
    expected_source_bindings_sha256: str,
    expected_focal_sha256: str,
    expected_tool_plan_sha256: str,
    expected_extractor_sha256: str,
    confirmed_at: datetime | None = None,
    expires_at: datetime | None = None,
) -> BoundedIntakeConfirmationV1:
    candidate = verify_bounded_candidate(candidate)
    authority = _strict_bounded_confirmation_authority(authority)
    for value, field_path in (
        (run_id, "confirmation.run_id"),
        (confirmation_id, "confirmation.confirmation_id"),
        (idempotency_key, "confirmation.idempotency_key"),
        (authority.authority_id, "confirmation.authority.authority_id"),
    ):
        _validate_control_id(value, field_path)
    projection = candidate.projection
    expected = (
        projection.source.content_sha256,
        candidate.candidate_sha256,
        projection.source_bindings_sha256,
        projection.focal_decision.focal_sha256,
        projection.tool_plan.tool_plan_sha256,
        projection.extractor_sha256,
    )
    supplied = (
        expected_source_sha256,
        expected_candidate_sha256,
        expected_source_bindings_sha256,
        expected_focal_sha256,
        expected_tool_plan_sha256,
        expected_extractor_sha256,
    )
    if supplied != expected:
        _fail(
            BoundedNaturalLanguageDiagnosticCode.CONFIRMATION_BINDING,
            "confirmation.expected_hashes",
        )
    confirmed_time = confirmed_at or datetime.now(UTC)
    expiry = expires_at or confirmed_time + timedelta(hours=24)
    provisional = BoundedIntakeConfirmationV1(
        run_id=run_id,
        intake_id=projection.intake_id,
        confirmation_id=confirmation_id,
        idempotency_key=idempotency_key,
        source_sha256=expected_source_sha256,
        candidate_sha256=expected_candidate_sha256,
        source_bindings_sha256=expected_source_bindings_sha256,
        focal_sha256=expected_focal_sha256,
        tool_plan_sha256=expected_tool_plan_sha256,
        extractor_sha256=expected_extractor_sha256,
        authority=authority,
        authority_snapshot_sha256=bounded_authority_snapshot_sha256(authority),
        confirmed_at=confirmed_time,
        expires_at=expiry,
        confirmation_sha256="0" * 64,
    )
    return provisional.model_copy(
        update={"confirmation_sha256": bounded_confirmation_sha256(provisional)}
    )


def _case_from_candidate(candidate: BoundedIntakeCandidateV1) -> CaseInput:
    projection = candidate.projection
    focal = projection.focal_decision
    plan = projection.tool_plan
    marker = {
        "contract_id": candidate.contract_id,
        "intake_id": projection.intake_id,
        "extractor_id": projection.extractor_id,
        "extractor_version": projection.extractor_version,
        "source_sha256": projection.source.content_sha256,
        "candidate_sha256": candidate.candidate_sha256,
        "focal_sha256": focal.focal_sha256,
        "tool_plan_sha256": plan.tool_plan_sha256,
    }
    return CaseInput(
        case_id=f"bounded-nl-{projection.intake_id}",
        kind="hand",
        raw_text=None,
        hand=projection.hand,
        focal_decision=FocalDecision(
            street=Street(focal.selector_street),
            action_index=focal.hero_action_index,
            actor="Hero",
        ),
        analysis_scope="retrospective",
        claims=[
            Claim(
                claim_id=f"claim-{projection.intake_id}",
                text=(
                    "ユーザー記述では、Heroの応答が確認済み構造化ハンドの"
                    "実行済みアクションとして記録されています。"
                ),
                label=EpistemicLabel.USER_CLAIM,
                confidence=ConfidenceGrade.C,
                limitations=["原文の記述であり、計算結果または戦略評価ではありません。"],
            )
        ],
        objective="bounded_japanese_nlhe_cash_review",
        requested_tools=list(BOUNDED_NL_TOOL_ORDER),
        metadata={
            "bounded_natural_language_review": marker,
            "tool_inputs": {
                "hand_pot_ledger": {
                    "schema_version": "1.0.0",
                    "rule_profile": plan.ledger_profile.model_dump(mode="json"),
                },
                "pot_odds": plan.pot_odds_input.model_dump(mode="json"),
            },
        },
    )


@dataclass(frozen=True, slots=True)
class BoundedNaturalLanguageAdmission:
    source_bytes: bytes
    candidate: BoundedIntakeCandidateV1
    confirmation: BoundedIntakeConfirmationV1
    admitted_at: datetime
    case: CaseInput


def _strict_confirmation(
    confirmation: BoundedIntakeConfirmationV1,
) -> BoundedIntakeConfirmationV1:
    if type(confirmation) is not BoundedIntakeConfirmationV1 or not _has_exact_declared_model_shape(
        confirmation
    ):
        _fail(BoundedNaturalLanguageDiagnosticCode.CONFIRMATION_BINDING, "confirmation")
    try:
        payload = canonical_json_bytes(confirmation)
        if len(payload) > MAX_BOUNDED_NL_ARTIFACT_BYTES:
            _fail(
                BoundedNaturalLanguageDiagnosticCode.CONFIRMATION_BINDING,
                "confirmation.size_bytes",
            )
        strict = BoundedIntakeConfirmationV1.model_validate_json(payload, strict=True)
    except (ValidationError, CanonicalStorageError):
        _fail(BoundedNaturalLanguageDiagnosticCode.CONFIRMATION_BINDING, "confirmation")
    for value, field_path in (
        (strict.run_id, "confirmation.run_id"),
        (strict.intake_id, "confirmation.intake_id"),
        (strict.confirmation_id, "confirmation.confirmation_id"),
        (strict.idempotency_key, "confirmation.idempotency_key"),
        (strict.authority.authority_id, "confirmation.authority.authority_id"),
    ):
        _validate_control_id(value, field_path)
    return strict


def _admit_bounded_at(
    source_bytes: bytes,
    candidate: BoundedIntakeCandidateV1,
    confirmation: BoundedIntakeConfirmationV1,
    *,
    admitted_at: datetime,
) -> BoundedNaturalLanguageAdmission:
    candidate = verify_bounded_candidate(candidate)
    confirmation = _strict_confirmation(confirmation)
    if admitted_at.tzinfo is None or admitted_at.utcoffset() is None:
        _fail(BoundedNaturalLanguageDiagnosticCode.CONFIRMATION_BINDING, "admission.admitted_at")
    projection = candidate.projection
    replay = prepare_bounded_natural_language_intake(
        source_bytes,
        intake_id=projection.intake_id,
        source_id=projection.source.source_id,
        source_kind=projection.source.source_kind,
        license_classification=projection.source.license_classification,
        usage_classification=projection.source.usage_classification,
        classification=projection.source.classification,
    )
    if (
        replay.status != "ready"
        or replay.candidate != candidate
        or replay.source != projection.source
    ):
        _fail(BoundedNaturalLanguageDiagnosticCode.CONFIRMATION_BINDING, "source")
    if confirmation.confirmation_sha256 != bounded_confirmation_sha256(confirmation):
        _fail(
            BoundedNaturalLanguageDiagnosticCode.CONFIRMATION_BINDING,
            "confirmation.confirmation_sha256",
        )
    if confirmation.authority_snapshot_sha256 != bounded_authority_snapshot_sha256(
        confirmation.authority
    ):
        _fail(
            BoundedNaturalLanguageDiagnosticCode.CONFIRMATION_AUTHORITY,
            "confirmation.authority_snapshot_sha256",
        )
    expected = (
        projection.intake_id,
        projection.source.content_sha256,
        candidate.candidate_sha256,
        projection.source_bindings_sha256,
        projection.focal_decision.focal_sha256,
        projection.tool_plan.tool_plan_sha256,
        projection.extractor_sha256,
        BOUNDED_NL_EXTRACTOR_ID,
        BOUNDED_NL_EXTRACTOR_VERSION,
    )
    actual = (
        confirmation.intake_id,
        confirmation.source_sha256,
        confirmation.candidate_sha256,
        confirmation.source_bindings_sha256,
        confirmation.focal_sha256,
        confirmation.tool_plan_sha256,
        confirmation.extractor_sha256,
        confirmation.extractor_id,
        confirmation.extractor_version,
    )
    if actual != expected:
        _fail(BoundedNaturalLanguageDiagnosticCode.CONFIRMATION_BINDING, "confirmation")
    if confirmation.confirmed_at > admitted_at or admitted_at > confirmation.expires_at:
        _fail(
            BoundedNaturalLanguageDiagnosticCode.CONFIRMATION_EXPIRED,
            "confirmation.expires_at",
        )
    case = _case_from_candidate(candidate)
    if case.raw_text is not None or not set(case.requested_tools).issubset(
        BOUNDED_NL_TOOL_ALLOWLIST
    ):
        _fail(BoundedNaturalLanguageDiagnosticCode.TOOL, "candidate.tool_plan")
    return BoundedNaturalLanguageAdmission(
        source_bytes=source_bytes,
        candidate=candidate,
        confirmation=confirmation,
        admitted_at=admitted_at,
        case=case,
    )


def admit_bounded_natural_language_review(
    source_bytes: bytes,
    candidate: BoundedIntakeCandidateV1,
    confirmation: BoundedIntakeConfirmationV1,
) -> BoundedNaturalLanguageAdmission:
    return _admit_bounded_at(
        source_bytes,
        candidate,
        confirmation,
        admitted_at=datetime.now(UTC),
    )


def default_bounded_confirmation_ids() -> tuple[str, str]:
    suffix = uuid4().hex
    return f"confirmation-{suffix[:12]}", f"idempotency-{suffix[12:24]}"


def bounded_terminal_revision_root_sha256(storage_root: Path | str) -> str:
    root = Path(storage_root)
    if not root.is_absolute():
        _fail(BoundedNaturalLanguageDiagnosticCode.REPORT, "storage_authority.revision_root")
    return _domain_sha256(
        BOUNDED_NL_PROVENANCE_CANONICALIZATION_ID + ":terminal-revision-root",
        str(root.resolve(strict=False)).replace("\\", "/"),
    )


def review_bounded_natural_language_intake(
    admission: BoundedNaturalLanguageAdmission,
    *,
    config: object | None = None,
) -> Any:
    """Run the local-only product path without provider or registry injection."""

    from poker_deliberation.config import AppConfig
    from poker_deliberation.orchestrator import Orchestrator
    from poker_deliberation.providers import LocalProvider

    if config is not None and not isinstance(config, AppConfig):
        raise TypeError("config must be AppConfig")
    orchestrator = Orchestrator(config=config, provider=LocalProvider())
    return orchestrator.run_bounded_natural_language_review(admission)
