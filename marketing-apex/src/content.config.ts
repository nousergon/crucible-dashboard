import { defineCollection, z } from 'astro:content';
import { glob } from 'astro/loaders';

/**
 * Retros: incident case studies sourced from markdown files in
 * `src/content/retros/`. Each retro is a curated narrative (what failed,
 * how it was caught, root cause, fix, structural change). The set of
 * retros is curated by hand; the index page displays them in date order,
 * latest first — `date` is the sole ordering key, so there is no separate
 * `order` field to keep in sync.
 */
const retros = defineCollection({
  loader: glob({ pattern: '**/*.md', base: './src/content/retros' }),
  schema: z.object({
    title: z.string(),
    date: z.string(), // YYYY-MM-DD; kept as string to preserve canonical formatting
    severity: z.enum(['P0', 'P1', 'P2', 'P3']),
    domain: z.string(),
    summary: z.string(),
  }),
});

export const collections = { retros };
