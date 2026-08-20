"use client"

// P&L tab.
//
// Two bases, side by side, never blended:
//   CASH   premium rupees at the resolved contract size — what buying these
//          options actually cost.
//   SKILL  spot %, from core.honest_performance — what the SIGNAL was worth,
//          with theta and IV taken out. This is the same computation the
//          Verdict tab renders, so the two tabs cannot disagree.
//
// A long-option book pays theta whatever the signal does, so the cash column
// is negative almost by construction. Showing only cash makes every strategy
// look identical; showing only skill hides the bill. Both, labelled.

import { useEffect, useState } from "react"

import { fetchTrades } from "@/lib/api"
import type { Trade, Stats } from "@/lib/types"

const EMPTY_STATS: Stats = {
  total: 0, qualified: 0, skipped_incomplete: 0, unresolved_lot: 0,
  wins: 0, losses: 0, expired: 0, win_rate: 0,
  total_pnl: 0, avg_pnl: 0, best_trade: 0, worst_trade: 0,
  basis: "",
}

const REFRESH_MS = 15_000
const GREEN = "var(--g)", RED = "var(--r)"

function pnlColor(pnl: number | null | undefined): string {
  if (pnl == null) return "var(--txd)"
  return pnl >= 0 ? GREEN : RED
}

function statusColor(status: string): string {
  if (status === "WIN") return GREEN
  if (status === "LOSS") return RED
  if (status === "EXPIRED") return "var(--y)"
  return "#6b84a0"
}

/** Rupees, always with a real currency symbol and an explicit sign. */
function rupees(n: number | null | undefined, dec = 0): string {
  if (n == null) return "—"
  const sign = n < 0 ? "-" : ""
  return `${sign}₹${Math.abs(n).toLocaleString("en-IN", {
    minimumFractionDigits: dec, maximumFractionDigits: dec,
  })}`
}

function pct(n: number | null | undefined, dec = 2): string {
  if (n == null) return "—"
  return `${n >= 0 ? "+" : ""}${n.toFixed(dec)}%`
}

function num(n: number | null | undefined, dec = 2): string {
  if (n == null) return "—"
  return n.toLocaleString("en-IN", { minimumFractionDigits: dec, maximumFractionDigits: dec })
}

function sortTrades(trades: Trade[]) {
  return [...trades].sort((a, b) => {
    const aT = Date.parse(a.timestamp ?? ""), bT = Date.parse(b.timestamp ?? "")
    if (!Number.isNaN(aT) && !Number.isNaN(bT) && aT !== bT) return bT - aT
    return (b.timestamp ?? "").localeCompare(a.timestamp ?? "")
  })
}

function formatFetchError(error: unknown) {
  if (error instanceof Error && error.message) return error.message
  return "trade feed still starting"
}

const TH: React.CSSProperties = {
  background: "var(--c2)", color: "var(--txd)",
  fontSize: ".66em", fontWeight: 700, textTransform: "uppercase",
  letterSpacing: ".08em", padding: "8px 10px",
  borderBottom: "1px solid var(--bdh)", whiteSpace: "nowrap", textAlign: "left",
}

const TD: React.CSSProperties = {
  padding: "6px 10px",
  borderBottom: "1px solid color-mix(in srgb, var(--bd) 60%, transparent)",
  color: "var(--tx)", fontSize: ".8em",
  fontFamily: "'JetBrains Mono', monospace", whiteSpace: "nowrap",
}

const CARD: React.CSSProperties = {
  background: "var(--c1)", border: "1px solid var(--bd)",
  borderRadius: 8, padding: "10px 14px",
}

function Tile({ label, value, color, sub }: {
  label: string; value: string; color?: string; sub?: string
}) {
  return (
    <div style={CARD}>
      <div style={{
        fontSize: ".58em", fontWeight: 600, textTransform: "uppercase",
        letterSpacing: ".1em", color: "var(--txs)", marginBottom: 3,
      }}>{label}</div>
      <div style={{
        fontSize: "1.35em", fontWeight: 700, color: color ?? "var(--tx)",
        fontFamily: "'JetBrains Mono', monospace",
      }}>{value}</div>
      {sub && <div style={{ fontSize: ".65em", color: "var(--txd)", marginTop: 2 }}>{sub}</div>}
    </div>
  )
}

function BasisHeading({ title, blurb }: { title: string; blurb: string }) {
  return (
    <div style={{ marginBottom: 8 }}>
      <div style={{
        fontSize: ".68em", fontWeight: 700, textTransform: "uppercase",
        letterSpacing: ".1em", color: "var(--tx)",
      }}>{title}</div>
      <div style={{ fontSize: ".7em", color: "var(--txd)", marginTop: 2 }}>{blurb}</div>
    </div>
  )
}

export default function TradesPage() {
  const [trades, setTrades] = useState<Trade[]>([])
  const [stats, setStats] = useState<Stats>(EMPTY_STATS)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [lastUpdated, setLastUpdated] = useState<string>("")

  useEffect(() => {
    let active = true

    const loadTrades = async () => {
      try {
        const data = await fetchTrades(30) as { trades?: Trade[]; stats?: Stats }
        if (!active) return
        setTrades(data.trades ?? [])
        setStats(data.stats ?? EMPTY_STATS)
        setError(null)
        setLastUpdated(new Date().toLocaleTimeString("en-IN", { hour12: false }))
      } catch (err: unknown) {
        if (!active) return
        setError(formatFetchError(err))
      } finally {
        if (active) setLoading(false)
      }
    }

    void loadTrades()
    const timer = window.setInterval(() => { void loadTrades() }, REFRESH_MS)
    return () => { active = false; window.clearInterval(timer) }
  }, [])

  const sorted = sortTrades(trades)
  const h = stats.honest
  const emptyMessage = loading
    ? "Loading trades…"
    : error ? "Waiting for trade feed…"
    : "No trades recorded in the last 30 days"

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 18 }}>
      <div className="secHdr">
        <div className="secDot" style={{ background: "var(--b)" }} />
        <div className="secTitle">Paper P&amp;L — last 30 days</div>
      </div>

      {(error || lastUpdated) && (
        <div style={{
          background: error ? "rgba(245,158,11,.12)" : "rgba(56,178,240,.10)",
          border: `1px solid ${error ? "rgba(245,158,11,.35)" : "rgba(56,178,240,.22)"}`,
          borderRadius: 8, color: error ? "var(--y)" : "var(--txd)",
          fontSize: ".74em", padding: "10px 12px",
        }}>
          {error
            ? sorted.length
              ? `Refresh issue: ${error}. Showing last loaded trades, retrying every 15s.`
              : `Trade feed unavailable: ${error}. Retrying every 15s.`
            : `Updated ${lastUpdated}. Auto-refresh every 15s. Source: signal journal.`}
        </div>
      )}

      {/* ── SKILL: what the signal was worth ─────────────────────────────── */}
      <section>
        <BasisHeading
          title="Signal skill — spot basis"
          blurb="Theta and IV removed. Identical to the Verdict tab, by construction."
        />
        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(160px, 1fr))", gap: 10 }}>
          <Tile label="Profit factor" value={num(h?.profit_factor)}
                color={h?.profit_factor != null && h.profit_factor >= 1 ? GREEN : RED} />
          <Tile label="Win rate"
                value={h?.win_rate != null ? `${(h.win_rate * 100).toFixed(1)}%` : "—"}
                color={h?.win_rate != null && h.win_rate >= 0.5 ? GREEN : RED} />
          <Tile label="Expectancy / trade" value={pct(h?.expectancy_pct, 3)}
                color={pnlColor(h?.expectancy_pct)} />
          <Tile label="Clean trades" value={String(h?.n_clean ?? "—")}
                sub={h?.n_excluded ? `${h.n_excluded} excluded` : undefined} />
        </div>
        {h?.note && (
          <p style={{ color: "var(--txd)", fontSize: ".72em", marginTop: 8 }}>
            {h.note}{h.trustworthy === false ? "  [METRICS NOT TRUSTWORTHY]" : ""}
          </p>
        )}
      </section>

      {/* ── CASH: what the options actually cost ─────────────────────────── */}
      <section>
        <BasisHeading
          title="Premium cash — what option buying cost"
          blurb="Premium move × resolved contract size. A long-option book pays theta whatever the signal does, so this runs negative by construction."
        />
        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(160px, 1fr))", gap: 10 }}>
          <Tile label="Total trades" value={String(stats.total)}
                sub={stats.unresolved_lot
                  ? `${stats.qualified} priced, ${stats.unresolved_lot} unscaled`
                  : `${stats.qualified} priced`} />
          <Tile label="Option hit rate" value={`${stats.win_rate}%`}
                color={stats.win_rate >= 50 ? GREEN : RED}
                sub={`${stats.wins}W / ${stats.losses}L / ${stats.expired}E`} />
          <Tile label="Total P&L" value={rupees(stats.total_pnl)} color={pnlColor(stats.total_pnl)} />
          <Tile label="Avg P&L" value={rupees(stats.avg_pnl)} color={pnlColor(stats.avg_pnl)} />
          <Tile label="Best trade" value={rupees(stats.best_trade)} color={GREEN} />
          <Tile label="Worst trade" value={rupees(stats.worst_trade)} color={RED} />
        </div>
        {stats.unresolved_lot > 0 && (
          <p style={{ color: "var(--y)", fontSize: ".72em", marginTop: 8 }}>
            {stats.unresolved_lot} trade{stats.unresolved_lot === 1 ? "" : "s"} had no
            resolvable contract size, so {stats.unresolved_lot === 1 ? "it is" : "they are"} listed
            but kept out of the cash total — counting them at one share is what
            understated this book roughly 5×.
          </p>
        )}
      </section>

      <div style={{ overflowX: "auto", border: "1px solid var(--bd)", borderRadius: 8, background: "var(--c1)" }}>
        <table style={{
          width: "100%", borderCollapse: "collapse",
          fontFamily: "'JetBrains Mono', monospace", fontSize: ".8em",
        }}>
          <thead>
            <tr>
              {["DATE", "SYMBOL", "DIR", "OPTION RESULT", "ENTRY", "EXIT", "SPOT %",
                "OPTION", "ENTRY PREM", "EXIT PREM", "QTY", "P&L", "PREM %"].map(hd => (
                <th key={hd} style={TH}>{hd}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {sorted.length === 0 && (
              <tr>
                <td colSpan={13} style={{ ...TD, textAlign: "center", color: "var(--txd)", padding: 24 }}>
                  {emptyMessage}
                </td>
              </tr>
            )}
            {sorted.map(t => {
              const isLong = t.direction === "LONG"
              const strike = t.option_strike
              return (
                <tr key={t.trade_id}>
                  <td style={{ ...TD, color: "var(--txs)" }}>
                    {t.timestamp ? new Date(t.timestamp).toLocaleDateString("en-IN") : "—"}
                  </td>
                  <td style={{ ...TD, fontWeight: 800 }}>{t.symbol}</td>
                  <td style={{ ...TD, fontWeight: 700, color: isLong ? GREEN : RED }}>
                    {t.direction}
                  </td>
                  <td style={{ ...TD, fontWeight: 700, color: statusColor(t.status) }}>
                    {t.status}
                  </td>
                  <td style={TD}>{num(t.entry_price)}</td>
                  <td style={TD}>{num(t.exit_price)}</td>
                  <td style={{ ...TD, color: pnlColor(t.spot_pct) }}>{pct(t.spot_pct)}</td>
                  <td style={{ ...TD, color: "var(--txd)" }}>
                    {strike != null ? `${strike} ${t.option_type ?? ""}`.trim() : "—"}
                  </td>
                  <td style={{ ...TD, color: "var(--txd)" }}>
                    {t.entry_premium != null ? `₹${num(t.entry_premium)}` : "—"}
                  </td>
                  <td style={{ ...TD, color: "var(--txd)" }}>
                    {t.exit_premium != null ? `₹${num(t.exit_premium)}` : "—"}
                  </td>
                  <td style={{ ...TD, color: t.lot_resolved ? "var(--tx)" : "var(--y)" }}
                      title={t.lot_resolved ? undefined : "contract size unknown — excluded from cash total"}>
                    {t.lot_resolved ? t.quantity : "?"}
                  </td>
                  <td style={{ ...TD, fontWeight: 700, color: pnlColor(t.pnl) }}>
                    {t.pnl == null ? "—" : `${t.pnl >= 0 ? "+" : ""}${rupees(t.pnl, 2)}`}
                  </td>
                  <td style={{ ...TD, color: pnlColor(t.pnl_percent) }}>{pct(t.pnl_percent, 1)}</td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>
    </div>
  )
}
