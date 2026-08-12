"""Local Codex Bridge Core: reusable app-server client and high-level API.

This package intentionally contains NO MCP layer yet; it is the backend core
that a future MCP bridge can build on.
"""

from .client import (
    AppServerError,
    AppServerProcessError,
    CodexAppServerClient,
    Logger,
    RequestTimeoutError,
)
from .core import BridgeCore, ThreadList, TurnResult

__version__ = "1.0.0"

__all__ = [
    "AppServerError",
    "AppServerProcessError",
    "BridgeCore",
    "CodexAppServerClient",
    "Logger",
    "RequestTimeoutError",
    "ThreadList",
    "TurnResult",
]
