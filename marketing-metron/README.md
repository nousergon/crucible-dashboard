# marketing-metron/ — Metron product landing page

Source for **`metron.nousergon.ai`** — the Metron marketing landing + **beta waitlist**
(metron-ops#29). Sibling of `marketing/` (Crucible) and `marketing-apex/` (lab landing) —
same Astro 6 + Tailwind 4 + Biome baseline.

Copy is **claims-disciplined**: every sentence is defensible by the product as it exists
today (descriptive analytics, read-only, no ads/trackers). The agentic/quant-research tier
is **not** mentioned here — it's post-beta. Pro analytics (benchmark/factor/attribution)
are framed explicitly as roadmap, not current capability.

Plan: `alpha-engine-config/private-docs/site_restructure_plan_260612.md` (config#905).

## Local dev

```sh
cd marketing-metron/
npm install
npm run dev      # http://localhost:4321
npm run build    # astro check && astro build → dist/
npm run lint     # biome (src/ only)
```

## Waitlist capture (Cloudflare D1)

The form POSTs to `functions/api/waitlist.ts` (a Pages Function) which inserts one row per
email into the D1 database **`metron-waitlist`** (bound as `WAITLIST_DB` in `wrangler.toml`).
Idempotent — re-submits are `INSERT OR IGNORE` on the email PK. Two fields are optional:
`segment` ("Which describes you?" — `fire` / `active_investor` / `index_investor` /
`day_trader` / `other`; an unrecognized value is a 400, a missing one stores `NULL`) and
`currentTool` (free text, <=500 chars, stored as `current_tool`).

Schema lives in `migrations/` (applied via `wrangler d1 migrations apply`, tracked so
re-running is a no-op); `schema.sql` is a read-only consolidated reference, not the
source of truth — see its header.

```sh
# One-time (already done): create the DB.
npx wrangler d1 create metron-waitlist

# Apply migrations (deploy-marketing.yml does this automatically on every push to main
# that touches this directory — see its "Apply D1 migrations" step):
npx wrangler d1 migrations apply metron-waitlist --remote

# Read / export signups:
npx wrangler d1 execute metron-waitlist --remote \
  --command "SELECT email, datetime(created_at,'unixepoch') AS joined, source, segment, current_tool FROM waitlist ORDER BY created_at DESC"
```

### Confirmation email (Resend)

On a **new** signup, `waitlist.ts` sends a "you're on the list" confirmation via the
[Resend](https://resend.com) REST API from `no-reply@nousergon.ai` — making good on the
landing copy's "we email you" promise. It's best-effort (a Resend failure never fails the
signup) and new-signups-only (`INSERT OR IGNORE` reports `changes=0` on a duplicate, so
re-submits don't re-send). The send is **opt-in by config**: it fires only when the
`RESEND_API_KEY` secret is bound; unset → DB-only, no third-party call.

```sh
# One-time: bind the Resend key as a Pages secret (uses the verified nousergon.ai domain).
npx wrangler pages secret put RESEND_API_KEY
```

## Funnel counters (metron-ops-I305)

No third-party tracker, no cookies, no IP storage — three server-side D1 counters, one
row per UTC day in `funnel_daily`:

- **visits** — bumped by `functions/_middleware.ts` on every `GET /` (root landing page
  only; assets, `/dash`, `/api/*` pass through uncounted).
- **waitlist_new** / **waitlist_dup** — bumped by `functions/api/waitlist.ts`, keyed on
  whether the `INSERT OR IGNORE` actually inserted a row.
- **email_sent** / **email_failed** — bumped by `functions/api/waitlist.ts`, keyed on the
  Resend response status (2xx vs not).

A metric that has **never** recorded an event reports `measured: false` with a null
total and a reason — never `0`. A counter nobody wired up and a counter with nothing to
count are indistinguishable from the outside, so the endpoint refuses to render the
first as the second. Once a metric has any lifetime record, a zero inside the requested
window is a real zero and is reported as one. A failed read answers **503** with the
same not-measured shape (metron-ops-I305 deliverable 4).

Read them with `GET /api/funnel` (`functions/api/funnel.ts`), bearer-token protected:

```sh
# One-time: bind the read token as a Pages secret. Unset -> the route 404s.
npx wrangler pages secret put FUNNEL_READ_TOKEN

curl -H "authorization: Bearer $FUNNEL_READ_TOKEN" \
  "https://metron.nousergon.ai/api/funnel?days=30"
```

## Demo video

The landing page renders the scripted 60-second walkthrough from
`public/demo/metron-demo.mp4` (optional poster: `public/demo/metron-demo-poster.jpg`).
The section is rendered **only when the file exists** — a `<video>` pointing at a missing
asset is a broken player on the product's front door — and the build logs a warning when
it does not, so its absence is loud rather than silent.

The recording is made by script, not by hand, from the `metron` repo
(`metron/web/demo/record.ts`, metron-ops-I305 deliverable 2):

```sh
# In the metron repo, against an authenticated build. 390x844, currency mask ON.
BASE_URL=<owner build origin> METRON_STORAGE_STATE=<playwright storage state json> \
  npm --prefix web run demo:record       # -> metron/web/demo/out/metron-demo.webm
ffmpeg -i metron-demo.webm -c:v libx264 -pix_fmt yuv420p metron-demo.mp4
```

Then commit `metron-demo.mp4` to `marketing-metron/public/demo/` here; merging to `main`
deploys it. The recorder refuses to finish if any page still shows a currency amount
while the mask is on, so a leaked balance cannot reach the published file.

## Analytics (Cloudflare Web Analytics)

Privacy-first, no cookies, no third-party tracker. The beacon is injected only when
`PUBLIC_CF_ANALYTICS_TOKEN` is set at **build time** (unset → no script shipped):

1. Cloudflare dashboard → Web Analytics → add site `metron.nousergon.ai` → copy the token.
2. Build + deploy with it set (see Deploy).

## Deploy

`nousergon-metron` is a **direct-upload** Pages project (no Git provider integration —
Cloudflare's own git-push-to-deploy is not what ships this). **Deploy is automated in
`.github/workflows/deploy-marketing.yml`: pushing to `main` with a change under this
directory builds, applies D1 migrations, and runs `wrangler pages deploy` for you** —
merging the PR is the whole deploy (corrected 2026-09-15: this doc previously said
merging does NOT deploy; that was true before deploy-marketing.yml existed and is no
longer true — see that workflow's header for the outage that made a repo-committed
deploy step the standard). Manual deploy (only for the migration/deploy pipeline itself
being down) is still:

```sh
PUBLIC_CF_ANALYTICS_TOKEN=<token> npm run build
npx wrangler d1 migrations apply metron-waitlist --remote
npx wrangler pages deploy            # reads wrangler.toml (project, dist/, functions/, D1)
```
