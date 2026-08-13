"""
health_checker.py — Data staleness and pipeline health checks.

Checks freshness of all critical data stores and flags stale data.
Designed to be called by the dashboard health page or as a standalone script.

Usage:
    python health_checker.py                    # check all, print report
    python health_checker.py --json             # machine-readable output
    python health_checker.py --alert            # send SNS alert on failures
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone

# Structured logging + flow-doctor singleton via alpha-engine-lib (shared
# pattern across all 5 entrypoints; see executor/main.py for reference).
# When FLOW_DOCTOR_ENABLED=1, attaches a FlowDoctorHandler at ERROR so
# every logger.error() call routes through flow-doctor's dispatch
# (email + GitHub issue) without explicit fd.report() plumbing.
# Module-top so import-time errors are also captured. Replaces the
# previous logging.basicConfig() call inside main().
#
# exclude_patterns starts empty by deliberate convention.
from nousergon_lib.logging import setup_logging
_FLOW_DOCTOR_EXCLUDE_PATTERNS: list[str] = []
_FLOW_DOCTOR_YAML = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "flow-doctor.yaml"
)
setup_logging(
    "dashboard-health-checker",
    flow_doctor_yaml=_FLOW_DOCTOR_YAML,
    exclude_patterns=_FLOW_DOCTOR_EXCLUDE_PATTERNS,
)

import boto3
from nousergon_lib.health import HEALTH_CHECK_CANDIDATES

logger = logging.getLogger(__name__)

# Stage-coverage window (config-I7214): the instant this stage started.
# An artifact older than this is a leftover from a previous cycle, not this
# run's output — an existence-only probe cannot tell those apart.
_STAGE_WINDOW_START = os.environ.get(
    "_STAGE_WINDOW_START", datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
)

DEFAULT_BUCKET = "alpha-engine-research"

# Staleness thresholds (calendar days)
THRESHOLDS = {
    "signals": 8,           # Research runs weekly Saturday
    "predictions": 2,       # Predictor runs daily Mon-Fri
    "features": 2,          # Feature store runs daily Mon-Fri
    "fundamentals": 100,    # FMP quarterly, updated weekly in DataPhase1
    # price_cache_slim retired (Wave-4): ArcticDB universe lib is canonical;
    # its freshness is monitored upstream in alpha-engine-data's preflight.
    "daily_closes": 2,      # Daily Mon-Fri
    # population RETIRED 2026-08-13 (alpha-engine-config-I6053) — see the
    # `universe_membership` entry below, which replaces it.
    "universe_membership": 8,  # Written weekly (and on every exercise cycle)
                               # by the Scanner; successor to `population`
}

# Per-module staleness thresholds for the `health/` module markers, in
# calendar days. Derived from each producer's DECLARED CADENCE plus one
# period of slack, per ARCHITECTURE §128 ("set the threshold from the
# producer's cadence with one period of slack") — NOT from what happens to
# pass today.
#
# Why this map exists (alpha-engine-config-I6053): every module marker was
# previously judged against a flat 2-day threshold. That is correct for the
# daily producers and structurally impossible for the weekly ones — the
# backtester and the research signals producer run on the Saturday weekly
# pipeline, so a 2-day budget marks them stale on Tuesday through Friday of
# every single week, whatever their actual health. Measured live 2026-08-13:
# `health/backtester.json` last written 2026-08-08 (the previous Saturday
# run, which SUCCEEDED) read as 5d stale against the flat 2d.
#
# This is NOT a threshold widened until a stale entry passes. `population`
# (33 days stale, a genuinely dead producer) is REMOVED rather than
# re-baselined, and `health/research` is repaired at the producer
# (crucible-research signals_envelope_handler, config-I6053) — it stays
# stale here until that producer's write lands. 8 days still catches a
# weekly producer that misses one whole cycle.
#
# The weekly figures match nousergon_lib.health.DASHBOARD_HEALTH_MODULES,
# which already declares research and predictor_training at 8*24 hours —
# the flat 2 in this file contradicted the lib's own cadence table.
HEALTH_MODULE_THRESHOLDS = {
    "data": 2,          # DataPhase1/2 — daily Mon-Fri
    "executor": 2,      # Trading daemon — daily Mon-Fri
    "predictor": 2,     # predictor_inference — daily Mon-Fri
    "research": 8,      # signals envelope — weekly Saturday pipeline
    "backtester": 8,    # Backtester stage — weekly Saturday pipeline
}

#: Fallback for a module present in HEALTH_CHECK_CANDIDATES but absent from
#: HEALTH_MODULE_THRESHOLDS. Deliberately the DAILY value: a new module
#: arriving without a declared cadence should read stale and prompt an
#: explicit entry, never inherit the most forgiving budget in the table.
DEFAULT_HEALTH_THRESHOLD_DAYS = 2


def _last_modified_age(s3, bucket: str, key: str) -> tuple[str | None, int | None]:
    """Get the last modified date and age in days for an S3 object."""
    try:
        resp = s3.head_object(Bucket=bucket, Key=key)
        modified = resp["LastModified"]
        age = (datetime.now(timezone.utc) - modified).days
        return modified.strftime("%Y-%m-%d %H:%M UTC"), age
    except Exception:
        return None, None


def _find_latest_prefix(s3, bucket: str, prefix: str) -> tuple[str | None, int | None]:
    """Find the most recent date-keyed object under a prefix.

    Recognizes two key shapes:
      - Directory-keyed: ``signals/2026-04-03/signals.json`` (the YYYY-MM-DD
        appears as its own path segment).
      - Filename-keyed: ``archive/fundamentals/2026-05-24.json`` (the
        YYYY-MM-DD is the filename basename WITHOUT extension).

    Without the filename-keyed branch, ``archive/fundamentals/`` is read as
    "no matching date" → marked missing even when today's run wrote
    ``2026-05-24.json``. Surfaced 2026-05-24 in the health-checker false-
    positives audit.
    """
    try:
        paginator = s3.get_paginator("list_objects_v2")
        latest_date = None
        for page in paginator.paginate(Bucket=bucket, Prefix=prefix, MaxKeys=100):
            for obj in page.get("Contents", []):
                key = obj["Key"]
                parts = key.replace(prefix, "").split("/")
                for part in parts:
                    # Strip a single trailing extension so filename-keyed
                    # entries (e.g., 2026-05-24.json) parse the same as
                    # directory-keyed entries (e.g., 2026-04-03/).
                    base = part.rsplit(".", 1)[0] if "." in part else part
                    if len(base) == 10 and base[4] == "-" and base[7] == "-":
                        try:
                            d = date.fromisoformat(base)
                            if latest_date is None or d > latest_date:
                                latest_date = d
                        except ValueError:
                            pass
        if latest_date:
            age = (date.today() - latest_date).days
            return str(latest_date), age
        return None, None
    except Exception:
        return None, None


def check_all(bucket: str = DEFAULT_BUCKET) -> list[dict]:
    """Run all health checks. Returns list of check results."""
    s3 = boto3.client("s3")
    results = []

    # 1. Signals (latest.json pointer)
    modified, age = _last_modified_age(s3, bucket, "signals/latest.json")
    if modified is None:
        # Fallback: scan signals/ prefix
        modified, age = _find_latest_prefix(s3, bucket, "signals/")
    threshold = THRESHOLDS["signals"]
    results.append({
        "check": "signals",
        "last_updated": modified,
        "age_days": age,
        "threshold_days": threshold,
        "status": "ok" if age is not None and age <= threshold else "stale" if age is not None else "missing",
    })

    # 2. Predictions
    modified, age = _last_modified_age(s3, bucket, "predictor/predictions/latest.json")
    threshold = THRESHOLDS["predictions"]
    results.append({
        "check": "predictions",
        "last_updated": modified,
        "age_days": age,
        "threshold_days": threshold,
        "status": "ok" if age is not None and age <= threshold else "stale" if age is not None else "missing",
    })

    # 3. Feature store
    today_str = date.today().isoformat()
    modified, age = _last_modified_age(s3, bucket, f"features/{today_str}/technical.parquet")
    if modified is None:
        # Check yesterday
        yesterday = (date.today() - timedelta(days=1)).isoformat()
        modified, age = _last_modified_age(s3, bucket, f"features/{yesterday}/technical.parquet")
    threshold = THRESHOLDS["features"]
    results.append({
        "check": "features",
        "last_updated": modified,
        "age_days": age,
        "threshold_days": threshold,
        "status": "ok" if age is not None and age <= threshold else "stale" if age is not None else "missing",
    })

    # 4. Fundamentals
    latest_date, age = _find_latest_prefix(s3, bucket, "archive/fundamentals/")
    threshold = THRESHOLDS["fundamentals"]
    results.append({
        "check": "fundamentals",
        "last_updated": latest_date,
        "age_days": age,
        "threshold_days": threshold,
        "status": "ok" if age is not None and age <= threshold else "stale" if age is not None else "missing",
    })

    # 5. Universe membership — REPLACES the retired `population` check
    #    (alpha-engine-config-I6053, closing config-I4921's deferred row).
    #
    #    `population/latest.json`'s sole producer was the multi-agent research
    #    graph's `archive_writer` node (crucible-research
    #    `archive/manager.py::save_population`). That graph was removed from
    #    the weekly SF on 2026-07-14 (nousergon-data#814, config-I2515), so
    #    the file has not changed since 2026-07-10 — 33 days stale against an
    #    8-day threshold when measured 2026-08-13, and the single largest
    #    reason SaturdayHealthCheck exits non-zero.
    #
    #    The check is REMOVED, not re-baselined. Widening 8 days to 40 would
    #    convert a working detector into a silent one, and no threshold is
    #    correct for an artifact nobody writes — a freshness check on a dead
    #    producer is `principles.md` §2.7's "no data rendered as green" in
    #    reverse (see ARCHITECTURE §128, which this artifact is the origin
    #    case of).
    #
    #    Its consumer was repointed on 2026-07-27: the predictor now resolves
    #    its daily universe from `universe_membership/{date}/membership.json`
    #    + `universe_membership/latest.json` (crucible-research-PR506
    #    producer, crucible-predictor-PR429 consumer, config-I4818). That is
    #    the live successor artifact and the thing whose staleness actually
    #    matters now, so the check follows the capability rather than being
    #    deleted outright. Registry row `universe_membership_latest` already
    #    exists in ARTIFACT_REGISTRY.yaml.
    modified, age = _last_modified_age(s3, bucket, "universe_membership/latest.json")
    threshold = THRESHOLDS["universe_membership"]
    results.append({
        "check": "universe_membership",
        "last_updated": modified,
        "age_days": age,
        "threshold_days": threshold,
        "status": "ok" if age is not None and age <= threshold else "stale" if age is not None else "missing",
    })

    # 6. price_cache_slim check RETIRED (Wave-4 predictor/price_cache_slim
    # deletion). The slim tier is being deleted; the ArcticDB universe lib
    # that replaces it has its freshness gated upstream in
    # alpha-engine-data's preflight (runs before consumers in every Step
    # Function), so a dashboard-side slim/universe freshness check would
    # only duplicate that gate. No repoint — deliberate removal.

    # 7. Daily closes — staging/ prefix per 2026-04-29 migration
    # (alpha-engine-data PR #112). The parquet is intermediate state with
    # 7-day S3 lifecycle; canonical home is ArcticDB universe library.
    # Walk back up to threshold+2 calendar days to find the latest written
    # parquet. The earlier today+yesterday-only lookup false-flagged
    # Sat/Sun runs as "missing" because Friday's parquet was always >1
    # calendar day back. Surfaced 2026-05-24 in the false-positives audit.
    modified, age = None, None
    for back in range(THRESHOLDS["daily_closes"] + 3):
        candidate = (date.today() - timedelta(days=back)).isoformat()
        modified, age = _last_modified_age(s3, bucket, f"staging/daily_closes/{candidate}.parquet")
        if modified is not None:
            break
    threshold = THRESHOLDS["daily_closes"]
    results.append({
        "check": "daily_closes",
        "last_updated": modified,
        "age_days": age,
        "threshold_days": threshold,
        "status": "ok" if age is not None and age <= threshold else "stale" if age is not None else "missing",
    })

    # Module health markers — candidate keys from lib (config#1728).
    # Threshold is per-module and cadence-derived (see
    # HEALTH_MODULE_THRESHOLDS), not a flat 2 days: the research and
    # backtester markers are written by the weekly Saturday pipeline and a
    # 2-day budget marked them stale on four days of every week regardless of
    # health (alpha-engine-config-I6053).
    for module, candidates in HEALTH_CHECK_CANDIDATES.items():
        modified, age = None, None
        chosen_key = None
        for candidate in candidates:
            m_, a_ = _last_modified_age(s3, bucket, f"health/{candidate}")
            if m_ is None:
                continue
            if age is None or (a_ is not None and a_ < age):
                modified, age, chosen_key = m_, a_, candidate
        threshold = HEALTH_MODULE_THRESHOLDS.get(
            module, DEFAULT_HEALTH_THRESHOLD_DAYS
        )
        results.append({
            "check": f"health/{module}",
            "last_updated": modified,
            "age_days": age,
            "threshold_days": threshold,
            "status": "ok" if age is not None and age <= threshold else "stale" if age is not None else "missing",
            # When multiple candidate filenames exist for a module, name
            # which one was chosen (most-recently-modified). Empty for
            # single-file modules.
            "source_key": chosen_key if chosen_key and chosen_key != f"{module}.json" else None,
        })

    return results


def format_report(results: list[dict]) -> str:
    """Format results as a human-readable report."""
    lines = ["Data Health Report", "=" * 50]
    n_ok = sum(1 for r in results if r["status"] == "ok")
    n_stale = sum(1 for r in results if r["status"] == "stale")
    n_missing = sum(1 for r in results if r["status"] == "missing")
    lines.append(f"OK: {n_ok}  Stale: {n_stale}  Missing: {n_missing}")
    lines.append("")

    for r in results:
        icon = {"ok": "✓", "stale": "⚠", "missing": "✗"}[r["status"]]
        age_str = f"{r['age_days']}d" if r["age_days"] is not None else "N/A"
        lines.append(
            f"  {icon} {r['check']:25s} age={age_str:5s} "
            f"threshold={r['threshold_days']}d  "
            f"last={r['last_updated'] or 'never'}"
        )

    failures = [r for r in results if r["status"] != "ok"]
    if failures:
        lines.append("")
        lines.append("ACTIONS NEEDED:")
        for r in failures:
            lines.append(f"  - {r['check']}: {r['status']} ({r.get('last_updated', 'never')})")

    return "\n".join(lines)


def _emit_cloudwatch_metrics(results: list[dict]) -> None:
    """Publish health check metrics to CloudWatch for dashboarding and alarms."""
    try:
        cw = boto3.client("cloudwatch", region_name="us-east-1")
        metric_data = []

        # Per-check staleness age
        for r in results:
            age = r.get("age_days")
            if age is not None:
                metric_data.append({
                    "MetricName": "DataStalenessAge",
                    "Dimensions": [{"Name": "Check", "Value": r["check"]}],
                    "Value": float(age),
                    "Unit": "Count",
                })

        # Aggregate: total OK vs failures
        n_ok = sum(1 for r in results if r["status"] == "ok")
        n_fail = sum(1 for r in results if r["status"] != "ok")
        metric_data.append({
            "MetricName": "HealthChecksOK",
            "Value": float(n_ok),
            "Unit": "Count",
        })
        metric_data.append({
            "MetricName": "HealthChecksFailed",
            "Value": float(n_fail),
            "Unit": "Count",
        })

        if metric_data:
            # CloudWatch PutMetricData max 20 per call
            for i in range(0, len(metric_data), 20):
                cw.put_metric_data(
                    Namespace="AlphaEngine",
                    MetricData=metric_data[i:i+20],
                )
            logger.info("Emitted %d CloudWatch metrics", len(metric_data))
    except Exception as e:
        logger.warning("CloudWatch metric emission failed (non-fatal): %s", e)


def _assert_stage_coverage(stage: str, window_start: str) -> None:
    """Per-stage output assertion (config-I7214, sf-pipeline-policy.md §2.1):
    assert THIS stage wrote what it declared, at the boundary where the fact
    becomes knowable. OBSERVE MODE — it can never fail the stage.

    `sys.executable` IS the fleet-standard absolute venv interpreter here:
    the SF invokes this script as
    `/home/ec2-user/alpha-engine-dashboard/.venv/bin/python health_checker.py`,
    so the running interpreter already resolves to that same absolute path —
    no separate LIB_PYTHON variable exists in this script.
    """
    try:
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "nousergon_lib.stage_coverage",
                "assert",
                "--stage",
                stage,
                "--window-start",
                window_start,
            ],
            check=False,
        )
        rc = result.returncode
    except Exception as e:  # pragma: no cover - defensive, observe mode only
        rc = e
    if rc != 0:
        print(
            f"WARNING: stage-coverage assertion did not run for {stage} "
            f"(rc={rc}) — observe mode, stage NOT failed (config-I7214)",
            file=sys.stderr,
        )


def main():
    parser = argparse.ArgumentParser(description="Check data pipeline health")
    parser.add_argument("--json", action="store_true", help="JSON output")
    parser.add_argument("--bucket", default=DEFAULT_BUCKET)
    parser.add_argument("--alert", action="store_true", help="Send SNS alert on failures")
    args = parser.parse_args()

    # setup_logging already ran at module-top (see comment near the
    # nousergon_lib.logging import). Apply the standard log level.
    logging.getLogger().setLevel(logging.WARNING)
    results = check_all(args.bucket)

    if args.json:
        print(json.dumps(results, indent=2, default=str))
    else:
        print(format_report(results))

    # Emit CloudWatch metrics
    _emit_cloudwatch_metrics(results)

    failures = [r for r in results if r["status"] != "ok"]
    if failures and args.alert:
        try:
            topic_arn = os.environ.get("SNS_TOPIC_ARN", "arn:aws:sns:us-east-1:711398986525:alpha-engine-alerts")
            sns_client = boto3.client("sns", region_name="us-east-1")
            sns_client.publish(
                TopicArn=topic_arn,
                Subject="Alpha Engine — Data Staleness Alert",
                Message=format_report(results),
            )
        except Exception as e:
            logger.warning("SNS alert failed: %s", e)

    # Per-stage output assertion (config-I7214, sf-pipeline-policy.md §2.1):
    # assert THIS stage wrote what it declared, at the boundary where the fact
    # becomes knowable. OBSERVE MODE — it can never fail the stage.
    _assert_stage_coverage("SaturdayHealthCheck", _STAGE_WINDOW_START)

    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
