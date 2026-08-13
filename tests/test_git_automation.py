#!/usr/bin/env python3
"""Regression tests for the Bridge sandbox-mode switch and git/GitHub automation.

Offline (always run):
- BRIDGE_SANDBOX_MODE / BRIDGE_NETWORK_ACCESS propagate to the spawned
  app-server `-c` overrides (network config propagation);
- bridge-workspace is a PURE permission profile: its `-c` injection never
  contains legacy `sandbox_mode` / `sandbox_workspace_write` keys, and legacy
  modes never inject `default_permissions` (mixing is not allowed);
- the bridge-workspace profile (extends=":workspace") carries exactly the
  minimal grants: `.git/` metadata writes + GitHub-only network allowlist,
  `.env`/`.codex/`/`.agents/` kept read-only, no full-disk access;
- the bridge-workspace child env drops the user's Git HTTP(S) proxy so the
  profile never depends on it;
- legacy sandbox key detection finds dirty CODEX_HOME configs.

Integration (optional): RUN_SANDBOX_TESTS=1 runs
scripts/verify_bridge_git_automation.sh in a real Terminal (needs Seatbelt) to
prove the app-server sandbox can `git init`/`git add`/`git commit` under the
bridge-workspace profile and that workspace-write denies it.
"""

import os
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from http_server.server import (
    BRIDGE_PERMISSION_PROFILE,
    BRIDGE_WORKSPACE_EXTENDS_OVERRIDE,
    BRIDGE_WORKSPACE_FILESYSTEM_OVERRIDE,
    BRIDGE_WORKSPACE_NETWORK_OVERRIDE,
    CONFIG_OVERRIDES,
    build_child_env,
    build_config_overrides,
    find_legacy_sandbox_keys,
    legacy_sandbox_keys_in_home,
    thread_start_permission_config,
)
from bridge import BridgeCore, Logger

TEMPLATE = os.path.join(ROOT, "config", "bridge-workspace.example.toml")

GITHUB_DOMAINS = (
    "github.com",
    "*.github.com",
    "api.github.com",
    "ssh.github.com",
    "*.githubusercontent.com",
    "objects.githubusercontent.com",
    "raw.githubusercontent.com",
)


class SandboxModeConfigTest(unittest.TestCase):

    def test_default_mode_is_workspace_write_without_network(self):
        overrides = build_config_overrides({})
        self.assertIn('sandbox_mode="workspace-write"', overrides)
        self.assertNotIn("sandbox_workspace_write.network_access=true", overrides)
        self.assertNotIn("default_permissions=", " ".join(overrides))

    def test_legacy_constant_matches_default(self):
        self.assertEqual(CONFIG_OVERRIDES, build_config_overrides({}))
        self.assertIn('approval_policy="on-request"', CONFIG_OVERRIDES)

    def test_network_access_flag_enables_workspace_network(self):
        overrides = build_config_overrides({"BRIDGE_NETWORK_ACCESS": "true"})
        self.assertIn("sandbox_workspace_write.network_access=true", overrides)
        self.assertIn('sandbox_mode="workspace-write"', overrides)
        for value in ("1", "yes", "on", "TRUE"):
            self.assertIn(
                "sandbox_workspace_write.network_access=true",
                build_config_overrides({"BRIDGE_NETWORK_ACCESS": value}),
            )

    def test_profile_mode_emits_default_permissions_and_no_sandbox_mode(self):
        overrides = build_config_overrides({"BRIDGE_SANDBOX_MODE": "bridge-workspace"})
        self.assertIn('default_permissions="%s"' % BRIDGE_PERMISSION_PROFILE, overrides)
        self.assertIn(BRIDGE_WORKSPACE_EXTENDS_OVERRIDE, overrides)
        self.assertIn(BRIDGE_WORKSPACE_FILESYSTEM_OVERRIDE, overrides)
        self.assertIn(BRIDGE_WORKSPACE_NETWORK_OVERRIDE, overrides)

    def test_profile_mode_never_injects_legacy_sandbox_keys(self):
        # Beta permission profiles cannot be mixed with the legacy sandbox:
        # bridge-workspace must be pure profile, always.
        overrides = build_config_overrides({"BRIDGE_SANDBOX_MODE": "bridge-workspace"})
        for override in overrides:
            self.assertNotIn("sandbox_mode", override, override)
            self.assertNotIn("sandbox_workspace_write", override, override)

    def test_legacy_modes_never_inject_profile_keys(self):
        # And the reverse: legacy modes must not smuggle in default_permissions
        # or permission-profile tables.
        for env in (
            {},
            {"BRIDGE_NETWORK_ACCESS": "true"},
            {"BRIDGE_SANDBOX_MODE": "danger-full-access"},
        ):
            overrides = build_config_overrides(env)
            for override in overrides:
                self.assertNotIn("default_permissions=", override, override)
                self.assertNotIn("permissions.", override, override)
                self.assertNotIn("extends=", override, override)

    def test_profile_mode_injection_is_self_contained(self):
        # The full profile must come from -c flags: no config-file dependency.
        text = " ".join(build_config_overrides({"BRIDGE_SANDBOX_MODE": "bridge-workspace"}))
        self.assertIn('extends=":workspace"', text)
        self.assertIn('".git/" = "write"', text)
        self.assertIn('".codex/" = "read"', text)
        self.assertIn('".agents/" = "read"', text)
        self.assertIn('".env" = "read"', text)
        self.assertIn('":minimal" = "read"', text)
        self.assertIn('"github.com" = "allow"', text)
        self.assertIn('".git/hooks/" = "read"', text)
        self.assertIn('"*.githubusercontent.com" = "allow"', text)
        self.assertNotIn('"*" = "allow"', text)

    def test_danger_mode_is_explicit_and_unchanged(self):
        overrides = build_config_overrides({"BRIDGE_SANDBOX_MODE": "danger-full-access"})
        self.assertIn('sandbox_mode="danger-full-access"', overrides)
        self.assertNotIn("default_permissions=", " ".join(overrides))

    def test_invalid_mode_raises(self):
        with self.assertRaises(ValueError):
            build_config_overrides({"BRIDGE_SANDBOX_MODE": "full-access"})
        with self.assertRaises(ValueError):
            build_config_overrides({"BRIDGE_SANDBOX_MODE": "workspace_write"})

    def test_approval_policy_on_request_in_all_modes(self):
        for env in (
            {},
            {"BRIDGE_NETWORK_ACCESS": "true"},
            {"BRIDGE_SANDBOX_MODE": "bridge-workspace"},
            {"BRIDGE_SANDBOX_MODE": "danger-full-access"},
        ):
            self.assertIn('approval_policy="on-request"', build_config_overrides(env))


class ChildEnvTest(unittest.TestCase):

    PROFILE_OVERRIDES = build_config_overrides({"BRIDGE_SANDBOX_MODE": "bridge-workspace"})

    def test_child_env_untouched_outside_bridge_workspace(self):
        base = {"http_proxy": "http://127.0.0.1:3128", "PATH": "/usr/bin", "HOME": "/tmp/x"}
        for overrides in (None, [], build_config_overrides({}),
                          build_config_overrides({"BRIDGE_SANDBOX_MODE": "danger-full-access"})):
            self.assertEqual(build_child_env(dict(base), overrides), base)

    def test_child_env_bridge_workspace_strips_git_proxy(self):
        base = {
            "http_proxy": "http://127.0.0.1:3128",
            "HTTPS_PROXY": "http://127.0.0.1:3128",
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "user.name",
            "GIT_CONFIG_VALUE_0": "someone",
            "PATH": "/usr/bin",
        }
        env = build_child_env(dict(base), self.PROFILE_OVERRIDES)
        for key in ("http_proxy", "HTTPS_PROXY"):
            self.assertNotIn(key, env)
        self.assertNotIn("user.name", env.values())
        self.assertEqual(env["GIT_CONFIG_COUNT"], "2")
        self.assertEqual(env["GIT_CONFIG_KEY_0"], "http.https://github.com.proxy")
        self.assertEqual(env["GIT_CONFIG_VALUE_0"], "")
        self.assertEqual(env["GIT_CONFIG_KEY_1"], "http.proxy")
        self.assertEqual(env["GIT_CONFIG_VALUE_1"], "")
        self.assertEqual(env["PATH"], "/usr/bin")

    def test_child_env_without_profile_keeps_git_config_env(self):
        base = {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "user.name", "PATH": "/usr/bin"}
        self.assertEqual(build_child_env(dict(base), []), base)


class LegacySandboxDetectionTest(unittest.TestCase):

    def test_find_legacy_sandbox_keys_detects_top_level_and_section(self):
        text = (
            '# header\n'
            'model = "deepseek-chat"\n'
            'sandbox_mode = "workspace-write"\n'
            'sandbox_workspace_write.network_access = true\n'
            '[sandbox_workspace_write]\n'
            'network_access = true\n'
            '[projects."/tmp/repo"]\n'
            'trust_level = "trusted"\n'
            'sandbox_mode = "workspace-write"\n'  # inside another table: not legacy
            '[permissions.bridge-workspace]\n'
            'description = "mentions sandbox_mode in prose only"\n'
        )
        keys, sections = find_legacy_sandbox_keys(text)
        self.assertEqual(keys, ["sandbox_mode", "sandbox_workspace_write.network_access"])
        self.assertEqual(sections, ["[sandbox_workspace_write]"])

    def test_find_legacy_sandbox_keys_clean(self):
        text = (
            'model = "deepseek-chat"\n'
            'default_permissions = "bridge-workspace"\n'
            '[permissions.bridge-workspace]\n'
            'extends = ":workspace"\n'
        )
        keys, sections = find_legacy_sandbox_keys(text)
        self.assertEqual((keys, sections), ([], []))

    def test_legacy_sandbox_keys_in_home_scans_config_dir(self):
        home = tempfile.mkdtemp(prefix="lcb-home-")
        try:
            os.makedirs(os.path.join(home, "config"))
            with open(os.path.join(home, "config.toml"), "w", encoding="utf-8") as fh:
                fh.write('sandbox_mode = "workspace-write"\n')
            with open(os.path.join(home, "config", "extra.toml"), "w", encoding="utf-8") as fh:
                fh.write('[sandbox_workspace_write]\nnetwork_access = true\n')
            found = legacy_sandbox_keys_in_home(home, workspace_root=None)
            self.assertEqual(
                {os.path.basename(p): (keys, sections) for p, (keys, sections) in found.items()},
                {
                    "config.toml": (["sandbox_mode"], []),
                    "extra.toml": ([], ["[sandbox_workspace_write]"]),
                },
            )
        finally:
            import shutil
            shutil.rmtree(home)

    def test_legacy_sandbox_keys_in_home_clean_returns_empty(self):
        home = tempfile.mkdtemp(prefix="lcb-home-")
        try:
            with open(os.path.join(home, "config.toml"), "w", encoding="utf-8") as fh:
                fh.write('default_permissions = "bridge-workspace"\n')
            self.assertEqual(legacy_sandbox_keys_in_home(home, workspace_root=None), {})
        finally:
            import shutil
            shutil.rmtree(home)

    def test_legacy_sandbox_keys_in_home_checks_project_codex_config(self):
        home = tempfile.mkdtemp(prefix="lcb-home-")
        project = tempfile.mkdtemp(prefix="lcb-project-")
        try:
            with open(os.path.join(home, "config.toml"), "w", encoding="utf-8") as fh:
                fh.write('model = "deepseek-chat"\n')
            os.makedirs(os.path.join(project, ".codex"))
            with open(os.path.join(project, ".codex", "config.toml"), "w", encoding="utf-8") as fh:
                fh.write('sandbox_mode = "workspace-write"\n')
            found = legacy_sandbox_keys_in_home(home, workspace_root=project)
            self.assertEqual(list(found), [os.path.join(project, ".codex", "config.toml")])
        finally:
            import shutil
            shutil.rmtree(home)
            shutil.rmtree(project)


class ProfileTemplateTest(unittest.TestCase):

    def setUp(self):
        with open(TEMPLATE, encoding="utf-8") as fh:
            self.text = fh.read()

    def test_template_grants_git_metadata_write_only_inside_workspace(self):
        self.assertIn('extends = ":workspace"', self.text)
        self.assertIn('".git/" = "write"', self.text)
        self.assertIn('".git/hooks/" = "read"', self.text)
        self.assertIn('"." = "write"', self.text)
        self.assertIn('":minimal" = "read"', self.text)
        self.assertIn('":tmpdir" = "write"', self.text)
        self.assertIn('":slash_tmp" = "write"', self.text)
        self.assertIn('".codex/" = "read"', self.text)
        self.assertIn('".agents/" = "read"', self.text)
        self.assertIn('".env" = "read"', self.text)

    def test_template_network_is_github_allowlist_only(self):
        self.assertIn("enabled = true", self.text)
        for domain in GITHUB_DOMAINS:
            self.assertIn('"%s" = "allow"' % domain, self.text)
        self.assertNotIn('"*" = "allow"', self.text)

    def test_template_has_no_legacy_sandbox_keys(self):
        self.assertNotIn("sandbox_mode =", self.text)
        self.assertNotIn("sandbox_workspace_write =", self.text)
        for line in self.text.splitlines():
            stripped = line.strip()
            self.assertFalse(stripped == "[sandbox_workspace_write]"
                             or stripped.startswith("[sandbox_workspace_write."), line)

    def test_template_has_no_secrets_or_paths(self):
        for needle in (
            "api_key", "DEEPSEEK_API_KEY", "token", "Bearer",
            "/Users/", "ZOTERO", "ngrok", "8321",
        ):
            self.assertNotIn(needle, self.text)

    def test_template_matches_runtime_overrides(self):
        # Single source of truth: the -c overrides and the reference template
        # must grant the same rules.
        runtime = " ".join(
            (BRIDGE_WORKSPACE_EXTENDS_OVERRIDE,
             BRIDGE_WORKSPACE_FILESYSTEM_OVERRIDE,
             BRIDGE_WORKSPACE_NETWORK_OVERRIDE)
        )
        self.assertIn('extends = ":workspace"', self.text)
        self.assertIn('extends=":workspace"', runtime)
        for rule in (
            '":minimal" = "read"',
            '":tmpdir" = "write"',
            '":slash_tmp" = "write"',
            '"." = "write"',
            '".git/" = "write"',
            '".git/hooks/" = "read"',
            '".codex/" = "read"',
            '".agents/" = "read"',
            '".env" = "read"',
            '"github.com" = "allow"',
            '"*.github.com" = "allow"',
            '"api.github.com" = "allow"',
            '"ssh.github.com" = "allow"',
            '"*.githubusercontent.com" = "allow"',
            '"objects.githubusercontent.com" = "allow"',
            '"raw.githubusercontent.com" = "allow"',
        ):
            self.assertIn(rule, self.text, rule)
        self.assertIn(rule, runtime, rule)


class ThreadStartParamsTest(unittest.TestCase):
    """Codex 0.147.0 app-server protocol regression tests.

    bridge-workspace must explicitly select the named permission profile on
    every new thread via thread/start `config.default_permissions`, and must
    never send the legacy `sandbox` / `sandboxPolicy` fields (their enums
    cannot express a profile; sending them forces the legacy sandbox and
    disables the profile). Legacy modes must not receive default_permissions.
    """

    class _RecordingClient:
        def __init__(self):
            self.requests = []  # [(method, params)]
            self.log = Logger(echo=False)

        def on(self, method, handler):
            pass

        def request(self, method, params, timeout=60):
            self.requests.append((method, dict(params)))
            if method == "thread/start":
                return {"thread": {"id": "t-1"}}
            if method == "thread/read":
                return {"thread": {"id": "t-1"}}
            if method == "turn/start":
                return {"turn": {"id": "tu-1", "status": "inProgress"}}
            return {}

    def _core_for_mode(self, env):
        overrides = build_config_overrides(env)
        client = self._RecordingClient()
        core = BridgeCore(client, thread_config=thread_start_permission_config(overrides))
        return client, core

    def test_thread_start_permission_config_derived_from_spawn_flags(self):
        for env in (
            {},
            {"BRIDGE_NETWORK_ACCESS": "true"},
            {"BRIDGE_SANDBOX_MODE": "danger-full-access"},
        ):
            self.assertIsNone(thread_start_permission_config(build_config_overrides(env)), env)
        self.assertEqual(
            thread_start_permission_config(
                build_config_overrides({"BRIDGE_SANDBOX_MODE": "bridge-workspace"})
            ),
            {"default_permissions": "bridge-workspace"},
        )

    def test_bridge_workspace_thread_start_selects_profile_explicitly(self):
        client, core = self._core_for_mode({"BRIDGE_SANDBOX_MODE": "bridge-workspace"})
        core.start("hello", cwd="/ws")
        method, params = client.requests[0]
        self.assertEqual(method, "thread/start")
        self.assertEqual(params["config"], {"default_permissions": "bridge-workspace"})
        self.assertNotIn("sandbox", params)
        self.assertEqual(params["cwd"], "/ws")
        self.assertEqual(params["model"], "deepseek-chat")
        self.assertEqual(params["modelProvider"], "deepseek")
        # turn/start: no sandboxPolicy, no config passthrough (schema has no
        # named-profile variant there).
        method, turn_params = client.requests[1]
        self.assertEqual(method, "turn/start")
        self.assertEqual(set(turn_params), {"threadId", "input"})

    def test_legacy_modes_never_send_profile_config(self):
        for env in ({}, {"BRIDGE_NETWORK_ACCESS": "true"},
                    {"BRIDGE_SANDBOX_MODE": "danger-full-access"}):
            client, core = self._core_for_mode(env)
            core.start("hello", cwd="/ws")
            method, params = client.requests[0]
            self.assertEqual(method, "thread/start", env)
            self.assertNotIn("config", params, env)
            self.assertNotIn("sandbox", params, env)
            method, turn_params = client.requests[1]
            self.assertEqual(method, "turn/start", env)
            self.assertEqual(set(turn_params), {"threadId", "input"}, env)

    def test_all_modes_never_send_legacy_sandbox_fields_on_thread_or_turn(self):
        for env in ({}, {"BRIDGE_SANDBOX_MODE": "bridge-workspace"},
                    {"BRIDGE_SANDBOX_MODE": "danger-full-access"}):
            client, core = self._core_for_mode(env)
            core.start("hello", cwd="/ws")
            _, thread_params = client.requests[0]
            _, turn_params = client.requests[1]
            self.assertNotIn("sandbox", thread_params, env)
            self.assertNotIn("sandbox", turn_params, env)
            self.assertNotIn("sandboxPolicy", turn_params, env)

    def test_continue_turn_start_never_carries_profile_config(self):
        client, core = self._core_for_mode({"BRIDGE_SANDBOX_MODE": "bridge-workspace"})
        core.continue_thread("t-1", "next")
        self.assertEqual([m for m, _ in client.requests], ["thread/read", "turn/start"])
        _, turn_params = client.requests[1]
        self.assertEqual(set(turn_params), {"threadId", "input"})


@unittest.skipUnless(
    os.environ.get("RUN_SANDBOX_TESTS") == "1",
    "set RUN_SANDBOX_TESTS=1 in a normal Terminal (needs Seatbelt)",
)
class SandboxGitAutomationIntegrationTest(unittest.TestCase):

    def test_profile_commits_and_workspace_write_denies(self):
        script = os.path.join(ROOT, "scripts", "verify_bridge_git_automation.sh")
        proc = subprocess.run(
            [script], capture_output=True, text=True, timeout=600
        )
        output = proc.stdout + proc.stderr
        self.assertEqual(proc.returncode, 0, output)
        self.assertIn("RESULT profile_git_commit=OK", output)
        self.assertIn("RESULT workspace_write_git_denied=OK", output)


if __name__ == "__main__":
    unittest.main()
