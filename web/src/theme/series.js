// Series colours, read from @redis-ui/styles 21.2.0 tokens and validated by
// `npm run validate-colors`. Never change a value here by eye -- re-run the
// validator, which grades the agent series as a categorical palette and the
// phase stack as an ordinal ramp.
//
// Why two chromatic hues instead of gray-plus-brand: the emphasis pairing
// (neutral + primary) fails the dark-mode lightness band. Every redis-ui neutral
// light enough to read as a series line sits above the band, so there is no
// neutral step that works in dark mode.
//
// Why discovery + primary specifically: Redis's other ramps are semantically
// named -- success, danger, attention, notice, informative are status families --
// so only primary (brand blue) and discovery (magenta) are status-neutral and
// available for series identity. This pair passes every check in *both* modes
// (CVD ΔE 20.5, normal 34.2), so identity does not change between themes, which
// matters because a reader who switches theme mid-talk should not have to
// re-learn the legend.
export const SERIES = {
  light: { baseline: "#D90B78", memory: "#0070f3" }, // discovery400, primary400
  dark: { baseline: "#D90B78", memory: "#0070f3" },
};

// prime -> retrieve -> review -> write is an *ordered* pipeline, so the stack
// takes a single-hue ordinal ramp rather than four categorical hues. That is the
// correct form and it sidesteps the status-name collision: a danger-red "write"
// segment would read as an error.
// The light and dark ramps differ by one step at the pale end: primary100
// (#8cc4fc) reads at only 1.79:1 against the light surface, below the 2:1 floor
// for the palest step, so light mode starts at primary200.
export const PHASE_RAMP = {
  light: ["#52a9ff", "#0091ff", "#0070f3", "#064ea2"], // primary200,300,400,600
  dark: ["#8cc4fc", "#0091ff", "#0070f3", "#064ea2"], // primary100,300,400,600
};

export const PHASES = ["prime", "retrieve", "review", "write"];

// Caching tiers for the per-PR stacked bars: also ordered (uncached is the
// expensive end), so also an ordinal ramp.
export const CACHE_TIERS = ["uncached", "cache_write", "cache_read"];
export const CACHE_RAMP = {
  light: ["#0091ff", "#0070f3", "#064ea2"],
  dark: ["#0091ff", "#0070f3", "#064ea2"],
};

// Status colours stay reserved for the quality guardrail, never for series.
export const STATUS = {
  good: "#16a34a", // success500
  bad: "#dc2626", // danger500
};

export const SURFACE = { light: "#fcfcfb", dark: "#1a1a19" };
