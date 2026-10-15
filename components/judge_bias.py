"""Judge-bias surface (config#1444 item 4).

The LLM-as-judge layer can score agent outputs with more than one judge
(a first pass, plus a stronger second opinion on borderline escalation). If
one judge systematically scores higher/lower than another on the same agents,
the rubric scores aren't comparable across judges. This aggregates the eval
rows into an (agent × grader) mean-score table + per-agent divergence so
systematic judge bias is visible.

**The grader is the model that SERVED the call, not the tier key**
(alpha-engine-config-I11484). ``judge_model`` (``claude-haiku-4-5`` /
``claude-sonnet-4-6``) names the tier, and since alpha-engine-config-I2997
both tiers on the sync rung are served by the router's ``low`` group. Grouping
by the tier key showed a "Haiku vs Sonnet" bias between two passes of the
same model. Rows carrying no served model are grouped under an explicit
``unrecorded (tier …)`` label rather than credited to the tier's namesake.

Pure ``judge_bias_summary`` (unit-tested) + a thin Streamlit ``render``.
Operates on eval rows shaped by ``loaders.eval_loader`` (one row per
(artifact, criterion) with ``judged_agent_id``, ``judge_model``,
``judge_served_model``, ``score``).
"""

from __future__ import annotations

from typing import Iterable

from loaders.judge_identity import grader_label


def _mean(xs: list[float]) -> float | None:
    return round(sum(xs) / len(xs), 3) if xs else None


def judge_bias_summary(records: Iterable[dict]) -> dict:
    """Aggregate eval rows into judge-bias structure.

    Returns::

        {
          "judges": [grader, ...],                 # sorted, present graders
          "overall": {grader: mean_score},         # systematic level per grader
          "per_agent": [
            {"agent": id, "means": {judge: mean}, "n": int, "divergence": float|None},
            ...                                     # divergence = max-min across judges
          ],
        }
    """
    by_agent_judge: dict[str, dict[str, list[float]]] = {}
    by_judge: dict[str, list[float]] = {}
    for r in records:
        score = r.get("score")
        if not isinstance(score, (int, float)) or score != score:  # skip None/NaN
            continue
        agent = r.get("judged_agent_id") or "—"
        judge = grader_label(r.get("judge_model"), r.get("judge_served_model"))
        by_agent_judge.setdefault(agent, {}).setdefault(judge, []).append(float(score))
        by_judge.setdefault(judge, []).append(float(score))

    judges = sorted(by_judge)
    overall = {j: _mean(v) for j, v in by_judge.items()}

    per_agent = []
    for agent in sorted(by_agent_judge):
        means = {j: _mean(scores) for j, scores in by_agent_judge[agent].items()}
        present = [m for m in means.values() if m is not None]
        divergence = round(max(present) - min(present), 3) if len(present) >= 2 else None
        n = sum(len(s) for s in by_agent_judge[agent].values())
        per_agent.append({"agent": agent, "means": means, "n": n, "divergence": divergence})

    # Most-divergent agents first (None divergence sinks to the bottom).
    per_agent.sort(key=lambda a: (a["divergence"] is None, -(a["divergence"] or 0.0)))
    return {"judges": judges, "overall": overall, "per_agent": per_agent}


def render(df) -> None:
    """Streamlit Judge-Bias tab. `df` is the eval DataFrame from eval_loader."""
    import streamlit as st

    st.subheader("Judge bias — score by (agent × served grader)")
    st.caption(
        "Mean rubric score per agent under each model that actually graded "
        "(the router's served model, not the judge tier name). A persistent "
        "gap between graders on the same agents means the scores aren't "
        "comparable across graders (config#1444, config#11484)."
    )
    if df is None or getattr(df, "empty", True):
        st.info("No eval rows in window.")
        return

    summary = judge_bias_summary(df.to_dict("records"))
    judges = summary["judges"]
    if len(judges) < 2:
        st.info(
            "Only one grader served this window's evals, so there is no "
            "cross-grader bias to measure. Both judge tiers can be served by "
            "the same model; the tier filter does not change that."
        )

    # Systematic level per judge.
    if summary["overall"]:
        cols = st.columns(len(summary["overall"]))
        for col, (judge, mean) in zip(cols, sorted(summary["overall"].items())):
            col.metric(f"{judge} mean", f"{mean:.2f}/5" if mean is not None else "—")

    # Per-agent table.
    import pandas as pd
    rows = []
    for a in summary["per_agent"]:
        row = {"Agent": a["agent"]}
        for j in judges:
            m = a["means"].get(j)
            row[j] = f"{m:.2f}" if m is not None else "—"
        row["Divergence"] = f"{a['divergence']:.2f}" if a["divergence"] is not None else "—"
        row["N"] = a["n"]
        rows.append(row)
    if rows:
        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
