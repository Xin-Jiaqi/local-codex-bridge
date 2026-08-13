#!/usr/bin/env bash
#
# Host-admin control for the pinned Bridge instances (local | hpc | maintenance).
#
# This is the ONLY sanctioned writer of instance state (config + backups +
# runtime dirs). Ordinary scripts, tests and migrations are read-only. The
# instance config is NON-SECRET: exactly the schema keys in
# scripts/bridge_instance_lib.sh, never API keys or other secret values.
# Secrets stay in their existing external files/env (e.g. .bridge_api_key);
# this script never reads or copies them.
#
# State lives OUTSIDE task workspaces and outside this repo:
#   ${XDG_STATE_HOME:-$HOME/.local/state}/local-codex-bridge/<instance>/
# Config files are chmod 600; all dirs are chmod 700; updates and migration
# write a same-directory backup first.
#
# The running instance is pinned at process startup via BRIDGE_INSTANCE
# (default local); changing instance state here does NOT change a running
# Bridge — stop it, apply admin changes, then start again.
#
# Usage:
#   ./scripts/bridge_instance.sh list
#   ./scripts/bridge_instance.sh show NAME
#   ./scripts/bridge_instance.sh create NAME [--template local|hpc|maintenance]
#   ./scripts/bridge_instance.sh update NAME key=value [key=value ...]
#   ./scripts/bridge_instance.sh migrate-current --dry-run
#   ./scripts/bridge_instance.sh migrate-current --apply
#
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
. "$ROOT/scripts/bridge_instance_lib.sh"

log() { printf '[instance] %s\n' "$*"; }
die() { printf '[instance] error: %s\n' "$*" >&2; exit 1; }

# Writes the canonical 7-field config atomically (tmp + mv), chmod 600.
write_instance_config() {
  local name="$1" mode="$2" approval="$3" network="$4" codex_home="$5" port="$6" runtime="$7"
  local api_key_file="${8:-}" domain_file="${9:-}"
  local dir f tmp
  dir="$(bridge_instance_dir "$name")"
  f="$(bridge_instance_config "$name")"
  mkdir -p "$dir" "$dir/backups" "$runtime"
  chmod 700 "$dir" "$dir/backups" "$runtime"
  tmp="$dir/.instance.conf.tmp.$$"
  {
    printf '# Local Codex Bridge instance config - NON-SECRET fields only.\n'
    printf 'name = "%s"\n' "$name"
    printf 'mode = "%s"\n' "$mode"
    printf 'approval_policy = "%s"\n' "$approval"
    printf 'network_access = "%s"\n' "$network"
    printf 'codex_home = "%s"\n' "$codex_home"
    printf 'port = "%s"\n' "$port"
    printf 'runtime_dir = "%s"\n' "$runtime"
    printf 'api_key_file = "%s"\n' "$api_key_file"
    printf 'ngrok_domain_file = "%s"\n' "$domain_file"
  } > "$tmp"
  chmod 600 "$tmp"
  mv -f "$tmp" "$f"
  chmod 600 "$f"
}

backup_config() {
  local name="$1" f ts
  f="$(bridge_instance_config "$name")"
  [[ -f "$f" ]] || return 0
  ts="$(date -u +%Y%m%dT%H%M%SZ)"
  cp -p "$f" "$(bridge_instance_dir "$name")/backups/instance.conf.$ts"
  chmod 600 "$(bridge_instance_dir "$name")/backups/instance.conf.$ts"
  printf '%s' "$(bridge_instance_dir "$name")/backups/instance.conf.$ts"
}

validate_value() {
  local name="$1" key="$2" value="$3"
  case "$key" in
    name)
      [[ "$value" == "$name" ]] || die "cannot change instance name (update name must equal '$name')"
      ;;
    mode)
      [[ "$value" == "$(bridge_instance_mode "$name")" ]] \
        || die "instance '$name' mode must be '$(bridge_instance_mode "$name")' (danger-full-access is NEVER allowed)"
      ;;
    approval_policy)
      case "$value" in on-request|never) ;; *) die "approval_policy must be on-request|never" ;; esac
      ;;
    network_access)
      case "$value" in true|false) ;; *) die "network_access must be true|false" ;; esac
      ;;
    codex_home|runtime_dir)
      [[ "$value" == /* ]] || die "$key must be an absolute path"
      ;;
    api_key_file|ngrok_domain_file)
      [[ -z "$value" || "$value" == /* ]] || die "$key must be an absolute path (or empty)"
      ;;
    port)
      [[ "$value" =~ ^[0-9]+$ ]] && (( value >= 1 && value <= 65535 )) || die "port must be 1..65535"
      ;;
    *) die "unknown config key '$key' (allowed: $(bridge_instance_keys))" ;;
  esac
}

instance_fields() {
  local name="$1"
  MODE="$(bridge_instance_get "$name" mode)"
  APPROVAL="$(bridge_instance_get "$name" approval_policy)"
  NETWORK="$(bridge_instance_get "$name" network_access)"
  CODEX_HOME="$(bridge_instance_get "$name" codex_home)"
  PORT="$(bridge_instance_get "$name" port)"
  RUNTIME="$(bridge_instance_get "$name" runtime_dir)"
  API_KEY_FILE="$(bridge_instance_get "$name" api_key_file)"
  DOMAIN_FILE="$(bridge_instance_get "$name" ngrok_domain_file)"
}

cmd_list() {
  printf '%-8s %-9s %-16s %-6s %s\n' INSTANCE CONFIG MODE PORT CONFIG_FILE
  for name in local hpc maintenance; do
    if bridge_instance_exists "$name"; then
      instance_fields "$name"
      printf '%-8s %-9s %-16s %-6s %s\n' "$name" yes "$MODE" "$PORT" "$(bridge_instance_config "$name")"
    else
      printf '%-8s %-9s %-16s %-6s %s\n' "$name" no - - "$(bridge_instance_config "$name")"
    fi
  done
}

cmd_show() {
  local name="$1"
  bridge_instance_valid "$name" || die "invalid instance '$name' (use local|hpc|maintenance)"
  local f
  f="$(bridge_instance_config "$name")"
  [[ -f "$f" ]] || die "no config for instance '$name' ($f) - run: ./scripts/bridge_instance.sh create $name"
  instance_fields "$name"
  log "instance: $name"
  log "config:   $f (chmod 600; dirs chmod 700)"
  log "mode:             $MODE"
  log "approval_policy:  $APPROVAL"
  log "network_access:   $NETWORK"
  log "codex_home:       $CODEX_HOME"
  log "port:             $PORT"
  log "runtime_dir:      $RUNTIME"
  log "api_key_file:     ${API_KEY_FILE:-<repo .bridge_api_key fallback>}"
  log "ngrok_domain_file: ${DOMAIN_FILE:-<none>}"
}

cmd_create() {
  local name="$1" template="$1"
  bridge_instance_valid "$name" || die "invalid instance '$name' (use local|hpc|maintenance)"
  if [[ "${2:-}" == "--template" ]]; then
    template="$3"
    bridge_instance_valid "$template" || die "invalid template '$template' (use local|hpc|maintenance)"
    [[ "$template" == "$name" ]] || die "template '$template' does not match instance '$name' (instance mode is pinned per name)"
  fi
  bridge_instance_exists "$name" && die "instance '$name' already exists ($(bridge_instance_config "$name")); use update or migrate-current --apply"
  log "creating instance '$name' (template: $template)"
  write_instance_config "$name" \
    "$(bridge_instance_mode "$name")" \
    "$(bridge_instance_default_approval "$name")" \
    "$(bridge_instance_default_network "$name")" \
    "$(bridge_instance_default_codex_home "$name")" \
    "$(bridge_instance_default_port "$name")" \
    "$(bridge_instance_default_runtime "$name")" "" ""
  log "wrote $(bridge_instance_config "$name") (chmod 600; dirs chmod 700)"
  log "takes effect on the next Bridge start: BRIDGE_INSTANCE=$name ./scripts/start_ngrok_bridge.sh"
}

cmd_update() {
  local name="$1" arg key value
  shift
  bridge_instance_valid "$name" || die "invalid instance '$name' (use local|hpc|maintenance)"
  bridge_instance_exists "$name" || die "no config for instance '$name' - run: ./scripts/bridge_instance.sh create $name"
  [[ $# -gt 0 ]] || die "update requires key=value pairs"
  instance_fields "$name"
  for arg in "$@"; do
    key="${arg%%=*}"
    value="${arg#*=}"
    [[ "$arg" == *=* ]] || die "invalid update '$arg' (expected key=value)"
    validate_value "$name" "$key" "$value"
    case "$key" in
      mode) MODE="$value" ;;
      approval_policy) APPROVAL="$value" ;;
      network_access) NETWORK="$value" ;;
      codex_home) CODEX_HOME="$value" ;;
      port) PORT="$value" ;;
      runtime_dir) RUNTIME="$value" ;;
      api_key_file) API_KEY_FILE="$value" ;;
      ngrok_domain_file) DOMAIN_FILE="$value" ;;
      name) ;; # validated to equal $name
    esac
  done
  backup="$(backup_config "$name")"
  write_instance_config "$name" "$MODE" "$APPROVAL" "$NETWORK" "$CODEX_HOME" "$PORT" \
    "$RUNTIME" "$API_KEY_FILE" "$DOMAIN_FILE"
  log "updated $(bridge_instance_config "$name")"
  log "backup: $backup"
}

cmd_verify() {
  local name="$1" f errors=0 coll="" val=""
  bridge_instance_valid "$name" || die "invalid instance '$name' (use local|hpc|maintenance)"
  f="$(bridge_instance_config "$name")"
  if [[ ! -f "$f" ]]; then
    log "verify: FAIL - no config for instance '$name' ($f)"
    return 1
  fi
  instance_fields "$name"
  [[ "$MODE" == "$(bridge_instance_mode "$name")" ]] || {
    log "verify: FAIL - mode '$MODE' is invalid for instance '$name' (must be '$(bridge_instance_mode "$name")'; danger-full-access is never allowed)"
    errors=1
  }
  case "$APPROVAL" in
    on-request|never) ;;
    *) log "verify: FAIL - invalid approval_policy '$APPROVAL' (use on-request|never)"; errors=1 ;;
  esac
  case "$NETWORK" in
    true|false) ;;
    *) log "verify: FAIL - invalid network_access '$NETWORK' (use true|false)"; errors=1 ;;
  esac
  [[ "$CODEX_HOME" == /* ]] || { log "verify: FAIL - codex_home must be an absolute path"; errors=1; }
  [[ "$PORT" =~ ^[0-9]+$ && "$PORT" -ge 1 && "$PORT" -le 65535 ]] || {
    log "verify: FAIL - invalid port '$PORT' (use 1..65535)"; errors=1
  }
  [[ "$RUNTIME" == /* ]] || { log "verify: FAIL - runtime_dir must be an absolute path"; errors=1; }
  for pair in "API_KEY_FILE:api_key_file" "DOMAIN_FILE:ngrok_domain_file"; do
    var="${pair%%:*}"
    key="${pair#*:}"
    val="$(printf '%s' "${!var}")"
    if [[ -n "$val" ]]; then
      [[ "$val" == /* ]] || { log "verify: FAIL - $key must be an absolute path (or empty)"; errors=1; }
      [[ -f "$val" ]] || { log "verify: FAIL - $key references missing file: $val"; errors=1; }
    fi
  done
  if coll="$(bridge_instance_collision "$name")"; then
    log "verify: FAIL - ${coll%%:*} collision with instance '${coll#*:}' (concurrent instances need distinct ports and runtime dirs)"
    errors=1
  fi
  if [[ "$errors" -eq 0 ]]; then
    log "verify: OK - instance '$name' config is valid ($f)"
    return 0
  fi
  return 1
}

cmd_migrate_current() {
  local action="${1:-}" singleton_mode="" mode="" note=""
  case "$action" in
    --dry-run|--apply) ;;
    *) die "migrate-current requires --dry-run or --apply" ;;
  esac
  # Migrate the legacy singleton into the default 'local' instance.
  local name="local"
  if [[ -f "$ROOT/.bridge_sandbox_mode" ]]; then
    singleton_mode="$(tr -d '[:space:]' < "$ROOT/.bridge_sandbox_mode" || true)"
  fi
  mode="$(bridge_instance_mode "$name")"
  if [[ -n "$singleton_mode" && "$singleton_mode" != "$mode" ]]; then
    note="(singleton mode '$singleton_mode' is not valid for instance '$name'; using '$mode' - danger-full-access is never an instance mode)"
  fi
  local approval network codex_home port runtime api_key_file="" domain_file=""
  approval="$(bridge_instance_default_approval "$name")"
  network="$(bridge_instance_default_network "$name")"
  codex_home="${CODEX_HOME:-$(bridge_instance_default_codex_home "$name")}"
  port="$(bridge_instance_default_port "$name")"
  runtime="$(bridge_instance_default_runtime "$name")"
  if [[ -f "$ROOT/.bridge_api_key" ]]; then
    api_key_file="$ROOT/.bridge_api_key"
  fi
  if bridge_instance_may_use_legacy_domain "$name" && [[ -f "$ROOT/.ngrok_domain" ]]; then
    domain_file="$ROOT/.ngrok_domain"
  fi
  [[ "$codex_home" == /* ]] || die "codex_home must be absolute: $codex_home"
  log "migrate-current -> instance '$name' (from legacy singleton)"
  log "  mode=$mode approval_policy=$approval network_access=$network"
  log "  codex_home=$codex_home port=$port runtime_dir=$runtime"
  log "  api_key_file=${api_key_file:-<repo .bridge_api_key fallback>} ngrok_domain_file=${domain_file:-<none>}"
  [[ -z "$note" ]] || log "  note: $note"
  if [[ "$action" == "--dry-run" ]]; then
    log "dry-run: no files written; re-run with --apply to create $(bridge_instance_config "$name")"
    return 0
  fi
  backup="$(backup_config "$name")"
  write_instance_config "$name" "$mode" "$approval" "$network" "$codex_home" "$port" \
    "$runtime" "$api_key_file" "$domain_file"
  log "wrote $(bridge_instance_config "$name") (chmod 600; dirs chmod 700)"
  [[ -z "$backup" ]] || log "backup: $backup"
  log "next Bridge start will use instance '$name'; legacy singleton is left untouched"
}

case "${1:-}" in
  list) cmd_list ;;
  show) [[ $# -ge 2 ]] || die "usage: bridge_instance.sh show NAME"; cmd_show "$2" ;;
  create) shift; cmd_create "$@" ;;
  update) shift; cmd_update "$@" ;;
  migrate-current) cmd_migrate_current "${2:-}" ;;
  verify) [[ $# -ge 2 ]] || die "usage: bridge_instance.sh verify NAME"; cmd_verify "$2" ;;
  *) cat >&2 <<EOF
usage: scripts/bridge_instance.sh <command>

commands:
  list                          list instances (local, hpc, maintenance) and config state
  show NAME                     show one instance config (non-secret)
  create NAME [--template ...]  create an instance config (refuses if exists)
  update NAME key=value [...]   update fields (backup first)
  migrate-current --dry-run     show what migration from the legacy singleton
  migrate-current --apply       create the 'local' instance from legacy state
  verify NAME                   validate an instance config (mode, fields,
                                referenced files, port/runtime collisions)
EOF
    exit 2 ;;
esac
