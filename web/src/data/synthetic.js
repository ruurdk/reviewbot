/**
 * SYNTHETIC fixture. No run has been executed -- the Claude API key is still
 * pending -- so every cost number here is an *illustrative placeholder*.
 *
 * Real: the 19 PRs, their numbers, titles, file counts, diff sizes, touched
 * spine modules, human-comment counts, beats, gold flags. From
 * data/sequence.json and the ingested PRs.
 *
 * Invented: every token count, dollar figure, and quality score, derived from
 * the real diff sizes with fixed multipliers so the layout has realistic
 * proportions.
 *
 * The page shows a permanent banner while this fixture is in use. Do not remove
 * it, and do not screenshot this page externally until a real report.json
 * exists -- plausible fake numbers are the easiest way for this project to
 * mislead someone, including us.
 */
import rows from "./sequence-rows.json";

const PRICE_IN = 5 / 1e6;
const PRICE_OUT = 25 / 1e6;
const CHARS_PER_TOKEN = 3.6;
const SOURCE_BUDGET_CHARS = 400000;

const perPr = [];
for (const row of rows) {
  const sourceChars = Math.min(row.py_files * 22000, SOURCE_BUDGET_CHARS);
  const diffTokens = Math.round((row.diff_size * 40) / CHARS_PER_TOKEN);
  const baselineCtx = Math.round(sourceChars / CHARS_PER_TOKEN) + diffTokens + 1200;
  const memoryCtx = diffTokens + 1200 + 1800;
  const baseOut = 900 + row.py_files * 40;
  const memOut = 850 + row.py_files * 30;
  const baseWrite = row.pr_ordinal === 1 ? 6000 : 0;
  const baseRead = row.pr_ordinal === 1 ? 0 : 6000;

  perPr.push({
    pr_ordinal: row.pr_ordinal,
    agent: "baseline",
    context_volume: baselineCtx,
    billed_usd:
      (baselineCtx - baseRead) * PRICE_IN + baseRead * PRICE_IN * 0.1 + baseOut * PRICE_OUT,
    billed_usd_production: baselineCtx * PRICE_IN + baseOut * PRICE_OUT,
    output_tokens: baseOut,
    by_phase: { review: baselineCtx },
    tiers: {
      uncached: baselineCtx - baseWrite - baseRead,
      cache_write: baseWrite,
      cache_read: baseRead
    },
    files_read: row.py_files
  });
  perPr.push({
    pr_ordinal: row.pr_ordinal,
    agent: "memory",
    context_volume: memoryCtx,
    billed_usd: memoryCtx * PRICE_IN + memOut * PRICE_OUT + 0.004,
    billed_usd_production: memoryCtx * PRICE_IN + memOut * PRICE_OUT + 0.004,
    output_tokens: memOut,
    by_phase: { retrieve: 1800, review: memoryCtx - 1800, write: 900 },
    tiers: {
      uncached: memoryCtx - 6000,
      cache_write: row.pr_ordinal === 1 ? 6000 : 0,
      cache_read: row.pr_ordinal === 1 ? 0 : 6000
    },
    retrieved: 7,
    memories_used: 3
  });
}

const primerCtx = 190000;
perPr.unshift({
  pr_ordinal: 0,
  agent: "memory",
  context_volume: primerCtx,
  billed_usd: primerCtx * PRICE_IN + 7000 * PRICE_OUT,
  billed_usd_production: primerCtx * PRICE_IN + 7000 * PRICE_OUT,
  output_tokens: 7000,
  by_phase: { prime: primerCtx },
  tiers: { uncached: primerCtx, cache_write: 0, cache_read: 0 }
});

const sum = (agent, key) =>
  perPr.filter((r) => r.agent === agent).reduce((acc, r) => acc + (r[key] ?? 0), 0);

export const SYNTHETIC_REPORT = {
  run_id: "SYNTHETIC-no-run-executed",
  config_fingerprint: "e2f9712728313b0d",
  rows,
  per_pr: perPr,
  accounting: {
    agents: {
      baseline: {
        context_volume: sum("baseline", "context_volume"),
        billed_usd: sum("baseline", "billed_usd"),
        billed_usd_production: sum("baseline", "billed_usd_production"),
        output_tokens: sum("baseline", "output_tokens"),
        memory_overhead_usd: 0
      },
      memory: {
        context_volume: sum("memory", "context_volume"),
        billed_usd: sum("memory", "billed_usd"),
        billed_usd_production: sum("memory", "billed_usd_production"),
        output_tokens: sum("memory", "output_tokens"),
        memory_overhead_usd: 1.05
      }
    },
    primer: { primer_usd: 1.125, prs: rows.length, primer_usd_per_pr: 1.125 / rows.length }
  },
  quality_gold: {
    baseline: {
      prs: 7, true_positives: 4, false_positives: 5, false_negatives: 2,
      precision: 4 / 9, recall: 4 / 6, f1: 0.533, false_positive_rate: 5 / 9,
      traps_flagged: 3, traps_total: 3
    },
    memory: {
      prs: 7, true_positives: 4, false_positives: 2, false_negatives: 2,
      precision: 4 / 6, recall: 4 / 6, f1: 0.667, false_positive_rate: 2 / 6,
      traps_flagged: 1, traps_total: 3
    }
  },
  quality_proxy: {
    baseline: { prs: 19, prs_with_human_comments: 16, prs_blind: 3, agreement: 0.31, coverage: 0.22 },
    memory: { prs: 19, prs_with_human_comments: 16, prs_blind: 3, agreement: 0.38, coverage: 0.24 }
  },
  warnings: [
    "SYNTHETIC DATA: no run has been executed. Token counts, dollar figures, and quality scores are placeholders."
  ]
};
