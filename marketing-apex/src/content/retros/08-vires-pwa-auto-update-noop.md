---
title: '"Configured" is not "working" — the PWA that never told open tabs to reload'
date: '2026-07-08'
severity: 'P1'
domain: 'Client reliability'
order: 8
summary: >-
  A PWA plugin was set to auto-update from day one, but the app never wired up
  the piece that actually tells an open tab to reload. Every deploy shipped
  correctly to the server; installed clients could keep running the old
  bundle indefinitely.
---

The deploy pipeline was perfect. Every merge built cleanly, every new bundle landed exactly where it should on the server. And an already-installed copy of the app could still be running code from months earlier, with no error, no warning, and no way to know. The gap wasn't in what shipped — it was in whether anything ever told the client to go get it.

**Date:** 2026-07-08 · **Severity:** P1 · **Resolution:** same-day fix

### Symptoms

There was no user-visible crash or error — the app worked, just on a stale bundle. The mismatch was only visible by comparing what a running installed instance was actually executing against what the latest deploy had shipped.

### Detection

Found by deliberately building the current merge commit locally and comparing its bundle hash against what the production server was actually serving to a client. The server-side artifact was correct; an already-running client's bundle hash didn't match it — the gap was entirely on the client side, in a place CI has no visibility into.

### Root cause

The PWA plugin had been configured with `autoUpdate` mode since the app's first day — which reads as "this is handled." But `autoUpdate` only enables the *mechanism*; using it requires the app to import and call the plugin's own register-and-reload module, and that import had never been added. Without it, the build silently fell back to the plugin's bare-minimum default: register a service worker, and nothing else. Each new deploy's service worker did correctly take control of the origin — but nothing ever told an already-open tab that a new version existed or that it should reload. An installed PWA could run the version it was installed with indefinitely, silently drifting further from production with every subsequent deploy, for as long as the tab or app stayed open.

### Fix

Wired in the plugin's real registration path — detecting an available update and prompting (or forcing, depending on context) a reload — so `autoUpdate` actually does what its name says instead of silently falling back to a no-op default. A companion service-worker-independent staleness signal was added afterward as a second, structurally different way for a client to learn it's running an old build, so the update path doesn't depend on a single mechanism working correctly.

### Systemic improvement

The generalizable lesson: a configuration flag that looks like it enables a behavior is not evidence the behavior is wired up end-to-end — "configured" and "working" are different claims, and only one of them is checkable by reading the config. The fix that actually closed this gap was empirical (compare a running client's bundle hash to what the server serves), not a code review of the config alone — and that check is now the standing way to verify a deploy actually reached its clients, not just its server.
