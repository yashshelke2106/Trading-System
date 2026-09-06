const BASE = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000"

// Must EXCEED the server's own _RUN_DEADLINE_SEC (12s), or the browser gives
// up before the API can answer and every slow-but-alive endpoint looks dead.
// At 8s that was guaranteed: the server returns a structured timeout at 12s,
// so the client aborted first — 100% of the time — and reported "is the
// backend running?" while it was running and answering fine.
const API_TIMEOUT_MS = 15_000

async function apiFetch(path: string) {
  const ctl = new AbortController()
  const timer = setTimeout(() => ctl.abort(), API_TIMEOUT_MS)
  try {
    const res = await fetch(`${BASE}${path}`, { cache: "no-store", signal: ctl.signal })
    if (!res.ok) throw new Error(`${path} fetch failed: ${res.status}`)
    return await res.json()
  } catch (e) {
    // A timeout and a refused connection are different faults with different
    // fixes. Collapsing both into "is the backend running?" sent us hunting a
    // dead server that was up the whole time.
    if ((e as Error).name === "AbortError")
      throw new Error(
        `${path} timed out after ${API_TIMEOUT_MS / 1000}s — the API is up but this `
        + `endpoint is slow (usually Dhan rate-limited). It retries automatically.`)
    if (e instanceof TypeError)   // fetch throws TypeError when it cannot connect
      throw new Error(`Cannot reach the API at ${BASE} — is the backend running? (.\\start_ui.bat)`)
    throw e
  } finally {
    clearTimeout(timer)
  }
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
export const fetchAllocation    = (refresh = false) => apiFetch(`/api/allocation${refresh ? "?refresh=true" : ""}`)
export const fetchSwing         = ()                => apiFetch("/api/swing")
export const fetchVersion       = ()                => apiFetch("/api/version")
export const fetchVerdict       = ()                => apiFetch("/api/verdict")
export const fetchCapture       = ()                => apiFetch("/api/capture")
export const fetchMarketState   = ()                => apiFetch("/api/market-state")
export const fetchStats         = ()                => apiFetch("/api/stats")
export const fetchLearningRules = ()                => apiFetch("/api/learning-rules")
export const fetchIntelligence  = ()                => apiFetch("/api/intelligence")
export const fetchLearning      = ()                => apiFetch("/api/learning")
export const fetchPortfolio     = ()                => apiFetch("/api/portfolio")
export const fetchMacroFund     = ()                => apiFetch("/api/macro-fund")

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
