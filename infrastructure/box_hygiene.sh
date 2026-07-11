#!/bin/bash
# box_hygiene.sh — weekly disk-hygiene pass for the dashboard EC2.
#
# The 2026-07-11 outage (config#2227) was the root disk hitting 100% with no
# single hog: package-manager caches (~2.4G npm+bun+pip) regrow on every
# deploy, and uncapped journald had accumulated 535M. journald is now capped
# by journald-size-cap.conf (installed by install-box-health.sh); this script
# reclaims the cache classes on a weekly timer so steady-state stays flat.
#
# Deliberately NOT touched: app checkouts, venvs, node_modules — those are
# live deploy surfaces; reclaiming them is a deploy/eviction concern
# (config#2231), not hygiene.
#
# Runs as root (dnf + journal need it); user-level caches via runuser.
# Installed to /usr/local/bin by install-box-health.sh; scheduled by
# box-hygiene.timer (weekly). Quiet-ish: per-step one-liners to the journal.
set -uo pipefail

log() { echo "box_hygiene: $*"; }

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

# Belt on top of the journald size cap (suspenders): vacuum anything beyond it.
journalctl --vacuum-size=100M >/dev/null 2>&1 && log "journal vacuumed to 100M" \
    || log "journal vacuum failed"

after_kb=$(df --output=avail / | tail -1 | tr -dc '0-9')
log "reclaimed $(( (after_kb - before_kb) / 1024 ))MB; available now $((after_kb / 1024))MB"
