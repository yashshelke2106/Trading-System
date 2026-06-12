"use client"
import { useEffect, useState } from "react"
import { fetchStatus, postToken } from "@/lib/api"

type SaveState = "idle" | "saving" | "ok" | "error"

export default function TokenAlert() {
  const [hoursLeft,  setHoursLeft]  = useState<number | null>(null)
  const [valid,      setValid]      = useState(true)
  const [val,        setVal]        = useState("")
  const [saveState,  setSaveState]  = useState<SaveState>("idle")
  const [dismissed,  setDismissed]  = useState(false)

  const poll = async () => {
    try {
      const s  = await fetchStatus()
      const th = s.trade_token
      setHoursLeft(th.hours_left)
      setValid(th.valid)
      // Re-show if token becomes invalid again after dismiss
      if (!th.valid || (th.hours_left !== null && th.hours_left < 2)) {
        setDismissed(false)
      }
    } catch {}
  }

  useEffect(() => {
    poll()
    const t = setInterval(poll, 60_000)
    return () => clearInterval(t)
  }, [])

  const needsAlert = !valid || (hoursLeft !== null && hoursLeft < 2)
  if (!needsAlert || dismissed) return null

  const isExpired = !valid || (hoursLeft !== null && hoursLeft <= 0)
  const clr = isExpired ? "#ff3d5e" : "#f59e0b"
  const bg  = isExpired ? "rgba(255,61,94,.06)" : "rgba(245,158,11,.06)"
  const bd  = isExpired ? "rgba(255,61,94,.2)"  : "rgba(245,158,11,.2)"

  const save = async () => {
    if (!val.trim()) return
    setSaveState("saving")
    try {
      const res = await postToken(val) as { error?: string }
      if (res.error) {
        setSaveState("error")
        setTimeout(() => setSaveState("idle"), 3000)
        return
      }
      setSaveState("ok")
      setVal("")
      await poll()
      setTimeout(() => setSaveState("idle"), 3000)
    } catch {
      setSaveState("error")
      setTimeout(() => setSaveState("idle"), 3000)
    }
  }

  const handleKey = (e: React.KeyboardEvent) => {
    if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) save()
  }

  return (
    <div style={{
      background: bg, border: `1px solid ${bd}`, borderRadius: 8,
      padding: "12px 16px", marginBottom: 14,
    }}>
      {/* Header row */}
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 8 }}>
        <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
          <span style={{
            fontSize: ".68em", fontWeight: 800, letterSpacing: ".1em",
            textTransform: "uppercase", color: clr,
          }}>
            {isExpired ? "⚠ Dhan Token Expired" : `⚠ Token Expiring — ${hoursLeft?.toFixed(0)}h left`}
          </span>
          <span style={{ fontSize: ".68em", color: "var(--txd)" }}>
            {isExpired
              ? "Scanner paused — paste new JWT to resume"
              : "Refresh before market open to avoid interruption"}
          </span>
        </div>
        <button
          onClick={() => setDismissed(true)}
          title="Dismiss (will re-appear if still expired)"
          style={{
            background: "none", border: "none", color: "var(--txs)",
            cursor: "pointer", fontSize: ".82em", padding: "2px 6px", lineHeight: 1,
          }}>
          ✕
        </button>
      </div>

      {/* Inline paste row */}
      <div style={{ display: "flex", gap: 8, alignItems: "flex-start" }}>
        <textarea
          value={val}
          onChange={e => setVal(e.target.value)}
          onKeyDown={handleKey}
          placeholder="eyJhbGci... (paste new Dhan JWT — Ctrl+Enter to save)"
          rows={2}
          style={{
            flex: 1, background: "var(--c2)", border: "1px solid var(--bdh)",
            color: "var(--tx)", borderRadius: 6, padding: "7px 10px",
            fontSize: ".78em", fontFamily: "'JetBrains Mono', monospace",
            outline: "none", resize: "none", boxSizing: "border-box",
          }}
        />
        <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
          <button
            onClick={save}
            disabled={saveState === "saving" || !val.trim()}
            style={{
              padding: "8px 18px", borderRadius: 6, cursor: "pointer",
              border: `1px solid ${clr}`, background: bg,
              color: clr, fontSize: ".76em", fontWeight: 700, letterSpacing: ".04em",
              opacity: (!val.trim() || saveState === "saving") ? 0.5 : 1,
              whiteSpace: "nowrap",
            }}>
            {saveState === "saving" ? "Saving…"
              : saveState === "ok" ? "✓ Saved"
              : "Save Token"}
          </button>
          {saveState === "error" && (
            <span style={{ fontSize: ".64em", color: "#ff3d5e", textAlign: "center" }}>
              Failed — check JWT format
            </span>
          )}
        </div>
      </div>
    </div>
  )
}
