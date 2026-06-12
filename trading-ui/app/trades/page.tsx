"use client"

import { useEffect, useState } from "react"

import { fetchTrades } from "@/lib/api"
import type { Trade, Stats } from "@/lib/types"

const EMPTY_STATS: Stats = {
  total: 0,
  wins: 0,
  losses: 0,
  expired: 0,
  win_rate: 0,
  total_pnl: 0,
  avg_pnl: 0,
  best_trade: 0,
  worst_trade: 0,
}

const REFRESH_MS = 15_000

function pnlColor(pnl: number): string {
  return pnl >= 0 ? "#00c896" : "#ff3d5e"
}

function statusColor(status: string): string {
  if (status === "WIN") return "#00c896"
  if (status === "LOSS") return "#ff3d5e"
  if (status === "EXPIRED") return "#f59e0b"
  return "#6b84a0"
}

function fmt(n: number | null | undefined, dec = 0) {
  if (n == null) return "-"
  return n.toLocaleString("en-IN", {
    minimumFractionDigits: dec,
    maximumFractionDigits: dec,
  })
}

function sortTrades(trades: Trade[]) {
  return [...trades].sort((a, b) => {
    const aTime = Date.parse(a.timestamp ?? "")
    const bTime = Date.parse(b.timestamp ?? "")
    if (!Number.isNaN(aTime) && !Number.isNaN(bTime) && aTime !== bTime) {
      return bTime - aTime
    }
    return (b.timestamp ?? "").localeCompare(a.timestamp ?? "")
  })
}

function formatFetchError(error: unknown) {
  if (error instanceof Error && error.message) {
    return error.message
  }
  return "trade feed still starting"
}

const TH: React.CSSProperties = {
  background: "var(--c2)",
  color: "var(--txd)",
  fontSize: ".66em",
  fontWeight: 700,
  textTransform: "uppercase",
  letterSpacing: ".08em",
  padding: "8px 10px",
  borderBottom: "1px solid var(--bdh)",
  whiteSpace: "nowrap",
  textAlign: "left",
}

const TD: React.CSSProperties = {
  padding: "6px 10px",
  borderBottom: "1px solid rgba(26,45,71,.6)",
  color: "var(--tx)",
  fontSize: ".8em",
  fontFamily: "'JetBrains Mono', monospace",
  whiteSpace: "nowrap",
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
        if (active) {
          setLoading(false)
        }
      }
    }

    void loadTrades()
    const timer = window.setInterval(() => {
      void loadTrades()
    }, REFRESH_MS)

    return () => {
      active = false
      window.clearInterval(timer)
    }
  }, [])

  const sorted = sortTrades(trades)
  const emptyMessage = loading
    ? "Loading trades..."
    : error
      ? "Waiting for trade feed..."
      : "No trades recorded in the last 30 days"

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 20 }}>
      <div className="secHdr">
        <div className="secDot" style={{ background: "#38b2f0" }} />
        <div className="secTitle">Paper P&amp;L - Last 30 Days</div>
      </div>

      {(error || lastUpdated) && (
        <div
          style={{
            background: error ? "rgba(245,158,11,.12)" : "rgba(56,178,240,.10)",
            border: `1px solid ${error ? "rgba(245,158,11,.35)" : "rgba(56,178,240,.22)"}`,
            borderRadius: 8,
            color: error ? "#f59e0b" : "var(--txd)",
            fontSize: ".74em",
            padding: "10px 12px",
          }}
        >
          {error
            ? sorted.length
              ? `Refresh issue: ${error}. Showing last loaded trades and retrying every 15s.`
              : `Trade feed unavailable: ${error}. Retrying every 15s.`
            : `Updated ${lastUpdated}. Auto-refresh every 15s.`}
        </div>
      )}

      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(160px, 1fr))", gap: 10 }}>
        {[
          { label: "TOTAL TRADES", value: String(stats.total), color: "var(--tx)" },
          {
            label: "WIN RATE",
            value: `${stats.win_rate}%`,
            color: stats.win_rate >= 50 ? "#00c896" : "#ff3d5e",
            sub: `${stats.wins}W / ${stats.losses}L / ${stats.expired}E`,
          },
          { label: "TOTAL P&L", value: `\u20B9${fmt(stats.total_pnl)}`, color: pnlColor(stats.total_pnl) },
          { label: "AVG P&L", value: `\u20B9${fmt(stats.avg_pnl)}`, color: pnlColor(stats.avg_pnl) },
          { label: "BEST TRADE", value: `\u20B9${fmt(stats.best_trade)}`, color: "#00c896" },
          { label: "WORST TRADE", value: `\u20B9${fmt(stats.worst_trade)}`, color: "#ff3d5e" },
        ].map(({ label, value, color, sub }) => (
          <div
            key={label}
            style={{
              background: "var(--c1)",
              border: "1px solid var(--bd)",
              borderRadius: 8,
              padding: "10px 14px",
            }}
          >
            <div
              style={{
                fontSize: ".58em",
                fontWeight: 600,
                textTransform: "uppercase",
                letterSpacing: ".1em",
                color: "var(--txs)",
                marginBottom: 3,
              }}
            >
              {label}
            </div>
            <div
              style={{
                fontSize: "1.35em",
                fontWeight: 700,
                color,
                fontFamily: "'JetBrains Mono', monospace",
              }}
            >
              {value}
            </div>
            {sub && (
              <div style={{ fontSize: ".65em", color: "var(--txd)", marginTop: 2 }}>{sub}</div>
            )}
          </div>
        ))}
      </div>

      <div style={{ overflow: "auto", border: "1px solid var(--bd)", borderRadius: 8, background: "var(--c1)" }}>
        <table
          style={{
            width: "100%",
            borderCollapse: "collapse",
            fontFamily: "'JetBrains Mono', monospace",
            fontSize: ".8em",
          }}
        >
          <thead>
            <tr>
              {["DATE", "SYMBOL", "DIR", "STATUS", "ENTRY", "EXIT", "OPTION", "ENTRY PREM", "EXIT PREM", "QTY", "P&L", "P&L%"].map((h) => (
                <th key={h} style={TH}>{h}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {sorted.length === 0 && (
              <tr>
                <td colSpan={12} style={{ ...TD, textAlign: "center", color: "var(--txd)", padding: "24px" }}>
                  {emptyMessage}
                </td>
              </tr>
            )}
            {sorted.map((t) => {
              const pnl = parseFloat(t.pnl ?? "0")
              const pct = parseFloat(t.pnl_percent ?? "0")
              const isLong = t.direction === "LONG"
              const optionStrike = t.option_strike?.trim() || "-"
              const optionType = t.option_type?.trim() || ""

              return (
                <tr key={t.trade_id}>
                  <td style={{ ...TD, color: "var(--txs)" }}>
                    {t.timestamp ? new Date(t.timestamp).toLocaleDateString("en-IN") : "-"}
                  </td>
                  <td style={{ ...TD, fontWeight: 800, color: "var(--tx)" }}>{t.symbol}</td>
                  <td style={{ ...TD, fontWeight: 700, color: isLong ? "#00c896" : "#ff3d5e" }}>
                    {t.direction}
                  </td>
                  <td style={{ ...TD, fontWeight: 700, color: statusColor(t.status) }}>{t.status}</td>
                  <td style={TD}>{t.entry_price}</td>
                  <td style={TD}>{t.exit_price}</td>
                  <td style={{ ...TD, color: "var(--txd)" }}>
                    {optionStrike !== "-" ? `${optionStrike} ${optionType}`.trim() : "-"}
                  </td>
                  <td style={{ ...TD, color: "var(--txd)" }}>
                    {t.entry_premium ? `\u20B9${t.entry_premium}` : "-"}
                  </td>
                  <td style={{ ...TD, color: "var(--txd)" }}>
                    {t.exit_premium ? `\u20B9${t.exit_premium}` : "-"}
                  </td>
                  <td style={{ ...TD, color: "var(--txd)" }}>{t.quantity}</td>
                  <td style={{ ...TD, fontWeight: 700, color: pnlColor(pnl) }}>
                    {pnl >= 0 ? "+" : ""}\u20B9{pnl.toLocaleString("en-IN")}
                  </td>
                  <td style={{ ...TD, color: pnlColor(pct) }}>
                    {pct >= 0 ? "+" : ""}{pct.toFixed(1)}%
                  </td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>
    </div>
  )
}
