"""whatifd - open experiment runner for LLM behavior changes."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("whatifd")
except PackageNotFoundError:
    # Source-only checkout (no `pip install`); see cascade-catalog.md.
    __version__ = "0.0.0+unknown"
