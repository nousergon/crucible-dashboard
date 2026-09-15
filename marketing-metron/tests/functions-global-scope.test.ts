// Guards the class of deploy failure seen on 2026-09-15: Cloudflare rejected the Pages
// Functions bundle because functions/api/funnel.ts built a Response at module scope
// ("Disallowed operation called within global scope"). The Workers runtime forbids
// constructing Request/Response/Headers, timers, random values and I/O while a module is
// evaluated; Node unit tests and `wrangler pages functions build` never evaluate it, so
// nothing caught it before production. This test parses every functions/ file with the
// TypeScript compiler and fails on a forbidden operation that runs at module scope, i.e.
// anywhere outside a function, method or arrow-function body.
import { readdirSync, readFileSync, statSync } from "node:fs";
import { join } from "node:path";
import ts from "typescript";
import { describe, expect, it } from "vitest";

const ROOT = join(__dirname, "..", "functions");
const FORBIDDEN_NEW = new Set(["Response", "Request", "Headers"]);
const FORBIDDEN_CALLS = new Set(["setTimeout", "setInterval", "fetch", "Math.random", "crypto.randomUUID", "crypto.getRandomValues"]);

function files(dir: string): string[] {
  return readdirSync(dir).flatMap((name) => {
    const full = join(dir, name);
    return statSync(full).isDirectory() ? files(full) : full.endsWith(".ts") ? [full] : [];
  });
}

function moduleScopeOffenders(path: string): string[] {
  const sf = ts.createSourceFile(path, readFileSync(path, "utf8"), ts.ScriptTarget.Latest, true);
  const out: string[] = [];
  const visit = (node: ts.Node): void => {
    // Code inside a function body runs per request, not at module evaluation.
    if (ts.isFunctionLike(node)) return;
    if (ts.isNewExpression(node) && FORBIDDEN_NEW.has(node.expression.getText(sf))) {
      out.push(`line ${sf.getLineAndCharacterOfPosition(node.getStart(sf)).line + 1}: ${node.getText(sf)}`);
    }
    if (ts.isCallExpression(node) && FORBIDDEN_CALLS.has(node.expression.getText(sf))) {
      out.push(`line ${sf.getLineAndCharacterOfPosition(node.getStart(sf)).line + 1}: ${node.getText(sf)}`);
    }
    ts.forEachChild(node, visit);
  };
  ts.forEachChild(sf, visit);
  return out;
}

describe("Pages Functions module scope", () => {
  for (const file of files(ROOT)) {
    it(`${file.slice(ROOT.length + 1)} performs no disallowed operation at global scope`, () => {
      expect(moduleScopeOffenders(file)).toEqual([]);
    });
  }

  it("the guard itself catches a module-scope Response", () => {
    const probe = join(__dirname, "__probe_global_response.ts");
    const sf = ts.createSourceFile(probe, 'const X = new Response("x");\nexport function f() { return new Response("ok"); }\n', ts.ScriptTarget.Latest, true);
    const hits: number[] = [];
    const visit = (node: ts.Node): void => {
      if (ts.isFunctionLike(node)) return;
      if (ts.isNewExpression(node) && FORBIDDEN_NEW.has(node.expression.getText(sf))) hits.push(1);
      ts.forEachChild(node, visit);
    };
    ts.forEachChild(sf, visit);
    expect(hits.length).toBe(1);
  });
});
