"""
Think Tank — Alpha Engine (private console)

The daily research think tank (config#1579, open-source-model-native): its
coverage, per-name theses, and — the headline — the analyst's INDEPENDENT
0-100 rating per covered name. The model is deliberately never shown the
scanner's attractiveness composite (crucible-research
``thinktank/analyst.py::_facts_board_row``), so the ``Δ vs scanner`` column
is a genuine two-opinion divergence, not an echo: big gaps either way are
the names worth reading first, and the cohort feeding the config#1580
restructure evidence.

Tabs: Ratings Board (rating vs scanner divergence) / Thesis Browser (full
narrative per name + version history) / Themes (macro + sector working
views) / Runs & Costs (daily manifests + month spend vs the SSM cap).

Reads only recorded ``thinktank/`` S3 artifacts — no LLM call, no cost.
Lives on console.nousergon.ai (Cloudflare Access-gated). Native Streamlit
chrome — no set_page_config (app.py's st.navigation owns it).
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
import streamlit as st

from loaders.s3_loader import (
    list_thinktank_manifest_keys,
    load_thinktank_month_costs,
    load_thinktank_ratings,
    load_thinktank_theme,
    load_thinktank_thesis,
    download_s3_json,
    _research_bucket,
)

st.markdown("### 🧠 Think Tank")
st.caption(
    "Daily open-model research desk: independent theses + ratings over the "
    "covered universe. The analyst never sees the scanner's attractiveness "
    "composite — Δ vs scanner is a real second opinion, not an echo."
)

board = load_thinktank_ratings()
if not board or not board.get("rows"):
    st.warning(
        "No ratings board published yet. It is written by every daily "
        "think-tank run (crucible-research `thinktank/ratings.py` → "
        "`thinktank/ratings/latest.json`); the first post-rating run "
        "populates it."
    )
    st.stop()

rows = board["rows"]
df = pd.DataFrame(list(rows.values()))
for col in ("rating", "conviction", "attractiveness_score",
            "rating_minus_attractiveness", "thesis_version"):
    if col in df.columns:
        df[col] = pd.to_numeric(df[col], errors="coerce")

# ── Headline ────────────────────────────────────────────────────────────────
n = len(df)
rated = df["rating"].notna().sum()
avg_rating = df["rating"].mean()
avg_delta = df["rating_minus_attractiveness"].abs().mean()
m1, m2, m3, m4 = st.columns(4)
m1.metric("Coverage", n)
m2.metric("Rated", f"{rated}/{n}")
m3.metric("Avg rating", f"{avg_rating:.1f}" if pd.notna(avg_rating) else "—")
m4.metric(
    "Avg |Δ vs scanner|",
    f"{avg_delta:.1f}" if pd.notna(avg_delta) else "—",
    help="Mean absolute divergence between the think tank's independent "
    "rating and the scanner attractiveness composite at thesis-write time.",
)
st.caption(f"Board as of {board.get('updated_at', '—')} (trading day {board.get('trading_day', '—')})")

tab_board, tab_thesis, tab_themes, tab_runs = st.tabs(
    ["Ratings Board", "Thesis Browser", "Themes", "Runs & Costs"]
)

# ── Ratings Board ───────────────────────────────────────────────────────────
with tab_board:
    display = df[
        [c for c in (
            "ticker", "sector", "rating", "attractiveness_score",
            "rating_minus_attractiveness", "stance", "conviction",
            "thesis_version", "thesis_trading_day", "update_reason", "summary",
        ) if c in df.columns]
    ].sort_values(
        "rating_minus_attractiveness",
        key=lambda s: s.abs(),
        ascending=False,
        na_position="last",
    )
    st.dataframe(
        display,
        hide_index=True,
        use_container_width=True,
        column_config={
            "ticker": st.column_config.TextColumn("Ticker"),
            "sector": st.column_config.TextColumn("Sector"),
            "rating": st.column_config.ProgressColumn(
                "TT Rating", min_value=0, max_value=100, format="%d",
                help="The think tank's independent 0-100 call — built only "
                "from filings, news/sentiment, weekly research, macro/sector "
                "themes, and raw metrics. Blank = thesis predates the rating "
                "field (refreshes organically or via operator backfill).",
            ),
            "attractiveness_score": st.column_config.ProgressColumn(
                "Scanner Attr.", min_value=0, max_value=100, format="%.0f",
                help="Scanner attractiveness composite at thesis-write time "
                "(metadata — never shown to the model).",
            ),
            "rating_minus_attractiveness": st.column_config.NumberColumn(
                "Δ vs scanner", format="%+.1f",
                help="Rating − attractiveness. Large positive: the think tank "
                "likes it more than the quant screen; large negative: the "
                "think tank is the skeptic. Both tails are reading material.",
            ),
            "stance": st.column_config.TextColumn("Stance"),
            "conviction": st.column_config.NumberColumn("Conviction", format="%d"),
            "thesis_version": st.column_config.NumberColumn("v", format="%d"),
            "thesis_trading_day": st.column_config.TextColumn("As of"),
            "update_reason": st.column_config.TextColumn("Last update"),
            "summary": st.column_config.TextColumn("Executive summary", width="large"),
        },
    )

# ── Thesis Browser ──────────────────────────────────────────────────────────
with tab_thesis:
    tickers = sorted(rows)
    sel = st.selectbox("Covered name", tickers, key="tt_thesis_ticker")
    thesis = load_thinktank_thesis(sel) if sel else None
    if not thesis:
        st.info("No thesis artifact found for this name.")
    else:
        t = thesis.get("thesis", {})
        c1, c2, c3, c4 = st.columns(4)
        rating = t.get("rating")
        c1.metric("TT Rating", rating if rating is not None else "—")
        c2.metric("Stance", t.get("stance", "—"))
        c3.metric("Conviction", t.get("conviction", "—"))
        c4.metric(
            "Version",
            f"v{thesis.get('version', '—')} ({thesis.get('update_reason', '—')})",
        )
        if t.get("rating_rationale"):
            st.markdown(f"**Why {rating}:** {t['rating_rationale']}")
        st.markdown(f"**Summary** — {t.get('summary', '—')}")
        sections = (
            ("Business", "business_summary"),
            ("Moat", "moat"),
            ("Filings review", "filings_review"),
            ("News & sentiment", "news_sentiment"),
            ("Valuation", "valuation"),
            ("Market dynamics", "market_dynamics"),
        )
        for title, key in sections:
            body = t.get(key)
            if body:
                with st.expander(title, expanded=False):
                    st.markdown(body)
        risks, catalysts = t.get("risks") or [], t.get("catalysts") or []
        rc1, rc2 = st.columns(2)
        with rc1:
            st.markdown("**Risks**")
            for r in risks:
                st.markdown(f"- {r}")
        with rc2:
            st.markdown("**Catalysts**")
            for c in catalysts:
                st.markdown(f"- {c}")
        st.caption(
            f"Sources: {', '.join(thesis.get('sources_used') or []) or '—'} · "
            f"model {thesis.get('model', '—')} · "
            f"${thesis.get('cost_usd', 0):.4f} · "
            f"scanner attractiveness at write: "
            f"{thesis.get('attractiveness_score', '—')}"
        )
        n_versions = int(thesis.get("version") or 1)
        if n_versions > 1:
            v = st.selectbox(
                "Version history",
                list(range(n_versions, 0, -1)),
                format_func=lambda x: f"v{x}",
                key="tt_thesis_version",
            )
            if v and v != n_versions:
                old = load_thinktank_thesis(sel, version=v)
                if old:
                    ot = old.get("thesis", {})
                    st.markdown(
                        f"**v{v}** ({old.get('trading_day', '—')}, "
                        f"{old.get('update_reason', '—')}, rating "
                        f"{ot.get('rating', '—')}): {ot.get('summary', '—')}"
                    )

# ── Themes ──────────────────────────────────────────────────────────────────
with tab_themes:
    macro = load_thinktank_theme("macro", "macro")
    if macro:
        th = macro.get("theme", {})
        st.markdown(
            f"**Macro — {th.get('stance', '—')}** · v{macro.get('version', '—')} "
            f"({macro.get('update_reason', '—')}, anchored to weekly "
            f"{macro.get('weekly_anchor_date', '—')})"
        )
        st.markdown(th.get("narrative", "—"))
        d1, d2 = st.columns(2)
        with d1:
            st.markdown("**Drivers**")
            for d in th.get("drivers") or []:
                st.markdown(f"- {d}")
        with d2:
            st.markdown("**Watch items**")
            for w in th.get("watch_items") or []:
                st.markdown(f"- {w}")
    else:
        st.info("No macro theme published yet.")
    sectors = sorted({r.get("sector") for r in rows.values() if r.get("sector")})
    if sectors:
        st.divider()
        sec = st.selectbox("Sector theme", sectors, key="tt_sector_theme")
        sec_theme = load_thinktank_theme("sector", sec) if sec else None
        if sec_theme:
            th = sec_theme.get("theme", {})
            st.markdown(
                f"**{sec} — {th.get('stance', '—')}** · "
                f"v{sec_theme.get('version', '—')}"
            )
            st.markdown(th.get("narrative", "—"))
        else:
            st.info("No theme artifact for this sector yet.")

# ── Runs & Costs ────────────────────────────────────────────────────────────
with tab_runs:
    month = datetime.now(timezone.utc).strftime("%Y-%m")
    costs = load_thinktank_month_costs(month)
    if costs:
        cm1, cm2 = st.columns(2)
        cm1.metric(f"{month} spend", f"${costs.get('spent_usd', 0):.2f}")
        cm2.metric("Runs this month", len(costs.get("runs") or []))
    keys = list_thinktank_manifest_keys(limit=20)
    if not keys:
        st.info("No run manifests yet.")
    else:
        records = []
        for k in keys:
            m = download_s3_json(_research_bucket(), k)
            if isinstance(m, dict):
                records.append(
                    {
                        "trading_day": m.get("trading_day"),
                        "mode": m.get("mode"),
                        "run_id": m.get("run_id"),
                        "theses": m.get("theses_written"),
                        "event_updates": m.get("event_updates_written"),
                        "swept": m.get("sweep_tickers"),
                        "theme_updates": m.get("theme_updates_written"),
                        "cost_usd": m.get("total_cost_usd"),
                        "started_at": m.get("started_at"),
                    }
                )
        if records:
            st.dataframe(
                pd.DataFrame(records),
                hide_index=True,
                use_container_width=True,
                column_config={
                    "cost_usd": st.column_config.NumberColumn(
                        "Cost", format="$%.4f"
                    ),
                },
            )
