# marketing-dev/ — Nous Ergon developer-tools category page

Source for **`dev.nousergon.ai`** — the developer-tools *category* landing page,
completing the apex's category-subdomain symmetry (finance / fitness / dev).
Replaces the bare github.com/nousergon org link, which showed every repo
unfiltered instead of the four tools. Sibling of `marketing/`, `marketing-apex/`,
`marketing-metron/`, `marketing-vires/`, `marketing-finance/` — same Astro 6 +
Tailwind 4 + Biome baseline.

Lists the open-source tools — **mnemon · krepis · flow-doctor · morning-signal** —
each with a one-line differentiator and a GitHub CTA. Static, no waitlist, no
Pages Functions.

## Local dev

```sh
cd marketing-dev/
npm install
npm run dev      # http://localhost:4321
npm run build    # astro check && astro build → dist/
npm run lint     # biome (src/ only)
```

## Deploy

Rides `.github/workflows/deploy-marketing.yml` on merge to `main` (Cloudflare Pages
project **`nousergon-dev`**, static-only). Custom domain `dev.nousergon.ai`:
proxied CNAME `dev` → `nousergon-dev.pages.dev` + domain attach on the Pages
project.
