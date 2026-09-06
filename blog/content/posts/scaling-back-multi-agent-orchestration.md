---
title: "I Deleted My Multi-Agent Research System"
date: 2026-09-01
description: "A multi-agent investment research pipeline lost to a much simpler quantitative approach. What replaced it is a champion/challenger process where every arm, including the incumbent, is re-scored every week."
tags: ["ai-engineering", "llm", "multi-agent", "system-design"]
canonical_url: "https://nousergon.ai/blog/posts/scaling-back-multi-agent-orchestration/"
ShowToc: false
TocOpen: false
---

*Originally posted on [LinkedIn](https://www.linkedin.com/posts/brian-c-mcmahon_earlier-this-year-i-built-a-multi-agent-system-activity-7500545725181644800-Idvb) on September 1, 2026.*

Earlier this year I built a multi-agent system for investment research. Several agents, each with a role, coordinated to produce a view on a set of stocks. I removed it in July.

The system was complex and expensive to run, and I could not test it properly. You can't write an assertion against a non-deterministic graph, so I had no reliable way of knowing whether the extra complexity was doing anything useful. When I eventually measured it against a much simpler quantitative approach, the simpler approach came out ahead.

I decided to rebuild from first principles, and to change how these decisions get made in the first place.

Each decision point now runs as an arm in a champion/challenger process. Every week each arm is scored on the same yardstick, over the longest window where every arm has data. The incumbent gets re-scored along with the challengers. Without that you end up comparing a new model against a stale copy of the old one.

I then went back to the bottom of the ladder. No agent at all to start, with a single agent preparing research entered as a challenger against it. There is a minimum number of weeks before a verdict counts, and until then the result is recorded as "not enough evidence." The contest is live and no arm has enough paired observations to call it yet.

Where have you seen multiple agents genuinely beat a single agent, and how did you establish it?
