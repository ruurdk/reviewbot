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

/**
 * Render once per data source. The synthetic pass verifies the fallback and its
 * banner; the real pass feeds an actual harness report.json (built from a real
 * ledger by tools/make-page-fixture.py) and verifies the page renders measured
 * numbers with the banner GONE. Only the synthetic path was covered for a
 * while, so `report.per_pr` could go missing from the harness and every check
 * still passed -- the page would only break on a real run.
 */
async function render({ fetchImpl }) {
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
  dom.window.fetch = fetchImpl;
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
  // Each act renders only while selected, so its content is absent from the DOM
  // until clicked. Visit every act and keep the union of what was rendered --
  // otherwise a broken chart or table in acts 2-4 is invisible to this test.
  const perAct = {};
  for (const tab of doc.querySelectorAll('[role="tab"]')) {
    const label = tab.textContent ?? "";
    tab.dispatchEvent(new dom.window.MouseEvent("click", { bubbles: true }));
    await settle();
    perAct[label] = {
      text: doc.body.textContent ?? "",
      rects: doc.querySelectorAll("svg rect").length,
      paths: doc.querySelectorAll("svg path").length,
      rows: doc.querySelectorAll("tbody tr").length,
      figures: doc.querySelectorAll("figure").length,
    };
  }
  // Back to the default act, so the first-paint checks below see first paint.
  const first = doc.querySelector('[role="tab"]');
  if (first) {
    first.dispatchEvent(new dom.window.MouseEvent("click", { bubbles: true }));
    await settle();
  }
  const allText = Object.values(perAct).map((a) => a.text).join(" ") + (doc.body.textContent ?? "");
  return { doc, text: doc.body.textContent ?? "", allText, perAct, errors };
}

const REAL_FIXTURE = "scripts/fixtures/report-real.json";
const synthetic = await render({ fetchImpl: () => Promise.reject(new Error("no report.json")) });
const real = existsSync(REAL_FIXTURE)
  ? await render({
      fetchImpl: () =>
        Promise.resolve({ ok: true, json: async () => JSON.parse(readFileSync(REAL_FIXTURE, "utf8")) }),
    })
  : null;

const { doc, text, errors } = synthetic;

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

// Checks against a real harness report. These are the ones that fail if the
// report's shape drifts from what contract.js reads.
const REAL_CHECKS = real
  ? [
      ["real report: renders", () => real.text.length > 400],
      ["real report: synthetic banner is gone", () => !/synthetic data/i.test(real.text)],
      ["real report: run id shown", () => /smoke-2/.test(real.text)],
      ["real report: cumulative chart drawn from per_pr", () =>
        real.doc.querySelectorAll("svg path").length > 1],
      ["real report: accounting table joins PR numbers, not blanks", () =>
        /4059/.test(real.allText) && /4054/.test(real.allText)],
      ["real report: accounting table has a row per measured PR", () =>
        Object.values(real.perAct).some((a) => a.rows >= 3)],
      ["real report: per-PR bars drawn", () =>
        Object.values(real.perAct).some((a) => a.rects > 2)],
      ["real report: quality chart rendered", () =>
        /precision|recall|proxy/i.test(real.allText)],
      ["real report: harness warnings surfaced", () => /production-equivalent|budget/i.test(real.text)],
    ]
  : [];

let failed = 0;
for (const [label, fn] of [...CHECKS, ...REAL_CHECKS]) {
  let ok = false;
  try { ok = !!fn(); } catch { ok = false; }
  if (!ok) failed += 1;
  console.log(`  [${ok ? "ok  " : "FAIL"}] ${label}`);
}
if (!real) {
  console.log(`  [skip] real-report checks -- no ${REAL_FIXTURE}`);
}

const allErrors = [...errors, ...(real?.errors ?? [])];
if (allErrors.length) {
  console.log(`\n  ${allErrors.length} console/jsdom error(s):`);
  for (const e of allErrors.slice(0, 6)) {
    console.log("   ", String(e.detail?.message ?? e.message ?? e).split("\n")[0].slice(0, 220));
  }
}

const fatal = failed > 0 || allErrors.length > 0;
console.log(fatal ? "\nsmoke test FAILED" : "\nsmoke test passed");
process.exit(fatal ? 1 : 0);
