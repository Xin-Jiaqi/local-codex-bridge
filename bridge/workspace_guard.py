"""Reusable task working-directory safety validators.

The Bridge control plane (this repo, the instance state root, the instance
CODEX_HOME, and $HOME) must never become a task workspace for ordinary
tasks: a task running with one of those as its cwd could rewrite control-plane
files. Every thread/start (new native thread) validates its cwd through
:func:`validate_task_cwd` before the app-server request is sent.

The maintenance instance is a HOST-ADMIN maintenance window: its tasks work
ON the Bridge repo itself, so :func:`validate_maintenance_cwd` inverts the
default rule — only the Bridge repo root or a real subdirectory is accepted,
and everything outside (including $HOME, "/", other projects, the maintenance
instance state and CODEX_HOME) is rejected.

Canonicalization uses realpath so symlinks cannot bypass the guards. Errors are
structured (:class:`TaskCwdError` with a generic reason and a category) and
never contain private paths.
"""

import os


class TaskCwdError(Exception):
    """Structured rejection for a task cwd inside the bridge control plane.

    ``reason`` is generic and safe to surface to clients; ``category`` is one
    of ``missing``, ``home``, ``bridge_repo``, ``instance_state``,
    ``codex_home``, ``outside``.
    """

    def __init__(self, reason, category):
        super().__init__(reason)
        self.reason = reason
        self.category = category


_GENERIC_REASON = (
    "task cwd must be an explicit project directory outside the bridge "
    "control plane"
)


def _real(path):
    return os.path.realpath(os.path.abspath(path))


def _same_or_ancestor(cwd, target):
    """True when cwd equals target or is an ancestor of it."""
    if cwd == target:
        return True
    if cwd == os.sep:
        return True  # "/" is an ancestor of every absolute path
    return target.startswith(cwd + os.sep)


def _same_or_inside(cwd, target):
    """True when cwd equals target or is inside it."""
    return cwd == target or cwd.startswith(target + os.sep)


def validate_task_cwd(cwd, home, repo_root, state_root=None, codex_home=None):
    """Validate a new-task cwd against the control plane.

    Rejects (after realpath canonicalization) a cwd that is:
    - missing/blank;
    - $HOME itself or an ancestor of $HOME (a sibling project under $HOME,
      e.g. $HOME/Desktop/some-project, remains allowed);
    - the bridge repo root, any ancestor of it, or any path inside it;
    - the instance state root, any ancestor of it, or any path inside it;
    - the instance CODEX_HOME, any ancestor of it, or any path inside it.

    Returns the canonical (realpath) cwd on success.
    """
    if not isinstance(cwd, str) or not cwd.strip():
        raise TaskCwdError(
            "task cwd is required: new tasks must run in an explicit project "
            "directory outside the bridge control plane",
            "missing",
        )
    c = _real(cwd)
    h = _real(home)
    if c == h or _same_or_ancestor(c, h):
        raise TaskCwdError(_GENERIC_REASON, "home")
    for label, target in (
        ("bridge_repo", repo_root),
        ("instance_state", state_root),
        ("codex_home", codex_home),
    ):
        if not target:
            continue
        t = _real(target)
        if _same_or_inside(c, t) or _same_or_ancestor(c, t):
            raise TaskCwdError(_GENERIC_REASON, label)
    return c


def validate_maintenance_cwd(cwd, repo_root, home=None, state_root=None,
                             codex_home=None):
    """Validate a maintenance-window task cwd against the Bridge repo.

    The maintenance instance exists so the Bridge can maintain its own repo.
    Its task workspace is the repo itself: only ``repo_root`` or a real
    subdirectory of it (after realpath canonicalization) is accepted. A cwd
    that is missing/blank, or whose canonical path is outside the repo, is
    rejected; $HOME, "/", ancestors of the repo and other projects are all
    outside the repo and therefore rejected. The maintenance instance state
    root and CODEX_HOME are rejected explicitly (defense in depth) even
    though they normally live outside the repo.

    Returns the canonical (realpath) cwd on success.
    """
    if not isinstance(cwd, str) or not cwd.strip():
        raise TaskCwdError(
            "task cwd is required: maintenance tasks must run inside the "
            "bridge repo",
            "missing",
        )
    c = _real(cwd)
    # Home/ancestors first (like the task guard, "/" is home-category), then
    # the explicit maintenance state/CODEX_HOME checks (precise categories
    # even though they normally live outside the repo), then the repo rule.
    if home:
        h = _real(home)
        if c == h or _same_or_ancestor(c, h):
            raise TaskCwdError(_GENERIC_REASON, "home")
    for label, target in (
        ("instance_state", state_root),
        ("codex_home", codex_home),
    ):
        if not target:
            continue
        t = _real(target)
        if _same_or_inside(c, t) or _same_or_ancestor(c, t):
            raise TaskCwdError(_GENERIC_REASON, label)
    r = _real(repo_root)
    if c == r or _same_or_inside(c, r):
        return c
    raise TaskCwdError(_GENERIC_REASON, "outside")
