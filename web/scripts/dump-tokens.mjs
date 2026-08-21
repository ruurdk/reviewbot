// Print the redis-ui theme tokens we build charts against.
//
// Values come from the installed package, never from memory: ramp steps move
// between versions, and the series-color validation recorded in the spec is only
// valid for the version it was run against. The package's `exports` map does not
// expose the per-theme token files, so they are read by path -- which is also why
// this script prints the version it read them from.
import { readFileSync } from "node:fs";
import { pathToFileURL } from "node:url";

const root = "node_modules/@redis-ui/styles";
const version = JSON.parse(readFileSync(`${root}/package.json`, "utf8")).version;

const load = async (theme) =>
  (await import(pathToFileURL(`${root}/dist/themes/${theme}/tokens.js`).href));

const flatten = (obj, prefix = "", out = {}) => {
  for (const [k, v] of Object.entries(obj ?? {})) {
    const key = prefix ? `${prefix}.${k}` : k;
    if (v && typeof v === "object") flatten(v, key, out);
    else out[key] = v;
  }
  return out;
};

console.log(`@redis-ui/styles ${version}`);
for (const theme of ["themeLight", "themeDark"]) {
  const mod = await load(theme);
  const tokens = mod.tokens ?? mod.default ?? mod;
  const flat = flatten(tokens);
  const hexes = Object.entries(flat).filter(([, v]) => /^#[0-9a-f]{3,8}$/i.test(String(v)));
  console.log(`\n=== ${theme}: ${hexes.length} colour tokens ===`);
  const want = /(primary|discovery|neutral|notice|informative|success|danger|attention)\d{2,3}$/i;
  const ramp = hexes.filter(([k]) => want.test(k));
  const seen = new Set();
  for (const [k, v] of ramp) {
    const short = k.split(".").slice(-2).join(".");
    if (seen.has(short)) continue;
    seen.add(short);
    console.log(`  ${short.padEnd(34)} ${v}`);
  }
  const surface = hexes.filter(([k]) => /surface|background/i.test(k)).slice(0, 4);
  for (const [k, v] of surface) console.log(`  [surface] ${k.padEnd(24)} ${v}`);
  const fonts = Object.entries(flat).filter(([k]) => /fontFamily/i.test(k)).slice(0, 3);
  for (const [k, v] of fonts) console.log(`  [font] ${k.split(".").slice(-1)[0].padEnd(21)} ${v}`);
}
