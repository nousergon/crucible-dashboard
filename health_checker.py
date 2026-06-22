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

logger = logging.getLogger(__name__)

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
    "population": 8,        # Updated weekly by Research
}


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

    # 5. Population
    modified, age = _last_modified_age(s3, bucket, "population/latest.json")
    threshold = THRESHOLDS["population"]
    results.append({
        "check": "population",
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

    # 8. Module health markers
    #
    # Most modules write a single ``health/{module}.json`` file. The
    # predictor is the exception: it writes one health file per surface
    # (``predictor_inference.json``, ``predictor_training.json``,
    # ``predictor_health_check.json``) and never a unified
    # ``predictor.json``. Using a per-module candidate list lets the
    # checker accept whichever surface emitted most recently. Surfaced
    # 2026-05-24 in the health-checker false-positives audit (looking for
    # ``predictor.json`` always missed because no producer writes it).
    MODULE_HEALTH_CANDIDATES = {
        "data_phase1": ["data_phase1.json"],
        "data_phase2": ["data_phase2.json"],
        "executor": ["executor.json"],
        "predictor": [
            "predictor_inference.json",
            "predictor_training.json",
            "predictor_health_check.json",
        ],
    }
    for module, candidates in MODULE_HEALTH_CANDIDATES.items():
        modified, age = None, None
        chosen_key = None
        for candidate in candidates:
            m_, a_ = _last_modified_age(s3, bucket, f"health/{candidate}")
            if m_ is None:
                continue
            if age is None or (a_ is not None and a_ < age):
                modified, age, chosen_key = m_, a_, candidate
        results.append({
            "check": f"health/{module}",
            "last_updated": modified,
            "age_days": age,
            "threshold_days": 2,
            "status": "ok" if age is not None and age <= 2 else "stale" if age is not None else "missing",
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

    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
