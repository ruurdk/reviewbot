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
const RUNS_FIXTURE = "scripts/fixtures/runs.json";

// URL-aware, because the page now fetches a run manifest and then one report
// per run. A fetch stub that ignores its argument would serve the manifest as a
// report (and vice versa) and every multi-run check would pass on nonsense.
const serve = (files) => (url) => {
  const name = String(url).split("/").pop();
  const path = files[name];
  if (!path || !existsSync(path)) return Promise.reject(new Error(`404 ${name}`));
  return Promise.resolve({ ok: true, json: async () => JSON.parse(readFileSync(path, "utf8")) });
};

const synthetic = await render({ fetchImpl: () => Promise.reject(new Error("no report.json")) });
// Single-run mode: no manifest, so the page must fall back to report.json.
const real = existsSync(REAL_FIXTURE)
  ? await render({ fetchImpl: serve({ "report.json": REAL_FIXTURE }) })
  : null;
// Multi-run mode: manifest plus one report per run.
const runsManifest = existsSync(RUNS_FIXTURE)
  ? JSON.parse(readFileSync(RUNS_FIXTURE, "utf8"))
  : null;
const multi = runsManifest
  ? await render({
      fetchImpl: serve({
        "runs.json": RUNS_FIXTURE,
        ...Object.fromEntries(
          runsManifest.runs.map((r) => [r.url, `scripts/fixtures/${r.url}`]),
        ),
      }),
    })
  : null;

const { doc, text, errors } = synthetic;

const CHECKS = [
  ["renders something", () => text.length > 400],
  ["hero thesis", () => /per-repo cost/i.test(text)],
  ["synthetic-data banner", () => /synthetic data/i.test(text)],
  // The headline is the *marginal* per-review saving. "net saving" alone is a
  // useless probe now: the words also appear in the caveat paragraph below the
  // tiles, so the old check passed even when the tile itself was empty.
  ["saving-per-review is the lead KPI", () => /saving per review/i.test(text)],
  ["the marginal figure renders a number, not a dash", () => {
    const m = text.match(/saving per review[\s\S]{0,80}?(-?\d+)%/i);
    return !!m;
  }],
  ["primer payback is priced in reviews", () =>
    /pays back in/i.test(text) && /\d+(\.\d+)?\s*reviews/i.test(text)],
  ["context-per-review KPI", () => /context per review/i.test(text)],
  ["the cumulative figure is qualified, not dropped", () =>
    /do not depend on sequence length/i.test(text) && /cumulative net saving/i.test(text)],
  ["the worst PR is disclosed beside the mean", () => /worst single PR/i.test(text)],
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
      // Read the id from the fixture: hardcoding it broke the moment the
      // fixture was regenerated from a different run.
      ["real report: run id shown", () =>
        real.text.includes(JSON.parse(readFileSync(REAL_FIXTURE, "utf8")).run_id)],
      ["real report: cumulative chart drawn from per_pr", () =>
        real.doc.querySelectorAll("svg path").length > 1],
      ["real report: accounting table joins PR numbers, not blanks", () => {
        const rows = JSON.parse(readFileSync(REAL_FIXTURE, "utf8")).rows ?? [];
        return rows.length > 0 && rows.every((r) => real.allText.includes(String(r.pr_number)));
      }],
      ["real report: accounting table has a row per measured PR", () =>
        Object.values(real.perAct).some((a) => a.rows >= 3)],
      ["real report: per-PR bars drawn", () =>
        Object.values(real.perAct).some((a) => a.rects > 2)],
      ["real report: quality chart rendered", () =>
        /precision|recall|proxy/i.test(real.allText)],
      ["real report: harness warnings surfaced", () => /production-equivalent|budget/i.test(real.text)],
      // The strongest check on the new headline: the number on screen must be
      // the one the harness computed. A wrong field path would render "--", and
      // a stale report.json would render last week's figure -- both of which
      // look like a working page.
      ["real report: headline % is the harness's marginal figure", () => {
        const fixture = JSON.parse(readFileSync(REAL_FIXTURE, "utf8"));
        const expected = fixture.accounting?.marginal?.as_measured?.aggregate_pct;
        if (expected == null) return false;
        return new RegExp(`saving per review[\\s\\S]{0,80}?${Math.round(expected)}%`, "i")
          .test(real.text);
      }],
      ["real report: primer payback matches the harness", () => {
        const fixture = JSON.parse(readFileSync(REAL_FIXTURE, "utf8"));
        const payback = fixture.accounting?.marginal?.as_measured?.primer_payback_prs;
        return payback != null && real.text.includes(`${payback.toFixed(1)} reviews`);
      }],
    ]
  : [];

// Checks that only make sense with more than one published run.
const MULTI_CHECKS = multi
  ? [
      ["multi-run: every run is selectable", () =>
        runsManifest.runs.every((r) => multi.text.includes(r.id))],
      ["multi-run: comparison chart present", () =>
        /both runs against no memory/i.test(multi.text)],
      ["multi-run: the no-memory reference is labelled", () =>
        /no memory \(baseline\)/i.test(multi.text)],
      // The single line the deliverable asked for. It comes from the manifest,
      // so an empty `note` would render a sentence that explains nothing.
      ["multi-run: one line explains what differs", () =>
        /differ only\s+in how memory is retrieved/i.test(multi.text.replace(/\s+/g, " ")) &&
        runsManifest.runs.every((r) => r.note && multi.text.includes(r.note.replace(/\.$/, "")))],
      ["multi-run: each run reports its own per-review saving", () => {
        // Two runs, two different numbers -- if the page reused one run's
        // accounting for both, these would be identical.
        const pcts = runsManifest.runs.map((r) => {
          const rep = JSON.parse(readFileSync(`scripts/fixtures/${r.url}`, "utf8"));
          return Math.round(rep.accounting.marginal.as_measured.aggregate_pct);
        });
        return pcts.every((v) => multi.text.includes(`${v}% per`));
      }],
      ["multi-run: a break-even per run", () =>
        (multi.text.match(/break-even/gi) ?? []).length >= runsManifest.runs.length],
      ["multi-run: one y-scale per figure (no dual axis)", () =>
        [...multi.doc.querySelectorAll("figure")].every((f) => f.querySelectorAll("svg").length <= 1)],
      ["multi-run: lines are distinguishable without colour", () => {
        // Two runs share one validated hue, so dash is the separator. If both
        // paths were solid the chart would be unreadable in greyscale.
        const dashes = [...multi.doc.querySelectorAll("svg path[stroke-dasharray]")];
        return dashes.length >= 1;
      }],
    ]
  : [];

let failed = 0;
for (const [label, fn] of [...CHECKS, ...REAL_CHECKS, ...MULTI_CHECKS]) {
  let ok = false;
  try { ok = !!fn(); } catch { ok = false; }
  if (!ok) failed += 1;
  console.log(`  [${ok ? "ok  " : "FAIL"}] ${label}`);
}
if (!real) {
  console.log(`  [skip] real-report checks -- no ${REAL_FIXTURE}`);
}

const allErrors = [...errors, ...(real?.errors ?? []), ...(multi?.errors ?? [])];
if (allErrors.length) {
  console.log(`\n  ${allErrors.length} console/jsdom error(s):`);
  for (const e of allErrors.slice(0, 6)) {
    console.log("   ", String(e.detail?.message ?? e.message ?? e).split("\n")[0].slice(0, 220));
  }
}

const fatal = failed > 0 || allErrors.length > 0;
console.log(fatal ? "\nsmoke test FAILED" : "\nsmoke test passed");
process.exit(fatal ? 1 : 0);
