"""spectra_mcp service package."""

from importlib.metadata import PackageNotFoundError, version

from .server import main, mcp

try:
    __version__ = version("spectra_mcp")
except PackageNotFoundError:
    __version__ = "0.0.0+unknown"

__all__ = ["main", "mcp", "__version__"]
