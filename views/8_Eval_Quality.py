"""Eval Quality page — Are the LLM agents producing good output?

Surfaces the LLM-as-judge eval corpus (PR 2-4 of ROADMAP §1617) so
quality regressions are visible weeks before they show up in alpha.

  • Trend tab       — per-agent line charts × criterion, time-series
                      view of judge scores. Toggle Haiku-vs-Sonnet to
                      spot tier disagreement (§1627 calibration).
  • Versions tab    — prompt-version → quality-score correlation
                      (§1633). Shows whether a rubric or agent prompt
                      bump moved scores up, down, or sideways.

Eval is observability per §1635 — this page names regressions; it
does not gate any deploy.
"""

import os
import sys
from datetime import date, timedelta

import pandas as pd
import plotly.express as px
import streamlit as st

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from loaders.eval_loader import (
    load_eval_artifacts,
    load_judged_artifact,
    load_recent_eval_artifacts_for_review,
    load_recent_evals_for_spotcheck,
    load_reviewed_ids,
    load_rubric_text,
    save_calibration_review,
    save_spotcheck_flag,
)
from loaders.s3_loader import load_latest_provenance_grounding


st.title("Eval Quality")
st.caption(
    "LLM-as-judge rubric scores per agent + criterion. "
    "Eval is observability, not a gate."
)


# ---------------------------------------------------------------------------
# Sidebar controls
# ---------------------------------------------------------------------------

with st.sidebar:
    st.subheader("Filters")
    today = date.today()
    default_start = today - timedelta(days=84)  # ~12 weeks
    start_date = st.date_input("Start date", value=default_start)
    end_date = st.date_input("End date", value=today)
    judge_filter = st.selectbox(
        "Judge tier",
        options=["both", "claude-haiku-4-5", "claude-sonnet-4-6"],
        index=0,
    )

df = load_eval_artifacts(start_date=start_date, end_date=end_date)

if df.empty:
    st.info(
        "No eval artifacts under "
        "`s3://alpha-engine-research/decision_artifacts/_eval/` for the "
        "selected window. The eval pipeline (PR 2-3 of the LLM-as-judge "
        "workstream) writes here every Saturday after the Research Lambda."
    )
    st.stop()

if judge_filter != "both":
    df = df[df["judge_model"] == judge_filter]

if df.empty:
    st.warning(f"No eval artifacts for judge model `{judge_filter}` in window.")
    st.stop()


# ---------------------------------------------------------------------------
# Tabs
# ---------------------------------------------------------------------------

(
    tab_trend, tab_bias, tab_versions, tab_provenance,
    tab_spotcheck, tab_calibrate, tab_data,
) = st.tabs(
    ["Trend", "Judge Bias", "Versions", "Provenance", "Spot-check", "Calibrate", "Data"]
)


# ── Judge Bias tab (config#1444 item 4) ───────────────────────────────────

with tab_bias:
    from components import judge_bias
    judge_bias.render(df)


# ── Trend tab ─────────────────────────────────────────────────────────────


with tab_trend:
    st.subheader("Score trend per agent")
    st.caption(
        "Each line is one rubric criterion. Per-artifact escalation in "
        "`evals/orchestrator.py` triggers a Sonnet pass when any Haiku "
        "score < 3 — that's the borderline-recheck signal worth watching."
    )

    agents = sorted(df["judged_agent_id"].unique())
    selected_agents = st.multiselect(
        "Agents", options=agents, default=agents,
    )
    sub = df[df["judged_agent_id"].isin(selected_agents)]

    if sub.empty:
        st.warning("No data for the selected agents.")
    else:
        for agent in selected_agents:
            agent_df = sub[sub["judged_agent_id"] == agent]
            if agent_df.empty:
                continue
            fig = px.line(
                agent_df,
                x="eval_date",
                y="score",
                color="criterion",
                line_dash="judge_model" if judge_filter == "both" else None,
                markers=True,
                title=f"{agent}",
                hover_data=["judge_model", "rubric_version", "reasoning"],
            )
            fig.update_yaxes(range=[0.5, 5.5], dtick=1)
            # Visual reference at the 4-week-mean alarm threshold.
            fig.add_hline(
                y=3.0, line_dash="dash", line_color="red",
                annotation_text="alarm threshold",
                annotation_position="bottom right",
            )
            st.plotly_chart(fig, use_container_width=True)


# ── Versions tab ──────────────────────────────────────────────────────────


with tab_versions:
    st.subheader("Prompt-version → quality-score correlation")
    st.caption(
        "Did a rubric or prompt bump move scores? Box plot of scores grouped "
        "by `rubric_version` per (agent, criterion). A version that drops the "
        "median worth investigating against the prompt diff."
    )

    agents_v = sorted(df["judged_agent_id"].unique())
    agent_pick = st.selectbox(
        "Agent", options=agents_v,
        index=0 if agents_v else None,
        key="version_agent_pick",
    )
    agent_df_v = df[df["judged_agent_id"] == agent_pick]
    if agent_df_v.empty:
        st.warning("No data for the selected agent.")
    else:
        # rubric_version uniqueness is the input to the correlation —
        # if there's only one version captured we say so.
        n_versions = agent_df_v["rubric_version"].nunique()
        if n_versions <= 1:
            st.info(
                f"Only one rubric version (`{agent_df_v['rubric_version'].iloc[0]}`) "
                f"observed for {agent_pick}. Bump the rubric to compare versions."
            )
        else:
            fig = px.box(
                agent_df_v,
                x="rubric_version",
                y="score",
                color="criterion",
                points="all",
                title=f"{agent_pick} — score distribution by rubric version",
            )
            fig.update_yaxes(range=[0.5, 5.5], dtick=1)
            st.plotly_chart(fig, use_container_width=True)


# ── Provenance tab ────────────────────────────────────────────────────────


with tab_provenance:
    st.subheader("Per-agent tool-call + input-trace metrics")
    st.caption(
        "Fourth leg of the agent-justification stack. Sourced from "
        "`s3://alpha-engine-research/backtest/{date}/provenance_grounding.json` "
        "emitted by the backtester evaluator. Detects agents emitting "
        "confident output without consulting tools (hallucination signal) "
        "or with collapsed tool-call distributions (rule-equivalence signal)."
    )

    prov = load_latest_provenance_grounding()
    if prov is None or prov.get("status") != "ok":
        status = (prov or {}).get("status", "missing")
        st.info(
            f"No provenance_grounding artifact available (status={status}). "
            "First emission lands on the next Saturday SF run after "
            "alpha-engine-backtester#148 deploys."
        )
    else:
        run_date = prov.get("most_recent_sf_date") or prov.get("_run_date")
        st.caption(f"Most recent Saturday SF: **{run_date}**")

        per_agent = prov.get("per_agent") or {}
        if not per_agent:
            st.info("No agent metrics for the most recent Saturday.")
        else:
            metric_rows = []
            for agent_id, m in sorted(per_agent.items()):
                metric_rows.append({
                    "agent_id": agent_id,
                    "n_artifacts": m.get("n_artifacts", 0),
                    "mean_tool_calls": m.get("mean_n_tool_calls", 0),
                    "distinct_tools": m.get("mean_n_distinct_tools", 0),
                    "pct_zero_call_outputs": m.get("pct_zero_call_outputs", 0),
                    "input_consumption": m.get("mean_input_consumption_ratio", 0),
                })
            metrics_df = pd.DataFrame(metric_rows)

            st.dataframe(
                metrics_df,
                use_container_width=True,
                hide_index=True,
                column_config={
                    "pct_zero_call_outputs": st.column_config.NumberColumn(
                        "% zero-call outputs",
                        help="Fraction of outputs emitted with zero tool calls. "
                             "For tool-equipped agents (macro + sector_team), "
                             "non-zero is a hallucination signal.",
                        format="%.1f%%",
                    ),
                    "input_consumption": st.column_config.NumberColumn(
                        "input consumption",
                        help="Fraction of input_data_snapshot top-level fields "
                             "referenced in agent_output prose. Substring match.",
                        format="%.2f",
                    ),
                    "mean_tool_calls": st.column_config.NumberColumn(
                        "mean tool calls",
                        format="%.1f",
                    ),
                    "distinct_tools": st.column_config.NumberColumn(
                        "distinct tools",
                        format="%.1f",
                    ),
                },
            )

            alarms = prov.get("tool_equipped_alarms") or []
            if alarms:
                st.error(
                    f"Tool-equipped agent zero-call alarm: **{', '.join(alarms)}**. "
                    "These agents emitted output without consulting any tools — "
                    "investigate against the agent's decision_artifact for the run."
                )

            # Rolling — show per-agent trend if multi-Saturday data exists
            rolling = (prov.get("rolling") or {}).get("per_agent") or {}
            if rolling:
                st.markdown("##### Rolling per-agent (8-week window)")
                rolling_rows = [
                    {
                        "agent_id": agent_id,
                        "n_saturdays": m.get("n_saturdays", 0),
                        "mean_pct_zero": m.get("mean_pct_zero_call_outputs", 0),
                        "mean_input_consumption": m.get(
                            "mean_input_consumption_ratio", 0,
                        ),
                        "distinct_tools_total": m.get("n_distinct_tools", 0),
                    }
                    for agent_id, m in sorted(rolling.items())
                ]
                st.dataframe(
                    pd.DataFrame(rolling_rows),
                    use_container_width=True,
                    hide_index=True,
                )


# ── Spot-check tab (ROADMAP L480 2026-05-29 re-scope — PRIMARY human surface) ──
#
# Read-only weekly transparency pass. For each judge call, render WHAT
# THE JUDGE SAW (the judged agent's output + input snapshot, hydrated
# via `judged_artifact_s3_key`) beside WHAT THE JUDGE SAID (per-dimension
# scores + reasoning). No blind scoring — eyeball, don't grade. The
# optional 👍/👎 captures the rare "this judge call is wrong" as a
# flagged exemplar for the outcome-IC study. Blind-κ (Calibrate tab) is
# now an optional deep-dive, not the primary obligation.


with tab_spotcheck:
    st.subheader("Judge spot-check")
    st.caption(
        "Read-only weekly transparency pass over recent LLM-as-judge "
        "calls. See **what the judge saw** (the agent's output + input) "
        "next to **what the judge said** (scores + reasoning) — and "
        "eyeball whether the verdict is reasonable. No scoring required. "
        "Hit 👍/👎 only on the rare call that looks clearly right or "
        "wrong; that flags an exemplar for the outcome-IC study. "
        "ROADMAP L480 (2026-05-29 re-scope)."
    )

    sc_cols = st.columns([1, 1, 2])
    sc_n = sc_cols[0].number_input(
        "How many", min_value=1, max_value=30, value=8, step=1,
        help="Recent judge calls to surface (newest date first).",
        key="sc_n",
    )
    sc_lookback = sc_cols[1].number_input(
        "Lookback days", min_value=7, max_value=180, value=30, step=7,
        help="How far back to draw recent judge calls from.",
        key="sc_lookback",
    )
    if sc_cols[2].button("🔄 Refresh", key="sc_refresh", help="Re-poll S3."):
        st.cache_data.clear()
        st.rerun()

    sc_batch = load_recent_evals_for_spotcheck(
        n=int(sc_n), lookback_days=int(sc_lookback),
    )

    if not sc_batch:
        st.info(
            f"No judge calls in the last {sc_lookback}d. "
            "Refresh after the next Saturday SF Research cycle."
        )
    else:
        st.caption(
            f"**{len(sc_batch)}** recent judge call(s), newest first "
            "(borderline calls — scores nearest the rubric midpoint — "
            "surface first within a date)."
        )

        for _i, art in enumerate(sc_batch):
            sid = art["_review_id"]
            # _review_id is not guaranteed unique within a batch (two reviews can
            # share date/thesis/model), which collides Streamlit widget keys. Use
            # a batch-index-suffixed key for widgets; keep `sid` for the record.
            wkey = f"{sid}__{_i}"
            agent_id = art.get("judged_agent_id", "—")
            rubric_id = art.get("rubric_id", "—")
            rubric_version = art.get("rubric_version", "—")
            judge_model = art.get("judge_model", "—")
            eval_date = art["_eval_date"]
            dim_scores = art.get("dimension_scores") or []
            overall_reasoning = art.get("overall_reasoning", "")
            uncertainty = art.get("_uncertainty", float("inf"))
            mean_score = (
                sum(float(d.get("score")) for d in dim_scores if d.get("score") is not None)
                / max(1, len([d for d in dim_scores if d.get("score") is not None]))
            ) if dim_scores else None
            mean_label = f"{mean_score:.1f}" if mean_score is not None else "—"

            with st.expander(
                f"**{agent_id}** · {eval_date} · `{rubric_id}` v{rubric_version} "
                f"· judge `{judge_model}` · mean score {mean_label}/5",
                expanded=False,
            ):
                left, right = st.columns(2)

                # ── What the judge SAID ──
                with left:
                    st.markdown("**What the judge said**")
                    for dim in dim_scores:
                        dim_name = dim.get("dimension", "")
                        score = dim.get("score")
                        reasoning = dim.get("reasoning", "")
                        st.markdown(f"**`{dim_name}`** → **{score}/5**")
                        st.caption(reasoning or "_(no reasoning)_")
                    if overall_reasoning:
                        st.markdown("**Overall**")
                        st.caption(overall_reasoning)

                # ── What the judge SAW ──
                with right:
                    st.markdown("**What the judge saw**")
                    judged = load_judged_artifact(art.get("judged_artifact_s3_key"))
                    if judged is None:
                        st.caption(
                            "_Judged artifact unavailable "
                            "(`judged_artifact_s3_key` missing or unfetchable)._"
                        )
                    else:
                        agent_output = judged.get("agent_output")
                        input_snapshot = judged.get("input_data_snapshot")
                        if agent_output is not None:
                            st.markdown("_Agent output (judged):_")
                            if isinstance(agent_output, (dict, list)):
                                st.json(agent_output, expanded=False)
                            else:
                                st.code(str(agent_output))
                        if input_snapshot is not None:
                            st.markdown("_Input snapshot (what the agent saw):_")
                            if isinstance(input_snapshot, (dict, list)):
                                st.json(input_snapshot, expanded=False)
                            else:
                                st.code(str(input_snapshot))
                        if agent_output is None and input_snapshot is None:
                            st.caption("_Judged artifact has no agent_output / input snapshot._")

                # ── Optional one-click verdict ──
                st.divider()
                v_cols = st.columns([1, 1, 4])
                note = v_cols[2].text_input(
                    "Note (optional)", key=f"sc_note__{wkey}",
                    placeholder="Only if flagging — what's right/wrong about this call?",
                )

                def _flag(verdict: str, _sid=sid, _art=art, _wkey=wkey, _note_key=f"sc_note__{wkey}"):
                    rec = {
                        "spotcheck_id": _sid,
                        "eval_date": _art["_eval_date"],
                        "judged_agent_id": _art.get("judged_agent_id"),
                        "run_id": _art.get("run_id"),
                        "rubric_id": _art.get("rubric_id"),
                        "judge_model": _art.get("judge_model"),
                        "verdict": verdict,
                        "note": st.session_state.get(_note_key) or None,
                        "source_eval_s3_key": _art.get("_s3_key"),
                        "reviewer": "operator",
                    }
                    if save_spotcheck_flag(rec):
                        st.session_state[f"sc_flagged__{_wkey}"] = verdict
                        st.cache_data.clear()
                        st.rerun()
                    else:
                        st.error("Flag save failed — check CloudWatch / S3 perms.")

                already = st.session_state.get(f"sc_flagged__{wkey}")
                if already:
                    st.success(f"✓ Flagged: {already}")
                else:
                    if v_cols[0].button("👍 Looks right", key=f"sc_up__{wkey}"):
                        _flag("looks_right")
                    if v_cols[1].button("👎 Looks wrong", key=f"sc_down__{wkey}"):
                        _flag("looks_wrong")


# ── Calibrate tab (ROADMAP L480, simplified 2026-07-13 per Brian) ─────────
#
# Single-step blind review: rubric anchors + agent output + a 1-5 rating
# per dimension, one submit, done. No reveal/revise step, no batch-size or
# lookback knobs — those add operator decisions that don't change the
# metric (κ(blind, llm) only needs blind_score paired with the already-
# known llm_score) and Brian explicitly asked for zero extra nonsense.
# Fixed daily batch of 5, active-sampling-ranked (same heuristic as
# before — band-midpoint distance, highest-information cases first).
# Submitted reviews persist to
# `decision_artifacts/_calibration/{today}/reviews.jsonl`.


_CALIBRATE_BATCH_SIZE = 5
_CALIBRATE_LOOKBACK_DAYS = 30


with tab_calibrate:
    st.subheader("Judge Calibration Review")
    st.caption(
        "Score each dimension 1-5 against the rubric anchors below, blind "
        "of the LLM judge's verdict. That's it — no reveal step, no follow-up."
    )

    reviewed_ids = load_reviewed_ids()
    batch = load_recent_eval_artifacts_for_review(
        n=_CALIBRATE_BATCH_SIZE,
        lookback_days=_CALIBRATE_LOOKBACK_DAYS,
        reviewed_ids=tuple(reviewed_ids),
    )

    if not batch:
        st.info(
            "No unreviewed eval artifacts right now — check back after the "
            "next Saturday SF Research cycle."
        )
    else:
        for art in batch:
            rid = art["_review_id"]
            agent_id = art.get("judged_agent_id", "—")
            rubric_id = art.get("rubric_id", "—")
            rubric_version = art.get("rubric_version", "—")
            judge_model = art.get("judge_model", "—")
            eval_date = art["_eval_date"]
            dim_scores = art.get("dimension_scores") or []

            submitted_key = f"cal_submitted__{rid}"
            if st.session_state.get(submitted_key):
                continue  # drops off-screen immediately after submit

            with st.container(border=True):
                st.markdown(f"**{agent_id}** · {eval_date}")

                rubric_text = load_rubric_text(rubric_id)
                with st.expander(f"Rubric — `{rubric_id}` v{rubric_version}", expanded=True):
                    if rubric_text:
                        st.text(rubric_text)
                    else:
                        st.caption("_Rubric text unavailable — scoring from dimension names only._")

                judged = load_judged_artifact(art.get("judged_artifact_s3_key"))
                with st.expander("Agent output (what's being judged)", expanded=True):
                    if judged is None:
                        st.caption(
                            "_Judged artifact unavailable "
                            "(`judged_artifact_s3_key` missing or unfetchable)._"
                        )
                    else:
                        agent_output = judged.get("agent_output")
                        input_snapshot = judged.get("input_data_snapshot")
                        if agent_output is not None:
                            st.markdown("_Agent output (judged):_")
                            if isinstance(agent_output, (dict, list)):
                                st.json(agent_output, expanded=False)
                            else:
                                st.code(str(agent_output))
                        if input_snapshot is not None:
                            st.markdown("_Input snapshot (what the agent saw):_")
                            if isinstance(input_snapshot, (dict, list)):
                                st.json(input_snapshot, expanded=False)
                            else:
                                st.code(str(input_snapshot))
                        if agent_output is None and input_snapshot is None:
                            st.caption("_Judged artifact has no agent_output / input snapshot._")

                with st.form(key=f"cal_form__{rid}"):
                    blind_scores: dict[str, int] = {}
                    for dim in dim_scores:
                        dim_name = dim.get("dimension", "")
                        blind_scores[dim_name] = st.slider(
                            f"`{dim_name}`", min_value=1, max_value=5, value=3, step=1,
                            key=f"cal_slider__{rid}__{dim_name}",
                        )
                    if st.form_submit_button("✓ Submit"):
                        per_dimension = [
                            {
                                "dimension": dim.get("dimension", ""),
                                "llm_score": dim.get("score"),
                                "blind_score": blind_scores.get(dim.get("dimension", "")),
                            }
                            for dim in dim_scores
                        ]
                        review_record = {
                            "review_id": rid,
                            "eval_date": eval_date,
                            "judged_agent_id": agent_id,
                            "run_id": art.get("run_id"),
                            "rubric_id": rubric_id,
                            "rubric_version": rubric_version,
                            "judge_model": judge_model,
                            "reviewer": "operator",
                            "per_dimension": per_dimension,
                            "source_eval_s3_key": art.get("_s3_key"),
                        }
                        if save_calibration_review(review_record):
                            st.session_state[submitted_key] = True
                            st.cache_data.clear()
                            st.rerun()
                        else:
                            st.error("Save failed — check Cloudwatch logs / S3 perms.")

    if reviewed_ids:
        st.caption(f"{len(reviewed_ids)} review(s) submitted so far.")

# ── Data tab ──────────────────────────────────────────────────────────────


with tab_data:
    st.subheader("Raw eval rows")
    st.caption(
        "One row per (artifact, dimension). Use the search box to filter "
        "by reasoning text — useful when an alarm fires and you want to "
        "find the artifact-level rationale that drove the regression."
    )

    search = st.text_input("Filter reasoning (case-insensitive)", value="")
    table = df.copy()
    if search:
        mask = (
            table["reasoning"].str.contains(search, case=False, na=False)
            | table["overall_reasoning"].str.contains(search, case=False, na=False)
        )
        table = table[mask]

    st.dataframe(
        table[[
            "eval_date", "judged_agent_id", "criterion", "score",
            "judge_model", "rubric_version", "reasoning",
        ]],
        use_container_width=True,
        hide_index=True,
    )
    st.caption(f"{len(table)} rows • {df['run_id'].nunique()} runs in window")
