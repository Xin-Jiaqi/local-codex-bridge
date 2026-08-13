#!/usr/bin/env python3
"""Offline tests for scripts/migrate_codex_home_permissions.py.

The helper migrates the dedicated CODEX_HOME config for the bridge-workspace
permission profile: it removes legacy `sandbox_mode` / `[sandbox_workspace_write]`
keys, sets `default_permissions = "bridge-workspace"`, installs/updates the
[permissions.bridge-workspace] block, preserves everything else, and never
prints secret values. All runs use temp configs; nothing outside /tmp is
touched.
"""

import os
import stat
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, "scripts", "migrate_codex_home_permissions.py")

FAKE_API_KEY = "sk-test-FAKE-please-never-print-0123456789abcdef"

DIRTY_CONFIG = """\
# Local Codex Bridge dedicated home (DeepSeek)
model = "deepseek-chat"
model_provider = "deepseek"
model_reasoning_effort = "medium"

# Legacy sandbox keys (migration must remove these)
sandbox_mode = "workspace-write"

[sandbox_workspace_write]
network_access = true

[model_providers.deepseek]
name = "DeepSeek"
base_url = "https://api.deepseek.com/v1"
env_key = "DEEPSEEK_API_KEY"
api_key = "%s"

[projects."/tmp/repo"]
trust_level = "trusted"
""" % FAKE_API_KEY

STALE_PROFILE = """\
[permissions.bridge-workspace]
description = "old stale profile without extends"

[permissions.bridge-workspace.filesystem]
".git" = "write"
"""


def run(args, cwd=None):
    return subprocess.run(
        [sys.executable, SCRIPT] + args,
        capture_output=True, text=True, cwd=cwd,
    )


class MigrateCodexHomePermissionsTest(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="lcb-migrate-")
        self.config = os.path.join(self.dir, "config.toml")
        with open(self.config, "w", encoding="utf-8") as fh:
            fh.write(DIRTY_CONFIG)
        os.chmod(self.config, 0o600)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.dir)

    def assert_no_secret_output(self, proc):
        self.assertNotIn(FAKE_API_KEY, proc.stdout)
        self.assertNotIn(FAKE_API_KEY, proc.stderr)
        self.assertNotIn("sk-test-", proc.stdout)

    def read(self):
        with open(self.config, "r", encoding="utf-8") as fh:
            return fh.read()

    def make_project(self, text=None):
        project = os.path.join(self.dir, "project")
        codex_dir = os.path.join(project, ".codex")
        os.makedirs(codex_dir)
        path = os.path.join(codex_dir, "config.toml")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text if text is not None else DIRTY_CONFIG)
        return project, path

    def make_config_dir(self, text=None):
        config_dir = os.path.join(self.dir, "home", "config")
        os.makedirs(config_dir)
        path = os.path.join(config_dir, "extra.toml")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text if text is not None else DIRTY_CONFIG)
        return config_dir, path

    def test_dry_run_reports_and_does_not_modify(self):
        before = self.read()
        proc = run(["--config", self.config, "--dry-run"])
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assert_no_secret_output(proc)
        self.assertIn("remove-key sandbox_mode", proc.stdout)
        self.assertIn("remove-section [sandbox_workspace_write]", proc.stdout)
        self.assertIn("add-key default_permissions", proc.stdout)
        self.assertIn("add-section [permissions.bridge-workspace]", proc.stdout)
        self.assertEqual(self.read(), before, "dry-run must not modify the config")

    def test_verify_flags_dirty_then_clean(self):
        proc = run(["--config", self.config, "--verify"])
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assert_no_secret_output(proc)
        self.assertIn("sandbox_mode", proc.stdout)
        run(["--config", self.config, "--apply"])
        proc = run(["--config", self.config, "--verify"])
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("clean", proc.stdout)

    def test_apply_removes_legacy_and_installs_profile_preserving_rest(self):
        proc = run(["--config", self.config, "--apply"])
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assert_no_secret_output(proc)
        self.assertIn("backup written", proc.stdout)
        text = self.read()

        self.assertNotIn("sandbox_mode =", text)
        for line in text.splitlines():
            stripped = line.strip()
            self.assertFalse(stripped.startswith("sandbox_mode"), line)
            self.assertFalse(stripped == "[sandbox_workspace_write]"
                             or stripped.startswith("[sandbox_workspace_write."), line)
        self.assertIn('default_permissions = "bridge-workspace"', text)
        self.assertIn("extends = \":workspace\"", text)
        self.assertIn('".git/" = "write"', text)
        self.assertIn('".env" = "read"', text)
        self.assertIn('"github.com" = "allow"', text)
        self.assertIn('"api.github.com" = "allow"', text)

        # untouched configuration survives byte-for-byte, including secrets
        # (which the tool must never print but also never delete).
        self.assertIn('model = "deepseek-chat"', text)
        self.assertIn('model_provider = "deepseek"', text)
        self.assertIn('env_key = "DEEPSEEK_API_KEY"', text)
        self.assertIn(FAKE_API_KEY, text)
        self.assertIn('[projects."/tmp/repo"]', text)
        self.assertIn('trust_level = "trusted"', text)
        self.assertIn("# Local Codex Bridge dedicated home (DeepSeek)", text)

        # backup exists, contains the original, and is chmod 600
        backup = self.config + ".bak"
        self.assertTrue(os.path.isfile(backup))
        with open(backup, "r", encoding="utf-8") as fh:
            self.assertEqual(fh.read(), DIRTY_CONFIG)
        mode = stat.S_IMODE(os.stat(backup).st_mode)
        self.assertEqual(mode, 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(self.config).st_mode), 0o600)

    def test_apply_is_idempotent(self):
        first = run(["--config", self.config, "--apply"])
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        after_first = self.read()
        second = run(["--config", self.config, "--apply"])
        self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
        self.assertIn("already up to date", second.stdout)
        self.assertEqual(self.read(), after_first)

    def test_apply_replaces_stale_profile_block(self):
        with open(self.config, "a", encoding="utf-8") as fh:
            fh.write("\n" + STALE_PROFILE)
        proc = run(["--config", self.config, "--apply"])
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        text = self.read()
        self.assertNotIn("old stale profile", text)
        self.assertIn("extends = \":workspace\"", text)
        self.assertNotIn('".git" = "write"', text)
        self.assertIn('".git/" = "write"', text)

    def test_replaces_existing_default_permissions(self):
        with open(self.config, "w", encoding="utf-8") as fh:
            fh.write('default_permissions = "other-profile"\nmodel = "deepseek-chat"\n')
        proc = run(["--config", self.config, "--apply"])
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn('default_permissions = "bridge-workspace"', self.read())
        self.assertNotIn("other-profile", self.read())

    def test_missing_config_behavior(self):
        missing = os.path.join(self.dir, "missing.toml")
        proc = run(["--config", missing, "--dry-run"])
        self.assertEqual(proc.returncode, 2)
        proc = run(["--config", missing, "--verify"])
        self.assertEqual(proc.returncode, 0)
        self.assertIn("nothing to migrate", proc.stdout)
        proc = run(["--config", missing, "--apply"])
        self.assertEqual(proc.returncode, 2)

    def test_verify_scans_project_config_and_flags_dirty(self):
        project, path = self.make_project()
        with open(path, "r", encoding="utf-8") as fh:
            before = fh.read()
        proc = run(["--config", self.config, "--project-root", project, "--verify"])
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assert_no_secret_output(proc)
        self.assertIn("project config", proc.stdout)
        self.assertIn("sandbox_mode", proc.stdout)
        self.assertIn("report-only", proc.stdout)
        with open(path, "r", encoding="utf-8") as fh:
            self.assertEqual(fh.read(), before, "verify must not modify project config")

    def test_verify_project_config_clean(self):
        project, _ = self.make_project('model = "deepseek-chat"\n')
        run(["--config", self.config, "--apply"])
        proc = run(["--config", self.config, "--project-root", project, "--verify"])
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("project config clean", proc.stdout)

    def test_dry_run_reports_project_config_without_modifying(self):
        project, path = self.make_project()
        with open(path, "r", encoding="utf-8") as fh:
            before = fh.read()
        proc = run(["--config", self.config, "--project-root", project, "--dry-run"])
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assert_no_secret_output(proc)
        self.assertIn("project config", proc.stdout)
        with open(path, "r", encoding="utf-8") as fh:
            self.assertEqual(fh.read(), before, "dry-run must not modify project config")

    def test_apply_never_touches_project_config(self):
        project, path = self.make_project()
        with open(path, "r", encoding="utf-8") as fh:
            before = fh.read()
        proc = run(["--config", self.config, "--project-root", project, "--apply"])
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("WARNING", proc.stdout)
        self.assertNotIn("sandbox_mode =", self.read(), "home config must be migrated")
        with open(path, "r", encoding="utf-8") as fh:
            self.assertEqual(fh.read(), before, "apply must never modify project config")

    def test_missing_project_config_is_clean(self):
        project = os.path.join(self.dir, "empty-project")
        os.makedirs(project)
        run(["--config", self.config, "--apply"])
        proc = run(["--config", self.config, "--project-root", project, "--verify"])
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("nothing to scan", proc.stdout)

    def test_verify_scans_config_dir_and_flags_dirty(self):
        config_dir, path = self.make_config_dir()
        with open(path, "r", encoding="utf-8") as fh:
            before = fh.read()
        proc = run(["--config", self.config, "--config-dir", config_dir, "--verify"])
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assert_no_secret_output(proc)
        self.assertIn("extra.toml", proc.stdout)
        self.assertIn("report-only", proc.stdout)
        with open(path, "r", encoding="utf-8") as fh:
            self.assertEqual(fh.read(), before, "verify must not modify config dir files")

    def test_verify_config_dir_clean(self):
        config_dir, _ = self.make_config_dir('model = "deepseek-chat"\n')
        run(["--config", self.config, "--apply"])
        proc = run(["--config", self.config, "--config-dir", config_dir, "--verify"])
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("extra config clean", proc.stdout)

    def test_apply_never_touches_config_dir(self):
        config_dir, path = self.make_config_dir()
        with open(path, "r", encoding="utf-8") as fh:
            before = fh.read()
        proc = run(["--config", self.config, "--config-dir", config_dir, "--apply"])
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("WARNING", proc.stdout)
        self.assertNotIn("sandbox_mode =", self.read(), "home config must be migrated")
        with open(path, "r", encoding="utf-8") as fh:
            self.assertEqual(fh.read(), before, "apply must never modify config dir files")

    def test_missing_config_dir_is_clean(self):
        missing = os.path.join(self.dir, "no-such-config-dir")
        run(["--config", self.config, "--apply"])
        proc = run(["--config", self.config, "--config-dir", missing, "--verify"])
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("clean", proc.stdout)

    def test_requires_explicit_action(self):
        proc = run(["--config", self.config])
        self.assertEqual(proc.returncode, 2)
        self.assertIn("--dry-run", proc.stderr + proc.stdout)


if __name__ == "__main__":
    unittest.main()
