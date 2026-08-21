import React, { useMemo, useState } from "react";
import { TableHeading } from "@redis-ui/components";
import { tokens, usd } from "../charts/scales";

/**
 * The always-reachable accounting view: every PR, every agent, every number the
 * charts are drawn from. This is what makes the charts checkable rather than
 * decorative, so it is a first-class view, not an appendix.
 *
 * Built on semantic <table> markup with redis-ui's TableHeading for the header
 * cells. Note: @redis-ui/components 51.2.0 has NO Table component -- only
 * TableHeading -- so the body is hand-built against theme tokens, exactly like
 * the charts.
 */
const COLUMNS = [
  { key: "pr_ordinal", label: "#", align: "right" },
  { key: "pr_number", label: "PR", align: "right" },
  { key: "title", label: "Title", align: "left", grow: true },
  { key: "context_volume", label: "Context volume", align: "right", format: tokens },
  { key: "billed_usd", label: "Billed", align: "right", format: usd },
  { key: "billed_usd_production", label: "Prod-cadence", align: "right", format: usd },
  { key: "output_tokens", label: "Output", align: "right", format: tokens },
  { key: "detail", label: "Detail", align: "left" },
];

export function AccountingTable({ report, agent }) {
  const [sort, setSort] = useState({ key: "pr_ordinal", dir: 1 });
  const byNumber = useMemo(
    () => Object.fromEntries((report.rows ?? []).map((r) => [r.pr_ordinal, r])),
    [report.rows],
  );

  const data = useMemo(() => {
    const rows = report.per_pr
      .filter((r) => r.agent === agent)
      .map((r) => {
        const meta = byNumber[r.pr_ordinal] ?? {};
        return {
          ...r,
          pr_number: meta.pr_number ?? (r.pr_ordinal === 0 ? "--" : ""),
          title: r.pr_ordinal === 0 ? "one-time repo primer" : meta.title ?? "",
          detail:
            agent === "memory"
              ? r.retrieved != null
                ? `${r.memories_used}/${r.retrieved} memories used`
                : "primer write"
              : `${r.files_read ?? 0} files read`,
        };
      });
    return rows.sort((a, b) => {
      const av = a[sort.key], bv = b[sort.key];
      if (typeof av === "number" && typeof bv === "number") return (av - bv) * sort.dir;
      return String(av).localeCompare(String(bv)) * sort.dir;
    });
  }, [report.per_pr, agent, byNumber, sort]);

  const toggle = (key) =>
    setSort((s) => ({ key, dir: s.key === key ? -s.dir : 1 }));

  return (
    <div style={{ overflowX: "auto" }}>
      <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 13 }}>
        <caption style={{ captionSide: "top", textAlign: "left", fontSize: 12, opacity: 0.7, paddingBottom: 8 }}>
          Every row the charts are drawn from, for the {agent} agent. Sortable; ordinal 0 is the primer.
        </caption>
        <thead>
          <tr>
            {COLUMNS.map((col) => (
              <th key={col.key} scope="col" style={{ textAlign: col.align, padding: 0 }}
                  aria-sort={sort.key === col.key ? (sort.dir === 1 ? "ascending" : "descending") : "none"}>
                {/* TableHeading is a styled div (HTMLAttributes only -- no sort
                    props in this version), so the sort affordance is our own
                    button inside it. */}
                <TableHeading>
                  <button type="button" onClick={() => toggle(col.key)}
                          style={{
                            all: "unset", cursor: "pointer", fontWeight: 600, fontSize: 12,
                            display: "inline-flex", gap: 4, alignItems: "center",
                            padding: "6px 10px", width: "100%",
                            justifyContent: col.align === "right" ? "flex-end" : "flex-start",
                          }}>
                    {col.label}
                    <span aria-hidden style={{ opacity: sort.key === col.key ? 0.9 : 0.25 }}>
                      {sort.key === col.key && sort.dir === -1 ? "\u25B2" : "\u25BC"}
                    </span>
                  </button>
                </TableHeading>
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {data.map((row) => (
            <tr key={`${row.agent}-${row.pr_ordinal}`}
                style={{ borderTop: "1px solid currentColor", borderTopColor: "rgba(128,128,128,0.2)" }}>
              {COLUMNS.map((col) => (
                <td key={col.key}
                    style={{
                      textAlign: col.align,
                      padding: "6px 10px",
                      whiteSpace: col.grow ? "normal" : "nowrap",
                      maxWidth: col.grow ? 340 : undefined,
                      fontVariantNumeric: "tabular-nums",
                    }}>
                  {col.format ? col.format(row[col.key] ?? 0) : row[col.key]}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
