"use client"
import { useEffect, useState, useCallback } from "react"
import { fetchAllocation } from "@/lib/api"

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

interface Target { state: string; equity_weight: number; cash_weight: number; note: string }
interface Perf { cagr: number; vol: number; sharpe: number; max_dd: number; years: number }
interface HorizonRow { horizon: string; days: number; accuracy: number; avg_return: number; worst: number; windows: number }
interface CurvePoint { d: string; v: number }
interface Comparison {
  label?: string; caveat?: string; backtest?: Perf
  curve?: { basket: CurvePoint[]; nifty: CurvePoint[] }
  error?: string
}
interface AllocationData {
  asof: string | null
  instrument: string
  nifty?: number
  ma200?: number
  distance_to_flip_pct?: number
  targets: Record<string, Target>
  backtest: Record<string, Perf>
  horizon_accuracy: Record<string, HorizonRow[]>
  comparison?: Comparison
  alert?: string | null
  error?: string
}

function CurveChart({ curve }: { curve: { basket: CurvePoint[]; nifty: CurvePoint[] } }) {
  const W = 640, H = 200, PAD = 36
  const all = [...curve.basket.map(p => p.v), ...curve.nifty.map(p => p.v)]
  const vMax = Math.max(...all), vMin = Math.min(...all)
  const x = (i: number, n: number) => PAD + (i / Math.max(n - 1, 1)) * (W - PAD - 8)
  const y = (v: number) => H - 24 - ((v - vMin) / (vMax - vMin || 1)) * (H - 40)
  const path = (pts: CurvePoint[]) => pts.map((p, i) => `${i ? "L" : "M"}${x(i, pts.length).toFixed(1)},${y(p.v).toFixed(1)}`).join(" ")
  const yearTicks = curve.nifty.map((p, i) => ({ p, i })).filter(({ p }) => p.d.endsWith("-12"))
  return (
    <svg viewBox={`0 0 ${W} ${H}`} style={{ width: "100%", height: "auto" }} role="img"
      aria-label="Growth of 100: top-100 F&O basket vs NIFTY">
      {[100, 200, 300, 400].filter(g => g >= vMin && g <= vMax).map(g => (
        <g key={g}>
          <line x1={PAD} x2={W - 8} y1={y(g)} y2={y(g)} stroke="var(--bd)" strokeWidth={1} />
          <text x={4} y={y(g) + 4} fill="var(--txs)" fontSize={10} fontFamily="'JetBrains Mono',monospace">{g}</text>
        </g>
      ))}
      {yearTicks.map(({ p, i }) => (
        <text key={p.d} x={x(i, curve.nifty.length)} y={H - 8} fill="var(--txs)" fontSize={10}
          textAnchor="middle" fontFamily="'JetBrains Mono',monospace">{String(Number(p.d.slice(0, 4)) + 1)}</text>
      ))}
      <path d={path(curve.nifty)} fill="none" stroke="var(--txd)" strokeWidth={1.5} strokeDasharray="5 4" />
      <path d={path(curve.basket)} fill="none" stroke="#38b2f0" strokeWidth={2} />
    </svg>
  )
}

const VARIANTS = [
  { id: "overlay", label: "Core + 200-DMA Overlay (active)" },
  { id: "core",    label: "Index Core (buy & hold)" },
] as const

export default function AllocationPage() {
  const [data, setData] = useState<AllocationData | null>(null)
  const [refreshing, setRefreshing] = useState(false)
  const [variant, setVariant] = useState<"overlay" | "core">("overlay")

  const reload = useCallback(async (refresh = false) => {
    if (refresh) setRefreshing(true)
    try { setData(await fetchAllocation(refresh)) } catch {}
    setRefreshing(false)
  }, [])

  useEffect(() => {
    reload()
    const id = setInterval(reload, 60000)   // auto-refresh — never goes stale
    return () => clearInterval(id)
  }, [reload])

  const t = data?.targets?.[variant]
  const riskOn = t?.state === "risk_on" || t?.state === "core_only"
  const stateClr = riskOn ? "#00c896" : "#f59e0b"
  const horizon = data?.horizon_accuracy?.[variant] ?? []

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
      {/* Section header */}
      <div className="secHdr">
        <div className="secDot" style={{ background: "#38b2f0" }} />
        <div className="secTitle">Path-#1 Allocation · Capture the Equity Premium Cheaply</div>
      </div>

      {/* Flip alert */}
      {data?.alert && (
        <div style={{
          background: "rgba(245,158,11,.08)", border: "1px solid #f59e0b",
          borderRadius: 8, padding: "10px 14px", fontSize: ".82em", color: "#f59e0b", fontWeight: 600,
        }}>
          ⚠ {data.alert}
        </div>
      )}
      {data?.error && (
        <div style={{ color: "#ff3d5e", fontSize: ".8em" }}>API error: {data.error}</div>
      )}

      {/* Variant toggle */}
      <div style={{ borderBottom: "1px solid var(--bd)" }}>
        {VARIANTS.map(v => (
          <button key={v.id} onClick={() => setVariant(v.id)} style={{
            padding: "7px 14px", border: "none", cursor: "pointer", background: "transparent",
            borderBottom: variant === v.id ? "2px solid #38b2f0" : "2px solid transparent",
            color: variant === v.id ? "#38b2f0" : "var(--txd)",
            fontSize: ".7em", fontWeight: 600, textTransform: "uppercase", letterSpacing: ".08em",
          }}>
            {v.label}
          </button>
        ))}
      </div>

      {/* Today's target */}
      {t && (
        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(160px,1fr))", gap: 8 }}>
          <div style={{ background: "var(--c1)", border: `1px solid ${stateClr}`, borderRadius: 8, padding: "10px 14px" }}>
            <div style={{ fontSize: ".58em", fontWeight: 600, textTransform: "uppercase", letterSpacing: ".1em", color: "var(--txs)", marginBottom: 3 }}>State · {data?.asof ?? ""}</div>
            <div style={{ fontSize: "1.3em", fontWeight: 700, color: stateClr, fontFamily: "'JetBrains Mono', monospace" }}>
              {t.state.replace("_", " ").toUpperCase()}
            </div>
          </div>
          <div style={{ background: "var(--c1)", border: "1px solid var(--bd)", borderRadius: 8, padding: "10px 14px" }}>
            <div style={{ fontSize: ".58em", fontWeight: 600, textTransform: "uppercase", letterSpacing: ".1em", color: "var(--txs)", marginBottom: 3 }}>Equity ({data?.instrument})</div>
            <div style={{ fontSize: "1.3em", fontWeight: 700, color: "#00c896", fontFamily: "'JetBrains Mono', monospace" }}>{fmt((t.equity_weight ?? 0) * 100, 0)}%</div>
          </div>
          <div style={{ background: "var(--c1)", border: "1px solid var(--bd)", borderRadius: 8, padding: "10px 14px" }}>
            <div style={{ fontSize: ".58em", fontWeight: 600, textTransform: "uppercase", letterSpacing: ".1em", color: "var(--txs)", marginBottom: 3 }}>Cash / Liquid</div>
            <div style={{ fontSize: "1.3em", fontWeight: 700, color: "var(--tx)", fontFamily: "'JetBrains Mono', monospace" }}>{fmt((t.cash_weight ?? 0) * 100, 0)}%</div>
          </div>
          {data?.nifty != null && (
            <div style={{ background: "var(--c1)", border: "1px solid var(--bd)", borderRadius: 8, padding: "10px 14px" }}>
              <div style={{ fontSize: ".58em", fontWeight: 600, textTransform: "uppercase", letterSpacing: ".1em", color: "var(--txs)", marginBottom: 3 }}>NIFTY vs 200-DMA</div>
              <div style={{ fontSize: "1.05em", fontWeight: 700, fontFamily: "'JetBrains Mono', monospace", color: (data.distance_to_flip_pct ?? 0) >= 0 ? "#00c896" : "#f59e0b" }}>
                {fmt(data.nifty, 0)} / {fmt(data.ma200, 0)}
                <span style={{ fontSize: ".72em", marginLeft: 6 }}>
                  ({(data.distance_to_flip_pct ?? 0) >= 0 ? "+" : ""}{fmt(data.distance_to_flip_pct, 2)}%)
                </span>
              </div>
            </div>
          )}
        </div>
      )}
      {t && <div style={{ fontSize: ".74em", color: "var(--txd)" }}>{t.note}</div>}

      {/* Backtest summary */}
      <div className="secHdr">
        <div className="secDot" style={{ background: "#a78bfa" }} />
        <div className="secTitle">Honest Backtest · net of switch cost, ETF expense, cash yield</div>
      </div>
      <div style={{ overflow: "auto", border: "1px solid var(--bd)", borderRadius: 8, background: "var(--c1)" }}>
        <table style={{ width: "100%", borderCollapse: "collapse" }}>
          <thead><tr>{["Variant", "CAGR", "Vol", "Sharpe", "Max DD", "Years"].map(h => <th key={h} style={TH}>{h}</th>)}</tr></thead>
          <tbody>
            {VARIANTS.map(v => {
              const p = data?.backtest?.[v.id]
              if (!p) return null
              return (
                <tr key={v.id} style={{ background: variant === v.id ? "rgba(56,178,240,.05)" : "transparent" }}>
                  <td style={{ ...TD, fontWeight: variant === v.id ? 700 : 400 }}>{v.label}</td>
                  <td style={{ ...TD, color: "#00c896", fontWeight: 700 }}>{fmt(p.cagr * 100, 2)}%</td>
                  <td style={TD}>{fmt(p.vol * 100, 1)}%</td>
                  <td style={TD}>{fmt(p.sharpe, 2)}</td>
                  <td style={{ ...TD, color: "#ff3d5e" }}>{fmt(p.max_dd * 100, 1)}%</td>
                  <td style={{ ...TD, color: "var(--txs)" }}>{fmt(p.years, 1)}</td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>

      {/* Accuracy by holding horizon */}
      <div className="secHdr">
        <div className="secDot" style={{ background: "#00c896" }} />
        <div className="secTitle">Probability of Profit by Holding Period</div>
      </div>
      <div style={{ overflow: "auto", border: "1px solid var(--bd)", borderRadius: 8, background: "var(--c1)" }}>
        <table style={{ width: "100%", borderCollapse: "collapse" }}>
          <thead><tr>{["Hold For", "Accuracy", "Avg Return", "Worst Case", "Windows"].map(h => <th key={h} style={TH}>{h}</th>)}</tr></thead>
          <tbody>
            {!horizon.length && (
              <tr><td colSpan={5} style={{ ...TD, textAlign: "center", color: "var(--txd)", padding: 20 }}>No data</td></tr>
            )}
            {horizon.map(h => {
              const acc = h.accuracy * 100
              const accClr = acc >= 70 ? "#00c896" : acc >= 60 ? "#f59e0b" : "var(--tx)"
              return (
                <tr key={h.horizon}>
                  <td style={{ ...TD, fontWeight: 600 }}>{h.horizon}</td>
                  <td style={{ ...TD, fontWeight: 700, color: accClr }}>{fmt(acc, 1)}%</td>
                  <td style={{ ...TD, color: h.avg_return >= 0 ? "#00c896" : "#ff3d5e" }}>
                    {h.avg_return >= 0 ? "+" : ""}{fmt(h.avg_return * 100, 2)}%
                  </td>
                  <td style={{ ...TD, color: h.worst >= 0 ? "#00c896" : "#ff3d5e" }}>{fmt(h.worst * 100, 2)}%</td>
                  <td style={{ ...TD, color: "var(--txs)" }}>{h.windows}</td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>
      <div style={{ fontSize: ".7em", color: "var(--txs)" }}>
        Overlapping windows — reads as &quot;odds of profit entering on a random day,&quot; not independent samples.
        Accuracy here comes from premium accrual over time, not prediction. Judge decisions on the 6–12 month frame.
      </div>

      {/* Comparison: top-100 F&O equal-weight (survivors-only) */}
      {data?.comparison?.curve && (
        <>
          <div className="secHdr">
            <div className="secDot" style={{ background: "#f59e0b" }} />
            <div className="secTitle">{data.comparison.label ?? "Comparison"} vs NIFTY · Growth of ₹100</div>
          </div>
          <div style={{ border: "1px solid var(--bd)", borderRadius: 8, background: "var(--c1)", padding: "14px 10px 6px" }}>
            <div style={{ display: "flex", gap: 16, fontSize: ".68em", color: "var(--txd)", padding: "0 8px 8px" }}>
              <span><span style={{ display: "inline-block", width: 18, height: 3, background: "#38b2f0", verticalAlign: "middle", marginRight: 6 }} />{data.comparison.label}</span>
              <span><span style={{ display: "inline-block", width: 18, height: 0, borderTop: "2px dashed var(--txd)", verticalAlign: "middle", marginRight: 6 }} />NIFTY 50</span>
              {data.comparison.backtest && (
                <span style={{ marginLeft: "auto", fontFamily: "'JetBrains Mono',monospace" }}>
                  basket CAGR {fmt((data.comparison.backtest.cagr ?? 0) * 100, 1)}% · maxDD {fmt((data.comparison.backtest.max_dd ?? 0) * 100, 1)}%
                </span>
              )}
            </div>
            <CurveChart curve={data.comparison.curve} />
          </div>
          <div style={{
            background: "rgba(245,158,11,.06)", border: "1px solid rgba(245,158,11,.4)",
            borderRadius: 8, padding: "8px 12px", fontSize: ".72em", color: "#f59e0b",
          }}>
            ⚠ {data.comparison.caveat}
          </div>
        </>
      )}

      {/* Controls */}
      <div style={{ display: "flex", gap: 10, alignItems: "center", flexWrap: "wrap" }}>
        <button onClick={() => reload(true)} disabled={refreshing} style={{
          padding: "6px 16px", borderRadius: 6, cursor: "pointer",
          border: "1px solid var(--b)", background: "rgba(56,178,240,.08)",
          color: "var(--b)", fontSize: ".76em", fontWeight: 600,
          opacity: refreshing ? 0.6 : 1,
        }}>
          {refreshing ? "Refreshing…" : "↻ Refresh Data & Recompute"}
        </button>
        <span style={{ fontSize: ".68em", color: "var(--txs)", marginLeft: "auto" }}>
          Execution is manual (Dhan / DEXT T3). No orders are placed — PAPER_TRADE stays on.
        </span>
      </div>
    </div>
  )
}
