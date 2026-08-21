import { readFileSync } from "node:fs";
import { JSDOM, VirtualConsole } from "jsdom";
const dom = new JSDOM('<!doctype html><body><div id=root></div></body>', {
  runScripts: "dangerously", url: "http://localhost/", pretendToBeVisual: true,
  virtualConsole: new VirtualConsole(),
});
dom.window.fetch = () => Promise.reject(new Error("none"));
dom.window.eval(readFileSync("dist-smoke/bundle.js", "utf8"));
await new Promise((r) => setTimeout(r, 600));
const doc = dom.window.document;
const txt = (e) => (e?.textContent || "").replace(/\s+/g, " ").trim();
const chart = [...doc.querySelectorAll("svg")].find((s) =>
  /cumulative/i.test(s.getAttribute("aria-label") || ""));
console.log("chart svg:", chart ? "found" : "MISSING");
const texts = [...chart.querySelectorAll("text")].map(txt);
console.log("  paths", chart.querySelectorAll("path").length,
            "| lines", chart.querySelectorAll("line").length,
            "| texts", texts.length);
console.log("  y labels:", texts.filter((t) => t.startsWith("$")).join(" "));
console.log("  x labels:", texts.filter((t) => t === "prime" || /^\d+$/.test(t)).join(" "));
console.log("  direct labels:", texts.filter((t) => /baseline|memory/.test(t)).join(", "));
console.log("  line colours:", [...chart.querySelectorAll("path")]
  .map((p) => p.getAttribute("stroke")).filter(Boolean).join(" "));
const h = doc.querySelector("h1,h2,h3,h4");
console.log("heading:", h?.tagName, "->", txt(h).slice(0, 78));
console.log("figures:", doc.querySelectorAll("figure").length,
            "| figcaptions:", doc.querySelectorAll("figcaption").length);
