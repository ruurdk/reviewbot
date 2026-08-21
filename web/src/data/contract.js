/**
 * The page renders exactly what `reviewbot report` writes -- nothing is computed
 * here that the harness did not measure. That is deliberate: every number on
 * screen must be traceable to a row in calls.jsonl.
 *
 * Shape (from analysis.summary + runner.RunReport):
 *   report.json   { run_id, config_fingerprint, results[], accounting, quality_gold,
 *                   quality_proxy, warnings[] }
 *   summary.json  the `accounting` block alone
 *
 * If no run has been executed, `loadReport` falls back to the synthetic fixture
 * and marks it. The banner it drives is not decoration: a demo page showing
 * plausible numbers with no run behind them is the single easiest way for this
 * project to mislead someone, including us.
 */

export const REGIMES = {
  as_measured: {
    label: "As measured",
    help:
      "Actual billed cost, caches warm. The conservative number -- memory has to win here.",
  },
  production_equivalent: {
    label: "Production cadence",
    help:
      "Same context volume, with cross-PR cache reads repriced at the full input rate, " +
      "because real PRs arrive further apart than the longest cache TTL (1 hour).",
  },
};

export async function loadReport(url = "report.json") {
  try {
    const res = await fetch(url, { cache: "no-store" });
    if (!res.ok) throw new Error(String(res.status));
    const data = await res.json();
    return { ...data, synthetic: false };
  } catch {
    const { SYNTHETIC_REPORT } = await import("./synthetic.js");
    return { ...SYNTHETIC_REPORT, synthetic: true };
  }
}

/** Cumulative cost per agent for one regime: [{ordinal, baseline, memory}]. */
export function cumulativeSeries(report, regime = "as_measured") {
  const key = regime === "as_measured" ? "billed_usd" : "billed_usd_production";
  const ordinals = [...new Set(report.per_pr.map((r) => r.pr_ordinal))].sort((a, b) => a - b);
  const running = { baseline: 0, memory: 0 };
  return ordinals.map((ordinal) => {
    for (const agent of ["baseline", "memory"]) {
      const row = report.per_pr.find((r) => r.pr_ordinal === ordinal && r.agent === agent);
      if (row) running[agent] += row[key];
    }
    return { ordinal, baseline: running.baseline, memory: running.memory };
  });
}

/**
 * First ordinal where the memory agent's cumulative cost drops below the
 * baseline's *and stays there*. Recomputed client-side only so the annotation
 * follows the regime toggle; the headline figure still comes from the harness,
 * which uses the same rule.
 */
export function crossover(series) {
  let answer = null;
  for (const point of series) {
    if (point.memory < point.baseline) {
      if (answer === null) answer = point.ordinal;
    } else {
      answer = null;
    }
  }
  return answer;
}

/** Net saving at the end of the sequence, in USD. Negative means memory lost. */
export function netSaving(series) {
  const last = series[series.length - 1];
  return last ? last.baseline - last.memory : 0;
}
