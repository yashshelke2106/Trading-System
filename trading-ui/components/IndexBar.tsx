"use client"
import { useEffect, useState } from "react"
import { fetchIndices } from "@/lib/api"
import type { IndexQuote } from "@/lib/types"

const LABELS: Record<string, string> = {
  NIFTY50:   "Nifty 50",
  BANKNIFTY: "Bank Nifty",
  INDIAVIX:  "India VIX",
}

interface Quotes { [key: string]: IndexQuote }

export default function IndexBar() {
  const [quotes, setQuotes] = useState<Quotes>({})
  const [updatedAt, setUpdatedAt] = useState<string>("")

  const load = async () => {
    try {
      const data = await fetchIndices()
      if (data.quotes) {
        setQuotes(data.quotes)
        setUpdatedAt(new Date().toLocaleTimeString("en-IN", { hour12: false }))
      }
    } catch {}
  }

  useEffect(() => {
    load()
    const t = setInterval(load, 30_000)
    return () => clearInterval(t)
  }, [])

  const entries = Object.entries(LABELS).filter(([k]) => quotes[k]?.ltp > 0)
  if (!entries.length) return null

  return (
    <div className="ixBar">
      {entries.map(([key, label]) => {
        const q   = quotes[key]
        const isVix = key === "INDIAVIX"
        const up    = q.chg >= 0
        const clr   = isVix ? (up ? "#f59e0b" : "#00c896") : (up ? "#00c896" : "#ff3d5e")
        const arrow = up ? "▲" : "▼"
        return (
          <div key={key} className="ixItem">
            <div className="ixLbl">{label}</div>
            <div>
              <span className="ixVal" style={{ color: clr }}>
                {q.ltp.toLocaleString("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}
              </span>
              <span className="ixChg" style={{ color: clr }}>
                {arrow} {Math.abs(q.chg).toLocaleString("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}
                {" "}({Math.abs(q.pct).toFixed(2)}%)
              </span>
            </div>
          </div>
        )
      })}
      {updatedAt && <div className="ixTs">Updated {updatedAt}</div>}
    </div>
  )
}
