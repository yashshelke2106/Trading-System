import { fetchPositions } from "@/lib/api"
import type { Position } from "@/lib/types"

function fmt(n: number, dec = 2) {
  return n.toLocaleString("en-IN", { minimumFractionDigits: dec, maximumFractionDigits: dec })
}

const TH: React.CSSProperties = {
  background: "var(--c2)", color: "var(--txd)",
  fontSize: ".66em", fontWeight: 700, textTransform: "uppercase",
  letterSpacing: ".08em", padding: "8px 10px",
  borderBottom: "1px solid var(--bdh)", whiteSpace: "nowrap", textAlign: "left",
}
const TD: React.CSSProperties = {
  padding: "6px 10px", borderBottom: "1px solid rgba(26,45,71,.6)",
  fontFamily: "'JetBrains Mono', monospace", fontSize: ".8em", whiteSpace: "nowrap",
  color: "var(--tx)",
}

export const dynamic = "force-dynamic"

export default async function PositionsPage() {
  let positions: Position[] = []
  let totalUnrealized = 0
  try {
    const data = await fetchPositions()
    positions = data.positions ?? []
    totalUnrealized = data.total_unrealized ?? 0
  } catch {}

  const pnlClr = (v: number) => v > 0 ? "#00c896" : v < 0 ? "#ff3d5e" : "var(--tx)"

  return (
    <div>
      <div className="secHdr">
        <div className="secDot" style={{ background: "#38b2f0" }} />
        <div className="secTitle">Live Positions</div>
      </div>

      {/* Summary */}
      <div style={{ display: "flex", gap: 10, marginBottom: 16 }}>
        {[
          { label: "Open Positions", value: String(positions.length), color: "var(--tx)" },
          { label: "Total Unrealized P&L", value: `₹${fmt(totalUnrealized)}`, color: pnlClr(totalUnrealized) },
        ].map(({ label, value, color }) => (
          <div key={label} style={{ background: "var(--c1)", border: "1px solid var(--bd)", borderRadius: 8, padding: "10px 16px", minWidth: 180 }}>
            <div style={{ fontSize: ".58em", fontWeight: 600, textTransform: "uppercase", letterSpacing: ".1em", color: "var(--txs)", marginBottom: 3 }}>{label}</div>
            <div style={{ fontSize: "1.4em", fontWeight: 700, color, fontFamily: "'JetBrains Mono', monospace" }}>{value}</div>
          </div>
        ))}
      </div>

      <div style={{ overflow: "auto", border: "1px solid var(--bd)", borderRadius: 8, background: "var(--c1)" }}>
        <table style={{ width: "100%", borderCollapse: "collapse" }}>
          <thead>
            <tr>
              {["Symbol", "Qty", "Avg ₹", "LTP ₹", "Unrealized"].map(h => (
                <th key={h} style={TH}>{h}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {positions.length === 0 && (
              <tr><td colSpan={5} style={{ ...TD, textAlign: "center", color: "var(--txd)", padding: "24px" }}>No open positions</td></tr>
            )}
            {positions.map((p, i) => (
              <tr key={i}>
                <td style={{ ...TD, fontWeight: 800 }}>{p.symbol}</td>
                <td style={TD}>{p.qty}</td>
                <td style={TD}>{fmt(p.avg_price)}</td>
                <td style={TD}>{fmt(p.ltp)}</td>
                <td style={{ ...TD, fontWeight: 700, color: pnlClr(p.unrealized) }}>
                  {p.unrealized > 0 ? "+" : ""}₹{fmt(p.unrealized)}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  )
}
