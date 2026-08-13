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
from .workspace_guard import (
    TaskCwdError,
    validate_maintenance_cwd,
    validate_task_cwd,
)

__version__ = "1.1.0"

__all__ = [
    "AppServerError",
    "AppServerProcessError",
    "BridgeCore",
    "CodexAppServerClient",
    "Logger",
    "RequestTimeoutError",
    "TaskCwdError",
    "ThreadList",
    "TurnResult",
    "validate_maintenance_cwd",
    "validate_task_cwd",
]
