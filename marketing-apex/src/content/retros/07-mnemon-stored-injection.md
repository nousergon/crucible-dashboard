---
title: 'Stored prompt injection — a memory system that flagged itself as malware'
date: '2026-05-17'
severity: 'P1'
domain: 'Security'
order: 7
summary: >-
  A memory server captured its own host's control markup as if it were
  ordinary conversation, replayed it into a later session, and watched a model
  correctly — and alarmingly — treat it as an active attack. The fix rewrote
  the system's entire trust model for recalled memory.
---

A long AI-assistant conversation started flagging its own recalled memories as prompt injection attempts, then escalated to warning that the user's prompts were being rewritten by malware. Nothing was actually compromised — but the model wasn't wrong to be suspicious, and figuring out why is what rewrote this memory system's security architecture.

**Date:** 2026-05-17 · **Severity:** P1 · **Resolution:** five-layer defense shipped across two releases (05-17, 05-18)

### Symptoms

A weekend-long conversation began treating its own recalled memories as an active prompt-injection attempt, and escalated to telling the user their prompts were being maliciously rewritten. No actual compromise had occurred — the model was reacting to something real in its context, just not the thing it thought.

### Detection

The behavior was visible directly in the conversation itself: a model that starts accusing its own tool results of being malware is not a subtle signal. The harder part was root-causing why a memory recall would ever look like an attack in the first place.

### Root cause

The session-extraction path that turns conversation transcripts into stored memories ran its capture pattern over the *raw* transcript — including the host application's own control-plane scaffolding, like system-reminder tags. One of the memory system's own "it appears that…" pattern-learning rules happened to match text from the very scaffolding around the incident's opening line, so that control markup got captured and stored as if it were a normal piece of conversation. Later, in a fresh session, that stored memory was recalled and replayed back into context — at which point a model reading what looked like live host control markup, appearing outside where such markup should legitimately appear, correctly treated it as an active injection attempt. The system had unintentionally manufactured its own attack payload out of faithfully-captured harness output.

### Fix

A five-layer defense, because no single layer is sufficient on its own: capture-time rejection is the actual root-cause fix — a detector now recognizes host control markup at the moment a memory would be written and refuses to store it, rather than trying to neutralize it later. Recall-time token defanging remains as pure defense-in-depth. Recalled context is wrapped in a spotlighting envelope — a nonce-fenced block that a stored memory cannot forge, since it can't predict the nonce. And hook-sourced, auto-captured memories carry a lower trust ceiling than a deliberately user-authored one, so an accidental capture can't outrank a real assertion. One hard limit is stated plainly rather than papered over: none of this can retroactively heal a conversation that already ingested the bad recall — only a fresh conversation is clean.

### Systemic improvement

The reframing that survived past the immediate fix: recalled memory is untrusted input being replayed into a privileged context, exactly like any other retrieval-augmented generation surface — never assumed safe just because the system itself produced it. That principle now governs every new recall path added to the system, not just the one that broke.
