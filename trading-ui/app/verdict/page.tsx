"use client"
// Verdict page — the "is the strategy good or does it need an upgrade" answer.
//
// This page RENDERS verdicts computed server-side (/api/verdict); it does not
// soften them. Design rule inherited from the repo's history: every dead feed
// and rejected hunt is shown, never hidden behind a spinner or an empty state.
// Swing detail lives in /swing, allocation in /allocation — this page is the
// summary that says which of those are worth opening.
import { useEffect, useState, useCallback } from "react"
import {
  fetchVerdict, fetchCapture, fetchMarketState, fetchLearningRules,
} from "@/lib/api"

function fmt(n: number | null | undefined, dec = 2) {
  if (n == null || Number.isNaN(n)) return "—"
  return n.toLocaleString("en-IN", { minimumFractionDigits: dec, maximumFractionDigits: dec })
}

const TH: React.CSSProperties = {
  background: "var(--c2)", color: "var(--txd)",
  fontSize: ".76em", fontWeight: 700, textTransform: "uppercase",
  letterSpacing: ".08em", padding: "8px 10px",
  borderBottom: "1px solid var(--bdh)", whiteSpace: "nowrap", textAlign: "left",
}
const TD: React.CSSProperties = {
  padding: "6px 10px", borderBottom: "1px solid color-mix(in srgb, var(--bd) 60%, transparent)",
  fontFamily: "'JetBrains Mono', monospace", fontSize: ".8em",
  color: "var(--tx)",
}
const CARD: React.CSSProperties = {
  background: "var(--c1)", border: "1px solid var(--bd)",
  borderRadius: 8, padding: "12px 14px", marginBottom: 14, overflowX: "auto",
}
const H2: React.CSSProperties = {
  fontSize: ".82em", textTransform: "uppercase", letterSpacing: ".1em",
  color: "var(--txd)", marginBottom: 10,
}

// Semantic tokens, not hex: globals.css re-points these per theme, so the
// same "REJECT is red" logic reads correctly on light and dark alike.
const GREEN = "var(--ok)", RED = "var(--bad)", AMBER = "var(--warn)"

function verdictColor(v: string) {
  const s = (v || "").toUpperCase()
  if (s.includes("ACTIVE") || s.includes("PASS")) return GREEN
  if (s.includes("REJECT") || s.includes("CLOSED") || s.includes("ABANDON")) return RED
  return AMBER
}

function Chip({ v }: { v: string }) {
  const c = verdictColor(v)
  return (
    <span style={{
      display: "inline-block", padding: "1px 8px", borderRadius: 10,
      fontSize: ".82em", fontFamily: "'JetBrains Mono', monospace",
      color: c, border: `1px solid ${c}`, whiteSpace: "nowrap",
    }}>{v || "open"}</span>
  )
}

function Stat({ label, value, color }: { label: string; value: string; color?: string }) {
  return (
    <div style={{ minWidth: 110 }}>
      <div style={{
        fontSize: "1.5em", fontFamily: "'JetBrains Mono', monospace",
        color: color ?? "var(--tx)",
      }}>{value}</div>
      <div style={{ fontSize: ".72em", textTransform: "uppercase", letterSpacing: ".08em", color: "var(--txd)" }}>
        {label}
      </div>
    </div>
  )
}

interface Perf {
  profit_factor?: number; win_rate?: number; expectancy_pct?: number
  n_clean?: number; trustworthy?: boolean; note?: string; error?: string
}
interface Lane { lane: string; verdict: string; evidence: string; action: string }
interface Hypothesis { thesis?: string; verdict?: string; error?: string }
interface ClaimAudit {
  unverified: number | null
  files?: { path: string; hits: { line: number; text: string }[]; n: number }[]
  error?: string
}
interface VerdictData {
  journal_perf?: Perf
  hypotheses?: Hypothesis[]
  swing_health?: Record<string, unknown> | null
  swing_decision?: { status: string; date: string; detail: string }
  dhan?: { expired: boolean; since: string; consequence: string }
  lanes?: Lane[]
  claim_audit?: ClaimAudit
  bottom_line?: string
  error?: string
}
interface CaptureData {
  eod_archive?: { trading_days?: number; symbols?: number; first?: string; last?: string }
  intraday?: { symbols?: number; worst_stale_days?: number; limit_days?: number; unrecoverable?: boolean }
  error?: string
}
interface MarketStateData {
  universe?: string
  tally?: Record<string, number>
  limitation?: string
  error?: string
}
interface GuardRule {
  feature: string; value: string
  holdout_lossrate?: number; baseline?: number
}
interface BoostRule {
  feature: string; value: string; boost: number
  holdout_winrate?: number; baseline?: number
}
interface Recurrence {
  kind: string; key: string; post_n: number
  post_rate?: number | null; verdict: string; detail?: string
}
interface LearningRules {
  guards?: GuardRule[]
  boosts?: BoostRule[]
  recurrence?: Recurrence[]
  retired?: Record<string, unknown>[]
  ledger?: {
    mistake?: { promote?: number; reject?: number }
    keeper?: { promote?: number; reject?: number }
  }
  error?: string
}

export default function VerdictPage() {
  const [v, setV] = useState<VerdictData | null>(null)
  const [cap, setCap] = useState<CaptureData | null>(null)
  const [ms, setMs] = useState<MarketStateData | null>(null)
  const [rules, setRules] = useState<LearningRules | null>(null)
  const [err, setErr] = useState<string | null>(null)

  const load = useCallback(async () => {
    try {
      const [vd, cd, md, rd] = await Promise.allSettled([
        fetchVerdict(), fetchCapture(), fetchMarketState(),
        fetchLearningRules(),
      ])
      if (vd.status === "fulfilled") setV(vd.value); else setErr(String(vd.reason))
      if (cd.status === "fulfilled") setCap(cd.value)
      if (md.status === "fulfilled") setMs(md.value)
      if (rd.status === "fulfilled") setRules(rd.value)
    } catch (e) {
      setErr(String(e))
    }
  }, [])

  useEffect(() => {
    load()
    const t = setInterval(load, 120_000)
    return () => clearInterval(t)
  }, [load])

  if (err && !v) return <main style={{ padding: 16 }}><p style={{ color: RED }}>{err}</p></main>
  if (!v) return <main style={{ padding: 16 }}><p style={{ color: "var(--txd)" }}>loading verdict…</p></main>

  const p = v.journal_perf ?? {}
  const pf = p.profit_factor
  const ic = cap?.intraday ?? {}
  const stale = ic.worst_stale_days
  const tally = ms?.tally ?? {}

  return (
    <main style={{ padding: 16, maxWidth: 1100, margin: "0 auto" }}>

      {v.dhan?.expired && (
        <div style={{
          border: `1px solid ${RED}`, color: RED,
          background: "color-mix(in srgb, var(--bad) 10%, transparent)",
          padding: "8px 12px", borderRadius: 6, fontSize: ".85em", marginBottom: 14,
        }}>
          DHAN DATA API EXPIRED ({v.dhan.since}) — {v.dhan.consequence}
        </div>
      )}

      <section style={CARD}>
        <div style={H2}>Verdict — is it good, or does it need an upgrade?</div>
        <div style={{ display: "flex", gap: 24, flexWrap: "wrap", marginBottom: 12 }}>
          <Stat label="profit factor" value={fmt(pf)} color={pf != null && pf >= 1 ? GREEN : RED} />
          <Stat label="win rate" value={p.win_rate != null ? fmt(p.win_rate * 100, 1) + "%" : "—"}
                color={p.win_rate != null && p.win_rate >= 0.5 ? GREEN : RED} />
          <Stat label="expectancy / trade" value={fmt(p.expectancy_pct) + "%"}
                color={p.expectancy_pct != null && p.expectancy_pct > 0 ? GREEN : RED} />
          <Stat label="clean trades" value={String(p.n_clean ?? "—")} />
        </div>
        <table style={{ width: "100%", borderCollapse: "collapse" }}>
          <thead><tr>
            <th style={TH}>Lane</th><th style={TH}>Verdict</th>
            <th style={TH}>Evidence</th><th style={TH}>Action</th>
          </tr></thead>
          <tbody>
            {(v.lanes ?? []).map(l => (
              <tr key={l.lane}>
                <td style={TD}>{l.lane}</td>
                <td style={TD}><Chip v={l.verdict} /></td>
                <td style={{ ...TD, whiteSpace: "normal" }}>{l.evidence}</td>
                <td style={{ ...TD, whiteSpace: "normal" }}>{l.action}</td>
              </tr>
            ))}
          </tbody>
        </table>
        <p style={{ color: "var(--txd)", fontSize: ".78em", marginTop: 10 }}>
          {p.note}{p.trustworthy === false ? "  [METRICS NOT TRUSTWORTHY]" : ""}
        </p>
      </section>

      <section style={CARD}>
        <div style={H2}>Hypothesis registry — every hunt, every outcome</div>
        <table style={{ width: "100%", borderCollapse: "collapse" }}>
          <thead><tr><th style={TH}>Verdict</th><th style={TH}>Thesis</th></tr></thead>
          <tbody>
            {(v.hypotheses ?? []).map((h, i) => (
              <tr key={i}>
                <td style={TD}><Chip v={h.verdict ?? "open"} /></td>
                <td style={{ ...TD, whiteSpace: "normal" }}>{h.thesis ?? h.error}</td>
              </tr>
            ))}
          </tbody>
        </table>
        <p style={{ color: "var(--txd)", fontSize: ".78em", marginTop: 10 }}>
          New ideas enter here BEFORE code. Re-proposing a rejected hunt with new
          parameters is the failure mode this table exists to stop.
        </p>
      </section>

      <section style={CARD}>
        <div style={H2}>Unverified claims — code that asserts an edge nothing checked</div>
        {v.claim_audit?.error ? (
          <p style={{ color: AMBER, fontSize: ".8em" }}>audit unavailable: {v.claim_audit.error}</p>
        ) : (v.claim_audit?.unverified ?? 0) === 0 ? (
          <p style={{ color: GREEN, fontSize: ".8em" }}>
            Every edge claim in the tree points at a closed hypothesis.
          </p>
        ) : (
          <>
            <table style={{ width: "100%", borderCollapse: "collapse" }}>
              <thead><tr>
                <th style={TH}>File</th><th style={TH}>Claim</th>
              </tr></thead>
              <tbody>
                {(v.claim_audit?.files ?? []).map(f => (
                  <tr key={f.path}>
                    <td style={{ ...TD, whiteSpace: "nowrap", color: AMBER }}>
                      {f.path}{f.n > f.hits.length ? ` (+${f.n - f.hits.length})` : ""}
                    </td>
                    <td style={{ ...TD, whiteSpace: "normal", fontSize: ".72em" }}>
                      {f.hits.map(h => (
                        <div key={h.line} style={{ color: "var(--txd)" }}>
                          L{h.line}: {h.text}
                        </div>
                      ))}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
            <p style={{ color: "var(--txd)", fontSize: ".78em", marginTop: 10 }}>
              A finding is a prompt, not a verdict: run the test, or delete the
              sentence. <code>pairs_program.py</code> called itself &ldquo;the one lead
              that survived the hunt&rdquo; for weeks while its own re-test sat
              unrun — the machinery to catch it all existed, nothing made it fire.
            </p>
          </>
        )}
      </section>

      <section style={CARD}>
        <div style={H2}>What the system taught itself — and whether it held</div>
        {(!rules || (!rules.guards?.length && !rules.boosts?.length)) ? (
          <p style={{ color: "var(--txd)", fontSize: ".8em" }}>
            No active rules yet. The learners promote one only after a temporal
            holdout, a recurrence check and Bonferroni correction.
          </p>
        ) : (
          <table style={{ width: "100%", borderCollapse: "collapse" }}>
            <thead><tr>
              <th style={TH}>Kind</th><th style={TH}>Rule</th>
              <th style={TH}>Effect at promotion</th>
              <th style={TH}>Post-deploy</th><th style={TH}>Verdict</th>
            </tr></thead>
            <tbody>
              {(rules.guards ?? []).map(g => {
                const key = `${g.feature}=${g.value}`
                const rec = (rules.recurrence ?? []).find(r => r.key === key && r.kind === "guard")
                return (
                  <tr key={`g-${key}`}>
                    <td style={{ ...TD, color: RED }}>avoid</td>
                    <td style={TD}>{key}</td>
                    <td style={TD}>loss {fmt((g.holdout_lossrate ?? 0) * 100, 0)}% vs base {fmt((g.baseline ?? 0) * 100, 0)}%</td>
                    <td style={TD}>{rec ? `n=${rec.post_n}` : "—"}</td>
                    <td style={TD}><Chip v={rec?.verdict ?? "PENDING"} /></td>
                  </tr>
                )
              })}
              {(rules.boosts ?? []).map(b => {
                const key = `${b.feature}=${b.value}`
                const rec = (rules.recurrence ?? []).find(r => r.key === key && r.kind === "keeper")
                return (
                  <tr key={`b-${key}`}>
                    <td style={{ ...TD, color: GREEN }}>prefer</td>
                    <td style={TD}>{key} <span style={{ color: "var(--txd)" }}>×{b.boost}</span></td>
                    <td style={TD}>win {fmt((b.holdout_winrate ?? 0) * 100, 0)}% vs base {fmt((b.baseline ?? 0) * 100, 0)}%</td>
                    <td style={TD}>{rec ? `n=${rec.post_n}` : "—"}</td>
                    <td style={TD}><Chip v={rec?.verdict ?? "PENDING"} /></td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        )}
        <p style={{ color: "var(--txd)", fontSize: ".76em", marginTop: 10 }}>
          <b style={{ color: AMBER }}>UNOBSERVABLE</b> is not success: a guard that
          stopped its own trades produced no evidence — obeyed, not proven.{" "}
          <b>UNPROVEN</b> = too few post-deploy trades yet. <b>REVERTED</b> rules are
          auto-retired and never re-proposed.
        </p>
        {rules?.ledger && (
          <p style={{ color: "var(--txd)", fontSize: ".76em", marginTop: 6 }}>
            mined vs kept — avoid: {rules.ledger.mistake?.promote ?? 0} promoted /
            {" "}{rules.ledger.mistake?.reject ?? 0} rejected · prefer:{" "}
            {rules.ledger.keeper?.promote ?? 0} promoted /{" "}
            {rules.ledger.keeper?.reject ?? 0} rejected
            {rules.retired?.length ? ` · ${rules.retired.length} retired after deployment` : ""}
          </p>
        )}
      </section>

      <section style={CARD}>
        <div style={H2}>Swing — standing decision</div>
        <p style={{ fontSize: ".85em" }}>
          <Chip v={v.swing_decision?.status ?? "?"} />{" "}
          <span style={{ color: "var(--txd)" }}>
            {v.swing_decision?.date} — {v.swing_decision?.detail}
          </span>
        </p>
        <p style={{ color: "var(--txd)", fontSize: ".78em", marginTop: 8 }}>
          Paper journal and decay monitor live in the Swing tab; sizing lives in Allocation.
        </p>
      </section>

      <section style={CARD}>
        <div style={H2}>Data reality — what the verdicts stand on</div>
        <div style={{ display: "flex", gap: 24, flexWrap: "wrap", marginBottom: 8 }}>
          <Stat label="EOD archive days" value={String(cap?.eod_archive?.trading_days ?? "—")} />
          <Stat label="EOD symbols (incl. delisted)" value={String(cap?.eod_archive?.symbols ?? "—")} />
          <Stat label="intraday symbols" value={String(ic.symbols ?? "—")} />
          <Stat label="worst stale (days)" value={String(stale ?? "—")}
                color={stale != null ? (stale > 45 ? RED : stale > 20 ? AMBER : GREEN) : undefined} />
        </div>
        {ic.unrecoverable && (
          <p style={{ color: RED, fontSize: ".8em" }}>
            INTRADAY DATA BEING LOST — capture scheduler is not keeping up (60-day window).
          </p>
        )}
        {/* A failed fetch used to render "up 0 · down 0 · sideways 0", which
            reads as a perfectly flat market rather than as no answer. This
            endpoint exceeds its 12s deadline under Dhan throttling, so the
            distinction is not hypothetical. */}
        {(!ms || ms.error || !Object.keys(tally).length) ? (
          <p style={{ color: AMBER, fontSize: ".8em" }}>
            tape unavailable — {ms?.error ? `market-state failed: ${ms.error}` :
            "no response from /api/market-state"}. Not the same as a flat tape.
          </p>
        ) : (
          <p style={{ color: "var(--txd)", fontSize: ".8em" }}>
            tape ({ms.universe ?? "top100"}): up {tally.up ?? 0} · down {tally.down ?? 0} ·
            sideways {tally.sideways ?? 0} — labels describe the past, not the future.
          </p>
        )}
      </section>

      <div style={{
        border: "1px solid var(--bd)", borderRadius: 8, padding: "10px 14px",
        fontSize: ".85em", background: "var(--c2)",
      }}>
        {v.bottom_line}
      </div>
    </main>
  )
}
