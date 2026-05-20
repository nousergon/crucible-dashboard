"""
Alpha Engine — RAG Knowledge Base Inventory (private console)

Inventory of the RAG corpus that backs the research-agent stack:
document counts by source type, per-ticker coverage rollup, embedding
stats, ingestion freshness.

Closes Workstream 3.5 of the presentation revamp plan. Lives on
console.nousergon.ai (Cloudflare Access-gated). The public site
intentionally describes the corpus only at high level (per
plan §3.6); detailed inventory stays here.

Per Decision 11 — sources from the upstream `rag/manifest/latest.json`
artifact emitted by `alpha-engine-data/rag/pipelines/emit_manifest.py`
as step 6/6 of `run_weekly_ingestion.sh`. The manifest is public-safe
aggregates only (counts + percentiles + freshness + embedding meta);
per-ticker doc lists, individual document titles, and chunk content
stay in pgvector behind the disclosure boundary.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timezone

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from loaders.s3_loader import load_rag_manifest

st.set_page_config(
    page_title="RAG Inventory — Alpha Engine",
    page_icon="📚",
    layout="wide",
)


st.divider()

# ---------------------------------------------------------------------------
# Page intro
# ---------------------------------------------------------------------------

st.markdown("### Knowledge Base (RAG) Inventory")
st.markdown(
    """
    The RAG corpus backs the research-agent stack: SEC filings (10-K,
    10-Q, 8-K), earnings transcripts, and internal investment theses.
    Documents are chunked, embedded with **voyage-3-lite** (512-dim),
    and queried against pgvector during agent reasoning.

    This inventory reads from the upstream manifest emitted weekly by
    `alpha-engine-data/rag/pipelines/emit_manifest.py` — `totals`,
    per-source rollups, ticker-coverage percentiles, embedding meta,
    and ingestion timestamps. Per the disclosure boundary, per-ticker
    document lists and chunk content stay in pgvector and surface only
    during interview screenshare on this gated console.
    """
)
st.caption(
    "Phase-2 framing: the corpus is the *retrieval substrate* for "
    "Phase 3 alpha tuning — agent quality is bounded by what's "
    "retrievable, so corpus expansion (broker reports, sell-side notes, "
    "earnings Q&A) is a Phase-3-uplift candidate gated on per-source "
    "IC contribution."
)

# ---------------------------------------------------------------------------
# Load manifest with graceful fallback
# ---------------------------------------------------------------------------

manifest = load_rag_manifest()

if not manifest or not isinstance(manifest, dict):
    st.info(
        "**Awaiting first manifest.** The RAG manifest emitter is wired "
        "as step 6/6 of the weekly ingestion script "
        "(`alpha-engine-data/rag/pipelines/emit_manifest.py`, shipped "
        "2026-05-05 via data PR #154). The first manifest fires on the "
        "next Saturday SF run — **2026-05-09** — and writes "
        "`s3://alpha-engine-research/rag/manifest/{date}.json` + "
        "`latest.json`.\n\n"
        "Until then this page renders with no data. Manual one-off run: "
        "`python -m rag.pipelines.emit_manifest --output-s3` from any "
        "host with the alpha-engine-lib RAG client installed and "
        "pgvector credentials in env."
    )
    st.stop()

# ---------------------------------------------------------------------------
# Top: totals + freshness
# ---------------------------------------------------------------------------

totals = manifest.get("totals") or {}
ingestion = manifest.get("ingestion") or {}
embedding = manifest.get("embedding") or {}
generated_at = manifest.get("generated_at", "—")

st.markdown("## Snapshot")
tc1, tc2, tc3, tc4 = st.columns(4)
tc1.metric("Documents", f"{int(totals.get('documents', 0)):,}")
tc2.metric("Chunks", f"{int(totals.get('chunks', 0)):,}")
tc3.metric("Tickers", f"{int(totals.get('tickers', 0)):,}")
last_ts = ingestion.get("last_run_ts") or "—"
if isinstance(last_ts, str) and last_ts != "—":
    try:
        last_dt = datetime.fromisoformat(last_ts.replace("Z", "+00:00"))
        delta = (datetime.now(timezone.utc) - last_dt).days
        tc4.metric("Last ingest", f"{delta}d ago", delta=last_dt.strftime("%Y-%m-%d"), delta_color="off")
    except Exception:
        tc4.metric("Last ingest", str(last_ts))
else:
    tc4.metric("Last ingest", "—")

st.caption(f"Manifest generated at {generated_at}")

st.divider()

# ---------------------------------------------------------------------------
# By source — doc_type rollup
# ---------------------------------------------------------------------------

st.markdown("## By source")
st.caption(
    "Per-document-type counts (documents · distinct tickers · chunks). "
    "Documents come from SEC EDGAR for filings (10-K, 10-Q, 8-K), "
    "Finnhub for earnings transcripts, and internal Research outputs "
    "for theses."
)

by_source = manifest.get("by_source") or {}
if by_source:
    rows = []
    for doc_type, vals in by_source.items():
        rows.append({
            "Source type": doc_type,
            "Documents": int(vals.get("documents", 0)),
            "Tickers": int(vals.get("tickers", 0)),
            "Chunks": int(vals.get("chunks", 0)),
        })
    src_df = pd.DataFrame(rows).sort_values("Documents", ascending=False).reset_index(drop=True)

    bs_col1, bs_col2 = st.columns([2, 3])
    with bs_col1:
        st.dataframe(src_df, use_container_width=True, hide_index=True)
    with bs_col2:
        bar = go.Figure()
        bar.add_trace(go.Bar(
            x=src_df["Source type"],
            y=src_df["Documents"],
            marker_color="#1a73e8",
            name="Documents",
        ))
        bar.add_trace(go.Bar(
            x=src_df["Source type"],
            y=src_df["Chunks"],
            marker_color="#5fa8f0",
            name="Chunks",
            yaxis="y2",
            opacity=0.55,
        ))
        bar.update_layout(
            height=280,
            margin=dict(l=10, r=10, t=20, b=20),
            paper_bgcolor="rgba(0,0,0,0)",
            plot_bgcolor="rgba(0,0,0,0)",
            yaxis=dict(title="Documents", gridcolor="rgba(255,255,255,0.05)"),
            yaxis2=dict(title="Chunks", overlaying="y", side="right", gridcolor="rgba(0,0,0,0)"),
            barmode="group",
            legend=dict(orientation="h", y=1.15),
        )
        st.plotly_chart(bar, use_container_width=True)
else:
    st.info("`by_source` not present in manifest.")

st.divider()

# ---------------------------------------------------------------------------
# Ticker coverage
# ---------------------------------------------------------------------------

st.markdown("## Ticker coverage")
st.caption(
    "How many tickers have at least one document, and the depth "
    "distribution. Per-ticker counts show as percentiles to keep the "
    "disclosure boundary intact — full per-ticker listings stay in "
    "pgvector."
)

coverage = manifest.get("by_ticker_coverage") or {}
if coverage:
    cv1, cv2, cv3, cv4 = st.columns(4)
    cv1.metric("Tickers with ≥1 doc", f"{int(coverage.get('tickers_with_any_doc', 0)):,}")
    cv2.metric("p25 docs/ticker", str(int(coverage.get("p25_docs_per_ticker", 0))))
    cv3.metric("p50 docs/ticker", str(int(coverage.get("p50_docs_per_ticker", 0))))
    cv4.metric("p75 docs/ticker", str(int(coverage.get("p75_docs_per_ticker", 0))))

    # Ticker coverage % vs total tracked universe (population target = 25; full
    # universe scan = ~900). This isn't in the manifest; render the manifest's
    # raw count and let the reader interpret depth via the percentiles.
    total_tickers_in_corpus = int(coverage.get("tickers_with_any_doc", 0))
    if total_tickers_in_corpus:
        st.caption(
            f"Distribution shape: half of covered tickers have ≤"
            f"{int(coverage.get('p50_docs_per_ticker', 0))} documents, "
            f"top quartile has ≥{int(coverage.get('p75_docs_per_ticker', 0))} "
            "documents. Bottom quartile (≤p25) is candidate for ingestion "
            "deepening if those tickers enter the population universe."
        )
else:
    st.info("`by_ticker_coverage` not present in manifest.")

st.divider()

# ---------------------------------------------------------------------------
# Embedding meta
# ---------------------------------------------------------------------------

st.markdown("## Embedding")
st.caption("Vector model + dimension used for chunk embeddings.")

ec1, ec2 = st.columns(2)
ec1.metric("Model", str(embedding.get("model", "—")))
ec2.metric("Dimension", str(embedding.get("dimension", "—")))

st.divider()

# ---------------------------------------------------------------------------
# Per-source ingestion freshness
# ---------------------------------------------------------------------------

st.markdown("## Ingestion freshness")
st.caption(
    "Per ingestion date × `doc_type`: documents (and chunks) landed that "
    "day. Drift between sources is normal — SEC filings come in on issuer "
    "cadence; earnings transcripts arrive within hours of call; theses "
    "regen weekly with the Saturday research run."
)

by_date_source = ingestion.get("by_date_source") or []
by_source_ts = ingestion.get("by_source_last_ts") or {}

if by_date_source:
    df_pivot_src = pd.DataFrame(by_date_source)
    # Cap to last 26 ingestion dates for legibility (~6 months at weekly cadence).
    distinct_dates = sorted(df_pivot_src["date"].unique(), reverse=True)
    cap = 26
    if len(distinct_dates) > cap:
        kept = set(distinct_dates[:cap])
        df_pivot_src = df_pivot_src[df_pivot_src["date"].isin(kept)]
        st.caption(
            f"Showing last {cap} ingestion dates of "
            f"{len(distinct_dates)} on record."
        )

    def _build_pivot(value_col: str) -> pd.DataFrame:
        pv = df_pivot_src.pivot_table(
            index="date",
            columns="doc_type",
            values=value_col,
            aggfunc="sum",
            fill_value=0,
        ).astype(int).sort_index(ascending=False)
        pv["Total"] = pv.sum(axis=1)
        totals = pv.sum(axis=0).to_frame().T
        totals.index = ["Total"]
        return pd.concat([pv, totals])

    tab_docs, tab_chunks = st.tabs(["Documents", "Chunks"])
    with tab_docs:
        st.dataframe(
            _build_pivot("documents").style.format("{:,}"),
            use_container_width=True,
        )
    with tab_chunks:
        st.dataframe(
            _build_pivot("chunks").style.format("{:,}"),
            use_container_width=True,
        )
elif by_source_ts:
    # Fallback for manifests pre-1.1.0 (no `by_date_source`). Renders the
    # legacy per-source last-ingest table so freshness still surfaces.
    st.caption(
        "Manifest predates schema 1.1.0 — rendering legacy last-ingest "
        "timestamps. Re-run `emit_manifest.py` to populate the date pivot."
    )
    ing_rows = []
    now = datetime.now(timezone.utc)
    for doc_type, ts_str in by_source_ts.items():
        try:
            dt = datetime.fromisoformat(str(ts_str).replace("Z", "+00:00"))
            age_days = (now - dt).days
            ing_rows.append({
                "Source type": doc_type,
                "Last ingest": dt.strftime("%Y-%m-%d %H:%M UTC"),
                "Age (days)": age_days,
            })
        except Exception:
            ing_rows.append({
                "Source type": doc_type,
                "Last ingest": str(ts_str),
                "Age (days)": "?",
            })
    ing_df = pd.DataFrame(ing_rows).sort_values("Age (days)").reset_index(drop=True)
    st.dataframe(ing_df, use_container_width=True, hide_index=True)
else:
    st.info("Per-source ingestion data not present in manifest.")

st.divider()
st.caption(
    "What the manifest deliberately excludes (disclosure boundary): "
    "per-ticker document lists, individual document titles, chunk text. "
    "Those stay in pgvector; queryable only via the agent stack during "
    "research runs."
)

