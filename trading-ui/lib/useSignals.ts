"use client"
import { useEffect, useRef, useState, useCallback } from "react"
import { getWsUrl } from "./api"
import type { Signal } from "./types"

interface SignalsState {
  signals: Signal[]
  ts: string | null
  meta: Record<string, unknown>
  connected: boolean
  lastUpdate: Date | null
}

export function useSignals() {
  const [state, setState] = useState<SignalsState>({
    signals: [],
    ts: null,
    meta: {},
    connected: false,
    lastUpdate: null,
  })
  const wsRef = useRef<WebSocket | null>(null)
  const retryRef = useRef<ReturnType<typeof setTimeout> | null>(null)

  const connect = useCallback(() => {
    if (wsRef.current?.readyState === WebSocket.OPEN) return

    const ws = new WebSocket(getWsUrl())
    wsRef.current = ws

    ws.onopen = () =>
      setState(s => ({ ...s, connected: true }))

    ws.onmessage = (e) => {
      try {
        const data = JSON.parse(e.data)
        if (data.type === "signals") {
          setState(s => ({
            ...s,
            signals: data.signals ?? [],
            ts: data.ts ?? null,
            meta: data.meta ?? {},
            lastUpdate: new Date(),
          }))
        }
      } catch {}
    }

    ws.onerror = () => ws.close()

    ws.onclose = () => {
      setState(s => ({ ...s, connected: false }))
      // Reconnect after 3s
      retryRef.current = setTimeout(connect, 3000)
    }
  }, [])

  useEffect(() => {
    connect()
    return () => {
      retryRef.current && clearTimeout(retryRef.current)
      wsRef.current?.close()
    }
  }, [connect])

  return state
}
