// Copy lint over the landing page source (metron-ops-I305 deliverable 3).
//
// The landing page is the one Metron surface an unauthenticated stranger reaches, so
// two things about it are load-bearing and neither is caught by `astro check`:
//
//   1. It carries the three positioning claims and the demo video section, which is
//      what deliverable 3 asks for.
//   2. It carries NO advice-flavored claim and NO live market data. Deploy cash is
//      L2-shaped and owner-build-only until ruling R2; free-tier quotes on a public
//      unauthenticated URL are the §3b Flag 2 exposure until ruling R3. A copy edit
//      that reintroduces either must fail here rather than on a lawyer's desk.

import { readFileSync } from "node:fs";
import path from "node:path";
import { describe, expect, it } from "vitest";

const SOURCE = readFileSync(path.join(__dirname, "..", "src", "pages", "index.astro"), "utf8");

describe("landing page copy", () => {
  it("carries the three positioning claims (metron.md §1 / §3g.1)", () => {
    expect(SOURCE).toContain(
      "One screen, on your phone, that answers everything you would actually check about your portfolio."
    );
    expect(SOURCE).toContain(
      "Your brokerage UI tells you what you hold; Metron tells you what it means."
    );
    expect(SOURCE).toContain("Attribution, factor risk and lot-level tax are one tap behind it.");
  });

  it("renders the demo recording, and only when the asset exists", () => {
    expect(SOURCE).toMatch(/<video/);
    expect(SOURCE).toContain("/demo/metron-demo.mp4");
    // Guarded on the asset: a <video> pointing at a missing file is a broken player on
    // the product's front door.
    expect(SOURCE).toMatch(/hasDemoVideo\s*&&/);
    expect(SOURCE).toContain("existsSync");
  });

  it("says the recording is masked, so a viewer is not misled about the figures", () => {
    expect(SOURCE).toMatch(/percentages of the\s+portfolio, not dollars/);
  });

  it("makes no advice-flavored or allocation claim (ruling R2 pending)", () => {
    const forbidden = [
      /deploy cash/i,
      /what to buy/i,
      /\brecommend(s|ed|ation)?\b/i,
      /buy signal/i,
      /investment advice(?!\.?["'<\s]*$)/i, // the footer's "not investment advice" is fine; a claim is not
    ];
    // Comments explain what is deliberately ABSENT, so they name the very phrases this
    // check forbids — strip them and lint the copy that actually ships.
    const body = SOURCE.replace(/<!--[\s\S]*?-->/g, "")
      .replace(/^\s*\/\/.*$/gm, "")
      .replace(/Portfolio analytics &mdash; not investment advice\./g, "");
    for (const re of forbidden) {
      expect(body, `landing copy must not match ${re}`).not.toMatch(re);
    }
  });

  it("quotes no market data on the public page (ruling R3 pending)", () => {
    // No price/quote strings, and no dollar amounts presented as market figures.
    expect(SOURCE).not.toMatch(/\blast price\b/i);
    expect(SOURCE).not.toMatch(/\bquote[sd]?\b/i);
    expect(SOURCE).not.toMatch(/\$\d/);
  });
});
