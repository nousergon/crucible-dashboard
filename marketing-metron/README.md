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
Idempotent — re-submits are `INSERT OR IGNORE` on the email PK. Schema in `schema.sql`.

```sh
# One-time (already done): create the DB + apply the schema.
npx wrangler d1 create metron-waitlist
npx wrangler d1 execute metron-waitlist --remote --file=./schema.sql

# Read / export signups:
npx wrangler d1 execute metron-waitlist --remote \
  --command "SELECT email, datetime(created_at,'unixepoch') AS joined, source FROM waitlist ORDER BY created_at DESC"
```

## Analytics (Cloudflare Web Analytics)

Privacy-first, no cookies, no third-party tracker. The beacon is injected only when
`PUBLIC_CF_ANALYTICS_TOKEN` is set at **build time** (unset → no script shipped):

1. Cloudflare dashboard → Web Analytics → add site `metron.nousergon.ai` → copy the token.
2. Build + deploy with it set (see Deploy).

## Deploy

`nousergon-metron` is a **direct-upload** Pages project (no Git provider) — **merging to
`main` does NOT deploy it.** `wrangler.toml` carries the project name, D1 binding, and
build-output dir, so a deploy is:

```sh
PUBLIC_CF_ANALYTICS_TOKEN=<token> npm run build
npx wrangler pages deploy            # reads wrangler.toml (project, dist/, functions/, D1)
```
