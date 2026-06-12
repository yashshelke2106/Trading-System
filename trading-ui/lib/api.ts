const BASE = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000"

async function apiFetch(path: string) {
  const res = await fetch(`${BASE}${path}`, { cache: "no-store" })
  if (!res.ok) throw new Error(`${path} fetch failed: ${res.status}`)
  return res.json()
}

async function apiPost(path: string, body: unknown) {
  const res = await fetch(`${BASE}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
    cache: "no-store",
  })
  if (!res.ok) throw new Error(`${path} post failed: ${res.status}`)
  return res.json()
}

export const fetchSignals    = ()           => apiFetch("/api/signals")
export const fetchSignalsAll = ()           => apiFetch("/api/signals/all")
export const fetchJournal    = (days = 30)  => apiFetch(`/api/journal?days=${days}`)
export const fetchTrades     = (days = 30)  => apiFetch(`/api/trades?days=${days}`)
export const fetchStatus     = ()           => apiFetch("/api/status")
export const fetchIndices    = ()           => apiFetch("/api/indices")
export const fetchPositions  = ()           => apiFetch("/api/positions")
export const fetchVolume     = ()           => apiFetch("/api/volume")
export const fetchChain      = (sym = "NIFTY") => apiFetch(`/api/chain?symbol=${sym}`)
export const fetchSpikeAlerts = (minConf = 55) => apiFetch(`/api/spike-alerts?min_confidence=${minConf}`)
export const fetchAccuracy      = ()                => apiFetch("/api/accuracy")
export const fetchIntelligence  = ()                => apiFetch("/api/intelligence")
export const fetchLearning      = ()                => apiFetch("/api/learning")

export const postToken     = (token: string)    => apiPost("/api/config/token",     { token })
export const postClientId  = (client_id: string) => apiPost("/api/config/client-id", { client_id })
export const postDataKey   = (api_key: string)  => apiPost("/api/config/data-key",  { api_key })
export const triggerLearning = ()               => apiPost("/api/learning/trigger", {})
export const resetLearning   = ()               => apiPost("/api/learning/reset",   {})

export function getWsUrl() {
  const base = (process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000")
    .replace(/^http/, "ws")
  return `${base}/ws/signals`
}
