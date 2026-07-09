"use client"
import { useEffect, useState } from "react"
import { fetchVersion } from "@/lib/api"

interface Ver { commit: string; subject: string; commit_date: string; served: string }

export default function VersionStamp() {
  const [v, setV] = useState<Ver | null>(null)
  const [now, setNow] = useState<string>("")

  useEffect(() => {
    const load = () => fetchVersion().then(setV).catch(() => {})
    load()
    const id = setInterval(() => { load(); setNow(new Date().toLocaleTimeString("en-IN")) }, 30000)
    setNow(new Date().toLocaleTimeString("en-IN"))
    return () => clearInterval(id)
  }, [])

  if (!v) return null
  return (
    <div style={{
      fontSize: ".62em", color: "var(--txs)", fontFamily: "'JetBrains Mono', monospace",
      padding: "10px 0 4px", borderTop: "1px solid var(--bd)", marginTop: 20,
      display: "flex", gap: 12, flexWrap: "wrap", alignItems: "center",
    }}>
      <span title="Git commit the running code is built from">
        build <b style={{ color: "var(--txd)" }}>{v.commit}</b> · {v.commit_date}
      </span>
      <span style={{ color: "var(--txd)" }}>{v.subject}</span>
      <span style={{ marginLeft: "auto" }}>data auto-refreshes · UI clock {now}</span>
    </div>
  )
}
