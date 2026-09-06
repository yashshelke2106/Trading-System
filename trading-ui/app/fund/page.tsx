"use client"
import { useEffect, useState, useCallback } from "react"
import { fetchMacroFund } from "@/lib/api"

// The paper macro fund (H-022). Distinct from /portfolio (a retrospective
// journal replay) and from the Rs 10L equity book behind /api/account: this is
// a USD NOTIONAL book across 18 global markets, so gross exposure exceeds NAV
// by design and the invariant is NAV == capital + realised + unrealised.

function fmt(n: number | null | undefined, dec = 2) {
  if (n == null || Number.isNaN(n)) return "—"
  return n.toLocaleString("en-US", { minimumFractionDigits: dec, maximumFractionDigits: dec })
}
function signed(n: number | null | undefined, dec = 2) {
  if (n == null || Number.isNaN(n)) return "—"
  // Decide the sign from the ROUNDED value, not the raw one. A P&L of
  // -0.001 formats as "-0.00" while testing n >= 0 as false... and testing
  // the raw number as negative-but-displayed-zero produced "+-0.00".
  const r = Number(n.toFixed(dec))
  const v = Object.is(r, -0) ? 0 : r
  return (v > 0 ? "+" : "") + fmt(v, dec)
}
const posCol = (n: number | null | undefined) =>
  n == null ? "var(--tx)" : n > 0 ? "var(--up)" : n < 0 ? "var(--dn)" : "var(--txd)"

const TH: React.CSSProperties = {
  background: "var(--c2)", color: "var(--txd)",
  fontSize: ".76em", fontWeight: 700, textTransform: "uppercase",
  letterSpacing: ".08em", padding: "8px 10px",
  borderBottom: "1px solid var(--bdh)", whiteSpace: "nowrap", textAlign: "left",
}
const TD: React.CSSProperties = {
  padding: "6px 10px", borderBottom: "1px solid rgba(26,45,71,.6)",
  fontFamily: "'JetBrains Mono', monospace", fontSize: ".8em",
  whiteSpace: "nowrap", color: "var(--tx)",
}
const CARD: React.CSSProperties = {
  background: "var(--c1)", border: "1px solid var(--bd)", borderRadius: 8, padding: 16,
}

interface Perf {
  nav: number; capital: number; total_return_pct: number
  realised_pnl: number; unrealised_pnl: number; total_costs: number
  max_drawdown_pct: number; sharpe_annualised: number
  days_live: number; n_marks: number; mature: boolean; evaluate_after: string
}
interface Exposure {
  gross: number; net: number; gross_usd: number; net_usd: number
  by_class: Record<string, number>; n_long: number; n_short: number
}
interface Position {
  ticker: string; name: string; asset_class: string; direction: string
  weight: number; units: number; entry_price: number; entry_date: string
  last_price: number; market_value: number; pnl: number
}
interface CurvePt { date: string; nav: number; gross: number; net: number }
interface FundData {
  ok: boolean; reason?: string
  mode?: string; hypothesis?: string; base_currency?: string
  started?: string; last_mark?: string; last_rebalance?: string
  performance?: Perf; exposure?: Exposure
  positions?: Position[]; closed?: Position[]; curve?: CurvePt[]
  note?: string
}

function NavChart({ curve, capital }: { curve: CurvePt[]; capital: number }) {
  const W = 660, H = 190, PAD = 44
  if (curve.length < 2) {
    return (
      <div style={{ ...CARD, color: "var(--txd)", fontSize: ".85em" }}>
        The NAV curve needs at least two marks. Run <code>python macro_task.py</code>{" "}
        after the US close on another session and it will start drawing.
      </div>
    )
  }
  const vals = curve.map(p => p.nav).concat([capital])
  const vMax = Math.max(...vals), vMin = Math.min(...vals)
  const span = vMax - vMin || 1
  const x = (i: number) => PAD + (i / (curve.length - 1)) * (W - PAD - 12)
  const y = (v: number) => H - PAD - ((v - vMin) / span) * (H - PAD - 16)
  const d = curve.map((p, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${y(p.nav).toFixed(1)}`).join(" ")
  const yCap = y(capital)
  return (
    <svg viewBox={`0 0 ${W} ${H}`} style={{ width: "100%", height: "auto", display: "block" }}>
      <line x1={PAD} y1={yCap} x2={W - 12} y2={yCap}
            stroke="var(--bdh)" strokeWidth="1" strokeDasharray="3 3" />
      <text x={PAD - 6} y={yCap + 3} textAnchor="end"
            style={{ fontSize: 9, fill: "var(--txd)", fontFamily: "'JetBrains Mono', monospace" }}>
        {fmt(capital, 0)}
      </text>
      <path d={d} fill="none" stroke="var(--ac)" strokeWidth="2"
            strokeLinejoin="round" strokeLinecap="round" />
      <circle cx={x(curve.length - 1)} cy={y(curve[curve.length - 1].nav)} r="3.5" fill="var(--ac)" />
      <text x={PAD} y={H - 8} style={{ fontSize: 9, fill: "var(--txd)", fontFamily: "'JetBrains Mono', monospace" }}>
        {curve[0].date}
      </text>
      <text x={W - 12} y={H - 8} textAnchor="end"
            style={{ fontSize: 9, fill: "var(--txd)", fontFamily: "'JetBrains Mono', monospace" }}>
        {curve[curve.length - 1].date}
      </text>
    </svg>
  )
}

function ExposureBars({ byClass }: { byClass: Record<string, number> }) {
  const entries = Object.entries(byClass)
  if (!entries.length) return null
  const max = Math.max(...entries.map(([, v]) => Math.abs(v)), 0.1)
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 7 }}>
      {entries.map(([k, v]) => {
        const pct = (Math.abs(v) / max) * 46
        return (
          <div key={k} style={{ display: "grid", gridTemplateColumns: "92px 1fr 64px", alignItems: "center", gap: 8 }}>
            <span style={{ fontSize: ".76em", color: "var(--txd)", textTransform: "uppercase", letterSpacing: ".06em" }}>{k}</span>
            <div style={{ position: "relative", height: 14, background: "var(--c2)", borderRadius: 3 }}>
              <div style={{ position: "absolute", left: "50%", top: 0, bottom: 0, width: 1, background: "var(--bdh)" }} />
              <div style={{
                position: "absolute", top: 2, bottom: 2, borderRadius: 2,
                background: v >= 0 ? "var(--up)" : "var(--dn)",
                left: v >= 0 ? "50%" : `${50 - pct}%`, width: `${pct}%`,
              }} />
            </div>
            <span style={{ fontFamily: "'JetBrains Mono', monospace", fontSize: ".78em", textAlign: "right", color: posCol(v) }}>
              {signed(v, 2)}x
            </span>
          </div>
        )
      })}
    </div>
  )
}

export default function FundPage() {
  const [d, setD] = useState<FundData | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)

  const load = useCallback(async () => {
    try {
      setD(await fetchMacroFund()); setErr(null)
    } catch (e) { setErr((e as Error).message) }
    finally { setLoading(false) }
  }, [])

  useEffect(() => { load(); const t = setInterval(load, 60_000); return () => clearInterval(t) }, [load])

  if (loading) return <div style={{ padding: 24, color: "var(--txd)" }}>Loading the fund book…</div>
  if (err) return <div style={{ padding: 24, color: "var(--dn)" }}>{err}</div>
  if (!d?.ok) {
    return (
      <div style={{ padding: 24 }}>
        <h1 style={{ fontSize: "1.4em", marginBottom: 10 }}>Macro fund</h1>
        <div style={{ ...CARD, color: "var(--txd)" }}>
          {d?.reason ?? "No book yet."}<br /><br />
          Create one with:{" "}
          <code style={{ color: "var(--ac)" }}>python macro_task.py --reset --capital 100000</code>
        </div>
      </div>
    )
  }

  const p = d.performance!, ex = d.exposure!, ccy = d.base_currency ?? "USD"

  return (
    <div style={{ padding: "18px 20px 60px", display: "flex", flexDirection: "column", gap: 18 }}>
      <header style={{ display: "flex", alignItems: "baseline", gap: 12, flexWrap: "wrap" }}>
        <h1 style={{ fontSize: "1.4em", margin: 0 }}>Macro fund</h1>
        <span style={{
          fontFamily: "'JetBrains Mono', monospace", fontSize: ".68em", fontWeight: 700,
          letterSpacing: ".08em", padding: "2px 7px", borderRadius: 3,
          border: "1px solid var(--ac)", color: "var(--ac)",
        }}>{d.mode} · {d.hypothesis}</span>
        <span style={{ color: "var(--txd)", fontSize: ".8em" }}>
          cross-asset trend · 18 global markets · marked {d.last_mark ?? "—"}
        </span>
      </header>

      {!p.mature && (
        <div style={{
          border: "1px solid var(--wn, #b8860b)", borderLeft: "3px solid var(--wn, #b8860b)",
          background: "rgba(184,134,11,.08)", borderRadius: 6, padding: "12px 15px", fontSize: ".85em",
        }}>
          <b style={{ color: "var(--wn, #b8860b)" }}>Not mature — {p.days_live} of 365 days.</b>{" "}
          H-022 pre-registers evaluation no earlier than <b>{p.evaluate_after}</b>, with four kill
          criteria fixed in advance. Everything below is a progress indicator, not a verdict.
          Reading a result off it now is the mistake the pre-registration exists to prevent.
        </div>
      )}

      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit,minmax(168px,1fr))", gap: 12 }}>
        {[
          { k: "NAV", v: `${ccy} ${fmt(p.nav, 0)}`, sub: `from ${fmt(p.capital, 0)}`, c: posCol(p.nav - p.capital) },
          { k: "Return", v: `${signed(p.total_return_pct)}%`, sub: `${p.days_live}d live`, c: posCol(p.total_return_pct) },
          { k: "Realised", v: signed(p.realised_pnl, 0), sub: `costs ${fmt(p.total_costs, 0)}`, c: posCol(p.realised_pnl) },
          { k: "Unrealised", v: signed(p.unrealised_pnl, 0), sub: `${ex.n_long}L / ${ex.n_short}S`, c: posCol(p.unrealised_pnl) },
          { k: "Gross exp.", v: `${fmt(ex.gross)}x`, sub: `net ${signed(ex.net)}x`, c: "var(--tx)" },
          { k: "Max DD", v: `${fmt(p.max_drawdown_pct)}%`, sub: p.n_marks > 20 ? `Sharpe ${fmt(p.sharpe_annualised)}` : "too few marks", c: "var(--tx)" },
        ].map(t => (
          <div key={t.k} style={CARD}>
            <div style={{ fontSize: ".72em", color: "var(--txd)", textTransform: "uppercase", letterSpacing: ".08em" }}>{t.k}</div>
            <div style={{ fontFamily: "'JetBrains Mono', monospace", fontSize: "1.35em", fontWeight: 700, color: t.c, marginTop: 4 }}>{t.v}</div>
            <div style={{ fontSize: ".72em", color: "var(--txd)", marginTop: 2 }}>{t.sub}</div>
          </div>
        ))}
      </div>

      <div style={{ display: "grid", gridTemplateColumns: "minmax(0,2fr) minmax(0,1fr)", gap: 16, alignItems: "start" }}>
        <div style={CARD}>
          <div style={{ fontSize: ".78em", color: "var(--txd)", textTransform: "uppercase", letterSpacing: ".08em", marginBottom: 10 }}>
            NAV since inception
          </div>
          <NavChart curve={d.curve ?? []} capital={p.capital} />
        </div>
        <div style={CARD}>
          <div style={{ fontSize: ".78em", color: "var(--txd)", textTransform: "uppercase", letterSpacing: ".08em", marginBottom: 12 }}>
            Net exposure by class
          </div>
          <ExposureBars byClass={ex.by_class} />
          <div style={{ fontSize: ".72em", color: "var(--txd)", marginTop: 12, lineHeight: 1.5 }}>
            Gross {fmt(ex.gross)}x of NAV ({ccy} {fmt(ex.gross_usd, 0)}). A notional book
            financed by margin — exposure above 1x is the design, not leverage drift.
          </div>
        </div>
      </div>

      <div>
        <div style={{ fontSize: ".78em", color: "var(--txd)", textTransform: "uppercase", letterSpacing: ".08em", marginBottom: 8 }}>
          Open positions ({d.positions?.length ?? 0}) · last rebalance {d.last_rebalance ?? "—"}
        </div>
        <div style={{ overflowX: "auto", border: "1px solid var(--bd)", borderRadius: 8 }}>
          <table style={{ borderCollapse: "collapse", width: "100%", minWidth: 720 }}>
            <thead>
              <tr>
                <th style={TH}>Market</th><th style={TH}>Class</th><th style={TH}>Dir</th>
                <th style={{ ...TH, textAlign: "right" }}>Weight</th>
                <th style={{ ...TH, textAlign: "right" }}>Entry</th>
                <th style={{ ...TH, textAlign: "right" }}>Last</th>
                <th style={{ ...TH, textAlign: "right" }}>Value</th>
                <th style={{ ...TH, textAlign: "right" }}>P&amp;L</th>
              </tr>
            </thead>
            <tbody>
              {(d.positions ?? []).map(pos => (
                <tr key={pos.ticker}>
                  <td style={{ ...TD, fontFamily: "inherit" }}>{pos.name}</td>
                  <td style={{ ...TD, color: "var(--txd)" }}>{pos.asset_class}</td>
                  <td style={{ ...TD, color: pos.direction === "long" ? "var(--up)" : "var(--dn)" }}>{pos.direction}</td>
                  <td style={{ ...TD, textAlign: "right" }}>{signed(pos.weight, 3)}</td>
                  <td style={{ ...TD, textAlign: "right" }}>{fmt(pos.entry_price)}</td>
                  <td style={{ ...TD, textAlign: "right" }}>{fmt(pos.last_price)}</td>
                  <td style={{ ...TD, textAlign: "right", color: "var(--txd)" }}>{signed(pos.market_value, 0)}</td>
                  <td style={{ ...TD, textAlign: "right", color: posCol(pos.pnl) }}>{signed(pos.pnl)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>

      <div style={{ ...CARD, fontSize: ".78em", color: "var(--txd)", lineHeight: 1.6 }}>
        <b style={{ color: "var(--tx)" }}>How to read this.</b> This is a notional book, so it does
        not reconcile against the ₹10L equity account or the journal replay on Portfolio — three
        different books, three different bases, all correct. Positions are USD ETFs, which is what
        an account funded through LRS would actually hold, so signal and execution share one
        instrument. Rebalance is monthly; costs are charged at 10bp on turnover both ways.
        Nothing here places an order.
      </div>
    </div>
  )
}
