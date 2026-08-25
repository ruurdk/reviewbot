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

/**
 * Load every published run, plus a manifest describing what differs between
 * them. Falls back to the single-run path (and then to the synthetic fixture),
 * so a checkout with one run still renders.
 */
export async function loadRuns(url = "runs.json") {
  try {
    const res = await fetch(url, { cache: "no-store" });
    if (!res.ok) throw new Error(String(res.status));
    const manifest = await res.json();
    const runs = await Promise.all(
      (manifest.runs ?? []).map(async (entry) => ({
        ...entry,
        report: { ...(await (await fetch(entry.url, { cache: "no-store" })).json()), synthetic: false },
      })),
    );
    if (!runs.length) throw new Error("empty manifest");
    return runs;
  } catch {
    const report = await loadReport();
    return [
      {
        id: report.run_id ?? "run",
        label: report.synthetic ? "Synthetic run" : `Run ${report.run_id}`,
        note: "",
        report,
      },
    ];
  }
}

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

/**
 * The marginal per-review saving, primer excluded -- the headline figure.
 *
 * Why this leads and the cumulative percentage does not: a cumulative saving is
 * a function of how many PRs happen to be in the sequence, because the one-time
 * primer is amortised over them. "21% over 19 PRs" is three numbers glued
 * together -- a per-review saving, a setup cost, and an arbitrary N -- and only
 * the first is a property of the technique. So the page leads with the
 * per-review number and reports the primer as what it is: a setup cost priced
 * in reviews.
 *
 * Computed by the harness (analysis.marginal_per_pr), never here.
 */
export function marginalFor(report, regime = "as_measured") {
  return report?.accounting?.marginal?.[regime] ?? null;
}

/**
 * Cumulative saving versus no memory, per PR: [{ordinal, saving}].
 *
 * This is the form that lets two runs share one chart. Absolute cost cannot:
 * each run contains its *own* baseline, and those baselines are not identical
 * even though they read byte-identical context (3,890,269 tokens in both) --
 * model output is non-deterministic, so run-1's control billed $32.67 and
 * run-2's $33.54. Plotting one run's memory line against the other run's
 * baseline would be comparing across controls, and picking one baseline to
 * stand for both would quietly discard the other.
 *
 * Differencing each run against its own control sidesteps that entirely, and
 * zero becomes the no-memory line: above it memory is ahead, below it memory is
 * behind, and the crossing is the break-even PR.
 */
export function savingSeries(report, regime = "as_measured") {
  return cumulativeSeries(report, regime).map((d) => ({
    ordinal: d.ordinal,
    saving: d.baseline - d.memory,
  }));
}
