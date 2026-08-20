"use client"

// Portfolio tab — two funded paper books, run as an actual ledger.
//
// The Overview tab used to show "Rs per trade = 50,000" times each trade's
// return. That is a per-trade statistic wearing a rupee sign: there is no pot,
// nothing is ever debited, so a trade the account could never have afforded
// pays out exactly like one it could, and 98 open positions look like 3.
//
// Here each sleeve starts with real capital. A position costs cash to open and
// returns cash when it closes, so the headline is a BALANCE, not a sum of
// independent returns — and a signal the sleeve could not afford is reported as
// skipped instead of silently paid.

import { useEffect, useState } from "react"

import { fetchPortfolio } from "@/lib/api"

const REFRESH_MS = 30_000
const GREEN = "var(--g)", RED = "var(--r)", AMBER = "var(--y)"

interface BookTrade {
  symbol: string; direction: string; entry_date: string; exit_date: string
  quantity: number; entry_price: number; exit_price: number
  cost_basis: number; pnl: number; pnl_pct: number; costs: number
  outcome: string; instrument: string; detail: string
}
interface CurvePoint { date: string; equity: number; cash: number; deployed: number; realized: number }
interface SleeveData {
  name: string; starting_capital: number; equity: number; realized_pnl: number
  return_pct: number; total_costs: number
  n_signals: number; n_taken: number; skipped_no_cash: number; skipped_no_data: number
  wins: number; losses: number; win_rate: number; profit_factor: number
  gross_profit: number; gross_loss: number
  avg_win: number; avg_loss: number; avg_trade: number
  peak_equity: number; max_drawdown_pct: number
  peak_deployed: number; peak_deployed_pct: number
  curve: CurvePoint[]; trades: BookTrade[]
}
interface PortfolioData {
  equity?: SleeveData; options?: SleeveData
  combined?: {
    starting_capital: number; equity: number; realized_pnl: number
    return_pct: number; n_taken: number; skipped_no_cash: number; total_costs: number
  }
  config?: Record<string, number | string>
  error?: string
}

function rupees(n: number | null | undefined, dec = 0): string {
  if (n == null) return "—"
  return `${n < 0 ? "-" : ""}₹${Math.abs(n).toLocaleString("en-IN", {
    minimumFractionDigits: dec, maximumFractionDigits: dec,
  })}`
}
const clr = (n: number) => (n > 0 ? GREEN : n < 0 ? RED : "var(--tx)")

const CARD: React.CSSProperties = {
  background: "var(--c1)", border: "1px solid var(--bd)", borderRadius: 8,
  padding: "14px 16px",
}
const TH: React.CSSProperties = {
  background: "var(--c2)", color: "var(--txd)", fontSize: ".64em", fontWeight: 700,
  textTransform: "uppercase", letterSpacing: ".08em", padding: "8px 10px",
  borderBottom: "1px solid var(--bdh)", whiteSpace: "nowrap", textAlign: "right",
}
const TD: React.CSSProperties = {
  padding: "6px 10px", borderBottom: "1px solid color-mix(in srgb, var(--bd) 60%, transparent)",
  color: "var(--tx)", fontSize: ".78em", fontFamily: "'JetBrains Mono', monospace",
  whiteSpace: "nowrap", textAlign: "right",
}
const LBL: React.CSSProperties = {
  fontSize: ".58em", fontWeight: 600, textTransform: "uppercase",
  letterSpacing: ".1em", color: "var(--txs)", marginBottom: 3,
}
const VAL: React.CSSProperties = {
  fontSize: "1.25em", fontWeight: 700, fontFamily: "'JetBrains Mono', monospace",
}

/** Realised-equity curve. Steps only on a close, which is the point: this is a
 *  ledger, not a mark-to-market. */
function Curve({ pts, start }: { pts: CurvePoint[]; start: number }) {
  if (pts.length < 2) return null
  const W = 720, H = 130, P = 4
  const vals = pts.map(p => p.equity)
  const lo = Math.min(start, ...vals), hi = Math.max(start, ...vals)
  const span = hi - lo || 1
  const x = (i: number) => P + (i / (pts.length - 1)) * (W - 2 * P)
  const y = (v: number) => P + (1 - (v - lo) / span) * (H - 2 * P)
  const line = pts.map((p, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${y(p.equity).toFixed(1)}`).join("")
  const area = `${line}L${x(pts.length - 1).toFixed(1)},${y(lo).toFixed(1)}L${x(0).toFixed(1)},${y(lo).toFixed(1)}Z`
  const end = pts[pts.length - 1].equity
  const up = end >= start
  const c = up ? GREEN : RED
  return (
    <svg viewBox={`0 0 ${W} ${H}`} width="100%" height={H} role="img"
         aria-label={`equity curve, ${rupees(start)} to ${rupees(end)}`}
         style={{ display: "block", overflow: "visible" }}>
      <line x1={P} x2={W - P} y1={y(start)} y2={y(start)} stroke="var(--bdh)"
            strokeWidth="1" strokeDasharray="3 3" />
      <path d={area} fill={c} opacity=".10" />
      <path d={line} fill="none" stroke={c} strokeWidth="1.6"
            strokeLinejoin="round" strokeLinecap="round" />
      <circle cx={x(pts.length - 1)} cy={y(end)} r="3.2" fill={c} />
    </svg>
  )
}

function Stat({ label, value, color, sub }: {
  label: string; value: string; color?: string; sub?: string
}) {
  return (
    <div>
      <div style={LBL}>{label}</div>
      <div style={{ ...VAL, color: color ?? "var(--tx)" }}>{value}</div>
      {sub && <div style={{ fontSize: ".64em", color: "var(--txd)", marginTop: 2 }}>{sub}</div>}
    </div>
  )
}

function SleeveCard({ s, blurb }: { s: SleeveData; blurb: string }) {
  const up = s.realized_pnl >= 0
  return (
    <section style={{ ...CARD, display: "flex", flexDirection: "column", gap: 14 }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline", gap: 12, flexWrap: "wrap" }}>
        <div>
          <div style={{ fontSize: ".8em", fontWeight: 800, textTransform: "uppercase", letterSpacing: ".08em" }}>
            {s.name === "equity" ? "Equity sleeve" : "F&O options sleeve"}
          </div>
          <div style={{ fontSize: ".68em", color: "var(--txd)", marginTop: 2 }}>{blurb}</div>
        </div>
        <div style={{ textAlign: "right" }}>
          <div style={{ fontSize: "1.7em", fontWeight: 800, color: clr(s.realized_pnl), fontFamily: "'JetBrains Mono', monospace" }}>
            {rupees(s.equity)}
          </div>
          <div style={{ fontSize: ".72em", color: clr(s.realized_pnl), fontFamily: "'JetBrains Mono', monospace" }}>
            {up ? "+" : ""}{rupees(s.realized_pnl)} ({s.return_pct >= 0 ? "+" : ""}{s.return_pct.toFixed(2)}%)
          </div>
        </div>
      </div>

      <Curve pts={s.curve} start={s.starting_capital} />

      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(118px,1fr))", gap: 12 }}>
        <Stat label="Started with" value={rupees(s.starting_capital)} />
        <Stat label="Win rate" value={`${s.win_rate.toFixed(1)}%`}
              color={s.win_rate >= 50 ? GREEN : RED} sub={`${s.wins}W / ${s.losses}L`} />
        <Stat label="Profit factor" value={s.profit_factor.toFixed(2)}
              color={s.profit_factor >= 1 ? GREEN : RED} />
        <Stat label="Avg / trade" value={rupees(s.avg_trade)} color={clr(s.avg_trade)} />
        <Stat label="Max drawdown" value={`${s.max_drawdown_pct.toFixed(2)}%`}
              color={s.max_drawdown_pct < -10 ? RED : AMBER} />
        <Stat label="Costs paid" value={rupees(s.total_costs)} color="var(--txd)" />
      </div>

      <div style={{
        display: "flex", gap: 18, flexWrap: "wrap", fontSize: ".7em",
        color: "var(--txd)", borderTop: "1px solid var(--bd)", paddingTop: 10,
      }}>
        <span><strong style={{ color: "var(--tx)" }}>{s.n_taken}</strong> taken</span>
        <span style={{ color: s.skipped_no_cash ? AMBER : undefined }}>
          <strong>{s.skipped_no_cash}</strong> skipped — sleeve was fully deployed
        </span>
        <span>peak deployed <strong style={{ color: "var(--tx)" }}>{s.peak_deployed_pct}%</strong></span>
        <span>avg win {rupees(s.avg_win)} · avg loss {rupees(s.avg_loss)}</span>
      </div>
    </section>
  )
}

function TradeTable({ trades, isOption }: { trades: BookTrade[]; isOption: boolean }) {
  const rows = [...trades].sort((a, b) => b.exit_date.localeCompare(a.exit_date)).slice(0, 40)
  return (
    <div style={{ overflowX: "auto", border: "1px solid var(--bd)", borderRadius: 8, background: "var(--c1)" }}>
      <table style={{ width: "100%", borderCollapse: "collapse", fontFamily: "'JetBrains Mono', monospace" }}>
        <thead><tr>
          {["EXIT", "SYMBOL", "DIR", isOption ? "CONTRACT" : "QTY", "ENTRY", "EXIT PX",
            "CAPITAL USED", "P&L", "ON CAPITAL", "OUTCOME"].map((h, i) => (
            <th key={h} style={{ ...TH, textAlign: i <= 3 ? "left" : "right" }}>{h}</th>
          ))}
        </tr></thead>
        <tbody>
          {rows.length === 0 && (
            <tr><td colSpan={10} style={{ ...TD, textAlign: "center", color: "var(--txd)", padding: 22 }}>
              No closed positions in this sleeve
            </td></tr>
          )}
          {rows.map((t, i) => (
            <tr key={`${t.symbol}-${t.exit_date}-${i}`}>
              <td style={{ ...TD, textAlign: "left", color: "var(--txs)" }}>{t.exit_date}</td>
              <td style={{ ...TD, textAlign: "left", fontWeight: 800 }}>{t.symbol}</td>
              <td style={{ ...TD, textAlign: "left", fontWeight: 700, color: t.direction === "LONG" ? GREEN : RED }}>
                {t.direction}
              </td>
              <td style={{ ...TD, textAlign: "left", color: "var(--txd)" }}>{t.detail}</td>
              <td style={TD}>{t.entry_price.toLocaleString("en-IN")}</td>
              <td style={TD}>{t.exit_price.toLocaleString("en-IN")}</td>
              <td style={{ ...TD, color: "var(--txd)" }}>{rupees(t.cost_basis)}</td>
              <td style={{ ...TD, fontWeight: 700, color: clr(t.pnl) }}>
                {t.pnl >= 0 ? "+" : ""}{rupees(t.pnl)}
              </td>
              <td style={{ ...TD, color: clr(t.pnl) }}>
                {t.pnl_pct >= 0 ? "+" : ""}{t.pnl_pct.toFixed(1)}%
              </td>
              <td style={{ ...TD, textAlign: "right", color: "var(--txd)" }}>{t.outcome}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

export default function PortfolioPage() {
  const [d, setD] = useState<PortfolioData | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const [tab, setTab] = useState<"equity" | "options">("equity")

  useEffect(() => {
    let on = true
    const load = async () => {
      try {
        const r = await fetchPortfolio() as PortfolioData
        if (!on) return
        setD(r); setErr(r.error ?? null)
      } catch (e) { if (on) setErr(String(e)) }
    }
    void load()
    const t = window.setInterval(() => void load(), REFRESH_MS)
    return () => { on = false; window.clearInterval(t) }
  }, [])

  if (err && !d?.combined) {
    return <div style={{ color: RED, fontSize: ".85em", padding: 16 }}>Portfolio unavailable: {err}</div>
  }
  if (!d?.combined) {
    return <div style={{ color: "var(--txd)", fontSize: ".85em", padding: 16 }}>Building the book…</div>
  }

  const c = d.combined
  const eq = d.equity, op = d.options
  const active = tab === "equity" ? eq : op

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 18 }}>
      <div className="secHdr">
        <div className="secDot" style={{ background: "var(--g)" }} />
        <div className="secTitle">Paper portfolio — ₹10,00,000 funded</div>
      </div>

      {/* Combined roll-up */}
      <section style={{ ...CARD, display: "flex", flexDirection: "column", gap: 12 }}>
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline", gap: 14, flexWrap: "wrap" }}>
          <div>
            <div style={LBL}>Total book value</div>
            <div style={{ fontSize: "2.4em", fontWeight: 800, lineHeight: 1.05, color: clr(c.realized_pnl), fontFamily: "'JetBrains Mono', monospace" }}>
              {rupees(c.equity)}
            </div>
          </div>
          <div style={{ textAlign: "right" }}>
            <div style={{ fontSize: "1.15em", fontWeight: 700, color: clr(c.realized_pnl), fontFamily: "'JetBrains Mono', monospace" }}>
              {c.realized_pnl >= 0 ? "+" : ""}{rupees(c.realized_pnl)}
            </div>
            <div style={{ fontSize: ".78em", color: clr(c.realized_pnl), fontFamily: "'JetBrains Mono', monospace" }}>
              {c.return_pct >= 0 ? "+" : ""}{c.return_pct.toFixed(2)}% on {rupees(c.starting_capital)}
            </div>
          </div>
        </div>
        <div style={{ fontSize: ".72em", color: "var(--txd)", borderTop: "1px solid var(--bd)", paddingTop: 10 }}>
          Two books held apart on purpose — different instruments, different cost
          structures, different failure modes. The total is their sum, never a
          blended return. {c.skipped_no_cash} signal{c.skipped_no_cash === 1 ? " was" : "s were"} not
          taken across the two books because the sleeve was already fully
          deployed — the system fires more signals than ₹5,00,000 a side supports.
        </div>
      </section>

      {eq && (
        <SleeveCard s={eq} blurb={`swing equity · ${rupees(Number(d.config?.equity_slot_rupees ?? 50000))} per position`} />
      )}
      {op && (
        <SleeveCard s={op} blurb="F&O journal · 1 lot per signal, premium paid up front" />
      )}

      {/* Ledger */}
      <div style={{ display: "flex", gap: 6, marginTop: 4 }}>
        {(["equity", "options"] as const).map(k => (
          <button key={k} onClick={() => setTab(k)} style={{
            padding: "6px 14px", borderRadius: 6, cursor: "pointer",
            border: `1px solid ${tab === k ? "var(--b)" : "var(--bd)"}`,
            background: tab === k ? "rgba(56,178,240,.10)" : "transparent",
            color: tab === k ? "var(--b)" : "var(--txd)",
            fontSize: ".74em", fontWeight: 600, textTransform: "uppercase",
            letterSpacing: ".06em",
          }}>
            {k === "equity" ? "Equity ledger" : "Options ledger"}
          </button>
        ))}
      </div>
      {active && <TradeTable trades={active.trades} isOption={tab === "options"} />}

      <p style={{ fontSize: ".7em", color: "var(--txd)", lineHeight: 1.6 }}>
        Closed positions only — the curve steps on an exit, because this is a
        ledger rather than a mark-to-market. Equity P&amp;L uses the swing book&rsquo;s
        own net return, which already carries its round trip
        (25bp delivery / 10bp futures); charging again here would double-count.
        Option P&amp;L applies {Number(d.config?.option_spread_pct_per_leg ?? 0.005) * 100}% of
        premium per leg for spread plus ₹{Number(d.config?.option_brokerage_per_leg ?? 20)} brokerage
        per leg, shown as &ldquo;costs paid&rdquo; rather than folded in silently.
      </p>
    </div>
  )
}
