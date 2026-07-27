from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st

from poker_deliberation.normalization import normalize_hand_bytes

BASE_LINES = (
    "game_type: NLHE",
    "format: cash",
    "table_size: 2",
    "small_blind: 1",
    "big_blind: 2",
    "player: hero, SB, 100",
    "player: villain, BB, 100",
)


@given(st.sampled_from(("\n", "\r\n")), st.booleans())
def test_supported_newline_and_final_newline_choices_are_deterministic(
    newline: str,
    final_newline: bool,
) -> None:
    text = newline.join(BASE_LINES) + (newline if final_newline else "")
    source = text.encode()

    first = normalize_hand_bytes(source)
    second = normalize_hand_bytes(source)

    assert first == second
    assert first.status == "success"


@given(st.text(alphabet="abcdefghijklmnopqrstuvwxyz0123456789 ", min_size=1, max_size=64))
def test_comment_changes_source_hash_without_changing_normalized_hand(comment: str) -> None:
    base = ("\n".join(BASE_LINES) + "\n").encode()
    changed = (f"# {comment}\n" + "\n".join(BASE_LINES) + "\n").encode()

    first = normalize_hand_bytes(base)
    second = normalize_hand_bytes(changed)

    assert first.status == second.status == "success"
    assert first.hand == second.hand
    assert first.provenance.normalized_hand_sha256 == second.provenance.normalized_hand_sha256
    assert first.provenance.source_bytes_sha256 != second.provenance.source_bytes_sha256
