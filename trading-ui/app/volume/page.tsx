import { fetchVolume } from "@/lib/api"
import type { VolumeRow } from "@/lib/types"

function ratioStyle(ratio: number): React.CSSProperties {
  if (ratio >= 2.0) return { background: "#0e2818", color: "#00c896", fontWeight: "bold" }
  if (ratio >= 1.5) return { background: "#2a2206", color: "#f59e0b" }
  return {}
}

const TH: React.CSSProperties = {
  background: "var(--c2)", color: "var(--txd)",
  fontSize: ".66em", fontWeight: 700, textTransform: "uppercase",
  letterSpacing: ".08em", padding: "8px 10px",
  borderBottom: "1px solid var(--bdh)", whiteSpace: "nowrap", textAlign: "left",
}
const TD: React.CSSProperties = {
  padding: "5px 10px", borderBottom: "1px solid rgba(26,45,71,.6)",
  fontFamily: "'JetBrains Mono', monospace", fontSize: ".8em", whiteSpace: "nowrap",
  color: "var(--tx)",
}

export const dynamic = "force-dynamic"

export default async function VolumePage() {
  let rows1h: VolumeRow[] = []
  let rows5m: VolumeRow[] = []
  try {
    const data = await fetchVolume()
    rows1h = data.rows_1h ?? []
    rows5m = data.rows_5m ?? []
  } catch {}

  function Table({ rows, cols }: { rows: VolumeRow[]; cols: { key: keyof VolumeRow; label: string }[] }) {
    return (
      <div style={{ overflow: "auto", border: "1px solid var(--bd)", borderRadius: 8, background: "var(--c1)" }}>
        <table style={{ width: "100%", borderCollapse: "collapse" }}>
          <thead>
            <tr>
              {cols.map(c => <th key={c.key} style={TH}>{c.label}</th>)}
              <th style={TH}>RATIO</th>
              <th style={TH}></th>
            </tr>
          </thead>
          <tbody>
            {rows.length === 0 && (
              <tr><td colSpan={cols.length + 2} style={{ ...TD, textAlign: "center", color: "var(--txd)", padding: "24px" }}>No data</td></tr>
            )}
            {rows.map((r, i) => (
              <tr key={i}>
                {cols.map(c => (
                  <td key={c.key} style={TD}>{r[c.key] != null ? String(r[c.key]) : "—"}</td>
                ))}
                <td style={{ ...TD, ...ratioStyle(r.ratio) }}>{r.ratio?.toFixed(2) ?? "—"}</td>
                <td style={{ ...TD, color: "var(--txd)" }}>{r.flag}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    )
  }

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 24 }}>
      <div className="secHdr">
        <div className="secDot" style={{ background: "#38b2f0" }} />
        <div className="secTitle">Volume Analytics</div>
      </div>

      <section>
        <div style={{ fontSize: ".72em", fontWeight: 600, textTransform: "uppercase", letterSpacing: ".08em", color: "var(--txd)", marginBottom: 8 }}>
          Last 1 Hour — vs expected pace
        </div>
        <Table
          rows={rows1h}
          cols={[
            { key: "symbol", label: "Symbol" },
            { key: "price",  label: "Price ₹" },
            { key: "vol_1h", label: "Vol (1h)" },
            { key: "expected_1h", label: "Expected" },
          ]}
        />
      </section>

      <section>
        <div style={{ fontSize: ".72em", fontWeight: 600, textTransform: "uppercase", letterSpacing: ".08em", color: "var(--txd)", marginBottom: 8 }}>
          Last 5 Minutes — vs prev bar
        </div>
        <Table
          rows={rows5m}
          cols={[
            { key: "symbol",    label: "Symbol" },
            { key: "price",     label: "Price ₹" },
            { key: "vol_last5", label: "Last 5m" },
            { key: "vol_prev5", label: "Prev 5m" },
          ]}
        />
      </section>
    </div>
  )
}
