"use client"
// THE dashboard — one page, two splits (user decision 2026-07-24).
//
//   LEFT  "NO API"      Everything that is true without any credential:
//                       the strategy verdict, capture-archive health, the
//                       standing swing decision. Nothing here can be taken
//                       down by an expired subscription.
//   RIGHT "LIVE (DHAN)" Everything that needs a working Dhan subscription:
//                       connection probe, credential entry, and the live
//                       scanner's breakout / pattern / news-action setups.
//
// The two halves are deliberately not interleaved. Mixing verified history
// with live-feed output is how a dead feed starts looking like "no setups
// today"; keeping the wall visible means the right pane can go dark without
// making the left pane look wrong.
//
// Deep dives live in the nav tabs; this page is the at-a-glance split.
import { useEffect, useState, useCallback } from "react"
import Link from "next/link"
import {
  fetchVerdict, fetchCapture, fetchSignalsAll, fetchSpikeAlerts,
  fetchStatus, postToken, postClientId, postDataKey,
} from "@/lib/api"

const BASE = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000"
const GREEN = "#2fbf71", RED = "#e5484d", AMBER = "#d9a514"

function fmt(n: number | null | undefined, dec = 2) {
  if (n == null || Number.isNaN(n)) return "—"
  return n.toLocaleString("en-IN", { minimumFractionDigits: dec, maximumFractionDigits: dec })
}

const TH: React.CSSProperties = {
  background: "var(--c2)", color: "var(--txd)", fontSize: ".62em", fontWeight: 700,
  textTransform: "uppercase", letterSpacing: ".08em", padding: "6px 8px",
  borderBottom: "1px solid var(--bdh)", whiteSpace: "nowrap", textAlign: "left",
}
const TD: React.CSSProperties = {
  padding: "5px 8px", borderBottom: "1px solid rgba(28,42,68,.6)",
  fontFamily: "'JetBrains Mono', monospace", fontSize: ".76em", color: "var(--tx)",
}
const BOX: React.CSSProperties = {
  background: "var(--c1)", border: "1px solid var(--bd)", borderRadius: 8,
  padding: "10px 12px", marginBottom: 10, overflowX: "auto",
}
const BOXH: React.CSSProperties = {
  fontSize: ".64em", textTransform: "uppercase", letterSpacing: ".1em",
  color: "var(--txd)", marginBottom: 8,
}
const INPUT: React.CSSProperties = {
  background: "var(--c2)", border: "1px solid var(--bd)", borderRadius: 4,
  color: "var(--tx)", padding: "5px 7px", fontFamily: "'JetBrains Mono', monospace",
  fontSize: ".74em", width: "100%", minWidth: 0,
}
const BTN: React.CSSProperties = {
  background: "var(--c2)", border: "1px solid var(--bdh)", borderRadius: 4,
  color: "var(--tx)", padding: "5px 10px", fontSize: ".72em", cursor: "pointer",
  whiteSpace: "nowrap",
}

function Chip({ v }: { v: string }) {
  const s = (v || "").toUpperCase()
  const c = s.includes("ACTIVE") || s.includes("PASS") || s.includes("WORKING") ? GREEN
    : s.includes("REJECT") || s.includes("CLOSED") || s.includes("EXPIRED") || s.includes("ABANDON") ? RED
    : AMBER
  return <span style={{
    display: "inline-block", padding: "1px 7px", borderRadius: 10, fontSize: ".68em",
    fontFamily: "'JetBrains Mono', monospace", color: c, border: `1px solid ${c}`,
    whiteSpace: "nowrap",
  }}>{v || "—"}</span>
}

function Stat({ label, value, color }: { label: string; value: string; color?: string }) {
  return (
    <div style={{ minWidth: 92 }}>
      <div style={{ fontSize: "1.3em", fontFamily: "'JetBrains Mono', monospace", color: color ?? "var(--tx)" }}>
        {value}
      </div>
      <div style={{ fontSize: ".58em", textTransform: "uppercase", letterSpacing: ".08em", color: "var(--txd)" }}>
        {label}
      </div>
    </div>
  )
}

interface Lane { lane: string; verdict: string; evidence: string; action: string }
interface Signal {
  symbol?: string; direction?: string; entry_price?: number; sl_price?: number
  target_price?: number; rr_ratio?: number; confluence_score?: number
  patterns_combined?: string; reason?: string
}

function ageMin(ts?: string | null) {
  if (!ts) return null
  const t = Date.parse(ts)
  return Number.isNaN(t) ? null : Math.round((Date.now() - t) / 60000)
}

export default function Dashboard() {
  const [v, setV] = useState<any>(null)
  const [cap, setCap] = useState<any>(null)
  const [live, setLive] = useState<any>(null)
  const [sig, setSig] = useState<any>(null)
  const [spikes, setSpikes] = useState<any[]>([])
  const [spikeSkip, setSpikeSkip] = useState<string | null>(null)
  const [marketLive, setMarketLive] = useState(false)
  const [tok, setTok] = useState(""); const [cid, setCid] = useState(""); const [key, setKey] = useState("")
  const [msg, setMsg] = useState<string | null>(null)
  const [probing, setProbing] = useState(false)

  const load = useCallback(async (reprobe = false) => {
    const r = await Promise.allSettled([
      fetchVerdict(), fetchCapture(),
      fetch(`${BASE}/api/dhan-live-status${reprobe ? "?refresh=true" : ""}`, { cache: "no-store" }).then(x => x.json()),
      fetchSignalsAll(), fetchSpikeAlerts(55), fetchStatus(),
    ])
    if (r[0].status === "fulfilled") setV(r[0].value)
    if (r[1].status === "fulfilled") setCap(r[1].value)
    if (r[2].status === "fulfilled") setLive(r[2].value)
    if (r[3].status === "fulfilled") setSig(r[3].value)
    if (r[4].status === "fulfilled") {
      const sp: any = r[4].value
      setSpikes(sp?.alerts ?? [])
      setSpikeSkip(sp?.skipped ? (sp.reason ?? "skipped") : sp?.timeout ? "scan timed out" : null)
    }
    if (r[5].status === "fulfilled") setMarketLive(!!(r[5].value as any)?.is_live)
  }, [])

  useEffect(() => { load(); const t = setInterval(() => load(), 60_000); return () => clearInterval(t) }, [load])

  async function save(kind: "token" | "client" | "key") {
    setMsg(null)
    try {
      if (kind === "token" && tok.trim()) await postToken(tok.trim())
      if (kind === "client" && cid.trim()) await postClientId(cid.trim())
      if (kind === "key" && key.trim()) await postDataKey(key.trim())
      setMsg("saved — re-probing…"); setProbing(true)
      await load(true); setProbing(false); setMsg("saved")
    } catch (e) { setMsg(`save failed: ${e}`) }
  }

  const perf = v?.journal_perf ?? {}
  const ic = cap?.intraday ?? {}
  const stale = ic.worst_stale_days
  const byGrade: Record<string, Signal[]> = sig?.by_grade ?? {}
  const scanAge = ageMin(sig?.ts)
  const scanStale = scanAge != null && scanAge > 30 && marketLive
  const st = live?.status ?? "…"
  const stColor = live?.working ? GREEN : st === "auth_ok_shape_issue" ? AMBER : RED

  return (
    <main style={{ padding: "12px 14px" }}>
      <div style={{
        display: "grid", gap: 14, alignItems: "start",
        gridTemplateColumns: "repeat(auto-fit, minmax(430px, 1fr))",
      }}>

        {/* ══════════ LEFT SPLIT — NO API ══════════ */}
        <section style={{ borderTop: `2px solid ${GREEN}`, paddingTop: 10 }}>
          <h2 style={{ fontSize: ".8em", letterSpacing: ".1em", marginBottom: 4 }}>
            <span style={{ color: GREEN }}>◆</span> NO&nbsp;API — always true
          </h2>
          <p style={{ color: "var(--txd)", fontSize: ".7em", marginBottom: 10 }}>
            Reads recorded history. No credential can take this down.
          </p>

          <div style={BOX}>
            <div style={BOXH}>Verdict — good, or needs upgrade?</div>
            <div style={{ display: "flex", gap: 16, flexWrap: "wrap", marginBottom: 10 }}>
              <Stat label="profit factor" value={fmt(perf.profit_factor)}
                    color={perf.profit_factor >= 1 ? GREEN : RED} />
              <Stat label="win rate" value={perf.win_rate != null ? fmt(perf.win_rate * 100, 1) + "%" : "—"}
                    color={perf.win_rate >= .5 ? GREEN : RED} />
              <Stat label="expectancy" value={fmt(perf.expectancy_pct) + "%"}
                    color={perf.expectancy_pct > 0 ? GREEN : RED} />
              <Stat label="clean trades" value={String(perf.n_clean ?? "—")} />
            </div>
            <table style={{ width: "100%", borderCollapse: "collapse" }}>
              <thead><tr><th style={TH}>Lane</th><th style={TH}>Verdict</th><th style={TH}>Action</th></tr></thead>
              <tbody>
                {(v?.lanes ?? []).map((l: Lane) => (
                  <tr key={l.lane}>
                    <td style={TD}>{l.lane}</td>
                    <td style={TD}><Chip v={l.verdict} /></td>
                    <td style={{ ...TD, whiteSpace: "normal" }}>{l.action}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            <Link href="/verdict" style={{ fontSize: ".7em", color: "var(--txd)" }}>
              full evidence + hypothesis registry →
            </Link>
          </div>

          <div style={BOX}>
            <div style={BOXH}>Captured data — what the verdict stands on</div>
            <div style={{ display: "flex", gap: 16, flexWrap: "wrap" }}>
              <Stat label="EOD days" value={String(cap?.eod_archive?.trading_days ?? "—")} />
              <Stat label="symbols (w/ delisted)" value={String(cap?.eod_archive?.symbols ?? "—")} />
              <Stat label="intraday syms" value={String(ic.symbols ?? "—")} />
              <Stat label="worst stale" value={stale != null ? `${stale}d` : "—"}
                    color={stale > 45 ? RED : stale > 20 ? AMBER : GREEN} />
            </div>
            {ic.unrecoverable && (
              <p style={{ color: RED, fontSize: ".72em", marginTop: 6 }}>
                DATA BEING LOST — capture not keeping up with the 60-day window.
              </p>
            )}
          </div>

          <div style={BOX}>
            <div style={BOXH}>Swing — standing decision</div>
            <p style={{ fontSize: ".78em" }}>
              <Chip v={v?.swing_decision?.status ?? "—"} />{" "}
              <span style={{ color: "var(--txd)" }}>{v?.swing_decision?.detail}</span>
            </p>
          </div>
        </section>

        {/* ══════════ RIGHT SPLIT — LIVE (DHAN) ══════════ */}
        <section style={{ borderTop: `2px solid ${stColor}`, paddingTop: 10 }}>
          <h2 style={{ fontSize: ".8em", letterSpacing: ".1em", marginBottom: 4 }}>
            <span style={{ color: stColor }}>◆</span> LIVE (DHAN) — needs a working sub
          </h2>
          <p style={{ color: "var(--txd)", fontSize: ".7em", marginBottom: 10 }}>
            Scans the live market for breakout · pattern · news-action setups.
          </p>

          <div style={BOX}>
            <div style={BOXH}>Connection — probed, not assumed</div>
            <div style={{ display: "flex", gap: 12, alignItems: "center", flexWrap: "wrap", marginBottom: 8 }}>
              <span style={{
                fontFamily: "'JetBrains Mono', monospace", fontSize: ".95em", color: stColor,
                border: `1px solid ${stColor}`, borderRadius: 5, padding: "3px 10px",
              }}>
                {probing ? "PROBING…" : String(st).toUpperCase()}{live?.http ? ` (${live.http})` : ""}
              </span>
              <button style={BTN} onClick={() => { setProbing(true); load(true).finally(() => setProbing(false)) }}>
                re-probe
              </button>
            </div>
            <p style={{ color: "var(--txd)", fontSize: ".72em", marginBottom: 8 }}>{live?.message}</p>

            <div style={{ display: "grid", gap: 6 }}>
              {([
                ["ACCESS TOKEN", live?.configured?.trade_token, tok, setTok, "token", "paste JWT (eyJ…)", true],
                ["CLIENT ID", live?.configured?.client_id, cid, setCid, "client", "client id", false],
                ["DATA API KEY", live?.configured?.data_api_key, key, setKey, "key", "data api key", true],
              ] as const).map(([label, has, val, set, kind, ph, secret]) => (
                <div key={label}>
                  <div style={{ fontSize: ".62em", color: "var(--txd)", marginBottom: 3 }}>
                    {label} {has ? "(stored)" : "(missing)"}
                  </div>
                  <div style={{ display: "flex", gap: 5 }}>
                    <input style={INPUT} type={secret ? "password" : "text"} placeholder={ph}
                           value={val} onChange={e => (set as (s: string) => void)(e.target.value)} />
                    <button style={BTN} onClick={() => save(kind as "token" | "client" | "key")}>save</button>
                  </div>
                </div>
              ))}
            </div>
            {msg && <p style={{ color: "var(--txd)", fontSize: ".7em", marginTop: 6 }}>{msg}</p>}
          </div>

          <div style={BOX}>
            <div style={BOXH}>
              Live setups — breakout · pattern · news
              {sig?.ts && (
                <span style={{ marginLeft: 8, color: scanStale ? AMBER : "var(--txd)" }}>
                  scan {scanAge}m ago{scanStale ? " — STALE" : ""}
                </span>
              )}
            </div>
            {(["A", "B"] as const).map(g => (
              <div key={g} style={{ marginBottom: 6 }}>
                <div style={{ fontSize: ".62em", color: "var(--txd)", margin: "4px 0" }}>
                  GRADE {g} — {byGrade[g]?.length ?? 0}
                </div>
                <table style={{ width: "100%", borderCollapse: "collapse" }}>
                  <thead><tr>
                    <th style={TH}>Sym</th><th style={TH}>Dir</th><th style={TH}>Entry</th>
                    <th style={TH}>SL</th><th style={TH}>Tgt</th><th style={TH}>R:R</th>
                  </tr></thead>
                  <tbody>
                    {(byGrade[g] ?? []).slice(0, 8).map((s, i) => (
                      <tr key={i}>
                        <td style={TD}>{s.symbol}</td>
                        <td style={{ ...TD, color: s.direction === "long" ? GREEN : RED }}>{s.direction}</td>
                        <td style={TD}>{fmt(s.entry_price)}</td>
                        <td style={TD}>{fmt(s.sl_price)}</td>
                        <td style={TD}>{fmt(s.target_price)}</td>
                        <td style={TD}>{fmt(s.rr_ratio, 1)}</td>
                      </tr>
                    ))}
                    {!(byGrade[g] ?? []).length && (
                      <tr><td style={{ ...TD, color: "var(--txd)" }} colSpan={6}>
                        {live?.working ? "none" : "idle — connect Dhan above"}
                      </td></tr>
                    )}
                  </tbody>
                </table>
              </div>
            ))}
            <Link href="/live" style={{ fontSize: ".7em", color: "var(--txd)" }}>
              full scanner view + reasons →
            </Link>
          </div>

          <div style={BOX}>
            <div style={BOXH}>Volume spike alerts</div>
            {spikes.length ? (
              <table style={{ width: "100%", borderCollapse: "collapse" }}>
                <thead><tr><th style={TH}>Sym</th><th style={TH}>Dir</th><th style={TH}>Conf</th></tr></thead>
                <tbody>
                  {spikes.slice(0, 6).map((a, i) => (
                    <tr key={i}>
                      <td style={TD}>{a.symbol}</td><td style={TD}>{a.direction}</td>
                      <td style={TD}>{fmt(a.confidence, 0)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            ) : (
              <p style={{ color: spikeSkip ? AMBER : "var(--txd)", fontSize: ".74em" }}>
                {spikeSkip ? `not scanned — ${spikeSkip}` : "no alerts"}
              </p>
            )}
          </div>
        </section>
      </div>

      <div style={{
        border: "1px solid var(--bd)", borderRadius: 8, padding: "9px 12px", marginTop: 12,
        fontSize: ".73em", color: "var(--txd)", background: "var(--c2)",
      }}>
        Left pane is recorded fact; right pane is a live feed that can go dark. The
        scanner FINDS setups — its honest record is PF {fmt(perf.profit_factor)} over{" "}
        {perf.n_clean ?? "—"} clean trades, so treat output as candidates, not edge.
        PAPER_TRADE=True; execution is manual.
      </div>
    </main>
  )
}
