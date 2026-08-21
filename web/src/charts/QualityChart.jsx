import React from "react";
import { SERIES } from "../theme/series";
import { barPath, band, linear, pct } from "./scales";

const METRICS = [
  { key: "precision", label: "Precision", better: "higher" },
  { key: "recall", label: "Recall", better: "higher" },
  { key: "false_positive_rate", label: "False-positive rate", better: "lower" },
];

/**
 * Quality lives on its OWN chart, never on the cost chart's axis.
 *
 * A dual-axis cost-and-quality chart is the most common charting error and, in
 * this demo specifically, the place a skeptic would most suspect a rigged
 * visual. Two charts, one scale each.
 *
 * Note the false-positive-rate group: lower is better there, so the axis label
 * says so rather than leaving the reader to infer that a shorter bar is good.
 */
export function QualityChart({ gold, mode = "light", height = 240 }) {
  const colors = SERIES[mode];
  const agents = ["baseline", "memory"].filter((a) => gold?.[a]);
  if (!agents.length) return null;

  const width = 560;
  const pad = { top: 18, right: 16, bottom: 52, left: 52 };
  const plotW = width - pad.left - pad.right;
  const plotH = height - pad.top - pad.bottom;
  const groups = band(METRICS.map((m) => m.key), [pad.left, pad.left + plotW], 0.32);
  const y = linear([0, 1], [pad.top + plotH, pad.top]);

  return (
    <figure style={{ margin: 0 }}>
      <svg viewBox={`0 0 ${width} ${height}`} width="100%" role="img"
           aria-label="Review quality on the hand-labelled gold subset: precision, recall, and false-positive rate per agent">
        {[0, 0.25, 0.5, 0.75, 1].map((t) => (
          <g key={t}>
            <line x1={pad.left} x2={pad.left + plotW} y1={y(t)} y2={y(t)}
                  stroke="currentColor" strokeOpacity={0.12} />
            <text x={pad.left - 8} y={y(t) + 4} textAnchor="end" fontSize={11}
                  fill="currentColor" fillOpacity={0.6}>{pct(t)}</text>
          </g>
        ))}
        {METRICS.map((metric) => {
          const gw = groups.bandwidth();
          const barW = gw / agents.length - 4;
          return (
            <g key={metric.key}>
              {agents.map((agent, i) => {
                const value = gold[agent][metric.key];
                if (value == null) return null;
                const bx = groups(metric.key) + i * (barW + 4);
                const top = y(value);
                return (
                  <g key={agent}>
                    <path d={barPath(bx, top, barW, pad.top + plotH - top)} fill={colors[agent]} />
                    <text x={bx + barW / 2} y={top - 5} textAnchor="middle" fontSize={11} fill="currentColor">
                      {pct(value)}
                    </text>
                  </g>
                );
              })}
              <text x={groups(metric.key) + gw / 2} y={height - 30} textAnchor="middle" fontSize={11}
                    fill="currentColor" fillOpacity={0.75}>
                {metric.label}
              </text>
              <text x={groups(metric.key) + gw / 2} y={height - 17} textAnchor="middle" fontSize={10}
                    fill="currentColor" fillOpacity={0.5}>
                {metric.better} is better
              </text>
            </g>
          );
        })}
      </svg>
      <div style={{ display: "flex", gap: 16, fontSize: 11, marginTop: 4 }}>
        {agents.map((agent) => (
          <span key={agent} style={{ display: "inline-flex", alignItems: "center", gap: 5 }}>
            <span style={{ width: 10, height: 10, borderRadius: 2, background: colors[agent] }} />
            {agent}
          </span>
        ))}
      </div>
      <figcaption style={{ fontSize: 12, opacity: 0.75, marginTop: 6 }}>
        Hand-labelled gold subset only. Token savings bought with worse reviews are a regression,
        not a result -- so this chart is reported even when it is unflattering.
      </figcaption>
    </figure>
  );
}
