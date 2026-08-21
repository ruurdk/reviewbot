import React, { useState } from "react";
import { CACHE_RAMP, CACHE_TIERS, PHASES, PHASE_RAMP } from "../theme/series";
import { MARK, band, barPath, linear, ticks, tokens } from "./scales";

const FACETS = {
  phase: { keys: PHASES, ramp: PHASE_RAMP, field: "by_phase", label: "by phase" },
  cache: { keys: CACHE_TIERS, ramp: CACHE_RAMP, field: "tiers", label: "by cache tier" },
};

const LABELS = {
  prime: "prime", retrieve: "retrieve", review: "review", write: "write",
  uncached: "uncached (1x)", cache_write: "cache write (1.25x)", cache_read: "cache read (0.1x)",
};

/**
 * Per-PR context volume, stacked, faceted by phase or by caching tier.
 *
 * Context volume rather than dollars on purpose: this is the caching-independent
 * series, so the stack shows how much the agent *read*, which is what "memory
 * means less context" actually claims.
 *
 * A 2px surface gap separates stacked segments so adjacent fills never merge.
 */
export function PerPrBreakdown({ rows, agent, facet = "phase", mode = "light", height = 260 }) {
  const [hover, setHover] = useState(null);
  const conf = FACETS[facet];
  const ramp = conf.ramp[mode];
  const data = rows.filter((r) => r.agent === agent).sort((a, b) => a.pr_ordinal - b.pr_ordinal);
  const pad = { top: 20, right: 16, bottom: 34, left: 60 };
  const width = 760;
  const plotW = width - pad.left - pad.right;
  const plotH = height - pad.top - pad.bottom;

  const ordinals = data.map((d) => d.pr_ordinal);
  const totals = data.map((d) =>
    conf.keys.reduce((acc, k) => acc + ((d[conf.field] ?? {})[k] ?? 0), 0),
  );
  const maxY = Math.max(...totals, 1);
  const x = band(ordinals, [pad.left, pad.left + plotW], 0.3);
  const y = linear([0, maxY * 1.05], [pad.top + plotH, pad.top]);
  const yTicks = ticks(0, maxY * 1.05, 4);
  const surface = mode === "dark" ? "#1a1a19" : "#fcfcfb";

  return (
    <figure style={{ margin: 0 }}>
      <svg viewBox={`0 0 ${width} ${height}`} width="100%" role="img"
           aria-label={`Per-pull-request context volume for the ${agent} agent, stacked ${conf.label}`}>
        {yTicks.map((t) => (
          <g key={t}>
            <line x1={pad.left} x2={pad.left + plotW} y1={y(t)} y2={y(t)}
                  stroke="currentColor" strokeOpacity={0.12} />
            <text x={pad.left - 8} y={y(t) + 4} textAnchor="end" fontSize={11}
                  fill="currentColor" fillOpacity={0.6}>{tokens(t)}</text>
          </g>
        ))}

        {data.map((d) => {
          const w = x.bandwidth();
          let cursor = 0;
          return (
            <g key={d.pr_ordinal}
               onMouseEnter={() => setHover(d)} onMouseLeave={() => setHover(null)}>
              {/* hit target spans the full column height, not just the bar */}
              <rect x={x(d.pr_ordinal)} y={pad.top} width={w} height={plotH} fill="transparent" />
              {conf.keys.map((key, i) => {
                const value = (d[conf.field] ?? {})[key] ?? 0;
                if (!value) return null;
                const y0 = y(cursor);
                const y1 = y(cursor + value);
                cursor += value;
                const h = Math.max(y0 - y1 - MARK.segmentGap, 1);
                const isTop = cursor >= totals[ordinals.indexOf(d.pr_ordinal)] - 0.5;
                return (
                  <path key={key}
                        d={isTop ? barPath(x(d.pr_ordinal), y1, w, h) :
                                   `M${x(d.pr_ordinal)},${y1} h${w} v${h} h${-w} Z`}
                        fill={ramp[i % ramp.length]}
                        stroke={surface} strokeWidth={0.5} />
                );
              })}
            </g>
          );
        })}

        <text x={pad.left} y={height - 8} fontSize={11} fill="currentColor" fillOpacity={0.6}>
          PR ordinal
        </text>
        {data.map((d) => (
          <text key={d.pr_ordinal} x={x(d.pr_ordinal) + x.bandwidth() / 2} y={height - 20}
                textAnchor="middle" fontSize={10} fill="currentColor" fillOpacity={0.55}>
            {d.pr_ordinal === 0 ? "P" : d.pr_ordinal}
          </text>
        ))}
      </svg>

      <div style={{ display: "flex", gap: 14, flexWrap: "wrap", fontSize: 11, marginTop: 6 }}>
        {conf.keys.map((key, i) => (
          <span key={key} style={{ display: "inline-flex", alignItems: "center", gap: 5 }}>
            <span style={{ width: 10, height: 10, borderRadius: 2, background: ramp[i % ramp.length] }} />
            {LABELS[key] ?? key}
          </span>
        ))}
      </div>

      <figcaption style={{ fontSize: 12, opacity: 0.75, marginTop: 6 }}>
        {hover
          ? `PR ${hover.pr_ordinal === 0 ? "primer" : hover.pr_ordinal}: ` +
            conf.keys
              .filter((k) => (hover[conf.field] ?? {})[k])
              .map((k) => `${LABELS[k] ?? k} ${tokens(hover[conf.field][k])}`)
              .join(" · ")
          : `Context volume per PR for the ${agent} agent, stacked ${conf.label}. Caching-independent.`}
      </figcaption>
    </figure>
  );
}
