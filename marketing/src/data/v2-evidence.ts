/**
 * Every current-state claim the Crucible home page makes, with its source
 * and as-of date (alpha-engine-config-I13021).
 *
 * The page renders FROM this module, so a claim cannot appear without the
 * source line beside it. Rules for editing it:
 *
 * - Copy only what Crucible v2's public status export already publishes
 *   (verdict words and layer statuses), what a public repository's code
 *   states, or a dated rollout fact — never a score, return, cost or count
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

export const TRADER_SOURCE: Source = {
  label: 'nousergon/crucible-trader README',
  href: 'https://github.com/nousergon/crucible-trader#readme',
};

export const RECONCILE_SOURCE: Source = {
  label: 'nousergon/crucible-trader — reconciliation.py and reconciliation_control.py',
  href: 'https://github.com/nousergon/crucible-trader/blob/main/crucible_trader/reconciliation_control.py',
};

export const PROMOTE_SOURCE: Source = {
  label: 'nousergon/crucible — crucible/promote.py',
  href: 'https://github.com/nousergon/crucible/blob/main/crucible/promote.py',
};

/** Dates of the v2 rebuild that are tracked in private issues, read on `asOf`. */
export const ROLLOUT_SOURCE: Source = {
  label: 'Crucible v2 rebuild tracker (private issue tracker)',
};

/**
 * Where the paper trader stands: a dated reading, refreshed by hand like
 * SNAPSHOT. Change `asOf` together with any other field, and rewrite the
 * rollout box when a scheduled date passes (alpha-engine-config-I13021).
 */
export const TRADER = {
  asOf: '2026-11-05',
  /** Phase-4 cutover: the paper trader moved onto the v2 contract. */
  cutover: '2026-10-30',
  /** The first v2 session; it placed no orders. */
  firstSession: '2026-11-03',
  /**
   * The first v2 session that placed orders; every order it placed filled
   * (paper). Read 2026-11-05 on the rebuild tracker's phase-4 audit issue.
   */
  firstTradingSession: '2026-11-04',
  /** Phase-4 exit check; v1 is decommissioned under phase 4. */
  exitCheck: '2026-11-06',
} as const;

export interface EvidenceRow {
  claim: string;
  detail: string;
  source: Source;
}

/** "Does it work?" — what the evidence supports today. Mechanisms and verdicts, never figures. */
export const SHOWN: EvidenceRow[] = [
  {
    claim: 'It decides under rules fixed in advance.',
    detail:
      'On the 2026-10-30 cycle one slot moved its pointer, two kept their incumbents and one was recorded as unmeasurable. A pointer moves only when the promotion rule declared before the comparison says so.',
    source: PROMOTE_SOURCE,
  },
  {
    claim: 'It refuses rather than guesses.',
    detail:
      'The trader re-checks the record of the run behind every champion on every read and trades nothing when that record is not clean. It has no fallback to yesterday’s feed or another slot.',
    source: TRADER_SOURCE,
  },
  {
    claim: 'Its checks are tested against planted faults.',
    detail:
      'Every reconciliation cycle replays its own inputs with one known discrepancy planted per class. A plant the reconciler misses voids that cycle’s reconciliation instead of lowering a score.',
    source: RECONCILE_SOURCE,
  },
];

/** What it has not shown yet. Stated plainly; each line is a reason not to over-read the page. */
export const NOT_SHOWN: EvidenceRow[] = [
  {
    claim: 'A performance edge.',
    detail:
      'Both signal-quality layers read red on the 2026-10-30 cycle. This site publishes no return, score or cost.',
    source: PUBLIC_EXPORT_SOURCE,
  },
  {
    claim: 'A v2 trading record.',
    detail: `The paper trader moved onto v2 on ${TRADER.cutover}. Its first v2 session (${TRADER.firstSession}) placed no orders; it first placed and filled orders on ${TRADER.firstTradingSession}. A few paper sessions are not a track record.`,
    source: ROLLOUT_SOURCE,
  },
  {
    claim: 'Real capital.',
    detail:
      'Paper trading only. Real capital is a reserved decision that cannot be taken before eight consecutive weeks of the v2 trader on paper with no unexplained reconciliation break.',
    source: ROLLOUT_SOURCE,
  },
];
