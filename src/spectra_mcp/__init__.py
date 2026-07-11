"""Spectra MCP 服务包。"""

from importlib.metadata import PackageNotFoundError, version

from .server import main, mcp

try:
    __version__ = version("spectra-mcp")
except PackageNotFoundError:
    __version__ = "0.0.0+unknown"

__all__ = ["main", "mcp", "__version__"]
