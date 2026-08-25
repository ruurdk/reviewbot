import React, { useEffect, useMemo, useState } from "react";
import { Badge, Banner, Card, Switch, Typography } from "@redis-ui/components";

/**
 * Component-API notes, all read from @redis-ui/components 51.2.0 rather than
 * assumed -- each of these was wrong on the first attempt and the DOM smoke test
 * caught it:
 *
 * - Typography sizes are uppercase unions: 'XXL' | 'XL' | 'L' | 'M' | 'S' | 'XS'.
 * - Banner takes `message` (ReactNode) and `variant` from
 *   informative | notice | danger | attention | success. There is NO "warning".
 * - Switch uses `onCheckedChange`, not `onChange`.
 * - Tabs is not a Radix-style Root/List/Trigger set; it wants a `tabs` array (or
 *   its Compose/TabBar/ContentPane subtree). The act switcher here is a plain
 *   role="tablist" of buttons, which is less coupling for a 4-way toggle.
 * - There is NO Table component in this version (only TableHeading), so the
 *   accounting table is hand-built -- see components/AccountingTable.jsx.
 * - Badge takes a `label` string; children are ignored and render nothing.
 */
import { CumulativeCost } from "./charts/CumulativeCost";
import { SavingVsNoMemory } from "./charts/SavingVsNoMemory";
import { PerPrBreakdown } from "./charts/PerPrBreakdown";
import { QualityChart } from "./charts/QualityChart";
import { AccountingTable } from "./components/AccountingTable";
import { REGIMES, crossover, cumulativeSeries, loadRuns, marginalFor, netSaving, savingSeries } from "./data/contract";
import { pct, tokens, usd } from "./charts/scales";

/**
 * Three acts (spec 8a), with progressive disclosure -- but every act is
 * reachable at any time and the accounting table is never more than one click
 * away. A skeptical audience should never have to take a chart on faith.
 *
 * Act 1  the claim: one hero figure, the cumulative net saving
 * Act 2  the mechanism: where the tokens went, per PR, per phase, per cache tier
 * Act 3  the guardrail: quality, on its own scale, reported even when it hurts
 */
const ACTS = [
  { id: "claim", label: "1 · The claim" },
  { id: "mechanism", label: "2 · Where the tokens went" },
  { id: "guardrail", label: "3 · Did quality hold?" },
  { id: "accounting", label: "Full accounting" },
];

function Kpi({ label, value, help }) {
  return (
    <Card style={{ padding: "14px 18px", minWidth: 168, flex: "1 1 168px" }}>
      <Typography.Body size="S" style={{ opacity: 0.7 }}>{label}</Typography.Body>
      <div style={{ fontSize: 26, fontWeight: 600, fontVariantNumeric: "tabular-nums", margin: "4px 0 2px" }}>
        {value}
      </div>
      {help ? <Typography.Body size="S" style={{ opacity: 0.55 }}>{help}</Typography.Body> : null}
    </Card>
  );
}

export function App({ mode, onToggleMode }) {
  const [regime, setRegime] = useState("as_measured");
  const [facet, setFacet] = useState("phase");
  const [act, setAct] = useState("claim");

  // Every published run, newest last. `report` is whichever the reader selected;
  // the comparison chart uses all of them.
  const [runs, setRuns] = useState(null);
  const [runId, setRunId] = useState(null);
  useEffect(() => {
    loadRuns().then((loaded) => {
      setRuns(loaded);
      setRunId(loaded[loaded.length - 1]?.id ?? null);
    });
  }, []);
  const report = runs ? (runs.find((r) => r.id === runId) ?? runs[runs.length - 1]).report : null;

  // Every hook runs on every render: an early return placed above a useMemo
  // changes the hook count between renders (React error #310).
  const series = useMemo(
    () => (report ? cumulativeSeries(report, regime) : []),
    [report, regime],
  );
  // Saving-vs-no-memory for every run, so two configurations sit on one scale.
  const comparison = useMemo(
    () =>
      (runs ?? []).map((run) => {
        const s = savingSeries(run.report, regime);
        // Crossing zero *and staying there* -- same rule the harness uses.
        let crossoverAt = null;
        for (const point of s) {
          if (point.saving > 0) { if (crossoverAt === null) crossoverAt = point.ordinal; }
          else crossoverAt = null;
        }
        return {
          id: run.id,
          shortLabel: run.id,
          label: run.label,
          note: run.note,
          series: s,
          crossoverAt,
          marginal: marginalFor(run.report, regime),
        };
      }),
    [runs, regime],
  );

  if (!report) return <div style={{ padding: 32 }}>Loading run…</div>;

  const crossoverAt = crossover(series);
  const saving = netSaving(series);
  const agents = report.accounting.agents;
  const primer = report.accounting.primer;
  const marginal = marginalFor(report, regime);
  const pct = (v) => (v == null ? "--" : `${v >= 0 ? "" : "-"}${Math.abs(v).toFixed(0)}%`);

  return (
    <div style={{ maxWidth: 900, margin: "0 auto", padding: "28px 24px 64px" }}>
      {report.synthetic && (
        <div style={{ marginBottom: 20 }}>
          <Banner
            variant="attention"
            show
            message={
              <span>
                <strong>Synthetic data — no run has been executed.</strong> The 19 PRs, their
                diffs, modules, human-comment counts and beats are real. Every token count, dollar
                figure and quality score on this page is a placeholder for layout only. Do not
                quote or screenshot these numbers.
              </span>
            }
          />
        </div>
      )}

      <header style={{ marginBottom: 18 }}>
        <Typography.Heading size="L" as="h1">
          Repo understanding is a per-repo cost, not a per-PR cost
        </Typography.Heading>
        <Typography.Body style={{ opacity: 0.75, marginTop: 6 }}>
          Two PR-review agents, same {report.rows?.length ?? 0} real redis-py PRs, same model,
          prompt and tools. The only difference is a memory layer. Prompt caching cannot close this
          gap: its longest TTL is one hour, and no real PR cadence fits inside that.
        </Typography.Body>
        <div style={{ display: "flex", gap: 10, alignItems: "center", marginTop: 10, flexWrap: "wrap" }}>
          {/* Badge takes `label`, not children -- children render as nothing,
              which silently dropped the run provenance from the page. */}
          <Badge label={`config ${report.config_fingerprint}`} />
          {(runs?.length ?? 0) > 1 ? (
            <span style={{ display: "inline-flex", gap: 6, alignItems: "center" }}>
              {runs.map((run) => (
                <button key={run.id} onClick={() => setRunId(run.id)}
                        aria-pressed={run.id === runId} title={run.note}
                        style={{
                          padding: "4px 10px", borderRadius: 6, cursor: "pointer", color: "inherit",
                          fontSize: 12,
                          border: "1px solid " + (run.id === runId ? "rgba(0,112,243,0.55)" : "rgba(128,128,128,0.3)"),
                          background: run.id === runId ? "rgba(0,112,243,0.12)" : "transparent",
                          fontWeight: run.id === runId ? 600 : 400,
                        }}>
                  {run.id}
                </button>
              ))}
            </span>
          ) : (
            <Badge label={`run ${report.run_id}`} />
          )}
          <span style={{ marginLeft: "auto", display: "inline-flex", alignItems: "center", gap: 8 }}>
            <Typography.Body size="S">Dark</Typography.Body>
            <Switch checked={mode === "dark"} onCheckedChange={onToggleMode} aria-label="Dark mode" />
          </span>
        </div>
      </header>

      <div role="tablist" aria-label="Story acts"
           style={{ display: "flex", gap: 6, flexWrap: "wrap", borderBottom: "1px solid rgba(128,128,128,0.25)", paddingBottom: 8 }}>
        {ACTS.map((a) => (
          <button key={a.id} role="tab" aria-selected={act === a.id} onClick={() => setAct(a.id)}
                  style={{
                    padding: "7px 13px", borderRadius: 6, cursor: "pointer", color: "inherit",
                    border: "1px solid " + (act === a.id ? "rgba(0,112,243,0.55)" : "rgba(128,128,128,0.3)"),
                    background: act === a.id ? "rgba(0,112,243,0.12)" : "transparent",
                    fontWeight: act === a.id ? 600 : 400,
                  }}>
            {a.label}
          </button>
        ))}
      </div>

      {/* The lead tile is the *marginal* per-review saving, not the cumulative
          percentage. A cumulative figure is a function of sequence length,
          because the one-time primer is amortised over it -- so it answers
          "what did this run cost" rather than "what does a review cost". */}
      <div style={{ display: "flex", gap: 12, flexWrap: "wrap", margin: "18px 0" }}>
        <Kpi label="Saving per review"
             value={pct(marginal?.aggregate_pct)}
             help={marginal
               ? `${usd(marginal.mean_saving_usd)} per review, size-weighted, once primed. Median PR ${pct(marginal.median_pct)}.`
               : "not yet measured"} />
        <Kpi label="Primer pays back in"
             value={marginal?.primer_payback_prs ? `${marginal.primer_payback_prs.toFixed(1)} reviews` : "--"}
             help={`one-time ${usd(marginal?.primer_usd ?? primer?.primer_usd ?? 0)} ÷ ${usd(marginal?.mean_saving_usd ?? 0)} saved per review`} />
        <Kpi label="Break-even" value={crossoverAt == null ? "not reached" : `PR ${crossoverAt}`}
             help="first PR where cumulative memory cost stays below baseline" />
        <Kpi label="Context per review"
             value={pct(marginal?.context_aggregate_pct)}
             help="less context read, caching-independent. Larger than the cost saving because output is priced 5x input." />
      </div>

      {marginal && (
        <Typography.Body size="S" style={{ opacity: 0.7, marginTop: -6, marginBottom: 14 }}>
          Per-review figures exclude the primer and so do not depend on sequence length.
          Over these {marginal.n_prs} PRs the cumulative net saving is {usd(saving)} —
          a smaller number, because it is still paying off the primer. The worst single PR
          ({marginal.worst_pr?.pr_id}) came in at {pct(marginal.worst_pr?.saving_pct)}: memory
          cost more there than the baseline, and a mean would hide it.
        </Typography.Body>
      )}

      {act === "claim" && (
        <section>
          <div style={{ display: "flex", gap: 10, alignItems: "center", marginBottom: 10 }}>
            {Object.entries(REGIMES).map(([key, conf]) => (
              <button key={key} onClick={() => setRegime(key)}
                      aria-pressed={regime === key}
                      style={{
                        padding: "6px 12px", borderRadius: 6, cursor: "pointer",
                        border: "1px solid rgba(128,128,128,0.35)",
                        fontWeight: regime === key ? 600 : 400,
                        background: regime === key ? "rgba(0,112,243,0.12)" : "transparent",
                        color: "inherit",
                      }}>
                {conf.label}
              </button>
            ))}
          </div>
          <Typography.Body size="S" style={{ opacity: 0.7, marginBottom: 12 }}>
            {REGIMES[regime].help}
          </Typography.Body>
          {comparison.length > 1 && (
            <div style={{ marginBottom: 30 }}>
              <Typography.Heading size="S" as="h2" style={{ marginBottom: 4 }}>
                Both runs against no memory
              </Typography.Heading>
              {/* The one line that explains what differs between the runs. It is
                  read from the run manifest, not written here, so it cannot
                  drift from what was actually executed. */}
              <Typography.Body size="S" style={{ opacity: 0.75, marginBottom: 10 }}>
                Same 19 PRs, same model config ({report.config_fingerprint}); the runs differ only
                in how memory is retrieved —{" "}
                {comparison.map((run, i) => (
                  <span key={run.id}>
                    {i > 0 ? "; " : ""}<strong>{run.id}</strong> {run.note.replace(/\.$/, "")}
                  </span>
                ))}.
              </Typography.Body>
              <SavingVsNoMemory runs={comparison} mode={mode}
                                regimeLabel={REGIMES[regime].label} />
              <div style={{ display: "flex", gap: 18, flexWrap: "wrap", marginTop: 12 }}>
                {comparison.map((run) => (
                  <Typography.Body key={run.id} size="S" style={{ opacity: 0.8 }}>
                    <strong>{run.id}</strong>: {run.marginal ? pct(run.marginal.aggregate_pct) : "--"} per
                    review, break-even PR {run.crossoverAt ?? "not reached"}, worst PR{" "}
                    {run.marginal ? pct(run.marginal.worst_pr?.saving_pct) : "--"}
                  </Typography.Body>
                ))}
              </div>
            </div>
          )}
          <Typography.Heading size="S" as="h2" style={{ marginBottom: 4 }}>
            {runId}: what each agent cost
          </Typography.Heading>
          <CumulativeCost series={series} crossoverAt={crossoverAt} mode={mode}
                          regimeLabel={REGIMES[regime].label} />
        </section>
      )}

      {act === "mechanism" && (
        <section>
          <div style={{ display: "flex", gap: 10, marginBottom: 12 }}>
            {[["phase", "By phase"], ["cache", "By cache tier"]].map(([key, label]) => (
              <button key={key} onClick={() => setFacet(key)} aria-pressed={facet === key}
                      style={{
                        padding: "6px 12px", borderRadius: 6, cursor: "pointer",
                        border: "1px solid rgba(128,128,128,0.35)",
                        fontWeight: facet === key ? 600 : 400,
                        background: facet === key ? "rgba(0,112,243,0.12)" : "transparent",
                        color: "inherit",
                      }}>
                {label}
              </button>
            ))}
          </div>
          {["baseline", "memory"].map((agent) => (
            <div key={agent} style={{ marginBottom: 26 }}>
              <Typography.Heading size="S" as="h2" style={{ marginBottom: 6 }}>{agent}</Typography.Heading>
              <PerPrBreakdown rows={report.per_pr} agent={agent} facet={facet} mode={mode} />
            </div>
          ))}
        </section>
      )}

      {act === "guardrail" && (
        <section>
          <QualityChart gold={report.quality_gold} mode={mode} />
          <div style={{ marginTop: 22 }}>
            <Typography.Heading size="S" as="h2">Merged-human-comment proxy</Typography.Heading>
            <Typography.Body size="S" style={{ opacity: 0.7 }}>
              Whole sequence. Reported separately from the gold subset and never averaged with it —
              the hand-labelled subset deserves far more weight, and averaging would hide that.
              {report.quality_proxy?.baseline?.prs_blind
                ? ` ${report.quality_proxy.baseline.prs_blind} PRs have no human inline comments, so the proxy is blind on those.`
                : null}
            </Typography.Body>
            <ul style={{ fontSize: 13, lineHeight: 1.7 }}>
              {Object.entries(report.quality_proxy ?? {}).map(([agent, s]) => (
                <li key={agent}>
                  <strong>{agent}</strong>: agreement {pct(s.agreement)}, coverage {pct(s.coverage)}
                </li>
              ))}
            </ul>
          </div>
        </section>
      )}

      {act === "accounting" && (
        <section>
          {["baseline", "memory"].map((agent) => (
            <div key={agent} style={{ marginBottom: 28 }}>
              <Typography.Heading size="S" as="h2" style={{ marginBottom: 8 }}>{agent}</Typography.Heading>
              <AccountingTable report={report} agent={agent} />
            </div>
          ))}
        </section>
      )}

      {report.warnings?.length ? (
        <footer style={{ marginTop: 28 }}>
          <Typography.Heading size="S" as="h2">Caveats the harness recorded</Typography.Heading>
          <ul style={{ fontSize: 12, opacity: 0.8, lineHeight: 1.6 }}>
            {report.warnings.map((w) => <li key={w}>{w}</li>)}
          </ul>
        </footer>
      ) : null}
    </div>
  );
}
