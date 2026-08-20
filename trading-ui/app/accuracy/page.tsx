"use client"
import { useEffect, useState, useCallback } from "react"
import { fetchAccuracy, fetchLearning, triggerLearning, resetLearning } from "@/lib/api"
import type { TrackingEntry } from "@/lib/types"

function fmt(n: number | null | undefined, dec = 2) {
  if (n == null) return "—"
  return n.toLocaleString("en-IN", { minimumFractionDigits: dec, maximumFractionDigits: dec })
}

const MIN_PATTERN_N = 5   // below this a pattern is noise, not evidence

/** Rupees with the sign OUTSIDE the symbol: -Rs 5,609, never Rs -5,609. */
function rupees(n: number | null | undefined, dec = 0) {
  if (n == null) return "—"
  return `${n < 0 ? "-" : ""}₹${fmt(Math.abs(n), dec)}`
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

interface LearningData {
  param_summary: Array<{ param: string; section: string; default: number; current: number; min: number; max: number }>
  pattern_stats: Record<string, { wins: number; losses: number; total: number; win_rate: number;
                                  avg_pnl: number; avg_spot_pct: number | null; spot_n: number }>
  regime_stats:  Record<string, { wins: number; losses: number; total: number; win_rate: number }>
  feature_importance: Array<{ feature: string; importance: number; direction: string }>
  recent_changes: Array<{ ts: string; n_trades: number; changes: Record<string, { from: number; to: number; reason: string }> }>
}

export default function AccuracyPage() {
  const [accuracy,  setAccuracy]  = useState<{ pnl_summary: Record<string,number>; tracking: TrackingEntry[] } | null>(null)
  const [learning,  setLearning]  = useState<LearningData | null>(null)
  const [triggering, setTriggering] = useState(false)
  const [triggerMsg, setTriggerMsg] = useState("")
  const [activeTab, setActiveTab]   = useState<"params"|"patterns"|"regimes"|"features"|"tracking"|"log">("params")

  const reload = useCallback(async () => {
    try {
      const [a, l] = await Promise.all([fetchAccuracy(), fetchLearning()])
      setAccuracy(a)
      setLearning(l)
    } catch {}
  }, [])

  useEffect(() => { reload() }, [reload])

  const doTrigger = async () => {
    setTriggering(true)
    setTriggerMsg("")
    try {
      const res = await triggerLearning() as { changes: Record<string, unknown>; error?: string }
      if (res.error) {
        setTriggerMsg(`Error: ${res.error}`)
      } else {
        const n = Object.keys(res.changes ?? {}).length
        setTriggerMsg(n > 0 ? `${n} parameter(s) updated` : "No changes (need ≥20 resolved trades)")
      }
      reload()
    } catch { setTriggerMsg("Request failed") }
    setTriggering(false)
  }

  const doReset = async () => {
    if (!confirm("Reset all learned parameters to defaults?")) return
    await resetLearning()
    setTriggerMsg("Parameters reset to defaults")
    reload()
  }

  const ps = accuracy?.pnl_summary ?? {}
  const tracking = accuracy?.tracking ?? []
  const pnlClr = (v: number) => v > 0 ? "#00c896" : v < 0 ? "#ff3d5e" : "var(--tx)"

  const TABS = [
    { id: "params",   label: "Parameters" },
    { id: "patterns", label: "Pattern Win Rates" },
    { id: "regimes",  label: "Regimes" },
    { id: "features", label: "Feature Importance" },
    { id: "tracking", label: "Live Tracking" },
    { id: "log",      label: "Change Log" },
  ] as const

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
      {/* Section header */}
      <div className="secHdr">
        <div className="secDot" style={{ background: "#a78bfa" }} />
        <div className="secTitle">Signal Accuracy · Self-Learning Engine</div>
      </div>

      {/* P&L summary metrics.
          Labelled on purpose. This tab counts the OPTIONS journal in premium
          rupees; the Overview tab counts the SWING paper book; the Verdict tab
          reports spot %. Three different questions that all used to be called
          "P&L", which is why the tabs looked like they disagreed. */}
      {Object.keys(ps).length > 0 && (
        <div style={{ fontSize: ".72em", color: "var(--txd)", marginBottom: -6 }}>
          Options journal · premium cash at the resolved contract size
          {(ps.unresolved_lot ?? 0) > 0 &&
            ` · ${ps.unresolved_lot} trade(s) excluded (contract size unknown)`}
          . Signal skill in spot % lives on the Verdict tab; the swing paper book
          is a separate book on Overview.
        </div>
      )}
      {Object.keys(ps).length > 0 && (
        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(130px,1fr))", gap: 8 }}>
          {[
            { k: "total_signals",    lbl: "Total Signals",  dec: 0, suffix: "" },
            { k: "closed_signals",   lbl: "Closed",         dec: 0, suffix: "" },
            { k: "target_hit",       lbl: "Target Hit",     dec: 0, suffix: "", clr: "#00c896" },
            { k: "sl_hit",           lbl: "SL Hit",         dec: 0, suffix: "", clr: "#ff3d5e" },
            { k: "win_rate_pct",     lbl: "Win Rate",       dec: 1, suffix: "%",
              clr: (ps.win_rate_pct ?? 0) >= 50 ? "#00c896" : "#ff3d5e" },
            { k: "total_pnl_rupees", lbl: "Total P&L",      dec: 0, suffix: "", clr: pnlClr(ps.total_pnl_rupees ?? 0) },
            { k: "avg_pnl_rupees",   lbl: "Avg P&L",        dec: 0, suffix: "", clr: pnlClr(ps.avg_pnl_rupees ?? 0) },
          ].filter(({ k }) => ps[k] != null).map(({ k, lbl, dec, suffix, clr }) => (
            <div key={k} style={{ background: "var(--c1)", border: "1px solid var(--bd)", borderRadius: 8, padding: "10px 14px" }}>
              <div style={{ fontSize: ".58em", fontWeight: 600, textTransform: "uppercase", letterSpacing: ".1em", color: "var(--txs)", marginBottom: 3 }}>{lbl}</div>
              <div style={{ fontSize: "1.3em", fontWeight: 700, color: clr ?? "var(--tx)", fontFamily: "'JetBrains Mono', monospace" }}>
                {k.endsWith("_rupees") ? rupees(ps[k], dec) : `${fmt(ps[k], dec)}${suffix}`}
              </div>
            </div>
          ))}
        </div>
      )}

      {/* Controls */}
      <div style={{ display: "flex", gap: 10, alignItems: "center", flexWrap: "wrap" }}>
        <button onClick={doTrigger} disabled={triggering} style={{
          padding: "6px 16px", borderRadius: 6, cursor: "pointer",
          border: "1px solid var(--b)", background: "rgba(56,178,240,.08)",
          color: "var(--b)", fontSize: ".76em", fontWeight: 600,
          opacity: triggering ? 0.6 : 1,
        }}>
          {triggering ? "Running…" : "▶ Trigger Learning Cycle"}
        </button>
        <button onClick={doReset} style={{
          padding: "6px 16px", borderRadius: 6, cursor: "pointer",
          border: "1px solid var(--bdh)", background: "var(--c2)",
          color: "var(--txd)", fontSize: ".76em", fontWeight: 600,
        }}>
          Reset to Defaults
        </button>
        {triggerMsg && (
          <span style={{ fontSize: ".76em", color: triggerMsg.startsWith("Error") ? "#ff3d5e" : "#00c896" }}>
            {triggerMsg}
          </span>
        )}
        <span style={{ fontSize: ".68em", color: "var(--txs)", marginLeft: "auto" }}>
          Needs ≥20 resolved trades. Runs auto every 10 scans.
        </span>
      </div>

      {/* Tabs */}
      <div style={{ borderBottom: "1px solid var(--bd)" }}>
        {TABS.map(t => (
          <button key={t.id} onClick={() => setActiveTab(t.id)} style={{
            padding: "7px 14px", border: "none", cursor: "pointer",
            background: "transparent",
            borderBottom: activeTab === t.id ? "2px solid #a78bfa" : "2px solid transparent",
            color: activeTab === t.id ? "#a78bfa" : "var(--txd)",
            fontSize: ".7em", fontWeight: 600, textTransform: "uppercase", letterSpacing: ".08em",
          }}>
            {t.label}
          </button>
        ))}
      </div>

      {/* Tab content */}
      {activeTab === "params" && (
        <div style={{ overflow: "auto", border: "1px solid var(--bd)", borderRadius: 8, background: "var(--c1)" }}>
          <table style={{ width: "100%", borderCollapse: "collapse" }}>
            <thead><tr>
              {["Parameter","Section","Default","Current","Range","Delta"].map(h => <th key={h} style={TH}>{h}</th>)}
            </tr></thead>
            <tbody>
              {!learning?.param_summary?.length && (
                <tr><td colSpan={6} style={{ ...TD, textAlign: "center", color: "var(--txd)", padding: 20 }}>No data</td></tr>
              )}
              {(learning?.param_summary ?? []).map(p => {
                const delta = p.current - p.default
                const changed = Math.abs(delta) > 0.001
                return (
                  <tr key={p.param}>
                    <td style={{ ...TD, fontWeight: 700, color: changed ? "#f59e0b" : "var(--tx)" }}>{p.param}</td>
                    <td style={{ ...TD, color: "var(--txd)", fontSize: ".72em" }}>{p.section}</td>
                    <td style={TD}>{p.default}</td>
                    <td style={{ ...TD, fontWeight: changed ? 700 : 400, color: changed ? "#f59e0b" : "var(--tx)" }}>{p.current}</td>
                    <td style={{ ...TD, color: "var(--txs)" }}>{p.min}–{p.max}</td>
                    <td style={{ ...TD, color: delta > 0 ? "#00c896" : delta < 0 ? "#ff3d5e" : "var(--txs)" }}>
                      {changed ? `${delta > 0 ? "+" : ""}${delta}` : "—"}
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>
      )}

      {activeTab === "patterns" && (
        <div>
          <p style={{ fontSize: ".72em", color: "var(--txd)", margin: "0 0 8px" }}>
            <strong style={{ color: "var(--tx)" }}>Win rate</strong> and{" "}
            <strong style={{ color: "var(--tx)" }}>Avg spot</strong>{" "}are the
            theta/IV-denoised SPOT result — the pattern&rsquo;s own edge, and the
            label the learner actually trains on.{" "}
            <strong style={{ color: "var(--tx)" }}>Avg premium</strong> is the
            cash a long-option position returned; it is negative for nearly every
            pattern by construction, because the book pays theta whatever the
            pattern does. Rank on spot, not on premium.
          </p>
          <div style={{ overflow: "auto", border: "1px solid var(--bd)", borderRadius: 8, background: "var(--c1)" }}>
            <table style={{ width: "100%", borderCollapse: "collapse" }}>
              <thead><tr>
                {["Pattern","Wins","Losses","Total","Win Rate (spot)","Avg spot %","Avg premium"]
                  .map(h => <th key={h} style={TH}>{h}</th>)}
              </tr></thead>
              <tbody>
                {!Object.keys(learning?.pattern_stats ?? {}).length && (
                  <tr><td colSpan={7} style={{ ...TD, textAlign: "center", color: "var(--txd)", padding: 20 }}>No pattern data yet</td></tr>
                )}
                {Object.entries(learning?.pattern_stats ?? {})
                  // Rank on spot edge, but a 1-trade pattern is noise, not the
                  // best pattern: anything under MIN_N sinks below the rest.
                  .sort(([,a], [,b]) => {
                    const thin = (x: typeof a) => (x.total >= MIN_PATTERN_N ? 0 : 1)
                    return thin(a) - thin(b)
                      || (b.avg_spot_pct ?? -99) - (a.avg_spot_pct ?? -99)
                  })
                  .map(([pattern, s]) => (
                    <tr key={pattern} style={s.total < MIN_PATTERN_N ? { opacity: .55 } : undefined}>
                      <td style={{ ...TD, fontWeight: 600 }}>
                        {pattern}
                        {s.total < MIN_PATTERN_N && (
                          <span style={{ color: "var(--txd)", fontWeight: 400 }} title={`only ${s.total} trade(s)`}>
                            {" "}· thin
                          </span>
                        )}
                      </td>
                      <td style={{ ...TD, color: "#00c896" }}>{s.wins}</td>
                      <td style={{ ...TD, color: "#ff3d5e" }}>{s.losses}</td>
                      <td style={TD}>{s.total}</td>
                      <td style={{ ...TD, fontWeight: 700, color: s.win_rate >= 0.6 ? "#00c896" : s.win_rate < 0.4 ? "#ff3d5e" : "#f59e0b" }}>
                        {(s.win_rate * 100).toFixed(1)}%
                      </td>
                      <td style={{ ...TD, fontWeight: 700, color: pnlClr(s.avg_spot_pct ?? 0) }}>
                        {s.avg_spot_pct == null ? "—"
                          : `${s.avg_spot_pct >= 0 ? "+" : ""}${s.avg_spot_pct.toFixed(3)}%`}
                      </td>
                      <td style={{ ...TD, color: pnlClr(s.avg_pnl) }}>{rupees(s.avg_pnl, 0)}</td>
                    </tr>
                  ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {activeTab === "regimes" && (
        <div style={{ overflow: "auto", border: "1px solid var(--bd)", borderRadius: 8, background: "var(--c1)" }}>
          <table style={{ width: "100%", borderCollapse: "collapse" }}>
            <thead><tr>
              {["Regime","Wins","Losses","Total","Win Rate"].map(h => <th key={h} style={TH}>{h}</th>)}
            </tr></thead>
            <tbody>
              {!Object.keys(learning?.regime_stats ?? {}).length && (
                <tr><td colSpan={5} style={{ ...TD, textAlign: "center", color: "var(--txd)", padding: 20 }}>No regime data yet</td></tr>
              )}
              {Object.entries(learning?.regime_stats ?? {})
                .filter(([,s]) => s.total >= 3)
                .sort(([,a], [,b]) => b.total - a.total)
                .map(([regime, s]) => (
                  <tr key={regime}>
                    <td style={{ ...TD, fontFamily: "monospace", fontSize: ".72em" }}>{regime}</td>
                    <td style={{ ...TD, color: "#00c896" }}>{s.wins}</td>
                    <td style={{ ...TD, color: "#ff3d5e" }}>{s.losses}</td>
                    <td style={TD}>{s.total}</td>
                    <td style={{ ...TD, fontWeight: 700, color: s.win_rate >= 0.6 ? "#00c896" : s.win_rate < 0.4 ? "#ff3d5e" : "#f59e0b" }}>
                      {(s.win_rate * 100).toFixed(1)}%
                    </td>
                  </tr>
                ))}
            </tbody>
          </table>
        </div>
      )}

      {activeTab === "features" && (
        <div>
          {!(learning?.feature_importance?.length) ? (
            <p style={{ color: "var(--txd)", fontSize: ".82em" }}>Feature importance requires ≥20 resolved signals. Uses logistic regression on signal features.</p>
          ) : (
            <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
              {learning.feature_importance.map(f => {
                const absMax = Math.max(...learning.feature_importance.map(x => Math.abs(x.importance)))
                const barW   = absMax > 0 ? Math.abs(f.importance) / absMax * 100 : 0
                const clr    = f.direction === "positive" ? "#00c896" : "#ff3d5e"
                return (
                  <div key={f.feature} style={{ display: "flex", alignItems: "center", gap: 10 }}>
                    <div style={{ width: 160, fontSize: ".76em", color: "var(--txd)", flexShrink: 0 }}>{f.feature}</div>
                    <div style={{ flex: 1, height: 14, background: "var(--c2)", borderRadius: 3, overflow: "hidden" }}>
                      <div style={{ width: `${barW}%`, height: "100%", background: clr, borderRadius: 3 }} />
                    </div>
                    <div style={{ width: 60, fontSize: ".76em", fontFamily: "'JetBrains Mono',monospace", color: clr, textAlign: "right" }}>
                      {f.importance > 0 ? "+" : ""}{f.importance.toFixed(3)}
                    </div>
                    <div style={{ width: 60, fontSize: ".68em", color: "var(--txs)" }}>{f.direction}</div>
                  </div>
                )
              })}
            </div>
          )}
        </div>
      )}

      {activeTab === "tracking" && (
        <div style={{ overflow: "auto", border: "1px solid var(--bd)", borderRadius: 8, background: "var(--c1)" }}>
          <table style={{ width: "100%", borderCollapse: "collapse" }}>
            <thead><tr>
              {["Symbol","Dir","Entry","Current","SL","Target","Status","P&L %","Age"].map(h => <th key={h} style={TH}>{h}</th>)}
            </tr></thead>
            <tbody>
              {!tracking.length && (
                <tr><td colSpan={9} style={{ ...TD, textAlign: "center", color: "var(--txd)", padding: 20 }}>No live tracking</td></tr>
              )}
              {tracking.map((t, i) => {
                const lng = t.direction === "long"
                const pclr = pnlClr(t.pnl_pct ?? 0)
                return (
                  <tr key={i}>
                    <td style={{ ...TD, fontWeight: 800 }}>{t.symbol}</td>
                    <td style={{ ...TD, fontWeight: 700, color: lng ? "#00c896" : "#ff3d5e" }}>{t.direction.toUpperCase()}</td>
                    <td style={TD}>{fmt(t.entry_price)}</td>
                    <td style={TD}>{fmt(t.current_price)}</td>
                    <td style={{ ...TD, color: "#ff3d5e" }}>{fmt(t.sl_price)}</td>
                    <td style={{ ...TD, color: "#00c896" }}>{fmt(t.target_price)}</td>
                    <td style={{ ...TD, color: "var(--txd)" }}>{t.status}</td>
                    <td style={{ ...TD, fontWeight: 700, color: pclr }}>
                      {t.pnl_pct != null ? `${t.pnl_pct >= 0 ? "+" : ""}${fmt(t.pnl_pct,1)}%` : "—"}
                    </td>
                    <td style={{ ...TD, color: "var(--txs)" }}>{t.elapsed_h != null ? `${t.elapsed_h.toFixed(1)}h` : "—"}</td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>
      )}

      {activeTab === "log" && (
        <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
          {!learning?.recent_changes?.length && (
            <p style={{ color: "var(--txd)", fontSize: ".82em" }}>No parameter changes yet.</p>
          )}
          {(learning?.recent_changes ?? []).slice().reverse().map((entry, i) => (
            <div key={i} style={{ background: "var(--c1)", border: "1px solid var(--bd)", borderRadius: 8, padding: "10px 14px" }}>
              <div style={{ display: "flex", gap: 10, marginBottom: 6, alignItems: "center" }}>
                <span style={{ fontSize: ".68em", color: "var(--txs)" }}>
                  {entry.ts ? new Date(entry.ts).toLocaleString("en-IN") : ""}
                </span>
                <span style={{ fontSize: ".68em", color: "var(--txd)", background: "var(--c2)", padding: "1px 6px", borderRadius: 4 }}>
                  {entry.n_trades} trades resolved
                </span>
              </div>
              {Object.entries(entry.changes ?? {}).map(([key, info]) => (
                <div key={key} style={{ display: "flex", gap: 8, alignItems: "baseline", marginBottom: 4 }}>
                  <span style={{ fontFamily: "'JetBrains Mono',monospace", fontSize: ".76em", color: "#f59e0b", minWidth: 200 }}>{key}</span>
                  <span style={{ fontSize: ".76em", color: "#ff3d5e" }}>{info.from}</span>
                  <span style={{ fontSize: ".76em", color: "var(--txs)" }}>→</span>
                  <span style={{ fontSize: ".76em", color: "#00c896" }}>{info.to}</span>
                  <span style={{ fontSize: ".68em", color: "var(--txs)", marginLeft: 6 }}>{info.reason}</span>
                </div>
              ))}
            </div>
          ))}
        </div>
      )}
    </div>
  )
}
