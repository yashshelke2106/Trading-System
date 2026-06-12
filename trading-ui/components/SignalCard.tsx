"use client"
import { memo } from "react"
import type { Signal } from "@/lib/types"

// ── helpers ──────────────────────────────────────────────────────────────────

function fmt(n: number | undefined | null, dec = 2): string {
  if (n == null || isNaN(n)) return "—"
  return n.toLocaleString("en-IN", { minimumFractionDigits: dec, maximumFractionDigits: dec })
}

function signedPct(val: number, ref: number): string {
  if (!ref) return ""
  const p = ((val - ref) / ref) * 100
  return `${p >= 0 ? "+" : ""}${p.toFixed(1)}%`
}

function absPct(a: number, b: number): string {
  if (!b) return ""
  return `${(Math.abs(a - b) / b * 100).toFixed(1)}%`
}

function elapsed(ts: string): string {
  const m = Math.floor((Date.now() - new Date(ts).getTime()) / 60000)
  if (m < 1) return "<1m"
  if (m < 60) return `${m}m`
  return `${Math.floor(m / 60)}h${m % 60 ? (m % 60) + "m" : ""}`
}

function fmtTs(ts: string): string {
  try {
    const d = new Date(ts)
    return d.toLocaleDateString("en-IN", { day: "2-digit", month: "2-digit" }) +
      " " + d.toLocaleTimeString("en-IN", { hour: "2-digit", minute: "2-digit", hour12: false })
  } catch { return "" }
}

// ── Option section ────────────────────────────────────────────────────────────

function OptionSection({ s }: { s: Signal }) {
  if (!s.option_strike) return null

  const isCE    = s.option_type === "CE"
  const optClr  = isCE ? "#4cc9f0" : "#ff9eb5"
  const isLive  = s.prem_source === "live" || s.prem_source === "live_quote"
  const ltpLbl  = s.prem_source === "live" ? "LTP" :
                  s.prem_source === "live_quote" ? "LIVE" : "BSM EST"
  const e       = s.entry_prem ?? 0
  const t       = s.target_prem ?? 0
  const sl      = s.sl_prem ?? 0
  const tgPP    = e > 0 ? ((t - e) / e * 100).toFixed(1) : "0"
  const slPP    = e > 0 ? ((e - sl) / e * 100).toFixed(1) : "0"
  const rr      = e > 0 && sl > 0 ? ((t - e) / (e - sl)).toFixed(1) : "—"

  const expFmt = s.option_expiry
    ? (() => {
        try {
          const d = new Date(s.option_expiry)
          return d.toLocaleDateString("en-IN", { day: "2-digit", month: "short" })
        } catch { return s.option_expiry }
      })()
    : null

  return (
    <div className="sOpt">
      <div className="sOptHdr">
        <span style={{ color: optClr, fontWeight: 800 }}>
          {s.symbol}&nbsp;{s.option_strike?.toLocaleString("en-IN")}&nbsp;{s.option_type}
        </span>
        <span style={{ color: "#3a4f66" }}>·</span>
        {expFmt && <span style={{ color: "#6b84a0" }}>{expFmt}</span>}
        {s.iv_pct != null && (
          <>
            <span style={{ color: "#3a4f66" }}>·</span>
            <span style={{ color: "#6b84a0" }}>IV&nbsp;{s.iv_pct.toFixed(1)}%</span>
          </>
        )}
        {s.delta != null && (
          <>
            <span style={{ color: "#3a4f66" }}>·</span>
            <span style={{ color: "#6b84a0" }}>Δ&nbsp;{s.delta >= 0 ? "+" : ""}{s.delta.toFixed(3)}</span>
          </>
        )}
        {isLive
          ? <span className="bLive">LIVE</span>
          : <span className="bModel">BSM</span>
        }
      </div>
      <div className="sOptGrid">
        <div className="sOptCell">
          <div className="sLbl">{ltpLbl}</div>
          <div className="sVal" style={{ color: optClr }}>
            {e ? `₹${fmt(e)}` : "—"}
          </div>
        </div>
        <div className="sOptCell">
          <div className="sLbl">Target (+{tgPP}%)</div>
          <div className="sVal" style={{ color: "#00c896" }}>
            {t ? `₹${fmt(t)}` : "—"}
          </div>
        </div>
        <div className="sOptCell">
          <div className="sLbl">Stop (−{slPP}%)</div>
          <div className="sVal" style={{ color: "#ff3d5e" }}>
            {sl ? `₹${fmt(sl)}` : "—"}
          </div>
        </div>
        <div className="sOptCell">
          <div className="sLbl">R:R</div>
          <div className="sVal">1:{rr}</div>
        </div>
      </div>
    </div>
  )
}

// ── Main card ─────────────────────────────────────────────────────────────────

function SignalCardInner({ signal: s }: { signal: Signal }) {
  const lng    = s.direction === "long"
  const clr    = lng ? "#00c896" : "#ff3d5e"
  const dirBg  = lng ? "rgba(0,200,150,.06)" : "rgba(255,61,94,.06)"
  const arrow  = lng ? "▲" : "▼"

  const gradeMap: Record<string, [string, string, string]> = {
    A: ["rgba(0,200,150,.1)",  "#00c896", "rgba(0,200,150,.3)"],
    B: ["rgba(245,158,11,.1)", "#f59e0b", "rgba(245,158,11,.25)"],
    C: ["rgba(255,152,0,.1)",  "#ff9800", "rgba(255,152,0,.25)"],
  }
  const [gbg, gclr, gbd] = gradeMap[s.confluence_grade] ?? ["rgba(80,80,80,.1)", "#888", "rgba(80,80,80,.2)"]

  const entry  = s.entry_price
  const stop   = s.sl_price
  const target = s.target_price
  const slPct  = entry > 0 ? absPct(stop, entry) : ""
  const tgtPct = entry > 0 ? absPct(target, entry) : ""

  const patterns = (s.patterns_combined ?? []).slice(0, 4).join(", ")
  const delivered = fmtTs(s.ts)
  const isFresh   = (Date.now() - new Date(s.ts).getTime()) < 120_000

  return (
    <div className="sCard" style={{ borderLeft: `3px solid ${clr}` }}>

      {/* ── Header ── */}
      <div className="sHdr" style={{ background: dirBg }}>
        <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
          <span style={{ color: clr, fontSize: ".88em" }}>{arrow}</span>
          <span className="sSym" style={{ color: clr }}>{s.symbol}</span>
          <span style={{ fontSize: ".7em", fontWeight: 600, color: clr, opacity: .75, textTransform: "uppercase", letterSpacing: ".1em" }}>
            {s.direction.toUpperCase()}
          </span>
          {isFresh && (
            <span className="bLive" style={{ marginLeft: 4 }}>NEW</span>
          )}
        </div>
        <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
          {delivered && (
            <span style={{ fontSize: ".66em", color: "#3a4f66" }}>{delivered}</span>
          )}
          <span
            className="sGrade"
            style={{ background: gbg, color: gclr, border: `1px solid ${gbd}` }}
          >
            Grade {s.confluence_grade}&nbsp;·&nbsp;{s.confluence_score}
          </span>
        </div>
      </div>

      {/* ── Prices 4-col grid ── */}
      <div className="sPrices">
        <div className="sPCol">
          <div className="sLbl">Entry</div>
          <div className="sVal">₹{fmt(entry)}</div>
        </div>
        <div className="sPCol">
          <div className="sLbl">Stop (−{slPct})</div>
          <div className="sVal" style={{ color: "#ff3d5e" }}>₹{fmt(stop)}</div>
        </div>
        <div className="sPCol">
          <div className="sLbl">Target (+{tgtPct})</div>
          <div className="sVal" style={{ color: "#00c896" }}>₹{fmt(target)}</div>
        </div>
        <div className="sPCol" style={{ borderLeft: "1px solid var(--bd)", paddingLeft: 12 }}>
          <div className="sLbl">R:R</div>
          <div className="sVal">1:{s.rr_ratio?.toFixed(1) ?? "—"}</div>
        </div>
      </div>

      {/* ── Patterns ── */}
      {patterns && <div className="sMeta">{patterns}</div>}

      {/* ── Reason ── */}
      {s.reason && <div className="sReason">{s.reason.substring(0, 180)}</div>}

      {/* ── Timeframe confluence ── */}
      {s.per_tf && Object.keys(s.per_tf).length > 0 && (
        <div style={{ display: "flex", gap: 6, padding: "4px 12px 8px" }}>
          {(["1d", "15m", "5m"] as const).map(tf => {
            const t = s.per_tf[tf]
            if (!t) return null
            const match = t.direction === s.direction
            return (
              <span key={tf} style={{
                fontSize: ".64em", fontWeight: 700, padding: "2px 7px",
                borderRadius: 4, letterSpacing: ".06em",
                background: match ? "rgba(0,200,150,.08)" : "rgba(107,132,160,.08)",
                color: match ? "#00c896" : "#6b84a0",
                border: `1px solid ${match ? "rgba(0,200,150,.2)" : "rgba(107,132,160,.15)"}`,
              }}>
                {tf.toUpperCase()} {match ? "✓" : "≈"} {t.vol_ratio ? `${t.vol_ratio.toFixed(1)}×` : ""}
              </span>
            )
          })}
        </div>
      )}

      {/* ── Option section ── */}
      <OptionSection s={s} />
    </div>
  )
}

// Memoize: only re-render when signal data actually changes.
// Compare by symbol + ts + entry_price (cheap, reliable identity).
const SignalCard = memo(SignalCardInner, (prev, next) => {
  return prev.signal.symbol === next.signal.symbol
    && prev.signal.ts === next.signal.ts
    && prev.signal.entry_price === next.signal.entry_price
    && prev.signal.confluence_score === next.signal.confluence_score
})

export default SignalCard
