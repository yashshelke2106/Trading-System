"use client"
import { useEffect, useState } from "react"
import { fetchStatus, postToken, postClientId, postDataKey } from "@/lib/api"
import type { MarketStatus } from "@/lib/types"

type SaveState = "idle" | "saving" | "ok" | "error"

// ── Shared credential card (Client ID, Data API Key) ─────────────────────────

function CredentialCard({
  title, description, placeholder, inputType = "text", multiline = false,
  onSave, status, statusBadge,
}: {
  title: string
  description: string
  placeholder: string
  inputType?: string
  multiline?: boolean
  onSave: (val: string) => Promise<void>
  status: SaveState
  statusBadge?: React.ReactNode
}) {
  const [val, setVal] = useState("")

  useEffect(() => {
    if (status === "ok") setVal("")
  }, [status])

  const inputStyle: React.CSSProperties = {
    width: "100%", background: "var(--c2)", border: "1px solid var(--bdh)",
    color: "var(--tx)", borderRadius: 6, padding: "8px 10px",
    fontSize: ".82em", fontFamily: "'JetBrains Mono', monospace",
    outline: "none", resize: multiline ? "vertical" : undefined,
    minHeight: multiline ? 80 : undefined, boxSizing: "border-box",
  }

  return (
    <div style={{ background: "var(--c1)", border: "1px solid var(--bd)", borderRadius: 10, padding: "16px 18px" }}>
      <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 6 }}>
        <div style={{ fontWeight: 700, fontSize: ".88em", color: "var(--tx)" }}>{title}</div>
        {statusBadge}
      </div>
      <div style={{ fontSize: ".72em", color: "var(--txd)", marginBottom: 10 }}>{description}</div>
      {multiline ? (
        <textarea style={inputStyle as React.CSSProperties} placeholder={placeholder}
          value={val} onChange={e => setVal(e.target.value)} rows={3} />
      ) : (
        <input style={inputStyle} type={inputType} placeholder={placeholder}
          value={val} onChange={e => setVal(e.target.value)} />
      )}
      <div style={{ display: "flex", alignItems: "center", gap: 10, marginTop: 10 }}>
        <button onClick={() => onSave(val)} disabled={status === "saving" || !val.trim()}
          style={{
            padding: "6px 16px", borderRadius: 6, cursor: "pointer",
            border: "1px solid var(--b)", background: "rgba(56,178,240,.08)",
            color: "var(--b)", fontSize: ".76em", fontWeight: 600, letterSpacing: ".04em",
            opacity: (!val.trim() || status === "saving") ? 0.5 : 1,
          }}>
          {status === "saving" ? "Saving…" : "Save"}
        </button>
        {status === "ok"    && <span style={{ color: "#00c896", fontSize: ".76em" }}>✓ Saved</span>}
        {status === "error" && <span style={{ color: "#ff3d5e", fontSize: ".76em" }}>✗ Failed</span>}
      </div>
    </div>
  )
}

// ── Token card — daily-refresh, with countdown + urgency ─────────────────────

function TokenCard({
  th, onSave, status,
}: {
  th: MarketStatus["trade_token"] | undefined
  onSave: (val: string) => Promise<void>
  status: SaveState
}) {
  const [val, setVal]               = useState("")
  const [lastSaved, setLastSaved]   = useState<number | null>(null)
  const [tick, setTick]             = useState(Date.now())

  // Clear input + record localStorage timestamp on successful save
  useEffect(() => {
    if (status === "ok") {
      setVal("")
      const ts = Date.now()
      try { localStorage.setItem("dhan_token_refreshed_at", String(ts)) } catch {}
      setLastSaved(ts)
    }
  }, [status])

  // Load last-saved timestamp from localStorage on mount; tick every minute
  useEffect(() => {
    try {
      const raw = localStorage.getItem("dhan_token_refreshed_at")
      if (raw) setLastSaved(parseInt(raw))
    } catch {}
    const t = setInterval(() => setTick(Date.now()), 60_000)
    return () => clearInterval(t)
  }, [])

  // Derived urgency
  const hoursLeft = th?.hours_left ?? null
  const isValid   = th?.valid ?? false
  const isExpired = !isValid || (hoursLeft !== null && hoursLeft <= 0)
  const isWarn    = isValid && hoursLeft !== null && hoursLeft < 4

  const lvl = isExpired ? "red" : isWarn ? "amber" : "green"
  const clr = lvl === "red" ? "#ff3d5e" : lvl === "amber" ? "#f59e0b" : "#00c896"
  const bg  = lvl === "red" ? "rgba(255,61,94,.05)" : lvl === "amber" ? "rgba(245,158,11,.05)" : "rgba(0,200,150,.04)"
  const bd  = lvl === "red" ? "rgba(255,61,94,.3)"  : lvl === "amber" ? "rgba(245,158,11,.3)"  : "rgba(0,200,150,.25)"

  // Progress bar (24h = 100%)
  const pct = hoursLeft != null && hoursLeft > 0 ? Math.min(100, (hoursLeft / 24) * 100) : 0

  // "Expires at HH:MM today/tomorrow"
  let expiresLabel = isExpired ? "Expired — paste new token now" : ""
  if (!isExpired && hoursLeft != null) {
    const exp  = new Date(Date.now() + hoursLeft * 3_600_000)
    const same = exp.toDateString() === new Date().toDateString()
    const hhmm = exp.toLocaleTimeString("en-IN", { hour: "2-digit", minute: "2-digit", hour12: false })
    expiresLabel = `Expires ${same ? "today" : "tomorrow"} at ${hhmm}`
  }

  // "Last refreshed X min ago"
  let refreshedLabel = ""
  if (lastSaved) {
    const diffMin = Math.floor((tick - lastSaved) / 60_000)
    refreshedLabel = diffMin < 1 ? "Last refreshed: just now"
      : diffMin < 60 ? `Last refreshed: ${diffMin}m ago`
      : `Last refreshed: ${Math.floor(diffMin / 60)}h ${diffMin % 60}m ago`
  }

  // Countdown display
  const countdownLabel = isExpired ? "EXPIRED"
    : hoursLeft != null
      ? `${Math.floor(hoursLeft)}h ${Math.round((hoursLeft % 1) * 60)}m remaining`
      : "—"

  const inputStyle: React.CSSProperties = {
    width: "100%", background: "var(--c2)", border: "1px solid var(--bdh)",
    color: "var(--tx)", borderRadius: 6, padding: "8px 10px",
    fontSize: ".82em", fontFamily: "'JetBrains Mono', monospace",
    outline: "none", resize: "vertical", minHeight: 80, boxSizing: "border-box",
  }

  return (
    <div style={{ background: bg, border: `1px solid ${bd}`, borderRadius: 10, padding: "16px 18px" }}>
      {/* Header */}
      <div style={{ display: "flex", alignItems: "flex-start", justifyContent: "space-between", marginBottom: 12 }}>
        <div>
          <div style={{ fontWeight: 700, fontSize: ".88em", color: "var(--tx)", marginBottom: 3 }}>
            Trading Token (JWT)
          </div>
          <div style={{ fontSize: ".72em", color: "var(--txd)" }}>
            Dhan app → Profile → API → Generate Access Token · Expires every 24h
          </div>
        </div>
        <div style={{
          background: isExpired ? "rgba(255,61,94,.12)" : `${clr}18`,
          border: `1px solid ${bd}`, borderRadius: 6, padding: "4px 10px",
          textAlign: "center", minWidth: 100,
        }}>
          <div style={{
            fontSize: ".6em", fontWeight: 800, letterSpacing: ".1em",
            textTransform: "uppercase", color: clr, marginBottom: 1,
          }}>
            {isExpired ? "EXPIRED" : isWarn ? "EXPIRING SOON" : "ACTIVE"}
          </div>
          <div style={{ fontSize: ".88em", fontWeight: 700, color: clr, fontFamily: "'JetBrains Mono', monospace" }}>
            {countdownLabel}
          </div>
        </div>
      </div>

      {/* Progress bar */}
      <div style={{ height: 4, background: "var(--c2)", borderRadius: 2, marginBottom: 10, overflow: "hidden" }}>
        <div style={{
          height: "100%", width: `${pct}%`, borderRadius: 2,
          background: clr,
          transition: "width .5s ease",
          boxShadow: pct > 10 ? `0 0 6px ${clr}60` : undefined,
        }} />
      </div>

      {/* Expiry + last refreshed */}
      <div style={{ display: "flex", gap: 16, marginBottom: 12, flexWrap: "wrap" }}>
        <div style={{ fontSize: ".70em", color: lvl === "red" ? "#ff3d5e" : "var(--txd)" }}>
          {expiresLabel}
        </div>
        {refreshedLabel && (
          <div style={{ fontSize: ".70em", color: "var(--txs)" }}>{refreshedLabel}</div>
        )}
      </div>

      {/* Input */}
      <textarea
        style={inputStyle as React.CSSProperties}
        placeholder="eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9... (paste new token here)"
        value={val}
        onChange={e => setVal(e.target.value)}
        rows={3}
      />

      {/* Save row */}
      <div style={{ display: "flex", alignItems: "center", gap: 10, marginTop: 10 }}>
        <button
          onClick={() => onSave(val)}
          disabled={status === "saving" || !val.trim()}
          style={{
            padding: "7px 20px", borderRadius: 6, cursor: "pointer",
            border: `1px solid ${clr}`, background: `${clr}14`,
            color: clr, fontSize: ".78em", fontWeight: 700, letterSpacing: ".04em",
            opacity: (!val.trim() || status === "saving") ? 0.5 : 1,
          }}>
          {status === "saving" ? "Saving…" : isExpired ? "Refresh Token" : "Update Token"}
        </button>
        {status === "ok"    && <span style={{ color: "#00c896", fontSize: ".76em" }}>✓ Token updated</span>}
        {status === "error" && <span style={{ color: "#ff3d5e", fontSize: ".76em" }}>✗ Failed — check JWT format</span>}
      </div>

      {/* Hint when expired */}
      {isExpired && (
        <div style={{
          marginTop: 10, fontSize: ".68em", color: "#ff3d5e",
          background: "rgba(255,61,94,.06)", borderRadius: 6, padding: "6px 10px",
        }}>
          Scanner will not fetch live data until token is refreshed. Generate new token from Dhan app each morning.
        </div>
      )}
    </div>
  )
}

// ── Page ─────────────────────────────────────────────────────────────────────

export default function ConfigPage() {
  const [mktStatus,    setMktStatus]    = useState<MarketStatus | null>(null)
  const [tokenState,   setTokenState]   = useState<SaveState>("idle")
  const [clientState,  setClientState]  = useState<SaveState>("idle")
  const [dataKeyState, setDataKeyState] = useState<SaveState>("idle")

  const loadStatus = () => fetchStatus().then(setMktStatus).catch(() => {})

  // Poll every 30s so countdown and status stay fresh
  useEffect(() => {
    loadStatus()
    const t = setInterval(loadStatus, 30_000)
    return () => clearInterval(t)
  }, [])

  const save = (setter: (s: SaveState) => void, fn: (v: string) => Promise<unknown>) =>
    async (val: string) => {
      setter("saving")
      try {
        const res = await fn(val) as { error?: string }
        if (res.error) { setter("error"); setTimeout(() => setter("idle"), 3000); return }
        setter("ok")
        loadStatus()
        setTimeout(() => setter("idle"), 3000)
      } catch {
        setter("error")
        setTimeout(() => setter("idle"), 3000)
      }
    }

  const th = mktStatus?.trade_token
  const da = mktStatus?.data_api

  return (
    <div style={{ maxWidth: 620, display: "flex", flexDirection: "column", gap: 20 }}>
      <div className="secHdr">
        <div className="secDot" style={{ background: "#38b2f0" }} />
        <div className="secTitle">Dhan Credentials</div>
      </div>

      {/* Quick status overview */}
      {mktStatus && (
        <div style={{
          display: "flex", gap: 10, flexWrap: "wrap",
          background: "var(--c2)", border: "1px solid var(--bd)",
          borderRadius: 8, padding: "10px 14px",
        }}>
          {[
            {
              label: "Trading Token",
              ok: !!th?.valid && !th.needs_refresh,
              detail: th?.valid
                ? `${th.hours_left != null ? th.hours_left.toFixed(0) + "h left" : "valid"}`
                : "expired",
            },
            {
              label: "Data API",
              ok: !!da?.valid,
              detail: da?.valid ? "live LTP active" : "yfinance fallback",
            },
          ].map(({ label, ok, detail }) => (
            <span key={label} style={{
              display: "inline-flex", alignItems: "center", gap: 5,
              padding: "3px 10px", borderRadius: 5, fontSize: ".69em", fontWeight: 700,
              background: ok ? "rgba(0,200,150,.08)" : "rgba(255,61,94,.08)",
              color: ok ? "#00c896" : "#ff3d5e",
              border: `1px solid ${ok ? "rgba(0,200,150,.25)" : "rgba(255,61,94,.25)"}`,
            }}>
              {ok ? "✓" : "✗"} {label} · {detail}
            </span>
          ))}
          <span style={{ fontSize: ".68em", color: "var(--txs)", marginLeft: "auto", alignSelf: "center" }}>
            {mktStatus.ist_time} IST
          </span>
        </div>
      )}

      {/* Token card — special with countdown */}
      <TokenCard
        th={th}
        onSave={save(setTokenState, (v) => postToken(v))}
        status={tokenState}
      />

      {/* Client ID */}
      <CredentialCard
        title="Client ID"
        description="Dhan account client ID (numeric). Found in Dhan app → Profile → Account settings. Permanent — set once."
        placeholder="1234567890"
        onSave={save(setClientState, (v) => postClientId(v))}
        status={clientState}
      />

      {/* Data API Key */}
      <CredentialCard
        title="Data API Key"
        description="Dhan Data API key — enables live option chain LTP. Dhan app → Profile → API → Data APIs. Without this, system uses yfinance + Black-Scholes theoretical premiums."
        placeholder="your-data-api-key"
        inputType="password"
        onSave={save(setDataKeyState, (v) => postDataKey(v))}
        status={dataKeyState}
        statusBadge={da?.valid
          ? <span style={{ fontSize: ".68em", color: "#00c896", fontWeight: 700 }}>● Live LTP</span>
          : <span style={{ fontSize: ".68em", color: "#f59e0b", fontWeight: 700 }}>○ BSM fallback</span>
        }
      />

      <div style={{ fontSize: ".70em", color: "var(--txs)", lineHeight: 1.7 }}>
        Credentials stored in Windows Credential Manager (keyring) +
        local files (<code style={{ color: "var(--txd)" }}>dhan_token.txt</code>,{" "}
        <code style={{ color: "var(--txd)" }}>.dhan_client_id</code>,{" "}
        <code style={{ color: "var(--txd)" }}>.dhan_data_apikey</code>).
        Never transmitted externally.
      </div>
    </div>
  )
}
