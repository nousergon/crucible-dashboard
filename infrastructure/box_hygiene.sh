#!/bin/bash
# box_hygiene.sh — weekly disk-hygiene pass for the dashboard EC2.
#
# The 2026-07-11 outage (config#2227) was the root disk hitting 100% with no
# single hog: package-manager caches (~2.4G npm+bun+pip) regrow on every
# deploy, and uncapped journald had accumulated 535M. journald is now capped
# by journald-size-cap.conf / zz-retention.conf (see below); this script
# reclaims the cache classes on a weekly timer so steady-state stays flat.
#
# Deliberately NOT touched: app checkouts, venvs, node_modules — those are
# live deploy surfaces; reclaiming them is a deploy/eviction concern
# (config#2231), not hygiene.
#
# JOURNAL VACUUM (alpha-engine-config-I10612): the vacuum below used to be a
# hardcoded `--vacuum-size=100M`, written when journald was uncapped. Once
# nous-ergon-ops installed a 7-day/1500M retention floor
# (/etc/systemd/journald.conf.d/zz-retention.conf, alpha-engine-config-I10253),
# journald enforces that floor itself and the hardcoded 100M vacuum became the
# ONLY thing still cutting the journal down — it threw away 626MB of history
# on 2026-09-13 the retention floor was supposed to guarantee. The vacuum now
# derives its bounds from the box's own EFFECTIVE journald config (last
# SystemMaxUse=/MaxRetentionSec= wins, exactly as journald resolves it) so
# raising the floor in one place is never silently overridden here.
#
# Runs as root (dnf + journal need it); user-level caches via runuser.
# Installed to /usr/local/bin by install-box-health.sh; scheduled by
# box-hygiene.timer (weekly). Quiet-ish: per-step one-liners to the journal.
set -uo pipefail

log() { echo "box_hygiene: $*"; }

# journald_effective_value KEY < CAT_CONFIG_TEXT
#   Returns the value of the LAST declared KEY= line across the effective,
#   merged journald config (as `systemd-analyze cat-config systemd/journald.conf`
#   prints it: each drop-in's content concatenated in the same load order
#   journald itself applies, later files winning). This is the same
#   "last one wins" rule systemd-analyze already resolves for us — reading its
#   output rather than re-implementing directory precedence is the point.
#
#   A commented-out or blank assignment (`#SystemMaxUse=...`, `SystemMaxUse=`)
#   is not a declaration and is ignored, which is also what leaves the caller
#   able to tell "explicitly reset to default" apart from "never set".
#
#   Pure function of stdin; no unit conversion is performed — journalctl's
#   --vacuum-size/--vacuum-time accept the identical K/M/G/T and
#   s/min/h/day/week/month/year suffix grammar journald.conf(5) uses, so the
#   raw string is forwarded as-is rather than re-parsed.
journald_effective_value() {
    local key="$1" value=""
    local line lhs rhs
    while IFS= read -r line; do
        line="${line%%#*}"
        [[ "$line" == *=* ]] || continue
        lhs="${line%%=*}"
        lhs="${lhs#"${lhs%%[![:space:]]*}"}"
        lhs="${lhs%"${lhs##*[![:space:]]}"}"
        [ "$lhs" = "$key" ] || continue
        rhs="${line#*=}"
        rhs="${rhs#"${rhs%%[![:space:]]*}"}"
        rhs="${rhs%"${rhs##*[![:space:]]}"}"
        value="$rhs"
    done
    printf '%s' "$value"
}

before_kb=$(df --output=avail / | tail -1 | tr -dc '0-9')

# npm + bun + pip/uv caches (ec2-user). Each step is independent — one
# missing tool must not abort the rest (set -e intentionally absent; the
# recording surface is the per-step journal line).
runuser -u ec2-user -- bash -c 'npm cache clean --force >/dev/null 2>&1' \
    && log "npm cache cleaned" || log "npm cache clean skipped/failed"
runuser -u ec2-user -- bash -c 'rm -rf "$HOME/.bun/install/cache"/*' \
    && log "bun cache cleared" || log "bun cache clear skipped/failed"
runuser -u ec2-user -- bash -c 'python3 -m pip cache purge >/dev/null 2>&1; rm -rf "$HOME/.cache/pip" "$HOME/.cache/uv"' \
    && log "pip/uv caches cleared" || log "pip/uv cache clear skipped/failed"

dnf clean all >/dev/null 2>&1 && log "dnf cache cleaned" || log "dnf clean failed"

# Vacuum to the box's OWN effective journald config, never a hardcoded cap —
# see the top-of-file note (alpha-engine-config-I10612).
_effective_journald_conf=$(systemd-analyze cat-config systemd/journald.conf 2>/dev/null)
_max_use=$(printf '%s\n' "$_effective_journald_conf" | journald_effective_value SystemMaxUse)
_retention=$(printf '%s\n' "$_effective_journald_conf" | journald_effective_value MaxRetentionSec)

_vacuum_args=()
[ -n "$_retention" ] && _vacuum_args+=(--vacuum-time="$_retention")
[ -n "$_max_use" ] && _vacuum_args+=(--vacuum-size="$_max_use")

if [ ${#_vacuum_args[@]} -eq 0 ]; then
    log "no SystemMaxUse=/MaxRetentionSec= declared in effective journald config; journald owns the cap, not this vacuum"
else
    journalctl "${_vacuum_args[@]}" >/dev/null 2>&1 && log "journal vacuumed (${_vacuum_args[*]})" \
        || log "journal vacuum failed (${_vacuum_args[*]})"
fi

after_kb=$(df --output=avail / | tail -1 | tr -dc '0-9')
log "reclaimed $(( (after_kb - before_kb) / 1024 ))MB; available now $((after_kb / 1024))MB"
