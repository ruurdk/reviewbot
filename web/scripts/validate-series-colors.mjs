// Validate every series colour the page uses, against the *installed*
// @redis-ui/styles, using the dataviz validator's own functions.
//
// Two different graders, because these are two different jobs:
//   - the agent series is a CATEGORICAL palette (identity) -> validate()
//   - the phase and cache stacks are ORDINAL ramps (ordered magnitude)
//     -> validateOrdinal(). Running the categorical checks on a ramp fails a
//     correct ramp by design: it spans the lightness band and its pale steps
//     drop below the chroma floor.
//
// Re-run after any @redis-ui/styles bump. The spec quotes these numbers.
import { readFileSync } from "node:fs";

const VALIDATOR =
  process.env.DATAVIZ_VALIDATOR ??
  "/tmp/claude-1000/bundled-skills/2.1.233/89ac679cda10bf23c2d9b74fdf854b18/dataviz/scripts/validate_palette.js";

const { validate, validateOrdinal } = await import(VALIDATOR);
const { SERIES, PHASE_RAMP, CACHE_RAMP, RUN_STROKE, SURFACE } = await import("../src/theme/series.js");

const version = JSON.parse(
  readFileSync("node_modules/@redis-ui/styles/package.json", "utf8"),
).version;

const show = (label, { ok, report }) => {
  console.log(`### ${label}: ${ok ? "PASS" : "FAIL"}`);
  for (const [name, passed, detail] of report) {
    console.log(`  [${passed ? "PASS" : "FAIL"}] ${name.padEnd(22)} ${detail}`);
  }
  console.log();
  return ok;
};

console.log(`@redis-ui/styles ${version}\n`);
let allOk = true;
for (const mode of ["light", "dark"]) {
  const surface = SURFACE[mode];
  allOk =
    show(`agent series, categorical (${mode})`,
      validate([SERIES[mode].baseline, SERIES[mode].memory], { mode, surface })) && allOk;
  // The run-comparison chart draws every run in one hue (dash separates them),
  // so it is a single-colour check: band, chroma and contrast, no CVD pair.
  allOk =
    show(`run comparison, single hue (${mode})`,
      validate([RUN_STROKE[mode]], { mode, surface })) && allOk;
  allOk =
    show(`phase stack, ordinal (${mode})`,
      validateOrdinal(PHASE_RAMP[mode], { mode, surface })) && allOk;
  allOk =
    show(`cache-tier stack, ordinal (${mode})`,
      validateOrdinal(CACHE_RAMP[mode], { mode, surface })) && allOk;
}
if (!allOk) {
  console.error("Palette validation failed. Do not ship these colours.");
  process.exit(1);
}
console.log("all palettes pass");
