// Ambient declarations for non-code side-effect imports (CSS, etc.).
//
// Next.js normally supplies the `*.css` module type via the GENERATED
// `next-env.d.ts` (gitignored, emitted only by `next dev` / `next build`).
// The CI `dash-web` job runs `typecheck` (`tsc --noEmit`) BEFORE the
// production build, so at typecheck time `next-env.d.ts` does not yet exist —
// and on the Next 15.x line the `next` package does not ship a standalone
// `*.css` declaration (Next 16 does, which is why this only surfaces on 15.x).
// The result is `TS2882: Cannot find module or type declarations for
// side-effect import of './globals.css'`.
//
// Declaring it here makes typecheck self-sufficient and independent of build
// ordering and Next/TypeScript version drift. The import is genuinely valid —
// `next build` bundles globals.css fine — so this declares a real module type,
// it does not silence a defect.
declare module "*.css";
