#!/usr/bin/env bash
#
# HOST-ADMIN global single-writer lock for the Bridge control plane.
#
# The control plane (activate/deactivate maintenance, runtime autorecovery
# bootstrap orchestration) is mutated by humans, ChatGPT and unattended
# automation. Concurrent writers race the same instance state and endpoint,
# so host-ops writes are serialized through ONE fixed lock directory under
# the instance state root:
#
#   <state_root>/host-ops.lock/
#     owner.pid   - pid of the acquiring process (numeric; never a secret)
#     operation   - operation name (non-secret, e.g. "activate-maintenance")
#     token       - per-acquisition random token (non-secret; the SAME token
#                   is exported to sub-scripts so nested control-plane calls
#                   are reentrant under one parent operation)
#     epoch       - unix epoch of acquisition (non-secret)
#
# Semantics:
#   - acquire is ATOMIC via `mkdir` (no lock file races);
#   - a second concurrent operation gets a BUSY answer (exit 1) and MUST NOT
#     mutate anything; the holder's token/pid/operation are printed (never
#     any secret);
#   - REENTRANT: any sub-script that sees the exported HOST_OPS_LOCK_TOKEN
#     matching the recorded token re-enters the same lock without side
#     effects (the parent owns the lock and releases it on EXIT);
#   - STALE: when the lock exists but the recorded owner pid is dead, ONE
#     guarded cleanup (fixed absolute path only) is allowed, then a single
#     fresh acquisition attempt;
#   - RELEASE: explicit host_ops_lock_release, and automatically on EXIT via
#     a trap installed at acquisition time (only the real owner releases).
#
# No pkill/killall; the only signal-related call is `kill -0` (liveness
# probe, never a signal). No secrets are ever recorded or printed. Sourcing
# has no side effects.

# Prints the fixed host-ops lock directory under the instance state root.
host_ops_lock_dir() {
  printf '%s/host-ops.lock' "$(bridge_instance_state_root)"
}

# Returns 0 when the recorded owner pid is alive (numeric pid > 1).
host_ops_lock_owner_alive() {
  local dir="$1" pid=""
  pid="$(cat "$dir/owner.pid" 2>/dev/null || true)"
  if [[ "$pid" =~ ^[0-9]+$ && "$pid" -gt 1 ]]; then
    if kill -0 "$pid" 2>/dev/null; then
      return 0
    fi
  fi
  return 1
}

# Generates a non-secret random acquisition token.
host_ops_lock_new_token() {
  printf 'hop-%s-%s-%s' "$$" "$(date +%s 2>/dev/null || true)" "${RANDOM:-0}${RANDOM:-0}"
}

# Acquires the host-ops lock for OPERATION. Prints BUSY and returns 1 when
# another live operation holds it. Reentrant for the exported token.
host_ops_lock_acquire() {
  local operation="${1:-host-op}" dir="" stored="" stale_pid=""
  dir="$(host_ops_lock_dir)"
  # Reentrant: the same parent operation already owns this lock (token
  # exported by the acquiring parent and inherited by sub-scripts).
  if [[ -n "${HOST_OPS_LOCK_TOKEN:-}" && -d "$dir" && -f "$dir/token" ]]; then
    stored="$(cat "$dir/token" 2>/dev/null || true)"
    if [[ -n "$stored" && "$stored" == "$HOST_OPS_LOCK_TOKEN" ]]; then
      printf '[host-ops-lock] reentered (operation=%s pid=%s; parent token already holds the lock)\n' \
        "$operation" "$$" >&2
      return 0
    fi
  fi
  if [[ -d "$dir" ]]; then
    if host_ops_lock_owner_alive "$dir"; then
      printf '[host-ops-lock] BUSY: host operation "%s" (pid %s) holds the lock; concurrent control-plane writes are refused\n' \
        "$(cat "$dir/operation" 2>/dev/null || true)" \
        "$(cat "$dir/owner.pid" 2>/dev/null || true)" >&2
      return 1
    fi
    # One-time stale cleanup: the recorded owner pid is dead. Only the fixed
    # absolute lock path may be removed.
    stale_pid="$(cat "$dir/owner.pid" 2>/dev/null || true)"
    if [[ "$dir" == "$(bridge_instance_state_root)/host-ops.lock" ]]; then
      rm -f "$dir/owner.pid" "$dir/operation" "$dir/token" "$dir/epoch" 2>/dev/null || true
      rmdir "$dir" 2>/dev/null || true
      printf '[host-ops-lock] stale lock from dead owner pid %s cleaned up (one-time)\n' \
        "${stale_pid:-<unknown>}" >&2
    fi
  fi
  if ! mkdir "$dir" 2>/dev/null; then
    printf '[host-ops-lock] BUSY: another host operation holds the lock (mkdir race)\n' >&2
    return 1
  fi
  HOST_OPS_LOCK_TOKEN="$(host_ops_lock_new_token)"
  HOST_OPS_LOCK_HELD=1
  printf '%s' "$$" > "$dir/owner.pid"
  printf '%s' "$operation" > "$dir/operation"
  printf '%s' "$HOST_OPS_LOCK_TOKEN" > "$dir/token"
  printf '%s' "$(date +%s 2>/dev/null || true)" > "$dir/epoch"
  chmod 700 "$dir" 2>/dev/null || true
  chmod 600 "$dir/owner.pid" "$dir/operation" "$dir/token" "$dir/epoch" 2>/dev/null || true
  export HOST_OPS_LOCK_TOKEN
  trap 'host_ops_lock_release' EXIT
  printf '[host-ops-lock] acquired (operation=%s pid=%s epoch=%s)\n' \
    "$operation" "$$" "$(cat "$dir/epoch" 2>/dev/null || true)" >&2
  return 0
}

# Releases the host-ops lock when this process is the real owner (token
# match + fixed path). Safe to call any number of times; never touches a
# lock re-acquired by a newer owner.
host_ops_lock_release() {
  local dir="" stored=""
  [[ "${HOST_OPS_LOCK_HELD:-0}" == 1 ]] || return 0
  dir="$(host_ops_lock_dir)"
  stored="$(cat "$dir/token" 2>/dev/null || true)"
  if [[ -n "${HOST_OPS_LOCK_TOKEN:-}" && -n "$stored" \
        && "$stored" == "$HOST_OPS_LOCK_TOKEN" \
        && "$dir" == "$(bridge_instance_state_root)/host-ops.lock" ]]; then
    rm -f "$dir/owner.pid" "$dir/operation" "$dir/token" "$dir/epoch" 2>/dev/null || true
    rmdir "$dir" 2>/dev/null || true
    printf '[host-ops-lock] released\n' >&2
  fi
  HOST_OPS_LOCK_HELD=0
  HOST_OPS_LOCK_TOKEN=""
  unset HOST_OPS_LOCK_TOKEN 2>/dev/null || true
  return 0
}
