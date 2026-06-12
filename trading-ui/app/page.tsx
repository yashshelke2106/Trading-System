"use client"
import { useEffect, useState, useCallback } from "react"
import { getWsUrl, fetchSignalsAll } from "@/lib/api"
import SignalCard from "@/components/SignalCard"
import TokenAlert from "@/components/TokenAlert"
import type { Signal } from "@/lib/types"

type GradeTab = "A" | "B" | "C"

interface AllSignals {
  by_grade: Record<GradeTab, Signal[]>
  ts: string | null
  meta: Record<string, unknown>
  counts: Record<string, number>
}

const GRADE_COLORS: Record<GradeTab, string> = {
  A: "#00c896",
  B: "#f59e0b",
  C: "#ff9800",
}

export default function HomePage() {
  const [allSigs, setAllSigs] = useState<AllSignals>({
    by_grade: { A: [], B: [], C: [] },
    ts: null,
    meta: {},
    counts: {},
  })
  const [activeTab, setActiveTab] = useState<GradeTab>("A")
  const [connected, setConnected] = useState(false)
  const [lastUpdate, setLastUpdate] = useState<Date | null>(null)

  // WebSocket for Grade A live push
  const connectWs = useCallback(() => {
    const ws = new WebSocket(getWsUrl())
    ws.onopen = () => setConnected(true)
    ws.onmessage = (e) => {
      try {
        const data = JSON.parse(e.data)
        if (data.type === "signals") {
          setAllSigs(prev => ({
            ...prev,
            by_grade: { ...prev.by_grade, A: data.signals ?? [] },
            ts: data.ts ?? prev.ts,
            meta: data.meta ?? prev.meta,
            counts: { ...prev.counts, A: (data.signals ?? []).length },
          }))
          setLastUpdate(new Date())
        }
      } catch {}
    }
    ws.onerror = () => ws.close()
    ws.onclose = () => {
      setConnected(false)
      setTimeout(connectWs, 3000)
    }
    return ws
  }, [])

  // Fetch B/C grades via REST (poll every 30s)
  const fetchBC = useCallback(async () => {
    try {
      const data: AllSignals = await fetchSignalsAll()
      setAllSigs(prev => ({
        ...prev,
        by_grade: {
          A: prev.by_grade.A,  // keep WS-driven A
          B: data.by_grade?.B ?? [],
          C: data.by_grade?.C ?? [],
        },
        ts: data.ts ?? prev.ts,
        meta: data.meta ?? prev.meta,
        counts: {
          ...data.counts,
          A: prev.counts.A ?? data.counts?.A ?? 0,
        },
      }))
    } catch {}
  }, [])

  useEffect(() => {
    const ws = connectWs()
    fetchBC()
    const t = setInterval(fetchBC, 30_000)
    return () => {
      ws.close()
      clearInterval(t)
    }
  }, [connectWs, fetchBC])

  const signals = allSigs.by_grade[activeTab] ?? []
  const lastScan  = allSigs.ts ? new Date(allSigs.ts).toLocaleTimeString("en-IN", { hour12: false }) : "—"
  const updatedAt = lastUpdate?.toLocaleTimeString("en-IN", { hour12: false }) ?? "—"
  const scanned   = (allSigs.meta as Record<string, unknown>)?.universe_size as number | undefined

  return (
    <div>
      {/* ── Token alert — shown when expired or < 2h remaining ── */}
      <TokenAlert />

      {/* ── Status bar ── */}
      <div className="statBar" style={{ marginBottom: 14 }}>
        <div className="statItem">
          <div className="statLbl">Scanner</div>
          <div className="statVal">
            <span style={{
              display: "inline-block", width: 6, height: 6, borderRadius: "50%",
              background: connected ? "#00c896" : "#ff3d5e",
              marginRight: 5, verticalAlign: "middle",
              animation: connected ? "pulse-dot 2s infinite" : undefined,
            }} />
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
        {(["A", "B", "C"] as GradeTab[]).map(g => (
          <div key={g} className="statItem">
            <div className="statLbl">Grade {g}</div>
            <div className="statVal" style={{ color: GRADE_COLORS[g] }}>
              {allSigs.counts[g] ?? allSigs.by_grade[g]?.length ?? 0}
            </div>
          </div>
        ))}
      </div>

      {/* ── Grade tabs ── */}
      <div style={{ display: "flex", gap: 0, marginBottom: 14, borderBottom: "1px solid var(--bd)" }}>
        {(["A", "B", "C"] as GradeTab[]).map(g => {
          const count = allSigs.by_grade[g]?.length ?? 0
          const isActive = activeTab === g
          return (
            <button
              key={g}
              onClick={() => setActiveTab(g)}
              style={{
                padding: "8px 20px",
                border: "none",
                borderBottom: isActive ? `2px solid ${GRADE_COLORS[g]}` : "2px solid transparent",
                background: "transparent",
                color: isActive ? GRADE_COLORS[g] : "var(--txd)",
                fontWeight: 700,
                fontSize: ".72em",
                textTransform: "uppercase" as const,
                letterSpacing: ".08em",
                cursor: "pointer",
                transition: "color .15s",
              }}
            >
              Grade {g}
              <span style={{
                marginLeft: 6, fontSize: ".85em",
                background: isActive ? `rgba(${g === "A" ? "0,200,150" : g === "B" ? "245,158,11" : "255,152,0"},.15)` : "var(--c2)",
                padding: "1px 6px", borderRadius: 10,
              }}>
                {count}
              </span>
            </button>
          )
        })}
      </div>

      {/* ── Signal cards ── */}
      {signals.length === 0 ? (
        <div className="empty">
          <svg width="34" height="34" viewBox="0 0 24 24" fill="none" stroke="currentColor" opacity={0.45}>
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5}
              d="M9 19v-6a2 2 0 00-2-2H5a2 2 0 00-2 2v6a2 2 0 002 2h2a2 2 0 002-2zm0 0V9a2 2 0 012-2h2a2 2 0 012 2v10m-6 0a2 2 0 002 2h2a2 2 0 002-2m0 0V5a2 2 0 012-2h2a2 2 0 012 2v14a2 2 0 01-2 2h-2a2 2 0 01-2-2z" />
          </svg>
          <p style={{ fontSize: "12px", fontWeight: 600, color: "var(--txm)" }}>
            {connected ? `No Grade ${activeTab} signals in current scan` : "Connecting to scanner…"}
          </p>
          <p style={{ fontSize: "10.5px", color: "var(--txd)", letterSpacing: ".04em" }}>
            {connected ? "Selective-fire is holding — quality over quantity" : "Reconnecting every 3s"}
          </p>
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
