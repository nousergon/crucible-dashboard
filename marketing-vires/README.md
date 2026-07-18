# marketing-vires/ — Vires product landing page

Source for **`vires.nousergon.ai`** — the Vires marketing landing + **beta waitlist**,
served at the product's own domain root (reversed 2026-07-18; previously lived at
`fitness.nousergon.ai`, with `vires.nousergon.ai` reserved for the live app — see
below for why that changed). `fitness.nousergon.ai` is now a redirect to
`vires.nousergon.ai`.

The live Vires app (Brian's installed PWA; since 2026-06-27) shares this domain at
**`vires.nousergon.ai/app`** — a Cloudflare Worker (`vires-app-proxy` in
`nous-ergon-ops`) forwards that path prefix to the app's own EC2 origin
(`vires-app.nousergon.ai`); everything else on the domain is this Pages project.
This only works because the app itself is namespaced under `/app` (Vite `base`,
FastAPI routers — `nousergon/vires`). Do not attach this Pages project to
`vires.nousergon.ai` without that namespacing in place: that's the mistake that
briefly hijacked the app URL at the edge on 2026-07-02, before the namespacing existed.
Sibling of `marketing/` (Crucible), `marketing-apex/` (lab landing), and
`marketing-metron/` (Metron) — same Astro 6 + Tailwind 4 + Biome baseline, and the
waitlist capture mirrors `marketing-metron/` exactly (D1 + Pages Function + optional
Resend confirmation).

Copy is **claims-disciplined**: every sentence is defensible by the product as it exists
today (workout logging, routines, hybrid exercise search, objective-driven AI coach with
fatigue awareness + missed-workout rescheduling, cross-training/ruck activity log,
installable PWA). The app repo is public: <https://github.com/nousergon/vires>.

## Local dev

```sh
cd marketing-vires/
npm install
npm run dev      # http://localhost:4321
npm run build    # astro check && astro build → dist/
npm run lint     # biome (src/ only)
```

## Waitlist capture (Cloudflare D1)

The form POSTs to `functions/api/waitlist.ts` (a Pages Function) which inserts one row per
email into the D1 database **`vires-waitlist`** (bound as `WAITLIST_DB` in `wrangler.toml`).
Idempotent — re-submits are `INSERT OR IGNORE` on the email PK. Schema in `schema.sql`.

```sh
# One-time (already done): create the DB + apply the schema.
npx wrangler d1 create vires-waitlist
npx wrangler d1 execute vires-waitlist --remote --file=./schema.sql

# Read / export signups:
npx wrangler d1 execute vires-waitlist --remote \
  --command "SELECT email, datetime(created_at,'unixepoch') AS joined, source FROM waitlist ORDER BY created_at DESC"
```

### Confirmation email (Resend)

On a **new** signup the function sends a confirmation email from
`Vires <no-reply@nousergon.ai>` via the Resend REST API — but **only when the
`RESEND_API_KEY` secret is bound**; unset → DB-only, no third-party call.

```sh
npx wrangler pages secret put RESEND_API_KEY   # project: nousergon-vires
```

## Deploy

Deploys ride `.github/workflows/deploy-marketing.yml` on merge to `main` (Cloudflare
Pages project **`nousergon-vires`**). The deploy step runs **inside this directory**
(`wrangler pages deploy` picks up `wrangler.toml` + `functions/` from the cwd) — do NOT
deploy with a path argument from the repo root: wrangler only bundles a `functions/`
directory it finds in the working directory, and a root-cwd deploy silently ships the
static site without the waitlist API (this exact failure took out the Metron waitlist
2026-06-28 → 2026-07-02).

Manual deploy:

```sh
cd marketing-vires/
npm run build
npx wrangler pages deploy --branch=main   # production
```
