"use client"
// Live page — the Dhan-powered half of the dashboard.
//
// Two-part design (user decision 2026-07-24): the OFFLINE tabs (Verdict,
// Swing, Allocation, ...) run with no API at all; THIS tab is the part that
// accepts Dhan credentials and, when the subscription is actually alive,
// surfaces the live scanner's trades (breakout / news-action / pattern
// signals from scan_only_v2.py via logs/signals.json).
//
// Honesty rules, inherited from the day this page was built:
//  - liveness comes from /api/dhan-live-status (a REAL authenticated probe),
//    never from "a token file exists" — that lie is how an expired sub
//    showed "Active" for weeks;
//  - stale scanner output is shown WITH its age, never as if fresh;
//  - the verdict banner stays visible: the signal lane's honest PF is 0.44,
//    execution is manual, PAPER_TRADE=True. This page finds trades; it does
//    not claim they are profitable.
import { useEffect, useState, useCallback } from "react"
import {
  fetchStatus, fetchSignalsAll, fetchSpikeAlerts,
  postToken, postClientId, postDataKey,
} from "@/lib/api"

const BASE = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000"

function fmt(n: number | null | undefined, dec = 2) {
  if (n == null || Number.isNaN(n)) return "—"
  return n.toLocaleString("en-IN", { minimumFractionDigits: dec, maximumFractionDigits: dec })
}

const TH: React.CSSProperties = {
  background: "var(--c2)", color: "var(--txd)",
  fontSize: ".66em", fontWeight: 700, textTransform: "uppercase",
  letterSpacing: ".08em", padding: "8px 10px",
  borderBottom: "1px solid var(--bdh)", whiteSpace: "nowrap", textAlign: "left",
}
const TD: React.CSSProperties = {
  padding: "6px 10px", borderBottom: "1px solid color-mix(in srgb, var(--bd) 60%, transparent)",
  fontFamily: "'JetBrains Mono', monospace", fontSize: ".8em",
  color: "var(--tx)", whiteSpace: "nowrap",
}
const CARD: React.CSSProperties = {
  background: "var(--c1)", border: "1px solid var(--bd)",
  borderRadius: 8, padding: "12px 14px", marginBottom: 14, overflowX: "auto",
}
const H2: React.CSSProperties = {
  fontSize: ".72em", textTransform: "uppercase", letterSpacing: ".1em",
  color: "var(--txd)", marginBottom: 10,
}
const INPUT: React.CSSProperties = {
  background: "var(--c2)", border: "1px solid var(--bd)", borderRadius: 4,
  color: "var(--tx)", padding: "6px 8px", fontFamily: "'JetBrains Mono', monospace",
  fontSize: ".78em", width: "100%",
}
const BTN: React.CSSProperties = {
  background: "var(--c2)", border: "1px solid var(--bdh)", borderRadius: 4,
  color: "var(--tx)", padding: "6px 12px", fontSize: ".75em", cursor: "pointer",
  whiteSpace: "nowrap",
}

// Semantic tokens, not hex: globals.css re-points these per theme, so the
// same "REJECT is red" logic reads correctly on light and dark alike.
const GREEN = "var(--ok)", RED = "var(--bad)", AMBER = "var(--warn)"

interface LiveStatus {
  // Keys mirror /api/dhan-live-status exactly. `data_api_key` being present
  // while every token is absent is the precise state that made the old
  // header chip claim "DATA API ✓ Active" — a key is not a session.
  configured?: {
    trade_token?: boolean; data_token?: boolean
    data_api_key?: boolean; client_id?: boolean
  }
  working?: boolean
  status?: string
  http?: number
  probed_with?: string | null
  message?: string
  // A transient state (429, timeout, Dhan 5xx) says nothing about your
  // credentials and clears itself. It must not be painted like a 401.
  transient?: boolean
  retry_after?: number
  stale?: boolean
  refreshing?: boolean
  age_sec?: number
}
interface Signal {
  symbol?: string; direction?: string; entry_price?: number; sl_price?: number
  target_price?: number; rr_ratio?: number; confluence_grade?: string
  confluence_score?: number; patterns_combined?: string; reason?: string; ts?: string
}
interface SpikeAlert {
  symbol?: string; direction?: string; confidence?: number; note?: string; ts?: string
}

function ageMinutes(ts?: string | number | null): number | null {
  if (!ts) return null
  const t = typeof ts === "number" ? ts * (ts < 2e10 ? 1000 : 1) : Date.parse(ts)
  if (Number.isNaN(t)) return null
  return Math.round((Date.now() - t) / 60000)
}

export default function LivePage() {
  const [live, setLive] = useState<LiveStatus | null>(null)
  const [sig, setSig] = useState<Record<string, unknown> | null>(null)
  const [spikes, setSpikes] = useState<SpikeAlert[]>([])
  const [spikeSkip, setSpikeSkip] = useState<string | null>(null)
  const [marketLive, setMarketLive] = useState<boolean>(false)
  const [tokenIn, setTokenIn] = useState("")
  const [clientIn, setClientIn] = useState("")
  const [keyIn, setKeyIn] = useState("")
  const [saveMsg, setSaveMsg] = useState<string | null>(null)
  const [probing, setProbing] = useState(false)

  const load = useCallback(async (reprobe = false) => {
    const results = await Promise.allSettled([
      fetch(`${BASE}/api/dhan-live-status${reprobe ? "?refresh=true" : ""}`,
            { cache: "no-store" }).then(r => r.json()),
      fetchSignalsAll(),
      fetchSpikeAlerts(55),
      fetchStatus(),
    ])
    if (results[0].status === "fulfilled") setLive(results[0].value)
    if (results[1].status === "fulfilled") setSig(results[1].value)
    if (results[2].status === "fulfilled") {
      const sp = results[2].value
      setSpikes((sp?.alerts ?? []) as SpikeAlert[])
      // The API short-circuits this scan when Dhan is down; say so rather
      // than rendering an empty table that looks like "no setups today".
      setSpikeSkip(sp?.skipped ? (sp.reason ?? "skipped")
                 : sp?.timeout ? "scan timed out" : null)
    }
    if (results[3].status === "fulfilled") setMarketLive(!!results[3].value?.is_live)
  }, [])

  useEffect(() => {
    load()
    const t = setInterval(() => load(), 30_000)
    return () => clearInterval(t)
  }, [load])

  async function save(kind: "token" | "client" | "key") {
    setSaveMsg(null)
    try {
      // The write is the only part that can fail for a credential reason, so
      // it is the only part we await. Previously the handler also awaited a
      // live probe, which turned a successful save into "TIMEOUT" whenever
      // Dhan was slow or rate-limited -- the credential was stored, but the
      // UI reported failure. The backend now re-probes in the background and
      // we watch for the answer instead of blocking on it.
      if (kind === "token" && tokenIn.trim()) await postToken(tokenIn.trim())
      if (kind === "client" && clientIn.trim()) await postClientId(clientIn.trim())
      if (kind === "key" && keyIn.trim()) await postDataKey(keyIn.trim())
      setSaveMsg("saved — checking connection…")
      setProbing(true)
      // Poll briefly for the background probe to land. Every tick refreshes
      // `configured`, so the (stored)/(missing) labels update immediately even
      // while the connection verdict is still pending.
      for (let i = 0; i < 12; i++) {
        await new Promise(r => setTimeout(r, 2_000))
        const s = await fetch(`${BASE}/api/dhan-live-status`, { cache: "no-store" })
          .then(r => r.json()).catch(() => null)
        if (s) setLive(s)
        if (s && !s.refreshing && s.status !== "timeout") {
          setSaveMsg(s.working ? "saved — connection live"
                               : `saved — ${s.status ?? "checking"}`)
          break
        }
      }
      setProbing(false)
      void load()
    } catch (e) {
      setSaveMsg(`save failed: ${String(e)}`)
    }
  }

  const st = live?.status ?? "…"
  // A transient state is amber, never red: 429 / timeout / Dhan 5xx clear
  // themselves and say nothing about the credentials. Painting them red is
  // what made a passing rate limit look like a broken subscription.
  const stColor = live?.working ? GREEN
    : (live?.transient || st === "auth_ok_shape_issue") ? AMBER : RED
  const byGrade = (sig?.by_grade ?? {}) as Record<string, Signal[]>
  const sigTs = sig?.ts as string | undefined
  const age = ageMinutes(sigTs)
  const scannerStale = age != null && age > 30 && marketLive

  return (
    <main style={{ padding: 16, maxWidth: 1100, margin: "0 auto" }}>

      {/* ── Connection: real probe, not file presence ─────────────────── */}
      <section style={CARD}>
        <div style={H2}>Dhan connection — probed, not assumed</div>
        <div style={{ display: "flex", gap: 18, alignItems: "center", flexWrap: "wrap", marginBottom: 10 }}>
          <span style={{
            fontFamily: "'JetBrains Mono', monospace", fontSize: "1.15em",
            color: stColor, border: `1px solid ${stColor}`, borderRadius: 6,
            padding: "4px 12px",
          }}>
            {probing ? "CHECKING…" : st.toUpperCase().replace(/_/g, " ")}
            {live?.http ? ` (HTTP ${live.http})` : ""}
          </span>
          <span style={{ color: "var(--txd)", fontSize: ".8em" }}>
            {live?.message}
            {live?.retry_after ? ` · auto-retry in ${live.retry_after}s` : ""}
            {live?.stale && !probing
              ? ` · last checked ${live.age_sec ?? "?"}s ago${live.refreshing ? ", refreshing" : ""}`
              : ""}
          </span>
          <button style={BTN} onClick={() => { setProbing(true); load(true).finally(() => setProbing(false)) }}>
            re-probe now
          </button>
        </div>
        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit,minmax(260px,1fr))", gap: 10 }}>
          <div>
            <div style={{ fontSize: ".68em", color: "var(--txd)", marginBottom: 4 }}>
              ACCESS TOKEN {live?.configured?.trade_token ? "(stored)" : "(missing)"}
            </div>
            <div style={{ display: "flex", gap: 6 }}>
              <input style={INPUT} type="password" placeholder="paste JWT (eyJ…)"
                     value={tokenIn} onChange={e => setTokenIn(e.target.value)} />
              <button style={BTN} onClick={() => save("token")}>save</button>
            </div>
          </div>
          <div>
            <div style={{ fontSize: ".68em", color: "var(--txd)", marginBottom: 4 }}>
              CLIENT ID {live?.configured?.client_id ? "(stored)" : "(missing)"}
            </div>
            <div style={{ display: "flex", gap: 6 }}>
              <input style={INPUT} placeholder="client id"
                     value={clientIn} onChange={e => setClientIn(e.target.value)} />
              <button style={BTN} onClick={() => save("client")}>save</button>
            </div>
          </div>
          <div>
            <div style={{ fontSize: ".68em", color: "var(--txd)", marginBottom: 4 }}>
              DATA API KEY {live?.configured?.data_api_key ? "(stored)" : "(missing)"}
            </div>
            <div style={{ display: "flex", gap: 6 }}>
              <input style={INPUT} type="password" placeholder="data api key"
                     value={keyIn} onChange={e => setKeyIn(e.target.value)} />
              <button style={BTN} onClick={() => save("key")}>save</button>
            </div>
          </div>
        </div>
        {saveMsg && <p style={{ color: "var(--txd)", fontSize: ".75em", marginTop: 8 }}>{saveMsg}</p>}
        {!live?.working && (
          <p style={{ color: AMBER, fontSize: ".78em", marginTop: 10 }}>
            Live scanning idle until the probe goes green. Once it does, start the
            scanner (start_trading.bat) — signals appear below as it writes them.
          </p>
        )}
      </section>

      {/* ── Scanner feed: breakout / news-action / pattern signals ─────── */}
      <section style={CARD}>
        <div style={H2}>
          Live scanner trades — breakout · news action · pattern
          {sigTs && (
            <span style={{ marginLeft: 10, color: scannerStale ? AMBER : "var(--txd)" }}>
              last scan {age != null ? `${age} min ago` : sigTs}{scannerStale ? " — STALE (scanner not running?)" : ""}
            </span>
          )}
        </div>
        {(["A", "B"] as const).map(g => (
          <div key={g} style={{ marginBottom: 10 }}>
            <div style={{ fontSize: ".7em", color: "var(--txd)", margin: "6px 0" }}>
              GRADE {g} — {byGrade[g]?.length ?? 0}
            </div>
            <table style={{ width: "100%", borderCollapse: "collapse" }}>
              <thead><tr>
                <th style={TH}>Symbol</th><th style={TH}>Dir</th><th style={TH}>Score</th>
                <th style={TH}>Entry</th><th style={TH}>SL</th><th style={TH}>Target</th>
                <th style={TH}>R:R</th><th style={TH}>Patterns / reason</th>
              </tr></thead>
              <tbody>
                {(byGrade[g] ?? []).slice(0, 15).map((s, i) => (
                  <tr key={`${s.symbol}-${i}`}>
                    <td style={TD}>{s.symbol}</td>
                    <td style={{ ...TD, color: s.direction === "long" ? GREEN : RED }}>{s.direction}</td>
                    <td style={TD}>{fmt(s.confluence_score, 0)}</td>
                    <td style={TD}>{fmt(s.entry_price)}</td>
                    <td style={TD}>{fmt(s.sl_price)}</td>
                    <td style={TD}>{fmt(s.target_price)}</td>
                    <td style={TD}>{fmt(s.rr_ratio, 1)}</td>
                    <td style={{ ...TD, whiteSpace: "normal", maxWidth: 420 }}>
                      {[s.patterns_combined, s.reason].filter(Boolean).join(" — ")}
                    </td>
                  </tr>
                ))}
                {!(byGrade[g] ?? []).length && (
                  <tr><td style={{ ...TD, color: "var(--txd)" }} colSpan={8}>none</td></tr>
                )}
              </tbody>
            </table>
          </div>
        ))}
      </section>

      {/* ── Volume spike alerts ────────────────────────────────────────── */}
      <section style={CARD}>
        <div style={H2}>Volume spike alerts</div>
        <table style={{ width: "100%", borderCollapse: "collapse" }}>
          <thead><tr>
            <th style={TH}>Symbol</th><th style={TH}>Dir</th>
            <th style={TH}>Confidence</th><th style={TH}>Note</th>
          </tr></thead>
          <tbody>
            {spikes.slice(0, 12).map((a, i) => (
              <tr key={i}>
                <td style={TD}>{a.symbol}</td>
                <td style={TD}>{a.direction}</td>
                <td style={TD}>{fmt(a.confidence, 0)}</td>
                <td style={{ ...TD, whiteSpace: "normal" }}>{a.note}</td>
              </tr>
            ))}
            {!spikes.length && (
              <tr><td style={{ ...TD, color: spikeSkip ? AMBER : "var(--txd)" }} colSpan={4}>
                {spikeSkip ? `not scanned — ${spikeSkip}` : "no alerts"}
              </td></tr>
            )}
          </tbody>
        </table>
      </section>

      {/* ── The reminder this page must carry ──────────────────────────── */}
      <div style={{
        border: "1px solid var(--bd)", borderRadius: 8, padding: "10px 14px",
        fontSize: ".78em", color: "var(--txd)", background: "var(--c2)",
      }}>
        Signal lane's honest record: PF 0.44 over 1,012 clean trades (see Verdict tab).
        This page FINDS setups; it does not claim they are profitable. Execution is
        manual in the Dhan app; PAPER_TRADE=True. News feed note: Google News RSS is
        stale (68h+) — "news action" context in reasons comes from the scanner's
        event classifier, not a live newswire.
      </div>
    </main>
  )
}
