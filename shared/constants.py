"""
Shared constants for the Alpha Engine Dashboard.

Centralizes signal colors, regime labels, cache defaults, and column schemas
to eliminate duplication across pages and loaders.
"""

import re

# ---------------------------------------------------------------------------
# Signal display
# ---------------------------------------------------------------------------

SIGNAL_COLORS = {
    "ENTER": "#d4edda",
    "EXIT": "#f8d7da",
    "REDUCE": "#fff3cd",
    "HOLD": "#f8f9fa",
}

VETO_COLOR = "#f5c6cb"

# 3-class Ang-Bekaert macro taxonomy (v0.42.0 / 2026-05-28 —
# caution-regime-retirement-260528.md). "caution" glyph preserved for
# grandfather of historical signals.json artifacts whose market_regime
# field still carries the legacy 4-class value; new emissions are 3-class.
# The portfolio-protective drawdown axis (risk_on/caution/risk_off) lives
# in DRAWDOWN_TIER_EMOJI below.
REGIME_EMOJI = {
    "bull": "🐂",
    "bear": "🐻",
    "neutral": "➡️",
    "caution": "⚠️",  # legacy 4-class grandfather (pre-v0.42.0 artifacts)
}
REGIME_EMOJI_DEFAULT = "📊"

# Drawdown leg protective-tier glyphs (3-state Bridgewater All-Weather
# hysteresis pattern preserved post-Phase-2A). Axis ORTHOGONAL to macro
# regime: composed by consumers via most-protective override.
DRAWDOWN_TIER_EMOJI = {
    "risk_on": "✅",
    "caution": "⚠️",
    "risk_off": "🛑",
}
DRAWDOWN_TIER_EMOJI_DEFAULT = "❓"

# ---------------------------------------------------------------------------
# Return styling (CSS)
# ---------------------------------------------------------------------------

POSITIVE_RETURN_CSS = "color: #155724; background-color: #d4edda"
NEGATIVE_RETURN_CSS = "color: #721c24; background-color: #f8d7da"

# ---------------------------------------------------------------------------
# S3 / cache defaults
# ---------------------------------------------------------------------------

DEFAULT_CACHE_TTL_SECONDS = 900

ISO_DATE_PATTERN = re.compile(r"\d{4}-\d{2}-\d{2}")

# ---------------------------------------------------------------------------
# Positions display columns (ordered for UI tables)
# ---------------------------------------------------------------------------

POSITION_DISPLAY_COLUMNS = [
    "ticker", "sector", "shares", "entry_price", "current_price",
    "unrealized_pnl", "return_pct", "days_held", "score", "signal",
]

# ---------------------------------------------------------------------------
# Display thresholds (with defaults; overridable via config.yaml `thresholds:`)
# ---------------------------------------------------------------------------

DEFAULT_THRESHOLDS: dict[str, float | int] = {
    "veto_confidence": 0.65,
    "model_healthy": 0.52,
    "model_degraded": 0.48,
    "accuracy_baseline": 0.50,
    "accuracy_outperform": 0.55,
    "hhi_diversified": 0.15,
    "hhi_concentrated": 0.25,
    "sharpe_min_rows": 30,
    "correlation_high": 0.8,
    "correlation_min_overlap": 20,
}


def get_thresholds() -> dict[str, float | int]:
    """Return display thresholds, merging config.yaml overrides onto defaults.

    Safe to call from any module — falls back to defaults if config load fails
    (avoids circular imports at module-load time by importing lazily).
    """
    merged = dict(DEFAULT_THRESHOLDS)
    try:
        from loaders.s3_loader import load_config  # local import to avoid cycles
        overrides = (load_config() or {}).get("thresholds") or {}
        for k, v in overrides.items():
            if k in merged and v is not None:
                merged[k] = v
    except Exception:
        pass
    return merged
