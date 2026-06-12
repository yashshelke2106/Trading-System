"use client"
import { useEffect, useState, useCallback } from "react"
import { fetchChain } from "@/lib/api"
import type { ChainRow } from "@/lib/types"

const SYMBOLS = [
  "NIFTY","BANKNIFTY","FINNIFTY","MIDCPNIFTY",
  "RELIANCE","TCS","INFY","HDFCBANK","ICICIBANK","SBIN",
]

function fmtLtp(v: number | undefined): string {
  return v && v > 0 ? `₹${v.toFixed(2)}` : "—"
}
function fmtOi(v: number | undefined): string {
  if (!v || v <= 0) return "—"
  if (v >= 1_000_000) return `${(v/1_000_000).toFixed(2)}M`
  if (v >= 1_000)     return `${(v/1_000).toFixed(1)}K`
  return String(v)
}
function fmtIv(v: number | undefined): string {
  return v && v > 0 ? `${v.toFixed(1)}%` : "—"
}

const TH: React.CSSProperties = {
  background: "var(--c2)", color: "var(--txd)",
  fontSize: ".66em", fontWeight: 700, textTransform: "uppercase",
  letterSpacing: ".08em", padding: "8px 10px",
  borderBottom: "1px solid var(--bdh)", whiteSpace: "nowrap",
  position: "sticky", top: 0,
}
const TD: React.CSSProperties = {
  padding: "5px 10px", borderBottom: "1px solid rgba(26,45,71,.6)",
  fontFamily: "'JetBrains Mono', monospace", fontSize: ".8em", whiteSpace: "nowrap",
}

export default function ChainPage() {
  const [symbol, setSymbol]   = useState("NIFTY")
  const [data,   setData]     = useState<{ rows: ChainRow[]; atm: ChainRow | null; spot: number; pcr: number | null; max_pain: number | null; expiry: string | null }>({ rows: [], atm: null, spot: 0, pcr: null, max_pain: null, expiry: null })
  const [loading, setLoading] = useState(false)

  const load = useCallback(async (sym: string) => {
    setLoading(true)
    try {
      const d = await fetchChain(sym)
      setData({
        rows:      d.rows      ?? [],
        atm:       d.atm       ?? null,
        spot:      d.spot      ?? 0,
        pcr:       d.pcr       ?? null,
        max_pain:  d.max_pain  ?? null,
        expiry:    d.expiry    ?? null,
      })
    } catch {}
    setLoading(false)
  }, [])

  useEffect(() => { load(symbol) }, [symbol, load])

  const atmStrike = data.atm?.strike ?? null
  const pcrClr = data.pcr == null ? "var(--tx)"
    : data.pcr > 1.2 ? "#00c896"
    : data.pcr < 0.8 ? "#ff3d5e" : "#f59e0b"
  const pcrBias = data.pcr == null ? "" : data.pcr > 1.2 ? "Bullish" : data.pcr < 0.8 ? "Bearish" : "Neutral"

  return (
    <div>
      <div className="secHdr">
        <div className="secDot" style={{ background: "#a78bfa" }} />
        <div className="secTitle">Option Chain</div>
      </div>

      {/* Symbol selector */}
      <div style={{ display: "flex", gap: 8, marginBottom: 12, flexWrap: "wrap" }}>
        {SYMBOLS.map(s => (
          <button key={s} onClick={() => setSymbol(s)} style={{
            padding: "4px 12px", borderRadius: 5, cursor: "pointer",
            border: "1px solid " + (symbol === s ? "var(--b)" : "var(--bd)"),
            background: symbol === s ? "rgba(56,178,240,.08)" : "var(--c2)",
            color: symbol === s ? "var(--b)" : "var(--txd)",
            fontSize: ".72em", fontWeight: 600, letterSpacing: ".06em",
          }}>
            {s}
          </button>
        ))}
      </div>

      {/* PCR bar */}
      {(data.pcr != null || data.max_pain != null || data.spot > 0) && (
        <div className="pcrBar" style={{ marginBottom: 12 }}>
          {data.spot > 0 && (
            <div className="pcrItem">
              <div className="pcrLbl">Spot</div>
              <div className="pcrVal">₹{data.spot.toLocaleString("en-IN", { minimumFractionDigits: 2 })}</div>
            </div>
          )}
          {data.pcr != null && (
            <div className="pcrItem">
              <div className="pcrLbl">PCR</div>
              <div className="pcrVal" style={{ color: pcrClr }}>{data.pcr.toFixed(2)} <span style={{ fontSize: ".75em" }}>{pcrBias}</span></div>
            </div>
          )}
          {data.max_pain != null && (
            <div className="pcrItem">
              <div className="pcrLbl">Max Pain</div>
              <div className="pcrVal">₹{data.max_pain.toLocaleString("en-IN")}</div>
            </div>
          )}
          {data.expiry && (
            <div className="pcrItem">
              <div className="pcrLbl">Expiry</div>
              <div className="pcrVal" style={{ fontSize: ".8em" }}>{data.expiry}</div>
            </div>
          )}
        </div>
      )}

      {/* Chain table */}
      {loading && <p style={{ color: "var(--txd)", fontSize: ".8em" }}>Loading…</p>}
      {!loading && data.rows.length === 0 && (
        <p style={{ color: "var(--txd)", fontSize: ".8em" }}>No chain data. Verify Dhan token + symbol is F&amp;O-enabled.</p>
      )}
      {!loading && data.rows.length > 0 && (
        <div style={{ overflow: "auto", border: "1px solid var(--bd)", borderRadius: 8, maxHeight: 520, background: "var(--c1)" }}>
          <table style={{ width: "100%", borderCollapse: "collapse", fontFamily: "'JetBrains Mono', monospace", fontSize: ".8em" }}>
            <thead>
              <tr>
                <th style={{ ...TH, color: "#4cc9f0", textAlign: "right" }}>CE LTP</th>
                <th style={{ ...TH, color: "#4cc9f0", textAlign: "right" }}>OI</th>
                <th style={{ ...TH, color: "#4cc9f0", textAlign: "right" }}>IV%</th>
                <th style={{ ...TH, textAlign: "center" }}>STRIKE</th>
                <th style={{ ...TH, color: "#ff9eb5" }}>IV%</th>
                <th style={{ ...TH, color: "#ff9eb5" }}>OI</th>
                <th style={{ ...TH, color: "#ff9eb5" }}>PE LTP</th>
              </tr>
            </thead>
            <tbody>
              {data.rows.map(row => {
                const isAtm = atmStrike != null && Math.abs(row.strike - atmStrike) < 0.5
                const ceIvHi = (row.ce_iv ?? 0) > 40
                const peIvHi = (row.pe_iv ?? 0) > 40
                return (
                  <tr key={row.strike} style={isAtm ? { background: "#091e3a", borderTop: "1px solid #1a5090", borderBottom: "1px solid #1a5090" } : {}}>
                    <td style={{ ...TD, color: "#4cc9f0", textAlign: "right" }}>{fmtLtp(row.ce_ltp)}</td>
                    <td style={{ ...TD, color: "#4cc9f0", textAlign: "right", fontSize: ".9em" }}>{fmtOi(row.ce_oi)}</td>
                    <td style={{ ...TD, color: ceIvHi ? "#f59e0b" : "#4cc9f0", textAlign: "right" }}>{fmtIv(row.ce_iv)}</td>
                    <td style={{ ...TD, fontWeight: isAtm ? 700 : 400, textAlign: "center", color: "var(--tx)" }}>
                      {row.strike.toLocaleString("en-IN")}
                      {isAtm && <span style={{ fontSize: ".58em", fontWeight: 700, background: "#1a5090", color: "#80c0ff", padding: "1px 5px", borderRadius: 3, marginLeft: 5 }}>ATM</span>}
                    </td>
                    <td style={{ ...TD, color: peIvHi ? "#f59e0b" : "#ff9eb5" }}>{fmtIv(row.pe_iv)}</td>
                    <td style={{ ...TD, color: "#ff9eb5", fontSize: ".9em" }}>{fmtOi(row.pe_oi)}</td>
                    <td style={{ ...TD, color: "#ff9eb5" }}>{fmtLtp(row.pe_ltp)}</td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}
