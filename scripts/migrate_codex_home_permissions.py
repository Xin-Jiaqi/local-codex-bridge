#!/usr/bin/env python3
"""Migrate the dedicated CODEX_HOME config for the bridge-workspace profile.

Background: bridge-workspace is a pure beta permission profile. Any loaded
legacy `sandbox_mode` / `[sandbox_workspace_write]` config makes Codex use the
legacy sandbox and ignore `default_permissions`, so the two must never mix.
This helper migrates $CODEX_HOME/config.toml for the dedicated Codex home:

  - removes top-level `sandbox_mode = ...` lines;
  - removes `sandbox_workspace_write` keys/tables (top-level dotted keys and
    `[sandbox_workspace_write]` sections);
  - sets top-level `default_permissions = "bridge-workspace"`;
  - installs / updates the `[permissions.bridge-workspace]` block from
    config/bridge-workspace.example.toml (single source of truth).

Everything else (model, model_provider, DeepSeek provider, env_key names,
comments, unknown sections) is preserved byte-for-byte. The script never
prints values of lines it removes or of secret-looking keys — output is
limited to key/table names and the template block it inserts. --apply writes a
same-directory backup (chmod 600) before touching the config.

Usage:
  scripts/migrate_codex_home_permissions.py --config PATH --dry-run
  scripts/migrate_codex_home_permissions.py --config PATH --verify
  scripts/migrate_codex_home_permissions.py --config PATH --apply
  # --codex-home DIR is an explicit-CODEX_HOME compatibility parameter: it
  #   defaults --config to <DIR>/config.toml and the extra scan to
  #   <DIR>/config/*.toml (used by the maintenance activation script for
  #   $HOME/.codex-deepseek-maintenance).
  # --config-dir DIR additionally scans DIR/*.toml, e.g. $CODEX_HOME/config
  #   (report-only; --apply only rewrites the --config file)
  # --project-root DIR additionally scans DIR/.codex/config.toml (report-only;
  # --apply never modifies project config)
  # --config defaults to $CODEX_HOME/config.toml or ~/.codex-deepseek/config.toml
"""

import argparse
import os
import shutil
import stat
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEMPLATE_PATH = os.path.join(ROOT, "config", "bridge-workspace.example.toml")
PROFILE = "bridge-workspace"
PROFILE_HEADER = "[permissions.%s]" % PROFILE


def read_template():
    with open(TEMPLATE_PATH, "r", encoding="utf-8") as fh:
        text = fh.read()
    if not text.endswith("\n"):
        text += "\n"
    return text


def template_parts(template):
    """Return (full_template, table_only). Comparisons of an existing profile
    block use table_only (the block starts at its own header, not at the
    template's leading comments); replacements insert the full template."""
    lines = template.splitlines(keepends=True)
    for idx, line in enumerate(lines):
        if line.strip() == PROFILE_HEADER:
            return template, "".join(lines[idx:])
    return template, template


def is_table_header(line):
    stripped = line.strip()
    return (
        stripped.startswith("[")
        and stripped.endswith("]")
        and not stripped.startswith("[[")
    )


def table_name(line):
    return line.strip()[1:-1].strip()


def is_legacy_section(name):
    return name == "sandbox_workspace_write" or name.startswith("sandbox_workspace_write.")


def is_profile_descendant(name):
    return name == PROFILE_HEADER[1:-1] or name.startswith("permissions.%s." % PROFILE)


def top_level_key(line):
    """Return the top-level key name of a `key = value` line, or None."""
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return None
    if "=" not in stripped:
        return None
    return stripped.split("=", 1)[0].strip().strip('"') or None


def transform(text, template):
    """Return (new_text, changes, needs_profile_block).

    changes: list of (action, name) with action in
    remove-key | remove-section | set-key | add-key | add-section | replace-section.
    Names never contain values, so no secret material can leak through output.
    """
    changes = []
    lines = text.splitlines(keepends=True)
    template_block, template_table = template_parts(template)
    out = []
    in_section = None          # name of the currently open table, if any
    skipping_section = False   # inside a removed [sandbox_workspace_write] block
    profile_start = None       # index in `out` where the old profile block began
    profile_line_start = None  # index in `lines` where the old profile block began
    in_profile = False         # inside the old [permissions.bridge-workspace] block
    had_profile = False        # saw the [permissions.bridge-workspace] header
    seen_default = False

    i = 0
    while i < len(lines):
        line = lines[i]
        if is_table_header(line):
            name = table_name(line)
            if in_profile and not is_profile_descendant(name):
                # old profile block ends here: keep it when it already IS the
                # template (idempotency), otherwise replace it.
                region = "".join(lines[profile_line_start:i])
                if region == template_table:
                    out.extend(lines[profile_line_start:i])
                else:
                    out[profile_start:] = [template_block]
                    changes.append(("replace-section", PROFILE_HEADER))
                in_profile = False
                profile_start = profile_line_start = None
                # fall through: handle the new header normally
            if skipping_section and not is_legacy_section(name):
                skipping_section = False
            if is_legacy_section(name):
                changes.append(("remove-section", line.strip()))
                skipping_section = True
                in_section = name
                i += 1
                continue
            if name == PROFILE_HEADER[1:-1]:
                had_profile = True
                profile_start = len(out)
                profile_line_start = i
                in_profile = True
                in_section = name
                i += 1
                continue
            in_section = name
            out.append(line)
            i += 1
            continue

        if in_profile:
            i += 1
            continue
        if skipping_section:
            i += 1
            continue
        if in_section:
            out.append(line)
            i += 1
            continue

        key = top_level_key(line)
        if key == "sandbox_mode" or (key or "").startswith("sandbox_workspace_write"):
            changes.append(("remove-key", key))
            i += 1
            continue
        if key == "default_permissions":
            seen_default = True
            value = line.split("=", 1)[1].strip().strip('"').strip("'")
            if value != PROFILE:
                indent = line[: len(line) - len(line.lstrip())]
                out.append('%sdefault_permissions = "%s"\n' % (indent, PROFILE))
                changes.append(("set-key", "default_permissions"))
            else:
                out.append(line)
            i += 1
            continue
        out.append(line)
        i += 1

    if in_profile:
        # profile block ran to EOF: keep or replace depending on content
        region = "".join(lines[profile_line_start:])
        if region == template_table:
            out.extend(lines[profile_line_start:])
        else:
            out[profile_start:] = [template_block]
            changes.append(("replace-section", PROFILE_HEADER))
    if not had_profile:
        if out and not out[-1].endswith("\n\n"):
            out.append("\n")
        out.append(template_block)
        changes.append(("add-section", PROFILE_HEADER))
    if not seen_default and not any(c[0] == "add-key" for c in changes):
        insert_at = 0
        while insert_at < len(out):
            stripped = out[insert_at].strip()
            if stripped and not stripped.startswith("#"):
                break
            insert_at += 1
        out.insert(insert_at, 'default_permissions = "%s"\n' % PROFILE)
        changes.append(("add-key", "default_permissions"))

    new_text = "".join(out)
    return new_text, changes


def format_changes(changes):
    return ["%s %s" % (action, name) for action, name in changes]


def verify_config(path, template):
    if not os.path.isfile(path):
        print("verify: no config at %s (nothing to migrate)" % path)
        return 0
    with open(path, "r", encoding="utf-8") as fh:
        text = fh.read()
    _, changes = transform(text, template)
    if not changes:
        print("verify: clean — no legacy sandbox keys; %s profile in place" % PROFILE_HEADER)
        return 0
    print("verify: legacy sandbox config found in %s:" % path)
    for change in format_changes(changes):
        print("  - %s" % change)
    print("verify: run with --apply to migrate (backup created, chmod 600)")
    return 1


def project_config_path(project_root):
    return os.path.join(project_root, ".codex", "config.toml")


def config_dir_paths(config_dir):
    """Return *.toml files under DIR, or [] when the dir does not exist."""
    if not config_dir or not os.path.isdir(config_dir):
        return []
    return [
        os.path.join(config_dir, name)
        for name in sorted(os.listdir(config_dir))
        if name.endswith(".toml")
    ]


def report_extra_configs(paths, template, mode):
    """Report-only scan of extra configs (HOME config/*.toml or project .codex).

    Never modifies them; returns 1 when any file is dirty. The bridge server
    guard scans the same set and rejects startup on legacy keys, so the
    start-time preflight must too.
    """
    dirty = 0
    for path in paths:
        if not os.path.isfile(path):
            continue
        with open(path, "r", encoding="utf-8") as fh:
            text = fh.read()
        _, changes = transform(text, template)
        legacy = [c for c in changes if c[0] in ("remove-key", "remove-section")]
        if not legacy:
            print("%s: extra config clean: %s" % (mode, path))
            continue
        print("%s: legacy sandbox config found in %s:" % (mode, path))
        for change in format_changes(legacy):
            print("  - %s" % change)
        print("%s: this file is report-only: this helper never modifies it; clean it manually" % mode)
        dirty = 1
    return dirty


def report_project_config(project_root, template, mode):
    """Report-only scan of a project .codex/config.toml for legacy sandbox keys.

    The project config is never modified (the bridge server guard rejects it at
    startup instead). Returns 1 when the project config is dirty; the caller
    decides how to act for its mode.
    """
    if not project_root:
        return 0
    path = project_config_path(project_root)
    if not os.path.isfile(path):
        print("%s: no project config at %s (nothing to scan)" % (mode, path))
        return 0
    with open(path, "r", encoding="utf-8") as fh:
        text = fh.read()
    _, changes = transform(text, template)
    # The profile block belongs in the dedicated CODEX_HOME config; a project
    # .codex/config.toml is only dangerous when it carries legacy sandbox keys,
    # so flag removals (legacy keys/sections) and ignore profile installs here.
    legacy = [c for c in changes if c[0] in ("remove-key", "remove-section")]
    if not legacy:
        print("%s: project config clean — no legacy sandbox keys in %s" % (mode, path))
        return 0
    print("%s: legacy sandbox config found in project config %s:" % (mode, path))
    for change in format_changes(legacy):
        print("  - %s" % change)
    print("%s: project config is report-only: this helper never modifies project files; clean it manually" % mode)
    return 1


def dry_run(path, template):
    if not os.path.isfile(path):
        print("dry-run: error: config not found: %s" % path, file=sys.stderr)
        return 2
    with open(path, "r", encoding="utf-8") as fh:
        text = fh.read()
    _, changes = transform(text, template)
    if not changes:
        print("dry-run: no changes needed for %s" % path)
        return 0
    print("dry-run: would apply %d change(s) to %s:" % (len(changes), path))
    for change in format_changes(changes):
        print("  - %s" % change)
    print("dry-run: nothing was modified")
    return 0


def apply_migration(path, template):
    if not os.path.isfile(path):
        print("apply: error: config not found: %s" % path, file=sys.stderr)
        return 2
    with open(path, "r", encoding="utf-8") as fh:
        text = fh.read()
    new_text, changes = transform(text, template)
    if not changes:
        print("apply: already up to date: %s" % path)
        return 0
    backup = path + ".bak"
    shutil.copy2(path, backup)
    os.chmod(backup, stat.S_IRUSR | stat.S_IWUSR)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".config.toml.migrate.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(new_text)
        os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    print("apply: migrated %s (%d change(s))" % (path, len(changes)))
    for change in format_changes(changes):
        print("  - %s" % change)
    print("apply: backup written to %s (chmod 600)" % backup)
    print("apply: restart the bridge for it to take effect")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Migrate the dedicated CODEX_HOME config to pure permission profiles."
    )
    parser.add_argument(
        "--codex-home",
        default=None,
        help="explicit CODEX_HOME; defaults --config to <DIR>/config.toml and the extra scan to <DIR>/config",
    )
    parser.add_argument("--config", default=None, help="path to config.toml")
    parser.add_argument(
        "--project-root",
        default=None,
        help="also scan <root>/.codex/config.toml for legacy keys (report-only; never modified)",
    )
    parser.add_argument(
        "--config-dir",
        default=None,
        help="also scan <dir>/*.toml for legacy keys, e.g. $CODEX_HOME/config (report-only; never modified)",
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--dry-run", action="store_true", help="show changes without writing")
    group.add_argument("--verify", action="store_true", help="exit 0 when clean, 1 when legacy keys remain")
    group.add_argument("--apply", action="store_true", help="migrate (backup first, chmod 600)")
    args = parser.parse_args(argv)

    if not os.path.isfile(TEMPLATE_PATH):
        print("error: profile template not found: %s" % TEMPLATE_PATH, file=sys.stderr)
        return 2
    codex_home = args.codex_home or os.environ.get("CODEX_HOME") or os.path.expanduser("~/.codex-deepseek")
    config = args.config or os.path.join(codex_home, "config.toml")
    template = read_template()
    config_dir = args.config_dir
    if config_dir is None:
        if args.config:
            # mirror the server guard's default scan next to an explicit --config
            default_dir = os.path.join(os.path.dirname(args.config), "config")
        else:
            # --codex-home (or the CODEX_HOME env) drives the default scan
            default_dir = os.path.join(codex_home, "config")
        if os.path.isdir(default_dir):
            config_dir = default_dir
    extra_paths = config_dir_paths(config_dir)

    if args.dry_run:
        rc = dry_run(config, template)
        if config_dir:
            report_extra_configs(extra_paths, template, "dry-run")
        if args.project_root:
            report_project_config(args.project_root, template, "dry-run")
        return rc
    if args.verify:
        rc = verify_config(config, template)
        if config_dir and report_extra_configs(extra_paths, template, "verify"):
            rc = 1
        if args.project_root and report_project_config(args.project_root, template, "verify"):
            rc = 1
        return rc
    if args.apply:
        rc = apply_migration(config, template)
        if config_dir and report_extra_configs(extra_paths, template, "apply"):
            print("apply: WARNING: config files under %s still have legacy sandbox keys; "
                  "bridge-workspace startup will keep refusing until they are cleaned manually."
                  % config_dir)
        if args.project_root and report_project_config(args.project_root, template, "apply"):
            print("apply: WARNING: bridge-workspace startup and the server guard will keep "
                  "refusing until %s is cleaned manually." % project_config_path(args.project_root))
        return rc
    print("error: choose one of --dry-run, --verify or --apply", file=sys.stderr)
    parser.print_usage(sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
