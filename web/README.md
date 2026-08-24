# Replay page

The narrative surface for the frozen run (spec §8a). React + styled-components v5
+ redis-ui, charts hand-built because redis-ui ships none.

```bash
npm install                # needs npm_config_cache set if ~/.npm is unwritable
npm run dev                # http://localhost:5173
npm run check              # palette validation + DOM smoke test
npm run build              # production bundle into dist/
npm run tokens             # print the real theme tokens from the installed package
```

## Showing a real run

The page reads `report.json` from the served root and falls back to a synthetic
fixture when it is absent:

```bash
python3 -m reviewbot report runs/<run-id>       # writes runs/<id>/report.json
cp runs/<run-id>/report.json web/public/
```

**While the fixture is in use the page shows a permanent banner and the run id
reads `SYNTHETIC-no-run-executed`.** The 19 PRs, their diffs, modules,
human-comment counts, beats and gold flags in the fixture are real, pulled from
`data/sequence.json`. Every token count, dollar figure and quality score is a
placeholder for layout only. Do not remove the banner, and do not screenshot the
page externally until a real `report.json` exists — plausible fake numbers are
the easiest way for this project to mislead someone.

## Why the charts are hand-built

`@redis-ui/components` 51.2.0 ships 56 components and `Gauge` is the only
visualization-adjacent one. There is also **no `Table`** — only `TableHeading`,
a styled `div` with no sort props. So redis-ui supplies the shell (theme,
typography, `Banner`, `Card`, `Badge`, `Switch`, `TableHeading`) and the charts
and the accounting table are built against its theme tokens.

## Two things to keep intact

**Colours are validated, not chosen.** `npm run validate-colors` grades the agent
series as a categorical palette and the phase/cache stacks as ordinal ramps,
against the installed `@redis-ui/styles`. It exits non-zero on failure. Re-run
after any version bump — the numbers quoted in the spec are only valid for
21.2.0.

**One scale per figure.** Cost and quality never share an axis. A dual-axis
chart is the most common charting error and, in this demo, the place a skeptic
would most suspect a rigged visual. The §7d as-measured / production-cadence
comparison is a toggle on one scale, not a second axis. The smoke test asserts
that every `<figure>` holds at most one `<svg>`.

## Verification

`npm run smoke` builds an IIFE bundle (jsdom cannot execute ES modules) and
renders the page in jsdom, asserting 15 things a viewer would notice: the hero
thesis, the synthetic banner, KPI tiles, the regime toggle, the act switcher, the
drawn chart with an accessible name, the crossover annotation, direct series
labels, a figure caption, a real heading hierarchy, clean axis labels, and the
single-scale-per-figure rule. It caught four genuine bugs that a successful
`vite build` did not: a React hooks-order violation, and three wrong redis-ui
component APIs.

## The smoke test renders twice

`npm run smoke` builds an IIFE bundle, evaluates it in jsdom, and asserts what a
viewer would actually see. It runs the page against two data sources:

1. **`fetch` rejecting** — the synthetic fallback, whose banner must be present.
2. **`scripts/fixtures/report-real.json`** — a report the harness produced, whose
   banner must be *gone* and whose PR numbers, bars and table rows must appear.

Only the first existed for a while, which is exactly how the harness's report
came to lack the `per_pr` array `src/data/contract.js` reads: every check passed
and a real run would have rendered a broken page. Regenerate the fixture from any
completed run:

```bash
python3 tools/make_page_fixture.py runs/<run-id>     # from the repo root
```

The test also clicks through all four acts. An act renders only while selected,
so a broken chart or table in acts 2-4 is invisible to a first-paint assertion.

Component APIs this has caught, none of which `vite build` objects to: a
hooks-order violation (React #310), `Tabs` not being Radix-shaped, `Banner`
having no `"warning"` variant, `Switch` using `onCheckedChange` -- and `Badge`,
which takes a `label` string and **silently renders nothing** when handed
children, which is how the run id and config fingerprint disappeared from the
header.
