"use client"
import { useEffect, useState } from "react"
import { fetchStatus } from "@/lib/api"
import type { MarketStatus } from "@/lib/types"

export default function MarketStatusBar() {
  const [status, setStatus] = useState<MarketStatus | null>(null)

  const load = async () => {
    try {
      const data = await fetchStatus()
      setStatus(data)
    } catch {}
  }

  useEffect(() => {
    load()
    const t = setInterval(load, 5_000)
    return () => clearInterval(t)
  }, [])

  if (!status) return null

  const mktClr: Record<string, string> = {
    LIVE: "#00c896", CLOSED: "#3a4f66", "PRE-MKT": "#f59e0b", WEEKEND: "#3a4f66",
  }
  const clr = mktClr[status.market_status] ?? "#6b84a0"
  const isLive = status.market_status === "LIVE"

  const th = status.trade_token
  const tokClr = th.valid && !th.needs_refresh ? "#00c896" : (th.valid ? "#f59e0b" : "#ff3d5e")
  const tokIcon = th.valid && !th.needs_refresh ? "✓" : (th.valid ? "⚠" : "✗")
  const tokLbl = th.hours_left != null ? `${tokIcon} ${th.hours_left.toFixed(0)}h` : `${tokIcon} —`

  const daClr = status.data_api.valid ? "#00c896" : "#f59e0b"

  return (
    <div className="statBar" style={{ marginBottom: 0, borderRadius: 0, border: "none", borderBottom: "1px solid var(--bd)" }}>
      <div className="statItem">
        <div className="statLbl">Market</div>
        <div className="statVal">
          <span style={{
            display: "inline-block", width: 6, height: 6, borderRadius: "50%",
            background: clr, marginRight: 5, verticalAlign: "middle",
            animation: isLive ? "pulse-dot 2s infinite" : undefined,
          }} />
          <span style={{ color: clr }}>{status.market_status}</span>
        </div>
      </div>
      <div className="statItem">
        <div className="statLbl">IST</div>
        <div className="statVal">{status.ist_time}</div>
      </div>
      <div className="statItem" style={{ minWidth: 140 }}>
        <div className="statLbl">Session</div>
        <div className="statVal" style={{ color: "#6b84a0", fontSize: ".78em" }}>{status.market_detail}</div>
      </div>
      <div className="statItem" style={{ marginLeft: "auto", borderLeft: "1px solid var(--bd)" }}>
        <div className="statLbl">Trade Token</div>
        <div className="statVal" style={{ color: tokClr }}>{tokLbl}</div>
      </div>
      <div className="statItem">
        <div className="statLbl">Data API</div>
        <div className="statVal" style={{ color: daClr }}>
          {status.data_api.valid ? "✓ Active" : "○ Fallback"}
        </div>
      </div>
    </div>
  )
}
