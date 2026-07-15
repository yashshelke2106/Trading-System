"use client"
import { useEffect, useState, useCallback } from "react"
import { fetchSwing } from "@/lib/api"

function fmt(n: number | null | undefined, dec = 2) {
  if (n == null) return "—"
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
const CARD: React.CSSProperties = {
  background: "var(--c1)", border: "1px solid var(--bd)",
  borderRadius: 8, padding: "10px 14px",
}
const LBL: React.CSSProperties = {
  fontSize: ".58em", fontWeight: 600, textTransform: "uppercase",
  letterSpacing: ".1em", color: "var(--txs)", marginBottom: 3,
}
const VAL: React.CSSProperties = {
  fontSize: "1.3em", fontWeight: 700, fontFamily: "'JetBrains Mono', monospace",
}

interface Candidate {
  symbol: string; direction: string; signal: string; close: number
  target: number; stop: number; target_pct: number; weight: number; bar: string
}
interface JournalRow {
  symbol: string; direction: string; signal: string; signal_date: string
  close: number; target: number; stop: number; status: string
  entry_px?: number; exit_px?: number; exit_date?: string; outcome?: string
  ret_net?: number
}
interface Health {
  status: string
  reasons: string[]
  funded_side: { n_resolved: number; rolling_pf: number | null; rolling_avg_bp: number; persistence: number | null }
  rules: { window: number; retire_pf: number; reinstate_pf: number; registered: string }
}
interface LearnerRow { direction: string; signal: string; regime: string; n: number; wins: number; weight: number }
interface SwingData {
  screen: { ts: string; regime: string; nifty: number; ma200: number; candidates: Candidate[] } | null
  open: JournalRow[]; resolved: JournalRow[]
  health: Health | null; learner: LearnerRow[]; error?: string
}

const HEALTH_CLR: Record<string, string> = {
  HEALTHY: "#00c896", WARN: "#f59e0b", RETIRED: "#ff3d5e", COLLECTING: "var(--txd)",
}

export default function SwingPage() {
  const [data, setData] = useState<SwingData | null>(null)
  const [notional, setNotional] = useState(50000)

  const [fetchErr, setFetchErr] = useState<string | null>(null)
  const reload = useCallback(async () => {
    try {
      setData(await fetchSwing())
      setFetchErr(null)
    } catch (e) {
      setFetchErr((e as Error).message)   // surface it — never a silent blank page
    }
  }, [])
  useEffect(() => {
    reload()
    const id = setInterval(reload, 60000)   // auto-refresh — never goes stale
    return () => clearInterval(id)
  }, [reload])

  const scr = data?.screen
  const riskOn = scr?.regime === "risk_on"
  // FUNDING POLICY (docs/research/short_side_policy.md): shorts are never
  // fundable — 15y evidence −125bp/trade, PF 0.65. Bench = learner only.
  const funded = (scr?.candidates ?? []).filter(c => c.direction === "long" && riskOn)
  const bench = (scr?.candidates ?? []).filter(c => !(c.direction === "long" && riskOn))
  const res = data?.resolved ?? []
  const rets = res.map(r => Number(r.ret_net ?? 0))
  const wins = rets.filter(r => r > 0)
  const gw = wins.reduce((a, b) => a + b, 0)
  const gl = -rets.filter(r => r <= 0).reduce((a, b) => a + b, 0)
  const h = data?.health

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
      <div className="secHdr">
        <div className="secDot" style={{ background: "#00c896" }} />
        <div className="secTitle">Swing Framework · symmetric 7-gate + learner (no API keys)</div>
      </div>
      {fetchErr && (
        <div style={{
          background: "rgba(255,61,94,.08)", border: "1px solid #ff3d5e", borderRadius: 8,
          padding: "10px 14px", fontSize: ".8em", color: "#ff3d5e", fontWeight: 600,
        }}>
          ⚠ Backend unreachable: {fetchErr} — retrying every 60s automatically.
        </div>
      )}
      {data?.error && <div style={{ color: "#ff3d5e", fontSize: ".8em" }}>API error: {data.error}</div>}

      {/* Regime + health cards */}
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(170px,1fr))", gap: 8 }}>
        {scr && (
          <>
            <div style={{ ...CARD, border: `1px solid ${riskOn ? "#00c896" : "#f59e0b"}` }}>
              <div style={LBL}>Gate 1 · Regime</div>
              <div style={{ ...VAL, color: riskOn ? "#00c896" : "#f59e0b" }}>
                {riskOn ? "RISK-ON" : "RISK-OFF"}
              </div>
              <div style={{ fontSize: ".64em", color: "var(--txs)" }}>{riskOn ? "LONGS fundable" : "STAND ASIDE — cash"}</div>
            </div>
            <div style={CARD}>
              <div style={LBL}>NIFTY vs 200-DMA</div>
              <div style={{ ...VAL, fontSize: "1.05em" }}>
                {fmt(scr.nifty, 0)} / {fmt(scr.ma200, 0)}
                <span style={{ fontSize: ".72em", marginLeft: 6, color: scr.nifty >= scr.ma200 ? "#00c896" : "#f59e0b" }}>
                  ({fmt((scr.nifty / scr.ma200 - 1) * 100, 2)}%)
                </span>
              </div>
            </div>
          </>
        )}
        {h && (
          <div style={{ ...CARD, border: `1px solid ${HEALTH_CLR[h.status] ?? "var(--bd)"}` }}>
            <div style={LBL}>🩺 Health (decay monitor)</div>
            <div style={{ ...VAL, color: HEALTH_CLR[h.status] ?? "var(--tx)" }}>{h.status}</div>
            <div style={{ fontSize: ".62em", color: "var(--txs)" }}>
              PF {h.funded_side.rolling_pf ?? "—"} · persistence {h.funded_side.persistence ?? "—"} · n={h.funded_side.n_resolved}
            </div>
          </div>
        )}
      </div>
      {h?.status === "RETIRED" && (
        <div style={{ background: "rgba(255,61,94,.08)", border: "1px solid #ff3d5e", borderRadius: 8, padding: "10px 14px", fontSize: ".8em", color: "#ff3d5e", fontWeight: 600 }}>
          🔴 RETIRED by pre-registered decay rule (rolling PF &lt; {h.rules.retire_pf}). Fund nothing — paper bench continues. Auto-reinstates at PF ≥ {h.rules.reinstate_pf}.
        </div>
      )}

      {/* Funded candidates — shorts are NEVER fundable (short_side_policy.md) */}
      <div className="secHdr">
        <div className="secDot" style={{ background: riskOn ? "#00c896" : "#f59e0b" }} />
        <div className="secTitle">
          {riskOn ? `Funded candidates (long) · ${funded.length}`
                  : "Funded action: NONE — stand aside"}
        </div>
      </div>
      {!riskOn && (
        <div style={{ background: "rgba(245,158,11,.06)", border: "1px solid rgba(245,158,11,.4)", borderRadius: 8, padding: "10px 14px", fontSize: ".78em", color: "#f59e0b" }}>
          Risk-off regime. Shorting is <b>not</b> the funded alternative — 15-year evidence
          (2,816 trades): <b>−125 bp/trade, PF 0.65</b>. The correct trade is <b>cash</b>
          (the allocation engine already holds it). Short setups sit on the paper bench below.
        </div>
      )}
      {riskOn && (
        <div style={{ overflow: "auto", border: "1px solid var(--bd)", borderRadius: 8, background: "var(--c1)" }}>
          <table style={{ width: "100%", borderCollapse: "collapse" }}>
            <thead><tr>{["#", "Symbol", "Dir", "Signal", "Close", "Target", "Target %", "Stop", "Learner W"].map(x => <th key={x} style={TH}>{x}</th>)}</tr></thead>
            <tbody>
              {!funded.length && (
                <tr><td colSpan={9} style={{ ...TD, textAlign: "center", color: "var(--txd)", padding: 20 }}>
                  No funded setups — most days that&apos;s the correct answer.
                </td></tr>
              )}
              {funded.slice(0, 15).map((c, i) => (
                <tr key={c.symbol}>
                  <td style={{ ...TD, color: "var(--txs)" }}>{i + 1}</td>
                  <td style={{ ...TD, fontWeight: 800 }}>{c.symbol}</td>
                  <td style={{ ...TD, fontWeight: 700, color: "#00c896" }}>{c.direction.toUpperCase()}</td>
                  <td style={{ ...TD, color: "var(--txd)", fontSize: ".72em" }}>{c.signal}</td>
                  <td style={TD}>{fmt(c.close)}</td>
                  <td style={{ ...TD, color: "#00c896" }}>{fmt(c.target)}</td>
                  <td style={{ ...TD, color: "#00c896", fontWeight: 700 }}>{fmt(c.target_pct, 1)}%</td>
                  <td style={{ ...TD, color: "#ff3d5e" }}>{fmt(c.stop)}</td>
                  <td style={{ ...TD, color: c.weight > 1 ? "#00c896" : c.weight < 1 ? "#ff3d5e" : "var(--txs)" }}>{fmt(c.weight)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {bench.length > 0 && (
        <details style={{ border: "1px solid var(--bd)", borderRadius: 8, background: "var(--c1)", padding: "8px 12px" }}>
          <summary style={{ fontSize: ".74em", color: "var(--txd)", cursor: "pointer" }}>
            🧪 Paper bench · {bench.length} setups (learner only — DO NOT FUND)
          </summary>
          <div style={{ overflow: "auto", marginTop: 8 }}>
            <table style={{ width: "100%", borderCollapse: "collapse" }}>
              <thead><tr>{["Symbol", "Dir", "Signal", "Close", "Target", "Stop", "Learner W"].map(x => <th key={x} style={TH}>{x}</th>)}</tr></thead>
              <tbody>
                {bench.slice(0, 15).map(c => (
                  <tr key={`${c.symbol}-${c.direction}`}>
                    <td style={{ ...TD, fontWeight: 800 }}>{c.symbol}</td>
                    <td style={{ ...TD, fontWeight: 700, color: c.direction === "long" ? "#00c896" : "#ff3d5e" }}>{c.direction.toUpperCase()}</td>
                    <td style={{ ...TD, color: "var(--txd)", fontSize: ".72em" }}>{c.signal}</td>
                    <td style={TD}>{fmt(c.close)}</td>
                    <td style={TD}>{fmt(c.target)}</td>
                    <td style={TD}>{fmt(c.stop)}</td>
                    <td style={TD}>{fmt(c.weight)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </details>
      )}
      {scr && <div style={{ fontSize: ".68em", color: "var(--txs)" }}>Screen as of {scr.ts} · entry next open · stop 2×ATR · winners ride until close &lt; 5-DMA · risk ≤1%/trade · sleeve ≤10% of capital</div>}

      {/* Paper P&L */}
      <div className="secHdr">
        <div className="secDot" style={{ background: "#a78bfa" }} />
        <div className="secTitle">Paper trades &amp; P&amp;L · {data?.open.length ?? 0} open / {res.length} resolved</div>
      </div>
      {res.length > 0 ? (
        <>
          <div style={{ display: "flex", gap: 10, alignItems: "center", flexWrap: "wrap" }}>
            <label style={{ fontSize: ".72em", color: "var(--txd)" }}>₹ per trade</label>
            <input type="number" value={notional} min={5000} step={5000}
              onChange={e => setNotional(Number(e.target.value) || 50000)}
              style={{ background: "var(--c2)", color: "var(--tx)", border: "1px solid var(--bd)", borderRadius: 6, padding: "4px 8px", width: 110, fontFamily: "'JetBrains Mono',monospace", fontSize: ".8em" }} />
          </div>
          <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(150px,1fr))", gap: 8 }}>
            <div style={CARD}><div style={LBL}>Total P&amp;L</div>
              <div style={{ ...VAL, color: rets.reduce((a, b) => a + b, 0) >= 0 ? "#00c896" : "#ff3d5e" }}>
                ₹{fmt(rets.reduce((a, b) => a + b, 0) * notional, 0)}</div></div>
            <div style={CARD}><div style={LBL}>Win rate</div>
              <div style={VAL}>{fmt(wins.length / rets.length * 100, 0)}%</div></div>
            <div style={CARD}><div style={LBL}>Profit factor</div>
              <div style={VAL}>{gl > 0 ? fmt(gw / gl) : "∞"}</div></div>
            <div style={CARD}><div style={LBL}>Avg / trade</div>
              <div style={VAL}>₹{fmt(rets.reduce((a, b) => a + b, 0) / rets.length * notional, 0)}</div></div>
          </div>
          <div style={{ overflow: "auto", border: "1px solid var(--bd)", borderRadius: 8, background: "var(--c1)" }}>
            <table style={{ width: "100%", borderCollapse: "collapse" }}>
              <thead><tr>{["Symbol", "Dir", "Signal date", "Entry", "Exit", "Target/SL date", "Outcome", "Net %", "P&L ₹"].map(x => <th key={x} style={TH}>{x}</th>)}</tr></thead>
              <tbody>
                {res.slice().reverse().slice(0, 25).map((r, i) => (
                  <tr key={i}>
                    <td style={{ ...TD, fontWeight: 800 }}>{r.symbol}</td>
                    <td style={{ ...TD, color: r.direction === "long" ? "#00c896" : "#ff3d5e" }}>{r.direction.toUpperCase()}</td>
                    <td style={TD}>{r.signal_date}</td>
                    <td style={TD}>{fmt(r.entry_px)}</td>
                    <td style={TD}>{fmt(r.exit_px)}</td>
                    <td style={TD}>{r.exit_date}</td>
                    <td style={{ ...TD, color: "var(--txd)" }}>{r.outcome}</td>
                    <td style={{ ...TD, fontWeight: 700, color: (r.ret_net ?? 0) > 0 ? "#00c896" : "#ff3d5e" }}>
                      {(r.ret_net ?? 0) > 0 ? "+" : ""}{fmt((r.ret_net ?? 0) * 100)}%</td>
                    <td style={{ ...TD, color: (r.ret_net ?? 0) > 0 ? "#00c896" : "#ff3d5e" }}>
                      ₹{fmt((r.ret_net ?? 0) * notional, 0)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      ) : (
        <div style={{ fontSize: ".78em", color: "var(--txd)" }}>
          Nothing resolved yet — {data?.open.length ?? 0} paper trades open; they resolve within 10 sessions
          (16:30 autopilot). The P&amp;L cards appear with the first resolution.
        </div>
      )}

      {/* Open trades */}
      {(data?.open.length ?? 0) > 0 && (
        <div style={{ overflow: "auto", border: "1px solid var(--bd)", borderRadius: 8, background: "var(--c1)" }}>
          <table style={{ width: "100%", borderCollapse: "collapse" }}>
            <thead><tr>{["Open trade", "Dir", "Signal date", "Ref close", "Target", "Stop"].map(x => <th key={x} style={TH}>{x}</th>)}</tr></thead>
            <tbody>
              {(data?.open ?? []).map((r, i) => (
                <tr key={i}>
                  <td style={{ ...TD, fontWeight: 800 }}>{r.symbol}</td>
                  <td style={{ ...TD, color: r.direction === "long" ? "#00c896" : "#ff3d5e" }}>{r.direction.toUpperCase()}</td>
                  <td style={TD}>{r.signal_date}</td>
                  <td style={TD}>{fmt(r.close)}</td>
                  <td style={{ ...TD, color: "#00c896" }}>{fmt(r.target)}</td>
                  <td style={{ ...TD, color: "#ff3d5e" }}>{fmt(r.stop)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {/* Learner */}
      {(data?.learner.length ?? 0) > 0 && (
        <>
          <div className="secHdr">
            <div className="secDot" style={{ background: "#38b2f0" }} />
            <div className="secTitle">🧠 Learner buckets (weight moves off 1.00 only past n≥20 + Wilson clears 50%)</div>
          </div>
          <div style={{ overflow: "auto", border: "1px solid var(--bd)", borderRadius: 8, background: "var(--c1)" }}>
            <table style={{ width: "100%", borderCollapse: "collapse" }}>
              <thead><tr>{["Dir", "Signal", "Regime", "n", "Wins", "Weight"].map(x => <th key={x} style={TH}>{x}</th>)}</tr></thead>
              <tbody>
                {(data?.learner ?? []).map((b, i) => (
                  <tr key={i}>
                    <td style={{ ...TD, color: b.direction === "long" ? "#00c896" : "#ff3d5e" }}>{b.direction}</td>
                    <td style={TD}>{b.signal}</td>
                    <td style={{ ...TD, color: "var(--txd)" }}>{b.regime}</td>
                    <td style={TD}>{b.n}</td>
                    <td style={TD}>{b.wins}</td>
                    <td style={{ ...TD, fontWeight: 700, color: b.weight > 1 ? "#00c896" : b.weight < 1 ? "#ff3d5e" : "var(--txs)" }}>{fmt(b.weight)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}

      <div style={{ fontSize: ".68em", color: "var(--txs)" }}>
        Data refreshes via the 16:30 autopilot (screen → journal → resolve → learn). Execution is manual —
        nothing here places orders. Evidence base: 15y / 16,953 trades, sleeve-grade (≤10% of capital).
      </div>
    </div>
  )
}
