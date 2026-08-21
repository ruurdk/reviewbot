import React, { useMemo, useState } from "react";
import { SERIES } from "../theme/series";
import { MARK, linePath, linear, ticks, usd } from "./scales";

/**
 * The close: cumulative billed cost per agent, with the crossover annotated.
 *
 * One axis. Never a second y-scale -- cost and quality live on separate charts
 * (the dual-axis chart is both the most common charting error and, here, exactly
 * where a skeptic would suspect a rigged visual).
 *
 * The memory line starts *above* the baseline because the primer sits at
 * ordinal 0. That upfront spike is the honest shape and it is what makes the
 * crossover mean something.
 */
export function CumulativeCost({ series, crossoverAt, mode = "light", regimeLabel, height = 340 }) {
  const [hover, setHover] = useState(null);
  const pad = { top: 24, right: 96, bottom: 40, left: 64 };
  const width = 760;
  const plotW = width - pad.left - pad.right;
  const plotH = height - pad.top - pad.bottom;
  const colors = SERIES[mode];

  const { x, y, yTicks } = useMemo(() => {
    const xs = series.map((d) => d.ordinal);
    const maxY = Math.max(...series.flatMap((d) => [d.baseline, d.memory]), 0.001);
    return {
      x: linear([Math.min(...xs), Math.max(...xs)], [pad.left, pad.left + plotW]),
      y: linear([0, maxY * 1.08], [pad.top + plotH, pad.top]),
      yTicks: ticks(0, maxY * 1.08, 5),
    };
  }, [series, plotW, plotH, pad.left, pad.top]);

  const paths = {
    baseline: linePath(series.map((d) => [x(d.ordinal), y(d.baseline)])),
    memory: linePath(series.map((d) => [x(d.ordinal), y(d.memory)])),
  };
  const last = series[series.length - 1];
  const nearest = (evt) => {
    const box = evt.currentTarget.getBoundingClientRect();
    const px = ((evt.clientX - box.left) / box.width) * width;
    const ordinal = Math.round(x.invert(px));
    setHover(series.find((d) => d.ordinal === ordinal) ?? null);
  };

  return (
    <figure style={{ margin: 0 }}>
      <svg
        viewBox={`0 0 ${width} ${height}`}
        width="100%"
        role="img"
        aria-label={`Cumulative billed cost per agent over ${series.length} pull requests, ${regimeLabel}`}
        onMouseMove={nearest}
        onMouseLeave={() => setHover(null)}
      >
        {/* recessive grid */}
        {yTicks.map((t) => (
          <g key={t}>
            <line
              x1={pad.left} x2={pad.left + plotW} y1={y(t)} y2={y(t)}
              stroke="currentColor" strokeOpacity={0.12} strokeWidth={1}
            />
            <text x={pad.left - 10} y={y(t) + 4} textAnchor="end" fontSize={11} fill="currentColor" fillOpacity={0.6}>
              {usd(t)}
            </text>
          </g>
        ))}
        {series.map((d) => (
          <text key={d.ordinal} x={x(d.ordinal)} y={height - 14} textAnchor="middle" fontSize={11}
                fill="currentColor" fillOpacity={0.6}>
            {d.ordinal === 0 ? "prime" : d.ordinal}
          </text>
        ))}

        {/* crossover annotation: a labelled rule, not a floating marker */}
        {crossoverAt != null && (
          <g>
            <line
              x1={x(crossoverAt)} x2={x(crossoverAt)} y1={pad.top} y2={pad.top + plotH}
              stroke="currentColor" strokeOpacity={0.35} strokeWidth={1} strokeDasharray="4 4"
            />
            <text x={x(crossoverAt) + 6} y={pad.top + 12} fontSize={11} fill="currentColor" fillOpacity={0.75}>
              break-even: PR {crossoverAt}
            </text>
          </g>
        )}

        <path d={paths.baseline} fill="none" stroke={colors.baseline} strokeWidth={MARK.lineWidth} />
        <path d={paths.memory} fill="none" stroke={colors.memory} strokeWidth={MARK.lineWidth} />

        {/* direct labels at the line ends: identity is never colour-alone */}
        {last && (
          <>
            <text x={pad.left + plotW + 8} y={y(last.baseline) + 4} fontSize={12} fill="currentColor">
              baseline
            </text>
            <text x={pad.left + plotW + 8} y={y(last.memory) + 4} fontSize={12} fill="currentColor">
              memory
            </text>
          </>
        )}

        {hover && (
          <g>
            <line x1={x(hover.ordinal)} x2={x(hover.ordinal)} y1={pad.top} y2={pad.top + plotH}
                  stroke="currentColor" strokeOpacity={0.25} strokeWidth={1} />
            {["baseline", "memory"].map((agent) => (
              <circle key={agent} cx={x(hover.ordinal)} cy={y(hover[agent])} r={MARK.markerRadius}
                      fill={colors[agent]} stroke={mode === "dark" ? "#1a1a19" : "#fcfcfb"}
                      strokeWidth={MARK.ringWidth} />
            ))}
          </g>
        )}
      </svg>

      <figcaption style={{ fontSize: 12, opacity: 0.75, marginTop: 8 }}>
        {hover ? (
          <span>
            PR {hover.ordinal === 0 ? "primer" : hover.ordinal}: baseline {usd(hover.baseline)} ·
            memory {usd(hover.memory)} · net{" "}
            {usd(hover.baseline - hover.memory)}
          </span>
        ) : (
          <span>
            Cumulative billed cost, {regimeLabel.toLowerCase()}. Ordinal 0 is the one-time primer.
          </span>
        )}
      </figcaption>
    </figure>
  );
}
