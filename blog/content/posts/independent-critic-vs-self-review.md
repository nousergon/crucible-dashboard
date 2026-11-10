---
title: "Does a second AI reviewer make research better?"
date: 2026-11-10
tags: ["ai-engineering", "llm", "multi-agent", "evaluation"]
canonical_url: "https://nousergon.ai/blog/posts/independent-critic-vs-self-review/"
ShowToc: false
TocOpen: false
---

**TL;DR.** I tested whether an independent AI critic improves an agent's research more than an agent reviewing its own work. An agent wrote 9 short equity research answers on Deere (3 questions, each run 3 times). A separate judge agent flagged a material error in 3 of the 9 first drafts, all the same omission. By its assessment, self-review fixed 2 of the 3 and an independent critic fixed all 3. The judge also classified 13 of the critic's 41 complaints as invalid, yet the writer accepted 12 of them. For a task this size, self-review looks like the more efficient choice, though I'd want to test more questions before deciding whether the critic's extra fix justifies 22.5% more cost.

## Introduction

A common pattern in agent workflows is to have one agent write and an independent agent, with fresh context, review the work. I use it often, and it has felt like a valuable check. But a second agent adds cost, and it's not obvious the benefit comes from the reviewer being separate. The same agent reviewing its own work might catch as much for less.

That matters for anyone building agent pipelines: if self-review is as good, an independent critic is wasted spend. If the critic is better, skipping it means letting errors through. My goal is to generate responses that are verifiably correct, with evidence for every claim, in as few tokens as possible.

I tested this directly. Does an independent critic improve an agent's research more than the same agent reviewing its own work? I compared one agent alone with the same agent using self-review, and with an independent critic. This gives me a foundation to build on in subsequent multi-agent experiments.

## Setup

The task was to respond to three short equity research questions on what drove Deere & Company's results in different quarters, and how well the evidence supported management's story. Agents could only read filings, call transcripts and slides published on or before each question's earnings-release cutoff, and every number had to cite its exact source line.

I compared three setups starting from the same initial draft:

- **A, single agent.** The agent researches and writes an answer.
- **B, self-review.** The same agent, in the same conversation, reviews its answer and revises once.
- **C, independent critic.** A fresh agent with the same tools and sources, but none of the author's history, reviews the answer. The author revises once.

Each agent responded to one of three questions about Deere & Company, each set at a past earnings date:

- *Demand, Q4 FY2023 (Nov 2023)*: Equipment operations net sales fell about 4% while net income rose. Break the sales change down by segment into price, currency and volume, judge which drivers mattered most, and explain what that means for the FY2024 outlook.
- *Margins, Q2 FY2026 (May 2026)*: Equipment operations operating margin was 16.9%, and full-year net income guidance was unchanged. How much of the margin is one-off versus underlying, how does it differ by segment, and does the unchanged guidance imply improvement or deterioration?
- *Recovery, Q1 FY2026 (Feb 2026)*: Equipment operations net sales rose 18% while net income fell 25%, and management called 2026 the bottom of the cycle. How strongly does the evidence support a real demand recovery versus alternatives such as an easy comparison, currency, production timing or government support?

For each question, the agent had access to 4 to 5 source documents (the earnings release, call transcripts and slides), at about 140 to 175 KB of text. Answers were capped at 600 words with a 500-word target, and the graded summary ran 225 to 337 words. That's roughly one analyst's short earnings note, small enough to fit into a single agent's context.

All setups used the same model (Opus 5.5) and tools. B and C received the same instructions and had the same limits on time, model calls and tool calls. The difference was context: for self-review, the agent continued in the same conversation it used to write the answer. The independent critic started a new conversation with the question and draft, and access to the same sources and tools.

Each question ran three times, resulting in 9 first drafts and 27 final answers (3 setups × 3 questions × 3 runs). The repetitions probed run-to-run variation; they did not add independent questions. The same omission recurred in all three initial answers to one question. A separate judge agent scored every answer against the sources and a reference checklist without knowing which setup wrote it. The evaluation plan was frozen prior to the final run.

## Results

|  | A: alone | B: self-review | C: independent critic |
| --- | :-: | :-: | :-: |
| Answers the judge flagged as materially defective (of 9) | 3 | 1 | 0 |
| Judge preferred it over A | | 9 of 9 | 9 of 9 |
| Estimated cost for 9 answers (API list price) | $10.34 | $16.59 | $20.32 |

Costs are estimates from recorded usage at API list prices, not charges paid. Each setup includes its initial draft and excludes evaluation.

**Both forms of review improved the observed results.** The judge detected fewer material errors and preferred the reviewed answer over the original in every comparison.

**The critic's edge is one answer.** Every judge-labelled material error concerned the same omission on the recovery question (Q1 FY2026). Deere's net income fell 24.5% while pre-tax income fell only 6.6%, mostly because the prior-year quarter had $163m of one-off tax benefits. All three first drafts missed this and overstated the operating decline. Both reviewers flagged the tax issue in all three runs. After revision, the judge found it resolved in all three critic-reviewed answers, but only two of the three self-reviewed answers. This one-answer advantage is too thin to call a winner, especially at 22.5% higher estimated cost than self-review.

**The writer deferred to bad feedback.** The judge checked every reviewer complaint against the draft and the sources: whether the problem was actually in the draft, and whether the sources back this up. The judge classified 13 of the critic's 41 complaints as invalid. The writer had to accept or reject each complaint, and it accepted 12 of those 13, despite being warned that feedback could be wrong. Self-review raised 5 such complaints out of 38, and the writer accepted all 5.

The judge detected no new material errors after revision. On harder material, a critic whose complaints often don't hold up could make answers worse.

## Discussion

One possible explanation is task size. The questions required judgment, but each had a small source packet and a short answer that fit within one agent's context, which may have left less for a fresh reviewer to add. I'd expect an independent critic to have more to add on bigger tasks, though I haven't tested that yet.

The hardest part was validation, not running the agents. I weighed human review against an LLM judge and how to combine them, and built an evidence-review tool so a person could trace each claim to its source and recompute each number. Without it, checking every result, and rechecking after every rehearsal rerun, isn't feasible for one person.

This study covers one company and three questions. The judge came from the same model family as the writer, so the two could share blind spots. I checked only 3 of the 27 answers by hand, none from the question driving the material-error results. My manual review also flagged an unsupported chart explanation that the judge missed; whether it met the material-error threshold remained unresolved. Next time, I would check the decisive cases and a random sample of answers the judge marked clean, while keeping the human reviewer blind to those labels.

This experiment tested review of a shared draft, not whether independent agents discover better competing hypotheses. That is a separate question for a future experiment.

## Conclusion

Based on this experiment, self-review looks like the more efficient choice for a task of this size. The critic fixed one more error, but at 22.5% more cost. I'd want to see that benefit hold up across more questions before drawing a stronger conclusion. Whether critics help more on larger tasks, or whether independent agents discover explanations a single researcher misses, remains untested. Whichever setup you use, reviewer feedback needs evidence and verification before it changes the answer.

Next I want to:

- Use a reviewer and judge from a different model family.
- Test other validation setups, such as agents that check each claim against its source before it reaches the author.
- Scale complexity from low to high and see how each setup holds up as tasks outgrow one agent's context.
