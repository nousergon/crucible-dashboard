# alpha-engine-dashboard — Dashboard Module

> System architecture, S3 layout, module overview, and cross-repo conventions: see [`~/Development/CLAUDE.md`](../CLAUDE.md). This file covers dashboard-specific operational details only.

## What this repo is

Multi-surface EC2 box (t3.small, 5 systemd services), auto-deploys on merge (~30s push-to-live: GHA OIDC → SSM → `git reset --hard` + `deploy-on-merge.sh` → restart + health-gate every service). Services: `dashboard` :8501 (private Streamlit console at `console.nousergon.ai`, Cloudflare Access), `nous-ergon-live` :8502 (public Streamlit at `live.nousergon.ai`), `crucible-dash` :8504 (Streamlit `/dash` skin, rollback-only since the 9-D cutover), `crucible-dash-api` :8506 (internal FastAPI), `crucible-dash-web` :3002 (Next.js 15/React 19 prosumer surface at `crucible.nousergon.ai/dash`, Cloudflare Access-gated, config#1957/#1973 — implements the Performance/Integrity/About experiment-results framework). **Alpha contribution:** operational visibility — surfaces data for informed manual interventions and (via `/dash`) the experiment-harness results surface.

## Stack

- Python 3.13+ (repo/README badge); CI pins 3.12 for lint/test — keep code compatible with both.
- Streamlit `>=1.40` (console, live, `/dash` skin) + `streamlit-calendar`; Plotly for charts; pandas `>=2.0`; boto3 `>=1.36` (S3 reads only — the whole repo is read-only w.r.t. every upstream module).
- FastAPI `>=0.115` + uvicorn `>=0.30` for `dash_api` (internal, 127.0.0.1:8506 only, not internet-facing).
- Next.js 15.5 / React 19 + Tailwind 3 + Recharts + TypeScript for `dash-web`; Vitest for unit tests, `tsc --noEmit` for typecheck.
- Astro static sites (`marketing/`, `marketing-apex/`, `marketing-dev/`, `marketing-finance/`, `marketing-metron/`, `marketing-telos/`, `marketing-vires/`) — separate positioning/landing surfaces, deployed independently (Cloudflare Pages), out of scope for the 5 systemd services above.
- nginx (`infrastructure/nginx.conf`) fronts everything behind Cloudflare (SSL terminated at Cloudflare Origin CA; private hostnames additionally gated by Cloudflare Access). Also proxies **Metron**'s `metron-dash-web` (:3003, `metron-dash.nousergon.ai`) and **Telos**'s dashboard (:3001) — this box and this nginx config are shared infra for those products; their app code lives in their own repos, only the vhost config lives here.
- Deploy: GHA OIDC → SSM `send-command` on `DASHBOARD_INSTANCE_ID` → `infrastructure/deploy-on-merge.sh` (lib/pip refresh, nginx reload on config diff, `systemctl restart` of all 5 services, health-gate against each `/_stcore/health` or `/api/health` endpoint). Daily `boot-pull.sh` (systemd timer) is the safety net for all repos/services on the box, independent of the fast-path.

## Key files

```
app.py                              # console entry (:8501) — pure triage router (fleet strip, KPIs, report card, regime, alerts)
views/                              # ~65 console pages (Streamlit multipage): Performance, Signals_and_Research, Predictor,
                                     #   Execution, Scanner, Model_Zoo, Decision_Queue, Fleet_Status, Backlog_Groom, etc.
loaders/                            # S3/db readers shared by console + dash: s3_loader.py, db_loader.py, signal_loader.py,
                                     #   eval_loader.py, fleet_status_loader.py, decision_queue_loader.py, pr_merge_loader.py, ...
shared/                             # cross-page pure logic: accuracy_metrics.py, attribution.py, correlation.py,
                                     #   normalizers.py, formatters.py, reconciliation.py, target_weights.py, view_host.py
components/                         # reusable Streamlit widgets: header.py, report_card.py / report_card_v2.py, director_plan.py,
                                     #   phase_indicator.py, uptime_kpi.py, sweep_distribution.py, judge_bias.py
charts/                             # Plotly chart builders (NAV, alpha, accuracy, IC, attribution)
fleet_status.py                     # cross-repo fleet-status aggregation (PRs, CI, deploys) — feeds Fleet_Status view + dash_api
health_checker.py                   # box/service health checks used by box_health.sh and console banners
trading_calendar.py                 # shared trading-day calendar helper

live/                                # public console (:8502, live.nousergon.ai) — thin st.navigation router, own loaders/charts
live/app.py                          #   entry; sets baseUrlPath="live" (routes incl. health live under /live)
live/pages/                          #   performance.py, holdings_and_trades.py, evaluation.py, system_pulse.py, uptime.py
live/shared.py, live/morning_brief*.py, live/ticker_detail.py, live/retros/

dash/app.py                          # crucible-dash (:8504, /dash rollback skin) — SAME view_model as console, config#1957/#1958
dash/.streamlit/config.toml          #   baseUrlPath + WebSocket CORS fix (WorkingDirectory-relative, see file header)

dash_api/main.py                     # crucible-dash-api (:8506, internal FastAPI) — fail-loud contract: 503 on real loader
                                      #   failure, honest-ABSENT rows on missing data, never a silently-empty 200 (config#2339)

dash-web/                            # crucible-dash-web (:3002, Next.js 15/React 19, crucible.nousergon.ai/dash)
dash-web/app/{page,risk,integrity,about}.tsx  # Performance / Risk / Integrity / About routes
dash-web/lib/api.ts, lib/rebase.ts   #   dash_api client + return-rebasing helpers
dash-web/components/                 #   alpha-bars.tsx, equity-chart.tsx, stat-card.tsx, verdict-chips.tsx
dash-web/__tests__/                  #   Vitest unit tests (api-client, rebase, risk-api, stat-card, verdict-chips)

infrastructure/deploy-on-merge.sh    # SSM-invoked deploy: lib refresh, nginx reload-on-diff, restart all 5 services, health-gate
infrastructure/nginx.conf            # vhosts for console/live/dash/dash-web + Metron + Telos (shared box)
infrastructure/*.service             # systemd units: dashboard, crucible-dash, crucible-dash-api, crucible-dash-web
infrastructure/systemd/              # boot-pull, box-health, box-hygiene timers/services (daily safety-net + housekeeping)
infrastructure/boot-pull.sh          # daily (12:00 UTC) fallback: git pull + restart-on-unit-change, safety net for all repos
infrastructure/box_health.sh         # health probe script run by box-health.timer
live/infrastructure/nous-ergon-live.service  # systemd unit for the :8502 live console (lives under live/, not infrastructure/)

.github/workflows/deploy.yml         # push-to-main → SSM deploy (console + live fast path)
.github/workflows/ci.yml             # Python 3.12 lint+pytest job, dash-web Node 20 Vitest+typecheck+build job
tests/                               # ~110+ pytest files mirroring views/loaders/components/shared 1:1
```

## Repo-specific rules

- **Read-only w.r.t. every upstream module.** All loaders read from S3 / `research.db` / `trades.db` — the dashboard never writes back to any producer's artifact. If a metric isn't already produced upstream, ship it upstream first (see README "Phase 2 measurement contribution").
- **This box is shared infrastructure**, not exclusive to alpha-engine: `infrastructure/nginx.conf` also fronts Metron's `metron-dash-web` (:3003) and Telos's dashboard (:3001). Metron's own nginx/service config is tracked here, not in `metron-ops` — a change to the shared vhost file affects all three products' routing.
- **`dash_api` fail-loud contract:** loader calls through `dash_api/main.py`'s `_guard` raise on real failure (503, never a fabricated empty 200); the ~40 direct console call sites into the same loaders are NOT opted into strict mode and keep the older degrade-to-ABSENT + `get_recent_s3_errors()` telemetry path. Don't assume the two call paths behave identically on an S3 access error.
