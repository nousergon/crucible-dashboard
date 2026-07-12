"""Crucible results surface — experiment-scoped view-model layer.

The product surface for Crucible (config#1957): renders ONE experiment's
results (v1: the stock Reference Rate experiment) from versioned S3
artifacts. Pure view-model builders live in ``view_model.py`` — no
Streamlit imports — so the same layer can later back the public
crucible.nousergon.ai/dash skin unchanged (plan §4.1 one-renderer-two-skins,
``private-docs/crucible_ux_output_plan_260708.md`` in alpha-engine-config).

Doctrine: derive, don't transcribe — builders reshape artifact values for
display and never compute a statistic; absent artifacts surface as honest
"absent" rows (mirroring the report card's own N/A discipline), never as
silent omissions.
"""
