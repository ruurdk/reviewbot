import React, { useMemo, useState } from "react";
import { RUN_DASH, RUN_STROKE } from "../theme/series";
import { MARK, linePath, linear, ticks, usd } from "./scales";

/**
 * The comparison figure: cumulative saving versus no memory, one line per run.
 *
 * Why saving and not absolute cost. Each run carries its own baseline, and the
 * two baselines are not the same number: they read byte-identical context
 * (3,890,269 tokens in both, which is the reproducibility check) but model
 * output is non-deterministic, so run-1's control billed $32.67 and run-2's
 * $33.54. Drawing run-1's memory line against run-2's baseline would compare
 * across controls; nominating one baseline to stand for both would silently
 * drop the other. Differencing each run against its own control avoids both.
 *
 * Zero is therefore the no-memory line, not a decorative axis: above it memory
 * is ahead, below it memory is behind, and each line's crossing is that run's
 * break-even PR. Both lines start *below* zero because the primer is paid at
 * ordinal 0 before a single review happens -- that dip is the honest shape and
 * it is what makes the crossing mean something.
 *
 * One axis, as everywhere here. Two runs on one scale is a comparison; two
 * scales would be a rigged visual.
 */
export function SavingVsNoMemory({ runs, mode = "light", regimeLabel, height = 340 }) {
  const [hover, setHover] = useState(null);
  const pad = { top: 26, right: 116, bottom: 40, left: 68 };
  const width = 760;
  const plotW = width - pad.left - pad.right;
  const plotH = height - pad.top - pad.bottom;
  const stroke = RUN_STROKE[mode];

  const { x, y, yTicks } = useMemo(() => {
    const all = runs.flatMap((r) => r.series);
    const xs = all.map((d) => d.ordinal);
    const values = all.map((d) => d.saving);
    // The domain must include zero: it is the reference the whole chart is
    // read against, so it can never fall outside the plotted range.
    const lo = Math.min(0, ...values);
    const hi = Math.max(0, ...values, 0.001);
    const span = hi - lo || 1;
    return {
      x: linear([Math.min(...xs), Math.max(...xs)], [pad.left, pad.left + plotW]),
      y: linear([lo - span * 0.08, hi + span * 0.1], [pad.top + plotH, pad.top]),
      yTicks: ticks(lo - span * 0.08, hi + span * 0.1, 5),
    };
  }, [runs, plotW, plotH, pad.left, pad.top]);

  const nearest = (evt) => {
    const box = evt.currentTarget.getBoundingClientRect();
    const px = ((evt.clientX - box.left) / box.width) * width;
    setHover(Math.round(x.invert(px)));
  };

  const zeroY = y(0);

  return (
    <figure style={{ margin: 0 }}>
      <svg
        viewBox={`0 0 ${width} ${height}`}
        width="100%"
        role="img"
        aria-label={
          `Cumulative saving versus no memory for ${runs.length} runs over ` +
          `${runs[0]?.series.length ?? 0} pull requests, ${regimeLabel}`
        }
        onMouseMove={nearest}
        onMouseLeave={() => setHover(null)}
      >
        {yTicks.map((t) => (
          <line key={t} x1={pad.left} x2={pad.left + plotW} y1={y(t)} y2={y(t)}
                stroke="currentColor" strokeOpacity={0.09} strokeWidth={1} />
        ))}
        {yTicks.map((t) => (
          <text key={`l${t}`} x={pad.left - 10} y={y(t) + 4} textAnchor="end"
                fontSize={11} fill="currentColor" fillOpacity={0.55}>
            {usd(t)}
          </text>
        ))}

        {/* The no-memory reference. Heavier than the grid because every value
            on this chart is read relative to it. */}
        <line x1={pad.left} x2={pad.left + plotW} y1={zeroY} y2={zeroY}
              stroke="currentColor" strokeOpacity={0.5} strokeWidth={1.5} />
        <text x={pad.left + 4} y={zeroY - 7} fontSize={11} fill="currentColor" fillOpacity={0.7}>
          no memory (baseline)
        </text>

        {runs.map((run, i) => {
          const pts = run.series.map((d) => [x(d.ordinal), y(d.saving)]);
          const last = run.series[run.series.length - 1];
          return (
            <g key={run.id}>
              <path d={linePath(pts)} fill="none" stroke={stroke}
                    strokeWidth={MARK.lineWidth} strokeLinecap="round"
                    strokeDasharray={RUN_DASH[Math.min(i, RUN_DASH.length - 1)]} />
              {/* Direct label: identity is never colour alone. */}
              <text x={pad.left + plotW + 8} y={y(last.saving) + 4} fontSize={12}
                    fill="currentColor" fillOpacity={0.85}>
                {run.shortLabel}
              </text>
              {run.crossoverAt != null && (
                <g>
                  <circle cx={x(run.crossoverAt)} cy={zeroY} r={MARK.markerSize / 2}
                          fill={stroke}
                          stroke="var(--chart-surface, #fff)" strokeWidth={2} />
                  <text x={x(run.crossoverAt)} y={zeroY + 18} textAnchor="middle" fontSize={11}
                        fill="currentColor" fillOpacity={0.75}>
                    break-even: PR {run.crossoverAt}
                  </text>
                </g>
              )}
            </g>
          );
        })}

        {hover != null && runs.some((r) => r.series.some((d) => d.ordinal === hover)) && (
          <g>
            <line x1={x(hover)} x2={x(hover)} y1={pad.top} y2={pad.top + plotH}
                  stroke="currentColor" strokeOpacity={0.25} strokeWidth={1} />
            {runs.map((run, i) => {
              const point = run.series.find((d) => d.ordinal === hover);
              if (!point) return null;
              return (
                <g key={run.id}>
                  <circle cx={x(hover)} cy={y(point.saving)} r={MARK.markerSize / 2}
                          fill={stroke}
                          stroke="var(--chart-surface, #fff)" strokeWidth={2} />
                  <text x={x(hover) + 10} y={pad.top + 14 + i * 16} fontSize={11}
                        fill="currentColor" fillOpacity={0.85}>
                    {run.shortLabel} {usd(point.saving)}
                  </text>
                </g>
              );
            })}
          </g>
        )}

        <text x={pad.left + plotW / 2} y={height - 8} textAnchor="middle" fontSize={11}
              fill="currentColor" fillOpacity={0.6}>
          pull request, in sequence order
        </text>
      </svg>
      <figcaption style={{ fontSize: 12, opacity: 0.7, marginTop: 8, lineHeight: 1.55 }}>
        Cumulative billed cost saved against that run&apos;s own baseline, {regimeLabel.toLowerCase()}.
        Each run has its own control, so differencing within a run is the only comparison
        that does not mix controls — the two baselines read identical context but bill
        differently, because model output is not deterministic. Below the line, memory has
        not yet paid for its primer.
      </figcaption>
    </figure>
  );
}
