/**
 * Render the built page in jsdom and assert what a viewer would see.
 *
 * The palette validator checks colour; this checks that the thing renders and
 * says what it should. It exists because the component APIs here were written
 * against a library the author had not used before -- a wrong prop shape on
 * Tabs, Banner, Switch or TableHeading is the likeliest failure, and it is
 * invisible in a successful `vite build`.
 */
import { readFileSync, existsSync } from "node:fs";
import { JSDOM, VirtualConsole } from "jsdom";
import path from "node:path";

const BUNDLE = "dist-smoke/bundle.js";
if (!existsSync(BUNDLE)) {
  console.error("no dist-smoke/bundle.js -- run `npm run smoke` (it builds first)");
  process.exit(1);
}

const errors = [];
const virtualConsole = new VirtualConsole();
virtualConsole.on("jsdomError", (e) => errors.push(e));
virtualConsole.on("error", (...args) => errors.push(args.join(" ")));

const dom = new JSDOM('<!doctype html><html><body><div id="root"></div></body></html>', {
  // Required for window.eval to execute in the window's own scope.
  runScripts: "dangerously",
  url: "http://localhost/",
  pretendToBeVisual: true,
  virtualConsole,
});

// No report.json here, so the page must fall back to the synthetic fixture --
// which is exactly the state whose banner we want to verify.
dom.window.fetch = () => Promise.reject(new Error("no report.json"));
dom.window.matchMedia ??= () => ({ matches: false, addEventListener() {}, removeEventListener() {} });

// jsdom cannot run ES modules, hence the IIFE bundle evaluated by hand.
try {
  dom.window.eval(readFileSync(BUNDLE, "utf8"));
} catch (e) {
  errors.push(e);
}

const settle = () => new Promise((r) => setTimeout(r, 400));
await settle();
await settle();

const doc = dom.window.document;
const text = doc.body.textContent ?? "";

const CHECKS = [
  ["renders something", () => text.length > 400],
  ["hero thesis", () => /per-repo cost/i.test(text)],
  ["synthetic-data banner", () => /synthetic data/i.test(text)],
  ["net-saving KPI", () => /net saving/i.test(text)],
  ["break-even KPI", () => /break-even/i.test(text)],
  ["regime toggle present", () => /as measured/i.test(text) && /production cadence/i.test(text)],
  ["act tabs present", () => /where the tokens went/i.test(text)],
  ["cumulative chart drawn", () => doc.querySelectorAll("svg path").length > 1],
  ["chart has an accessible name", () =>
    [...doc.querySelectorAll("svg")].some((s) => /cumulative billed cost/i.test(s.getAttribute("aria-label") ?? ""))],
  ["crossover annotated", () => /break-even: PR/i.test(text)],
  ["series legend or direct labels", () => /baseline/i.test(text) && /memory/i.test(text)],
  ["figure has a caption", () => doc.querySelectorAll("figcaption").length > 0],
  ["real heading hierarchy for screen readers", () =>
    doc.querySelector("h1") && doc.querySelectorAll("h2").length > 0],
  ["axis labels are clean (no $0.000)", () =>
    ![...doc.querySelectorAll("text")].some((t) => /^\$0\.0+$/.test(t.textContent ?? ""))],
  ["no dual-axis (single y per figure)", () => {
    // Each figure must carry exactly one <svg>; two scales in one figure would
    // be the dual-axis error the spec forbids.
    return [...doc.querySelectorAll("figure")].every((f) => f.querySelectorAll("svg").length <= 1);
  }],
];

let failed = 0;
for (const [label, fn] of CHECKS) {
  let ok = false;
  try { ok = !!fn(); } catch { ok = false; }
  if (!ok) failed += 1;
  console.log(`  [${ok ? "ok  " : "FAIL"}] ${label}`);
}

if (errors.length) {
  console.log(`\n  ${errors.length} console/jsdom error(s):`);
  for (const e of errors.slice(0, 6)) {
    console.log("   ", String(e.detail?.message ?? e.message ?? e).split("\n")[0].slice(0, 220));
  }
}

const fatal = failed > 0 || errors.length > 0;
console.log(fatal ? "\nsmoke test FAILED" : "\nsmoke test passed");
process.exit(fatal ? 1 : 0);
