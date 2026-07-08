"""Pure view-model builders for the Crucible results surface.

Each builder takes already-loaded artifact payloads (dicts / DataFrames from
``loaders.s3_loader``) and returns plain lists/dicts shaped for display. No
Streamlit, no boto3, no statistics — a missing or malformed artifact yields
an explicit absent marker so every page renders honestly instead of blank.

Persona note (Brian ruling 2026-07-08): the v1 audience is a hedge fund
testing strategies — institutionally literate. ``HELP`` therefore carries
one-line definitions (DSR, date-clustered IC, BH-FDR), not primers.
"""
from __future__ import annotations

from typing import Any

import pandas as pd

ABSENT = "—"

# One-line hover definitions for the institutional statistics (plan §8.3).
HELP: dict[str, str] = {
    "alpha": "Cumulative portfolio return minus SPY over the same window (paper-traded, net of modeled costs).",
    "sharpe": "Annualized excess return per unit of volatility, computed on realized daily returns.",
    "psr": "Probabilistic Sharpe Ratio — confidence that the true Sharpe exceeds zero given the observed sample (Bailey & López de Prado 2012); guards against short-window luck.",
    "hit_rate": "Share of ENTER signals that beat SPY at the canonical 21-day horizon, with a Wilson score interval.",
    "max_dd": "Largest peak-to-trough NAV decline in the window.",
    "ic": "Date-clustered rank information coefficient: weekly cross-sectional Spearman correlation of score vs realized 21-day alpha (weeks as N, not pooled rows).",
    "fdr": "Benjamini-Hochberg false-discovery-rate control across the tested sub-scores; 'significant' means the correlation survives at q=0.05.",
    "pit_parity": "Lookahead audit: the same strategy scored with point-in-time data vs current (lookahead-contaminated) data. A near-zero log-alpha delta means the backtest is not flattered by information it could not have had.",
}


def _num(value: Any) -> float | None:
    """Coerce to float, else None (never raises — display layer)."""
    try:
        if value is None or isinstance(value, bool):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _pct(value: Any, digits: int = 1) -> str:
    v = _num(value)
    return ABSENT if v is None else f"{v:+.{digits}f}%"


# ---------------------------------------------------------------------------
# §A Overview
# ---------------------------------------------------------------------------

def build_identity(card: dict | None, backtest_date: str | None) -> dict:
    """Identity block: what ran, exactly. Reproducibility before performance.

    v1 renders the stock Reference Rate experiment; slot descriptors are the
    stock references until ``experiment_record.v1`` exists (plan Phase A),
    at which point this builder consumes the run record instead.
    """
    provenance = (card or {}).get("_provenance") or {}
    return {
        "experiment_id": "reference-rate",
        "slots": [
            ("R · research", "stock — multi-agent sector teams + macro + CIO"),
            ("M · model", "stock — stacked meta-ensemble (3×L1 + Ridge L2)"),
            ("S · strategy", "stock — reference exit/risk rule set"),
        ],
        "report_card_date": provenance.get("run_date") or ABSENT,
        "grader_source": provenance.get("grader_source") or ABSENT,
        "backtest_date": backtest_date or ABSENT,
    }


def build_headline(
    eod_pnl: pd.DataFrame | None,
    signal_metrics: dict | None,
    portfolio_stats: dict | None,
) -> list[dict]:
    """Headline stat strip. Values are read from artifacts, never recomputed.

    Sources (verified against producers 2026-07-08):
    - ``eod_pnl`` — executor EOD ledger, per-day ``alpha_pct`` column;
    - ``signal_metrics`` — weekly ``metrics.json`` (= signal_quality overall:
      ``accuracy_21d``, ``n_21d``);
    - ``portfolio_stats`` — weekly ``portfolio_stats.json`` from the vectorbt
      production sim (``sharpe_ratio``, ``max_drawdown`` fraction, ``psr``).
    Each stat renders ABSENT when its source column/key is missing.
    """
    stats: list[dict] = []
    cum_alpha = None
    n_days = 0
    sub = "eod_pnl.csv absent"
    if eod_pnl is not None and not eod_pnl.empty:
        # Ledger column is daily_alpha_pct (verified against the live CSV
        # 2026-07-08 — the producer's data manifest uses alpha_pct, the CSV
        # does not).
        if "daily_alpha_pct" in eod_pnl.columns:
            alpha = pd.to_numeric(eod_pnl["daily_alpha_pct"], errors="coerce").dropna()
            n_days = len(alpha)
            if n_days:
                cum_alpha = alpha.sum()  # display-level sum of daily alpha, matching the EOD ledger
                sub = f"n={n_days} sessions"
        else:
            sub = "ledger loaded; daily_alpha_pct column absent"
    stats.append({
        "label": "Alpha vs SPY (cum)",
        "value": _pct(cum_alpha, 2),
        "sub": sub,
        "help": HELP["alpha"],
    })

    ps = portfolio_stats or {}
    sharpe = _num(ps.get("sharpe_ratio"))
    stats.append({
        "label": "Sharpe (ann.)",
        "value": ABSENT if sharpe is None else f"{sharpe:.2f}",
        "sub": "vectorbt production sim",
        "help": HELP["sharpe"],
    })
    psr = _num(ps.get("psr"))
    stats.append({
        "label": "PSR",
        "value": ABSENT if psr is None else f"{psr:.2f}",
        "sub": "P(true Sharpe > 0)" if psr is not None else "not computed this run",
        "help": HELP["psr"],
    })

    sm = signal_metrics or {}
    hit = _num(sm.get("accuracy_21d"))
    n_sig = sm.get("n_21d")
    stats.append({
        "label": "Hit rate · 21d",
        "value": ABSENT if hit is None else (f"{hit:.1%}" if hit <= 1 else f"{hit:.1f}%"),
        "sub": f"n={n_sig} finalized signals" if n_sig else "ENTER signals vs SPY",
        "help": HELP["hit_rate"],
    })
    dd = _num(ps.get("max_drawdown"))
    stats.append({
        "label": "Max drawdown",
        "value": ABSENT if dd is None else (f"{dd:.1%}" if abs(dd) <= 1 else f"{dd:.1f}%"),
        "sub": "peak-to-trough NAV",
        "help": HELP["max_dd"],
    })
    return stats


def equity_frame(eod_pnl: pd.DataFrame | None) -> pd.DataFrame:
    """Cumulative return series for the equity chart (portfolio vs SPY).

    Cumulative compounding of the ledger's daily returns — a display
    transform of recorded values, not a new statistic.
    """
    if eod_pnl is None or eod_pnl.empty:
        return pd.DataFrame()
    need = {"date", "daily_return_pct", "spy_return_pct"}
    if not need.issubset(eod_pnl.columns):
        return pd.DataFrame()
    df = eod_pnl[["date", "daily_return_pct", "spy_return_pct"]].copy()
    for col in ("daily_return_pct", "spy_return_pct"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.dropna().sort_values("date")
    if df.empty:
        return pd.DataFrame()
    df["Portfolio"] = ((1 + df["daily_return_pct"] / 100).cumprod() - 1) * 100
    df["SPY"] = ((1 + df["spy_return_pct"] / 100).cumprod() - 1) * 100
    return df[["date", "Portfolio", "SPY"]]


def alpha_by_period(eod_pnl: pd.DataFrame | None, period: str) -> pd.DataFrame:
    """Ledger daily alpha aggregated to a display period since inception.

    ``period`` ∈ {"D", "W", "M"}: daily rows pass through; weekly buckets to
    the trading week (W-FRI label = week-ending Friday); monthly to month
    end. Returns columns ``[label, alpha_pct, n_days]`` — a pure display
    aggregation (sums of the recorded ``daily_alpha_pct`` column, matching
    the headline's cumulative convention), never a new statistic.
    """
    if eod_pnl is None or eod_pnl.empty or not {"date", "daily_alpha_pct"}.issubset(eod_pnl.columns):
        return pd.DataFrame()
    df = eod_pnl[["date", "daily_alpha_pct"]].copy()
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df["daily_alpha_pct"] = pd.to_numeric(df["daily_alpha_pct"], errors="coerce")
    df = df.dropna().sort_values("date")
    if df.empty:
        return pd.DataFrame()
    if period == "D":
        out = df.rename(columns={"date": "label", "daily_alpha_pct": "alpha_pct"})
        out["n_days"] = 1
        return out[["label", "alpha_pct", "n_days"]]
    rule = {"W": "W-FRI", "M": "ME"}.get(period)
    if rule is None:
        return pd.DataFrame()
    grouped = df.set_index("date")["daily_alpha_pct"].resample(rule).agg(["sum", "count"])
    grouped = grouped[grouped["count"] > 0].reset_index()
    grouped.columns = ["label", "alpha_pct", "n_days"]
    return grouped


def rolling_alpha_frame(eod_pnl: pd.DataFrame | None, window: int = 20) -> pd.DataFrame:
    """Rolling mean of ledger daily alpha (default ≈1 trading month).

    The descriptive "is it improving" overlay: columns ``[date,
    rolling_mean]``, empty until ``window`` sessions exist. A display
    smoothing of recorded values — trend ADJUDICATION (slope, significance)
    belongs to the evaluator, not this layer.
    """
    if eod_pnl is None or eod_pnl.empty or not {"date", "daily_alpha_pct"}.issubset(eod_pnl.columns):
        return pd.DataFrame()
    df = eod_pnl[["date", "daily_alpha_pct"]].copy()
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df["daily_alpha_pct"] = pd.to_numeric(df["daily_alpha_pct"], errors="coerce")
    df = df.dropna().sort_values("date")
    roll = df.set_index("date")["daily_alpha_pct"].rolling(window).mean().dropna()
    if roll.empty:
        return pd.DataFrame()
    return roll.rename("rolling_mean").reset_index()


# ---------------------------------------------------------------------------
# §B Validation (backtester detail)
# ---------------------------------------------------------------------------

def attribution_rows(attribution: dict | None) -> list[dict]:
    """Sub-score → outcome attribution with the FDR verdict displayed.

    Reads the univariate ``correlations`` map (each label carries
    ``{target: corr, f"{target}_fdr_significant": bool}``); the primary
    21d-alpha target is preferred, first available target otherwise.
    """
    if not attribution or not isinstance(attribution.get("correlations"), dict):
        return []
    rows: list[dict] = []
    for label, targets in attribution["correlations"].items():
        if not isinstance(targets, dict):
            continue
        corr_keys = [k for k in targets if not k.endswith("_fdr_significant")]
        preferred = [k for k in corr_keys if "21d" in k] or corr_keys
        if not preferred:
            continue
        target = preferred[0]
        corr = _num(targets.get(target))
        if corr is None:
            continue
        rows.append({
            "sub_score": label,
            "target": target,
            "correlation": corr,
            "fdr_significant": bool(targets.get(f"{target}_fdr_significant", False)),
        })
    rows.sort(key=lambda r: abs(r["correlation"]), reverse=True)
    return rows


def integrity_rows(
    pit_parity: dict | None,
    sample_size: dict | None,
    walk_forward: dict | None,
    optimizer_churn: dict | None,
) -> list[dict]:
    """The "can you trust this backtest" panel — one row per integrity leg.

    Each artifact carries its own ``status``; an absent artifact is an
    explicit ABSENT row (the honesty is the feature), never a dropped row.
    """
    legs = [
        ("Lookahead audit (PIT vs current)", pit_parity, HELP["pit_parity"]),
        ("Sample-size adequacy", sample_size, "Finalized-signal count vs the minimum-N floor on the weakest measurement leg."),
        ("Walk-forward stability", walk_forward, "Dispersion of optimizer-selected parameters across walk-forward folds."),
        ("Optimizer churn", optimizer_churn, "How often the auto-apply loop changed live parameters recently."),
    ]
    rows: list[dict] = []
    for label, artifact, help_text in legs:
        if not isinstance(artifact, dict):
            rows.append({"check": label, "status": "ABSENT", "detail": "artifact not emitted for this run", "help": help_text})
            continue
        # Status only when the producer declares one — the dashboard reports
        # verdicts, it never adjudicates thresholds itself.
        status = str(artifact["status"]).upper() if artifact.get("status") else "REPORTED"
        delta = _num(artifact.get("headline_log_alpha_delta"))
        if delta is not None:  # pit_parity's headline verdict (schema pit_parity-1.x)
            detail = f"headline log-alpha delta (PIT − lookahead): {delta:+.3f}"
        else:
            detail_keys = [k for k in ("summary", "detail", "note", "reason") if artifact.get(k)]
            detail = str(artifact[detail_keys[0]]) if detail_keys else ", ".join(
                f"{k}={artifact[k]}" for k in sorted(artifact)
                if isinstance(artifact[k], (int, float, str, bool)) and k not in ("status", "schema", "run_date")
            )[:200]
        rows.append({"check": label, "status": status, "detail": detail or ABSENT, "help": help_text})
    return rows


# ---------------------------------------------------------------------------
# §D Execution (execution-sim detail)
# ---------------------------------------------------------------------------

def execution_headline(
    trigger_scorecard: dict | None,
    exit_timing: dict | None,
    shadow_book: dict | None,
) -> list[dict]:
    """Headline strip for the execution tab — recorded values only."""
    ts = (trigger_scorecard or {}).get("summary") or {}
    et = (exit_timing or {}).get("summary") or {}
    sb = shadow_book or {}
    n_rt = _num(et.get("n_roundtrips"))
    win = _num(et.get("win_rate"))
    cap = _num(et.get("winsorized_capture_ratio"))
    lift = _num(sb.get("guard_lift"))
    return [
        {"label": "Entries (window)", "value": ABSENT if _num(ts.get("total_entries")) is None else f"{int(ts['total_entries'])}",
         "sub": "trigger-timed fills", "help": "Entries executed by the intraday daemon's technical triggers in the analysis window."},
        {"label": "Roundtrips", "value": ABSENT if n_rt is None else f"{int(n_rt)}",
         "sub": "closed positions", "help": "Fully closed positions with entry and exit fills recorded."},
        {"label": "Win rate", "value": ABSENT if win is None else f"{win:.1%}",
         "sub": "roundtrips > 0", "help": "Share of closed roundtrips with positive realized return."},
        {"label": "Capture ratio (wins.)", "value": ABSENT if cap is None else f"{cap:.2f}",
         "sub": "realized / max favorable", "help": "Winsorized share of the maximum favorable excursion the exit rules actually captured — exit-timing quality."},
        {"label": "Risk-guard lift", "value": ABSENT if lift is None else f"{lift:+.3f}",
         "sub": str(sb.get("assessment", "")) or "blocked vs traded", "help": "Return difference between what the risk guard traded and what it blocked (shadow book counterfactual). Near zero = the guard is neither adding nor destroying value."},
    ]


def trigger_rows(trigger_scorecard: dict | None) -> list[dict]:
    """Per-trigger execution quality (slippage vs signal/open)."""
    rows = []
    for t in (trigger_scorecard or {}).get("triggers") or []:
        if not isinstance(t, dict):
            continue
        rows.append({
            "trigger": t.get("trigger", ABSENT),
            "n_trades": t.get("n_trades", ABSENT),
            "slippage_vs_signal": ABSENT if _num(t.get("avg_slippage_vs_signal")) is None else f"{t['avg_slippage_vs_signal']:+.2f}%",
            "slippage_vs_open": ABSENT if _num(t.get("avg_slippage_vs_open")) is None else f"{t['avg_slippage_vs_open']:+.2f}%",
            "win_rate_vs_spy": ABSENT if _num(t.get("win_rate_vs_spy")) is None else f"{t['win_rate_vs_spy']:.1%}",
        })
    return rows


def exit_type_rows(exit_timing: dict | None) -> list[dict]:
    """Per-exit-rule timing quality (MFE/MAE/capture)."""
    rows = []
    for e in (exit_timing or {}).get("by_exit_type") or []:
        if not isinstance(e, dict):
            continue
        rows.append({
            "exit_type": e.get("exit_type", ABSENT),
            "n": e.get("n", ABSENT),
            "avg_mfe": ABSENT if _num(e.get("avg_mfe")) is None else f"{e['avg_mfe']:+.2f}%",
            "avg_mae": ABSENT if _num(e.get("avg_mae")) is None else f"{e['avg_mae']:+.2f}%",
            "avg_realized": ABSENT if _num(e.get("avg_realized")) is None else f"{e['avg_realized']:+.2f}%",
            "avg_capture": ABSENT if _num(e.get("avg_capture")) is None else f"{e['avg_capture']:.2f}",
        })
    return rows


def shadow_classification_rows(shadow_book: dict | None) -> list[dict]:
    """Risk-guard confusion summary from the shadow book counterfactual."""
    cls = (shadow_book or {}).get("classification") or {}
    if not cls:
        return []
    return [
        {"measure": name, "value": ABSENT if _num(cls.get(key)) is None else
            (f"{cls[key]:.1%}" if key in ("precision", "recall", "f1", "accuracy") else f"{int(cls[key])}")}
        for name, key in [
            ("Precision (traded → beat SPY)", "precision"), ("Recall", "recall"),
            ("F1", "f1"), ("Accuracy", "accuracy"), ("N classified", "n"),
        ]
    ]


# ---------------------------------------------------------------------------
# §E Feedback loop (governed auto-apply)
# ---------------------------------------------------------------------------

def apply_audit_rows(audit: dict | None) -> list[dict]:
    """Per-loop auto-apply outcome from ``config/apply_audit/latest.json``
    (schema v1). Empty list when the artifact has not been emitted yet —
    the view states the first-emission date rather than rendering blank.
    """
    loops = (audit or {}).get("loops") or {}
    rows = []
    for loop, rec in loops.items():
        if not isinstance(rec, dict):
            continue
        blocked_by = rec.get("blocked_by")
        rows.append({
            "loop": loop,
            "outcome": rec.get("outcome", ABSENT),
            "blocked_by": ", ".join(blocked_by) if isinstance(blocked_by, list) else (blocked_by or ABSENT),
            "consecutive_blocked_weeks": rec.get("consecutive_blocked_weeks", ABSENT),
        })
    return sorted(rows, key=lambda r: r["loop"])


def config_snapshot_rows(meta: dict | None) -> list[dict]:
    """Live auto-apply config artifacts: present/absent + last write.

    An absent artifact means that optimizer has NEVER promoted to live —
    a true statement about the governed loop, displayed as such.
    """
    rows = []
    for name, info in (meta or {}).items():
        if not isinstance(info, dict):
            continue
        if info.get("present"):
            keys = info.get("keys") or []
            rows.append({
                "config": name, "state": "LIVE",
                "last_written": info.get("last_modified", ABSENT),
                "detail": f"{len(keys)} keys" + (f" · {', '.join(keys[:5])}…" if len(keys) > 5 else f" · {', '.join(keys)}"),
            })
        else:
            rows.append({
                "config": name, "state": "NEVER WRITTEN",
                "last_written": ABSENT,
                "detail": "no live promotion has ever cleared this loop's gates",
            })
    return sorted(rows, key=lambda r: r["config"])


# ---------------------------------------------------------------------------
# §C Evaluation (evaluator detail)
# ---------------------------------------------------------------------------

def tile_labels(card: dict | None) -> list[tuple[str, str]]:
    """(key, display label) per tile present on the card, card order."""
    tiles = (card or {}).get("tiles") or {}
    return [(key, key.replace("_", " ").title()) for key in tiles]


def metric_rows(card: dict | None, tile_key: str) -> list[dict]:
    """Full MetricRecord table for one tile — the contract rendered, not
    summarized: value, CI, N, target/red-line, trend, status, status_reason.
    """
    tile = ((card or {}).get("tiles") or {}).get(tile_key) or {}
    rows: list[dict] = []
    for comp in tile.get("components") or []:
        if not isinstance(comp, dict):
            continue
        value = _num(comp.get("value"))
        ci_low, ci_high = _num(comp.get("ci_low")), _num(comp.get("ci_high"))
        rows.append({
            "metric": comp.get("name", ABSENT),
            "value": ABSENT if value is None else f"{value:.4g}",
            "ci": ABSENT if ci_low is None or ci_high is None else f"[{ci_low:.3g}, {ci_high:.3g}]",
            "n": comp.get("n_samples") if comp.get("n_samples") is not None else ABSENT,
            "target": comp.get("target") if comp.get("target") is not None else ABSENT,
            "red_line": comp.get("red_line") if comp.get("red_line") is not None else ABSENT,
            "trend": comp.get("trend_decoration") or ABSENT,
            "criticality": comp.get("criticality", ABSENT),
            "status": comp.get("status", "N/A"),
            "reason": comp.get("status_reason") or ABSENT,
        })
    return rows
