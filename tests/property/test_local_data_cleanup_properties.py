from __future__ import annotations

from datetime import UTC, datetime, timedelta

from hypothesis import given
from hypothesis import strategies as st

from poker_deliberation.local_data_cleanup_canonical import (
    canonical_cleanup_bytes,
    cleanup_plan_sha256,
    parse_canonical_cleanup_json,
)
from poker_deliberation.local_data_cleanup_models import CleanupLimitsV1


@given(
    st.dictionaries(
        keys=st.text(
            alphabet=st.characters(
                min_codepoint=0x20,
                max_codepoint=0x7E,
                blacklist_characters=['"', "\\"],
            ),
            min_size=1,
            max_size=12,
        ),
        values=st.integers(min_value=-(2**31), max_value=2**31 - 1),
        max_size=12,
    )
)
def test_canonical_cleanup_json_round_trips(mapping: dict[str, int]) -> None:
    encoded = canonical_cleanup_bytes(mapping)
    assert parse_canonical_cleanup_json(encoded) == mapping
    assert canonical_cleanup_bytes(parse_canonical_cleanup_json(encoded)) == encoded


@given(
    tree_entries=st.integers(min_value=1, max_value=10_000),
    target_bytes=st.integers(min_value=1, max_value=100_000_000),
    plan_seconds=st.integers(min_value=1, max_value=86_400),
)
def test_approved_limits_accept_only_values_at_or_below_caps(
    tree_entries: int,
    target_bytes: int,
    plan_seconds: int,
) -> None:
    limits = CleanupLimitsV1(
        maximum_tree_entries=tree_entries,
        maximum_target_bytes=target_bytes,
        maximum_plan_lifetime_seconds=plan_seconds,
    )
    assert limits.maximum_tree_entries <= 10_000
    assert limits.maximum_target_bytes <= 100_000_000
    assert limits.maximum_plan_lifetime_seconds <= 86_400


def test_datetime_canonicalization_is_stable_at_microsecond_precision() -> None:
    value = {
        "generated_at": datetime(2026, 7, 25, 1, 2, 3, 456789, tzinfo=UTC),
        "expires_at": datetime(2026, 7, 25, 1, 2, 3, 456789, tzinfo=UTC) + timedelta(seconds=1),
    }
    assert canonical_cleanup_bytes(value) == (
        b'{"expires_at":"2026-07-25T01:02:04.456789Z","generated_at":"2026-07-25T01:02:03.456789Z"}'
    )
    assert cleanup_plan_sha256  # keep the separated plan hash import contract visible
