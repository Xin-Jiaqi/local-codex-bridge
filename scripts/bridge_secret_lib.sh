#!/usr/bin/env bash
#
# Read-only shared helper: secure persistent reference for the DeepSeek
# provider key (DEEPSEEK_API_KEY).
#
# The ONLY persistent store is the macOS login Keychain:
#   service = local-codex-bridge-deepseek
#   account = DEEPSEEK_API_KEY
#
# The value is never written to plists, the repo, the runtime release, logs,
# pid files or checkpoints. Every function captures the value into a shell
# variable; nothing here echoes secret content. If the Keychain read fails,
# the caller must fail closed (do not start the app-server).
#
# Test overrides (never used in production):
#   BRIDGE_KEYCHAIN_SERVICE / BRIDGE_KEYCHAIN_ACCOUNT  (isolated test names)
#   BRIDGE_SECURITY_BIN                                 (fake `security` binary)
#
# Sourcing has no side effects; no secrets involved in the source itself.
#

# Prints the Keychain service name.
bridge_secret_service() {
  printf '%s' "${BRIDGE_KEYCHAIN_SERVICE:-local-codex-bridge-deepseek}"
}

# Prints the Keychain account name.
bridge_secret_account() {
  printf '%s' "${BRIDGE_KEYCHAIN_ACCOUNT:-DEEPSEEK_API_KEY}"
}

# Runs the `security` binary (override for tests).
bridge_security() {
  "${BRIDGE_SECURITY_BIN:-security}" "$@"
}

# Returns 0 when the Keychain entry exists (no value is printed).
bridge_secret_keychain_present() {
  bridge_security find-generic-password \
    -s "$(bridge_secret_service)" -a "$(bridge_secret_account)" >/dev/null 2>&1
}

# Prints the Keychain value to stdout. CAPTURE-ONLY: callers must assign it
# to a variable; never pass this through a log/echo path.
bridge_secret_keychain_get() {
  bridge_security find-generic-password -w \
    -s "$(bridge_secret_service)" -a "$(bridge_secret_account)" 2>/dev/null || return 1
}

# One-time migration: when DEEPSEEK_API_KEY is present in the environment and
# the Keychain entry is missing, write it to the Keychain. Never prints the
# value. Returns 0 when the Keychain entry exists afterwards.
bridge_secret_keychain_import() {
  if bridge_secret_keychain_present; then
    return 0
  fi
  local value="${DEEPSEEK_API_KEY:-${1:-}}"
  [[ -n "$value" ]] || return 1
  bridge_security add-generic-password \
    -s "$(bridge_secret_service)" -a "$(bridge_secret_account)" \
    -w "$value" -U >/dev/null 2>&1 || return 1
  bridge_secret_keychain_present
}

# Loads DEEPSEEK_API_KEY into the current shell environment: uses the
# existing env value when set (the supervisor already loaded it from the
# Keychain), otherwise reads the Keychain. Returns 1 when neither is
# available (callers must fail closed and report a readiness failure).
bridge_secret_load_deepseek() {
  if [[ -n "${DEEPSEEK_API_KEY:-}" ]]; then
    export DEEPSEEK_API_KEY
    return 0
  fi
  local value=""
  value="$(bridge_secret_keychain_get)" || return 1
  [[ -n "$value" ]] || return 1
  export DEEPSEEK_API_KEY="$value"
}

# Returns 0 when the provider secret reference is readable (value non-empty).
# No value is ever printed.
bridge_secret_check() {
  local value=""
  value="$(bridge_secret_keychain_get)" || return 1
  [[ -n "$value" ]]
}
