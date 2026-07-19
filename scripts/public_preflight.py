# ruff: noqa: E402, I001

from __future__ import annotations

import sys
from pathlib import Path

WORKSPACE = Path(__file__).resolve().parents[1]
SOURCE_ROOT = WORKSPACE / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from poker_deliberation.public_preflight import main


if __name__ == "__main__":
    raise SystemExit(main())
