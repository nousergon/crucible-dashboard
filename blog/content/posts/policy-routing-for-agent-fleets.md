---
title: "Keeping an Agent Fleet Consistent: Policy Routing with oiax"
date: 2026-08-04
description: "Neither injecting every rule nor trusting an agent to self-select works. oiax surfaces the governing policy on every prompt, before the agent decides anything."
tags: ["ai-engineering", "llm", "system-design"]
canonical_url: "https://nousergon.ai/blog/posts/policy-routing-for-agent-fleets/"
ShowToc: false
TocOpen: false
---

*Originally posted on [LinkedIn](https://lnkd.in/p/geaac3MF) on August 4, 2026.*

In my last post I introduced Nous Ergon, an applied AI lab. Since then, while building inside my multi-platform agentic workflows, I have been running head-first into a notable pain point: how to keep a fleet of agents doing things the same way?

Every agent does things slightly differently. Same task, different direction, different quality. As a solo operator supervising this fleet, I have a strong interest in making sure the agents are delivering a standardized, conforming set of deliverables — so I wrote my expectations down. That corpus is now 35 policy documents and well over 100,000 tokens.

There are two obvious ways to get these rules to an agent, and both of them fail. We could inject everything, but this bloats context and cascades into attention dilution and a shorter, more expensive session. We could also let the agent decide what to load. However, an agent that never loaded the governing policy looks exactly like an agent that loaded it and complied. Nothing anywhere shows the difference.

I've been building a third option: on every prompt, surface the policies that bear on it, before the agent decides anything. Selection is an embedding search over the prompt alongside plain keyword matching. That's **oiax** — "handle of a rudder" in ancient Greek — which I open sourced this week.

This is different from typical RAG (Retrieval-Augmented Generation) — if the agent is working on a prompt that falls within a relevant policy, the agent should be familiar with the entire policy, not just a small part of it. My auto-merge policy, for instance, says agents never merge my pull requests, and then names the few exceptions where they can. Retrieve a chunk, such as the rule without its exceptions, and the agent is now "obeying a rule" I don't actually have. So the unit is the whole document, never a fragment of it.

I'd love to know if you have any novel ideas on context management curation!

[github.com/nousergon/oiax](https://github.com/nousergon/oiax)
