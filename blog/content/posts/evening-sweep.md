---
title: "The Evening Sweep: One Coordinator for an Overnight Agent Fleet"
date: 2026-11-02
description: "Agents working my backlog overnight left pages of PRs nobody owned. A single Claude coordinator, the Director, now plans each night's sweep, runs it, and reports back each morning."
tags: ["ai-engineering", "llm", "multi-agent", "system-design"]
canonical_url: "https://nousergon.ai/blog/posts/evening-sweep/"
ShowToc: false
TocOpen: false
---

This past summer, I set agents to work through my backlog each evening and ended up with pages of open PRs I didn't know what to do with.

The program assigned batches of issues by priority and selected agents based on complexity. But whenever an agent encountered ambiguity, it would leave a comment and move on. I tried updating the label and categorization schemes, but they were all quite brittle and the unresolved work kept accumulating. Nothing in this process owned the outcome, so I never felt in control.

Over the past month, I have been testing a Claude coordinator that is responsible for managing this daily "evening sweep". The coordinator, which I call the Director, runs in a persistent Claude Projects thread. Each afternoon, it proposes a plan based on the priorities I've set. A nightly thread then executes the plan, spinning up the agreed number of agents to work through the issues overnight.

Each morning, I am provided an updated report of what was accomplished, the extent to which my various work streams are on schedule, what now needs to be addressed by me, and various metrics on the run. I have also set up rules defining which PRs require my approval and which can merge automatically, which the Director takes into account.

The Director now manages all my open PRs, telling me which are ready to merge and which are blocked. The daily goal is no more than 20 PRs open at any one time; when it's over, it explains why.

Using a single coordinator has been a substantial improvement over the initial version. Now when I need to make a change, such as changing priorities or adjusting usage available for the sweep, I discuss with the Director and it handles the rest.

There are a few improvements to this setup that I'm now working on. This includes incorporating agents from different model families, such as Codex, Grok, Composer and DeepSeek, all managed under a single Claude coordinator. I'm also working on implementing validation to check the work done overnight by a model from a different model family than the author. I'm trialling Codex to validate my Claude-based workflow at the moment — on its first night, Claude checked the evidence for each of Codex's 11 flags and confirmed 10 as real bugs.
