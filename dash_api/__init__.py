"""crucible-dash-api — read-only JSON API over the Crucible results view-model.

Phase 9-B of the ratified plan (config#1973): the data layer for the
Metron-grade Next.js /dash frontend (mirrors the metron/api ↔ metron/web
split). Every endpoint is a veneer over ``results.view_model`` — the API
computes nothing, exactly as the Streamlit skin computes nothing.

Serves on 127.0.0.1:8506 (box port survey 2026-07-08: 22/80/443/3000/3001/
8000/8501–8505/8530 taken) — internal-only; the Next.js server consumes it
same-box. Loaders are the existing ``loaders.s3_loader`` functions: their
``st.cache_data`` decorators degrade gracefully outside a Streamlit runtime
(in-process memory cache with the same TTLs), so the S3 read layer stays
single-sourced instead of forking a second reader stack.
"""
