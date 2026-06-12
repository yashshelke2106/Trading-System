import { fetchJournal } from "@/lib/api"
import type { JournalRecord } from "@/lib/types"

function outcomeColor(outcome: string | null): string {
  if (outcome === "TARGET_HIT") return "#00c896"
  if (outcome === "SL_HIT")     return "#ff3d5e"
  if (outcome === "EXPIRED")    return "#f59e0b"
  return "#6b84a0"
}

function pnlColor(pnl: number | null): string {
  if (pnl == null) return "#6b84a0"
  return pnl >= 0 ? "#00c896" : "#ff3d5e"
}

function fmt(n: number | null | undefined) {
  if (n == null) return "—"
  return n.toLocaleString("en-IN", { maximumFractionDigits: 2 })
}

const TH: React.CSSProperties = {
  background: "var(--c2)", color: "var(--txd)",
  fontSize: ".66em", fontWeight: 700, textTransform: "uppercase",
  letterSpacing: ".08em", padding: "8px 10px",
  borderBottom: "1px solid var(--bdh)", whiteSpace: "nowrap",
  textAlign: "left",
}
const TD: React.CSSProperties = {
  padding: "6px 10px", borderBottom: "1px solid rgba(26,45,71,.6)",
  color: "var(--tx)", fontSize: ".8em", fontFamily: "'JetBrains Mono', monospace",
  whiteSpace: "nowrap",
}

export const dynamic = "force-dynamic"

export default async function JournalPage() {
  let open: JournalRecord[] = []
  let resolved: JournalRecord[] = []
  try {
    const data = await fetchJournal(30)
    open     = data.open     ?? []
    resolved = data.resolved ?? []
  } catch {}

  const openByDate = [...open].sort((a, b) => b.ts.localeCompare(a.ts))
  const resolvedByDate = [...resolved].sort((a, b) =>
    (b.exit_ts ?? b.ts).localeCompare(a.exit_ts ?? a.ts)
  )

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 24 }}>
      {/* ── Open signals ── */}
      <section>
        <div className="secHdr">
          <div className="secDot" style={{ background: "#f59e0b" }} />
          <div className="secTitle">Open Signals ({open.length})</div>
        </div>
        <div style={{ overflow: "auto", border: "1px solid var(--bd)", borderRadius: 8, background: "var(--c1)" }}>
          <table style={{ width: "100%", borderCollapse: "collapse", fontFamily: "'JetBrains Mono', monospace", fontSize: ".8em" }}>
            <thead>
              <tr>
                {["SYMBOL","DIR","ENTRY","SL","TARGET","OPTION","PREM SRC","AGE"].map(h => (
                  <th key={h} style={TH}>{h}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {openByDate.length === 0 && (
                <tr><td colSpan={8} style={{ ...TD, textAlign: "center", color: "var(--txd)", padding: "24px" }}>No open signals</td></tr>
              )}
              {openByDate.map(r => {
                const lng  = r.direction === "long"
                const diffH = ((Date.now() - new Date(r.ts).getTime()) / 3600000).toFixed(1)
                return (
                  <tr key={r.signal_id} style={{ cursor: "default" }}>
                    <td style={{ ...TD, fontWeight: 800, color: "var(--tx)" }}>{r.symbol}</td>
                    <td style={{ ...TD, fontWeight: 700, color: lng ? "#00c896" : "#ff3d5e" }}>
                      {r.direction?.toUpperCase()}
                    </td>
                    <td style={TD}>{fmt(r.entry_price)}</td>
                    <td style={{ ...TD, color: "#ff3d5e" }}>{fmt(r.sl_price)}</td>
                    <td style={{ ...TD, color: "#00c896" }}>{fmt(r.target_price)}</td>
                    <td style={{ ...TD, color: "#c8d8e8" }}>
                      {r.option_strike ? `${r.option_strike} ${r.option_type}` : "—"}
                    </td>
                    <td style={TD}>
                      {r.prem_source === "live"
                        ? <span className="bLive">LIVE</span>
                        : r.prem_source === "theoretical"
                          ? <span className="bModel">BSM</span>
                          : "—"}
                    </td>
                    <td style={{ ...TD, color: "var(--txd)" }}>{diffH}h</td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>
      </section>

      {/* ── Resolved ── */}
      <section>
        <div className="secHdr">
          <div className="secDot" style={{ background: "#6b84a0" }} />
          <div className="secTitle">Resolved · Last 30 Days ({resolved.length})</div>
        </div>
        <div style={{ overflow: "auto", border: "1px solid var(--bd)", borderRadius: 8, background: "var(--c1)" }}>
          <table style={{ width: "100%", borderCollapse: "collapse", fontFamily: "'JetBrains Mono', monospace", fontSize: ".8em" }}>
            <thead>
              <tr>
                {["SYMBOL","DIR","OUTCOME","ENTRY","EXIT","P&L","OPTION","ENTRY PREM","EXIT PREM","DATE"].map(h => (
                  <th key={h} style={TH}>{h}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {resolvedByDate.length === 0 && (
                <tr><td colSpan={10} style={{ ...TD, textAlign: "center", color: "var(--txd)", padding: "24px" }}>No resolved signals</td></tr>
              )}
              {resolvedByDate.map(r => (
                <tr key={r.signal_id}>
                  <td style={{ ...TD, fontWeight: 800, color: "var(--tx)" }}>{r.symbol}</td>
                  <td style={{ ...TD, fontWeight: 700, color: r.direction === "long" ? "#00c896" : "#ff3d5e" }}>
                    {r.direction?.toUpperCase()}
                  </td>
                  <td style={{ ...TD, fontWeight: 700, color: outcomeColor(r.outcome) }}>
                    {r.outcome ?? "—"}
                  </td>
                  <td style={TD}>{fmt(r.entry_price)}</td>
                  <td style={TD}>{fmt(r.exit_price)}</td>
                  <td style={{ ...TD, fontWeight: 700, color: pnlColor(r.pnl_rupees) }}>
                    {r.pnl_rupees != null ? `₹${fmt(r.pnl_rupees)}` : "—"}
                  </td>
                  <td style={{ ...TD, color: "var(--txd)" }}>
                    {r.option_strike ? `${r.option_strike} ${r.option_type}` : "—"}
                  </td>
                  <td style={{ ...TD, color: "var(--txd)" }}>{r.entry_prem ? `₹${fmt(r.entry_prem)}` : "—"}</td>
                  <td style={{ ...TD, color: "var(--txd)" }}>{r.exit_prem ? `₹${fmt(r.exit_prem)}` : "—"}</td>
                  <td style={{ ...TD, color: "var(--txs)" }}>
                    {r.exit_ts ? new Date(r.exit_ts).toLocaleDateString("en-IN") : "—"}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>
    </div>
  )
}
