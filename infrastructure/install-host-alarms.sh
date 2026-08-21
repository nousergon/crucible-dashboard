#!/bin/bash
# install-host-alarms.sh — RETIRED as an alarm-creation path.
#
# alpha-engine-config-I8035, completing the config-I7339 ownership ruling: an
# alarm is an account resource (infrastructure-ownership-policy.md §2), and
# this repo is PUBLIC while the alarm definitions and their applier live in the
# PRIVATE nous-ergon-ops repo. This script's six `put-metric-alarm` calls are
# gone — tests/test_no_imperative_alarm_authorship.py fails the build if they
# come back.
#
# WHY IT HAD TO STOP, not merely why it moved. This script ran from deploy.yml
# on every merge to main, and every alarm it created is ALSO codified under
# nous-ergon-ops/infrastructure/cloudwatch/alarms/, applied there by
# cloudwatch-alarm-apply-on-merge.yml. Two appliers, one resource: the live
# alarm was whichever ran last. Verified live 2026-08-21, all eight
# alpha-engine-dashboard-* alarms matched their JSON exactly — which is the
# dangerous state, not the safe one. An intentional edit in nous-ergon-ops
# survives only until the next dashboard deploy, and nothing anywhere goes red
# when it is reverted.
#
# Every alarm this script used to create is codified as a JSON file:
#
#   nous-ergon-ops/infrastructure/cloudwatch/alarms/
#     alpha-engine-dashboard-mem-available-warn.json    (mem_available_percent < 15%, 3min)
#     alpha-engine-dashboard-mem-available-crit.json    (mem_available_percent < 8%, 2min)
#     alpha-engine-dashboard-oom-kill.json              (OOMKills delta >= 1)
#     alpha-engine-dashboard-swap-used-warn.json        (swap_used_percent > 75%, 5min)
#     alpha-engine-dashboard-disk-warn.json             (disk_used_percent >= 80%)
#     alpha-engine-dashboard-disk-crit.json             (disk_used_percent >= 90%)
#
# plus the two that were inlined in deploy.yml rather than here:
#
#     alpha-engine-dashboard-box-disk-critical.json     (breaching on missing — dead-box detector)
#     alpha-engine-dashboard-health-problems.json       (the watchdog backstop)
#
# To change one, edit that file in nous-ergon-ops and open a PR there — never
# edit this script and run it. To apply immediately rather than waiting for the
# next merge touching that file, an operator with nous-ergon-ops checked out
# runs:
#
#   infrastructure/cloudwatch/apply.py --prefix alpha-engine-dashboard-
#
# THE THRESHOLD RATIONALES ARE NOT LOST — they moved into the JSON files'
# AlarmDescription fields and the README next to them. Three worth restating
# because they read as mistakes otherwise:
#
#   * Swap warns at 75%, not 50%. Linux does not proactively page swapped-out
#     memory back in when pressure subsides; after the 2026-07-27 incident the
#     box sat at ~50% swap with 1.8 GB available and was entirely healthy.
#     mem_available is the primary signal, swap occupancy the weak one.
#   * The disk alarms carry THREE dimensions (InstanceId, path, fstype). The
#     agent's `drop_device: true` removes only `device`. A CloudWatch alarm must
#     match a metric's dimension set exactly, so an alarm on InstanceId alone
#     matches nothing and sits in INSUFFICIENT_DATA forever — which reads as
#     "no data yet" rather than "misconfigured".
#   * OOMKills is a DELTA (see emit_oom_metric.sh), so any nonzero datapoint is
#     a NEW kill and the alarm self-clears. A cumulative counter would latch on
#     forever after the first kill.
#
# This file is a pointer, not a stub with hidden behavior: it does nothing and
# exits 0 so a stale muscle-memory invocation is a no-op, not a failure.

set -euo pipefail
echo "install-host-alarms.sh no longer creates alarms (alpha-engine-config-I8035)."
echo "Edit nous-ergon-ops/infrastructure/cloudwatch/alarms/alpha-engine-dashboard-*.json instead."
echo "To apply immediately: nous-ergon-ops/infrastructure/cloudwatch/apply.py --prefix alpha-engine-dashboard-"
exit 0
