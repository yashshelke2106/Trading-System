"use client"
import { useSignals } from "@/lib/useSignals"
import SignalCard from "./SignalCard"

export default function SignalTable() {
  const { signals, ts, connected, lastUpdate, meta } = useSignals()

  const lastScan  = ts ? new Date(ts).toLocaleTimeString("en-IN", { hour12: false }) : "—"
  const updatedAt = lastUpdate?.toLocaleTimeString("en-IN", { hour12: false }) ?? "—"
  const scanned   = (meta as Record<string, unknown>)?.universe_size as number | undefined

  return (
    <div>
      {/* ── Status bar ── */}
      <div className="statBar" style={{ marginBottom: 14 }}>
        <div className="statItem">
          <div className="statLbl">Scanner</div>
          <div className="statVal">
            <span
              style={{
                display: "inline-block", width: 6, height: 6, borderRadius: "50%",
                background: connected ? "#00c896" : "#ff3d5e",
                marginRight: 5, verticalAlign: "middle",
                animation: connected ? "pulse-dot 2s infinite" : undefined,
              }}
            />
            <span style={{ color: connected ? "#00c896" : "#ff3d5e" }}>
              {connected ? "LIVE" : "RECONNECTING"}
            </span>
          </div>
        </div>
        <div className="statItem">
          <div className="statLbl">Last Scan</div>
          <div className="statVal">{lastScan}</div>
        </div>
        <div className="statItem">
          <div className="statLbl">Updated</div>
          <div className="statVal">{updatedAt}</div>
        </div>
        {scanned != null && (
          <div className="statItem">
            <div className="statLbl">Universe</div>
            <div className="statVal">{scanned}</div>
          </div>
        )}
        <div className="statItem" style={{ marginLeft: "auto", borderLeft: "1px solid var(--bd)", borderRight: "none" }}>
          <div className="statLbl">Grade A Signals</div>
          <div className="statVal" style={{ color: "#f59e0b" }}>
            {signals.length}
          </div>
        </div>
      </div>

      {/* ── Signal cards ── */}
      {signals.length === 0 ? (
        <div style={{
          display: "flex", flexDirection: "column", alignItems: "center", justifyContent: "center",
          height: 200, borderRadius: 8, border: "1px dashed var(--bd)",
          color: "var(--txs)", gap: 8,
        }}>
          <svg width="32" height="32" viewBox="0 0 24 24" fill="none" stroke="currentColor" opacity={0.4}>
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5}
              d="M9 19v-6a2 2 0 00-2-2H5a2 2 0 00-2 2v6a2 2 0 002 2h2a2 2 0 002-2zm0 0V9a2 2 0 012-2h2a2 2 0 012 2v10m-6 0a2 2 0 002 2h2a2 2 0 002-2m0 0V5a2 2 0 012-2h2a2 2 0 012 2v14a2 2 0 01-2 2h-2a2 2 0 01-2-2z" />
          </svg>
          <p style={{ fontSize: ".82em", color: "var(--txd)" }}>
            {connected ? "No Grade A signals in current scan" : "Connecting to scanner…"}
          </p>
          {connected && (
            <p style={{ fontSize: ".72em", color: "var(--txs)" }}>
              Score ≥ 80 required · 1D opposing suppresses signal
            </p>
          )}
        </div>
      ) : (
        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(520px, 1fr))", gap: 12 }}>
          {signals.map(sig => (
            <SignalCard
              key={`${sig.symbol}-${sig.direction}-${sig.ts}`}
              signal={sig}
            />
          ))}
        </div>
      )}
    </div>
  )
}
