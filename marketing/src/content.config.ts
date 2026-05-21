import { defineCollection, z } from 'astro:content';
import { glob } from 'astro/loaders';

/**
 * Retros: incident case studies sourced from markdown files in
 * `src/content/retros/`. Each retro is a curated narrative (what failed,
 * how it was caught, root cause, fix, structural change) — not a
 * chronological feed.
 *
 * Schema mirrors the Streamlit page's `_RETROS` registry one-for-one so
 * the listing page can render exactly the same metadata.
 */
const retros = defineCollection({
  loader: glob({ pattern: '**/*.md', base: './src/content/retros' }),
  schema: z.object({
    title: z.string(),
    date: z.string(), // YYYY-MM-DD; kept as string to preserve canonical formatting
    severity: z.enum(['P0', 'P1', 'P2', 'P3']),
    domain: z.string(),
    summary: z.string(),
    /**
     * Display order in the index. Lower = top. Brian's curation is
     * "case studies, not a chronological feed" — so order isn't by date.
     */
    order: z.number().int().nonnegative(),
  }),
});

export const collections = { retros };
