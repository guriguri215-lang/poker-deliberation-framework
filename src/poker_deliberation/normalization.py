"""Conservative key-value free-text normalization into the canonical hand schema."""

from __future__ import annotations

import csv
import re
from dataclasses import dataclass

from pydantic import ValidationError

from poker_deliberation.schemas import CanonicalHand


@dataclass(frozen=True, slots=True)
class HandNormalizationResult:
    hand: CanonicalHand | None
    warnings: tuple[str, ...]


def _fields(value: str) -> list[str]:
    return [item.strip() for item in next(csv.reader([value], skipinitialspace=True))]


def normalize_hand_text(text: str) -> HandNormalizationResult:
    """Parse a small, documented line format; never infer missing poker facts."""

    data: dict[str, object] = {"players": [], "actions": []}
    warnings: list[str] = []
    scalar_keys = {
        "game_type": str,
        "format": str,
        "table_size": int,
        "small_blind": float,
        "big_blind": float,
        "ante": float,
        "rake": float,
        "hero_player_id": str,
        "analysis_objective": str,
    }
    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        match = re.match(r"^([A-Za-z_]+)\s*:\s*(.*)$", line)
        if match is None:
            warnings.append(f"line {line_number}: ignored unrecognized free text")
            continue
        key, value = match.group(1).lower(), match.group(2).strip()
        try:
            if key in scalar_keys:
                data[key] = scalar_keys[key](value)
            elif key in {"hero_cards", "board"}:
                data[key] = [item for item in re.split(r"[\s,]+", value) if item]
            elif key == "player":
                items = _fields(value)
                if len(items) != 3:
                    raise ValueError("player requires id, position, starting_stack")
                players = data["players"]
                assert isinstance(players, list)
                players.append(
                    {
                        "player_id": items[0],
                        "position": items[1],
                        "starting_stack": float(items[2]),
                    }
                )
            elif key == "action":
                items = _fields(value)
                if len(items) not in {4, 5}:
                    raise ValueError("action requires street, actor, action, amount[, to_amount]")
                action: dict[str, object] = {
                    "street": items[0],
                    "actor": items[1],
                    "action": items[2],
                    "amount": float(items[3]),
                }
                if len(items) == 5 and items[4]:
                    action["to_amount"] = float(items[4])
                actions = data["actions"]
                assert isinstance(actions, list)
                actions.append(action)
            else:
                warnings.append(f"line {line_number}: ignored unknown key {key!r}")
        except (ValueError, TypeError) as exc:
            warnings.append(f"line {line_number}: {exc}")
    try:
        return HandNormalizationResult(CanonicalHand.model_validate(data), tuple(warnings))
    except ValidationError as exc:
        warnings.append(f"canonical validation failed: {exc}")
        return HandNormalizationResult(None, tuple(warnings))
