#!/usr/bin/env bash
# Which interpreter this box's ALERTS are published through.
#
# Source it, then use "$ALERT_PY":
#
#     . "$(dirname "${BASH_SOURCE[0]}")/alert_py.sh"
#     "$ALERT_PY" -m krepis.alerts publish --message ... --severity ... \
#         --source box-health
#
# `--source` is not optional in the example on purpose: alpha-engine-config's
# `Check alert-class registry drift` scans this repo for alert source literals
# and cross-checks them against `playbooks.yaml`, and a `publish` with no
# `--source` reads to it as an uncovered alert class. It reported this file as
# the sole fleet-wide drift on 2026-08-13 — from THIS comment, since the file
# has no live call site. An example that omits the flag also teaches the next
# call site to omit it, which is the real cost.
#
# WHY THIS EXISTS (alpha-engine-config-I7168)
# ------------------------------------------
# Six alert call sites on this box — box_health.sh, alert_on_failure.sh,
# reboot_if_needed.sh, morning-signal-watchdog.sh, boot-pull.sh (x2) and
# deploy-on-merge.sh (x2) — each resolved krepis through
# /home/ec2-user/alpha-engine-dashboard/.venv, i.e. through
# crucible-dashboard/requirements.txt, a file whose owners have no reason to
# think about alert delivery.
#
# That is exactly the failure krepis-venv/pin.txt was created to end for the
# spot launchers (I6931), still live one consumer along, and it cost a real
# alert: krepis.telegram._escape_markdown SUBSTITUTED markdown characters
# instead of escaping them, so a box-health WARNING named
# `/home/ec2-user/flow-doctor/flow-doctor.db` — a path that does not exist,
# while two OTHER files on this box genuinely are named `flow-doctor.db`.
# Fixed in krepis 0.59.0. MEASURED 2026-08-13, after the pinned venv had
# already been converged to 0.59.0:
#
#   /opt/nousergon/krepis-venv           0.59.0   '/a\_b'   <- the fix
#   alpha-engine-dashboard/.venv         0.54.0   '/a-b'    <- what alerts used
#
# krepis is fleet infrastructure and must not be resolved by accident. The
# declared venv has a committed pin and a daily drift check; the dashboard's
# venv has neither, for this purpose.
#
# WHY THE FALLBACK IS NOT A SILENT SWALLOW
# ----------------------------------------
# A missing declared venv would otherwise silence every alert on this box —
# including check-krepis-venv-drift.sh's own FINDING that the venv is missing,
# which is delivered through this same path. That circularity is the one case
# where degrading beats failing: an alert on an older krepis is worth
# incomparably more than no alert.
#
# The degrade is not silent. (a) The failure mode swallowed is "the declared
# krepis venv is absent or broken"; (b) alert delivery, the primary
# deliverable, survives on the fallback interpreter; (c) it is recorded on
# stderr of the calling unit — captured by journald for every one of these
# scripts — and the condition itself is independently reported as a FINDING by
# check-krepis-venv-drift.sh on ops-config-drift.timer, which does not depend
# on this resolution.

# Already set by the caller (or a test) wins, so this is overridable without
# editing six scripts.
if [ -z "${ALERT_PY:-}" ]; then
    _declared_alert_py="${KREPIS_VENV:-/opt/nousergon/krepis-venv}/bin/python"
    _fallback_alert_py="/home/ec2-user/alpha-engine-dashboard/.venv/bin/python"
    # `-x` and an import probe: a venv directory that exists with a broken
    # install fails at publish time, which is the moment there is a problem to
    # report and the worst moment to discover the reporter is broken.
    if [ -x "$_declared_alert_py" ] && "$_declared_alert_py" -c 'import krepis.alerts' 2>/dev/null; then
        ALERT_PY="$_declared_alert_py"
    else
        ALERT_PY="$_fallback_alert_py"
        echo "alert_py: declared krepis venv unusable at $_declared_alert_py — falling back to $_fallback_alert_py; alerts may be on an unpinned krepis (config-I7168)" >&2
    fi
    unset _declared_alert_py _fallback_alert_py
fi
export ALERT_PY
