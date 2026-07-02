# marketing-finance/ — Nous Ergon finance category page

Source for **`finance.nousergon.ai`** — the finance *category* landing page: the URL
Brian hands to finance-interested people directly (rather than the general apex).
Sibling of `marketing/` (Crucible), `marketing-apex/` (lab landing / category
directory), `marketing-metron/` and `marketing-vires/` (product pages + waitlists) —
same Astro 6 + Tailwind 4 + Biome baseline.

Lists the finance products in likely release order — **Metron → Crucible → Telos** —
each with an honest status (`Beta waitlist open` / `In development`; nothing here is
monetized yet, so nothing is labeled "Live") and a steering CTA to its product
surface (metron.nousergon.ai waitlist, crucible.nousergon.ai, Telos on GitHub).
No waitlist of its own — capture happens on the product pages.

Copy is **claims-disciplined**: every sentence is defensible by the products as they
exist today.

## Local dev

```sh
cd marketing-finance/
npm install
npm run dev      # http://localhost:4321
npm run build    # astro check && astro build → dist/
npm run lint     # biome (src/ only)
```

## Deploy

Rides `.github/workflows/deploy-marketing.yml` on merge to `main` (Cloudflare Pages
project **`nousergon-finance`**, static-only — no Pages Functions). Custom domain
`finance.nousergon.ai` is attached in the CF dashboard (Pages → nousergon-finance →
Custom domains).
