// Minimal scale + axis helpers. redis-ui ships no chart components (691 stories,
// `Gauge` is the only viz-adjacent one), so the charts are hand-built -- but the
// geometry rules come from the visualization method, not from taste:
//   thin marks, 2px lines, >=8px markers, 4px rounded data-ends anchored to the
//   baseline, a 2px surface gap between stacked segments, recessive grid/axes.

export const MARK = {
  lineWidth: 2,
  markerRadius: 4.5,
  barRadius: 4,
  segmentGap: 2,
  ringWidth: 2,
};

export const linear = (domain, range) => {
  const [d0, d1] = domain;
  const [r0, r1] = range;
  const span = d1 - d0 || 1;
  const scale = (v) => r0 + ((v - d0) / span) * (r1 - r0);
  scale.invert = (p) => d0 + ((p - r0) / (r1 - r0 || 1)) * span;
  scale.domain = domain;
  scale.range = range;
  return scale;
};

export const band = (values, [r0, r1], padding = 0.25) => {
  const n = values.length || 1;
  const step = (r1 - r0) / n;
  const width = step * (1 - padding);
  const scale = (v) => {
    const i = values.indexOf(v);
    return r0 + i * step + (step - width) / 2;
  };
  scale.bandwidth = () => width;
  scale.step = step;
  scale.values = values;
  return scale;
};

/** "Nice" ticks: at most `count`, on 1/2/5 x 10^n steps. */
export const ticks = (min, max, count = 5) => {
  const span = max - min || 1;
  const raw = span / count;
  const mag = 10 ** Math.floor(Math.log10(raw));
  const norm = raw / mag;
  const step = (norm >= 5 ? 5 : norm >= 2 ? 2 : 1) * mag;
  const start = Math.ceil(min / step) * step;
  const out = [];
  for (let v = start; v <= max + step * 1e-9; v += step) out.push(+v.toFixed(10));
  return out;
};

export const usd = (v) =>
  v === 0
    ? "$0"
    : v >= 10
      ? `$${v.toFixed(0)}`
      : v >= 1
        ? `$${v.toFixed(2)}`
        : `$${v.toFixed(3)}`;

export const tokens = (v) =>
  v >= 1e6 ? `${(v / 1e6).toFixed(1)}M` : v >= 1e3 ? `${Math.round(v / 1e3)}k` : String(v);

export const pct = (v) => (v == null ? "--" : `${Math.round(v * 100)}%`);

/** Path for a polyline through [x,y] points. */
export const linePath = (points) =>
  points.map((p, i) => `${i ? "L" : "M"}${p[0].toFixed(2)},${p[1].toFixed(2)}`).join(" ");

/** Rounded-top bar anchored to the baseline (flat bottom, rounded data-end). */
export const barPath = (x, y, w, h, r = MARK.barRadius) => {
  const radius = Math.min(r, w / 2, Math.max(h, 0));
  if (h <= 0) return "";
  return [
    `M${x},${y + h}`,
    `L${x},${y + radius}`,
    `Q${x},${y} ${x + radius},${y}`,
    `L${x + w - radius},${y}`,
    `Q${x + w},${y} ${x + w},${y + radius}`,
    `L${x + w},${y + h}`,
    "Z",
  ].join(" ");
};
