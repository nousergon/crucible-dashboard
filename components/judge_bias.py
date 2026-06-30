"""Judge-bias surface (config#1444 item 4).

The LLM-as-judge layer scores agent outputs with more than one judge model
(e.g. Haiku for the first pass, Sonnet on borderline escalation). If one judge
systematically scores higher/lower than another on the same agents, the rubric
scores aren't comparable across judges. This aggregates the eval rows into a
(agent × judge_model) mean-score table + per-agent divergence so systematic
judge bias is visible.

Pure ``judge_bias_summary`` (unit-tested) + a thin Streamlit ``render``.
Operates on eval rows shaped by ``loaders.eval_loader`` (one row per
(artifact, criterion) with ``judged_agent_id``, ``judge_model``, ``score``).
"""

from __future__ import annotations

from typing import Iterable


def _mean(xs: list[float]) -> float | None:
    return round(sum(xs) / len(xs), 3) if xs else None


def judge_bias_summary(records: Iterable[dict]) -> dict:
    """Aggregate eval rows into judge-bias structure.

    Returns::

        {
          "judges": [judge_model, ...],            # sorted, present judges
          "overall": {judge_model: mean_score},    # systematic level per judge
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
        judge = r.get("judge_model") or "—"
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

    st.subheader("Judge bias — score by (agent × judge model)")
    st.caption(
        "Mean rubric score per agent under each judge model. A persistent gap "
        "between judges on the same agents means the scores aren't comparable "
        "across judges (config#1444)."
    )
    if df is None or getattr(df, "empty", True):
        st.info("No eval rows in window.")
        return
    if df["judge_model"].nunique() < 2:
        st.info(
            "Only one judge model in this window — set the judge filter to "
            "**both** to compare. Showing single-judge means."
        )

    summary = judge_bias_summary(df.to_dict("records"))
    judges = summary["judges"]

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
