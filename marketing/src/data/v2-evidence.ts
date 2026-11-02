/**
 * Every current-state claim the Crucible home page makes, with its source
 * and as-of date (alpha-engine-config-I13021).
 *
 * The page renders FROM this module, so a claim cannot appear without the
 * source line beside it. Rules for editing it:
 *
 * - Copy only what Crucible v2's public status export already publishes:
 *   verdict words and layer statuses, never a score, return, cost or count
 *   of money. That export's own boundary (`crucible/console/public.py`,
 *   FORBIDDEN_SOURCE_FIELDS) is the publication rule; this page does not
 *   loosen it.
 * - No infrastructure identifiers (buckets, roles, account ids, hosts).
 * - A snapshot is a dated reading, not a live feed. When it is refreshed,
 *   change `SNAPSHOT` and every row together; never edit one row's words
 *   without its date.
 */

export interface Source {
  /** Where a reader (or reviewer) can check the claim. */
  label: string;
  href?: string;
}

/** The dated public reading the "latest cycle" and "integrity" rows quote. */
export const SNAPSHOT = {
  tradingDay: '2026-10-30',
  generated: '2026-10-31',
  source: {
    label: 'Crucible v2 public status export (verdicts and statuses only; no figures)',
  } satisfies Source,
} as const;

export const CRUCIBLE_REPO: Source = {
  label: 'nousergon/crucible README',
  href: 'https://github.com/nousergon/crucible#readme',
};

export const SLOTS_SOURCE: Source = {
  label: 'nousergon/crucible — crucible/slots/__init__.py',
  href: 'https://github.com/nousergon/crucible/blob/main/crucible/slots/__init__.py',
};

export const PUBLIC_EXPORT_SOURCE: Source = {
  label: 'nousergon/crucible — crucible/console/public.py',
  href: 'https://github.com/nousergon/crucible/blob/main/crucible/console/public.py',
};

/** The four decisions Crucible v2 runs champion/challenger experiments on. */
export const SLOTS = [
  { key: 'U', name: 'Universe', decision: 'Which names the portfolio may hold.' },
  { key: 'R', name: 'Research', decision: 'How those names are scored into signals.' },
  { key: 'M', name: 'Model', decision: 'Which trained model produces the return predictions.' },
  { key: 'S', name: 'Strategy', decision: 'Exit and risk rules.' },
] as const;

export type VerdictTone = 'moved' | 'held' | 'open';

/** The snapshot's arena cycle, one row per slot, in the export's own vocabulary. */
export const LATEST_CYCLE: { slot: string; verdict: string; tone: VerdictTone; meaning: string }[] =
  [
    {
      slot: 'R',
      verdict: 'Decided — pointer moved',
      tone: 'moved',
      meaning:
        'A comparison ran and chose a new serving arm. Provisional: the full evidence chain behind this decision is not yet independently verified.',
    },
    {
      slot: 'U',
      verdict: 'Held',
      tone: 'held',
      meaning: 'A comparison ran and the incumbent kept the pointer.',
    },
    {
      slot: 'S',
      verdict: 'Held',
      tone: 'held',
      meaning: 'A comparison ran and the incumbent kept the pointer.',
    },
    {
      slot: 'M',
      verdict: 'Unmeasurable',
      tone: 'open',
      meaning:
        'The cycle could not be scored, so no arm was chosen. The harness records that rather than guessing.',
    },
  ];

export type StatusTone = 'green' | 'red' | 'na';

/** Measurement-integrity layer statuses from the same snapshot. Statuses, never values. */
export const INTEGRITY: { layer: string; status: string; tone: StatusTone }[] = [
  { layer: 'Data coverage', status: 'GREEN', tone: 'green' },
  { layer: 'Signal rank-IC (R)', status: 'RED', tone: 'red' },
  { layer: 'Prediction rank-IC (M)', status: 'RED', tone: 'red' },
  { layer: 'Portfolio excess return (S)', status: 'N/A — missing input', tone: 'na' },
  { layer: 'Execution shortfall', status: 'N/A — sample too small', tone: 'na' },
];

/** The v1 promotion record /live still renders, and its most recent decision date. */
export const V1_RECORD = {
  href: '/live',
  lastDecisionObserved: '2026-10-23',
  observed: '2026-11-01',
} as const;

/** Dated history the page states in the past tense. */
export const HISTORY = {
  sixTeamRetired: '2026-07-12',
  v2PlanDated: '2026-09-01',
} as const;
