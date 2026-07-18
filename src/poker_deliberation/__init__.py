"""Auditable poker deliberation framework."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("poker-deliberation-framework")
except PackageNotFoundError:  # pragma: no cover - editable source without metadata
    __version__ = "0.1.0"

__all__ = ["__version__"]
