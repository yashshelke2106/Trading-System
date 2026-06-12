import { fetchIntelligence } from "@/lib/api"

export const dynamic = "force-dynamic"

type EvidenceItem = string | {
  score?: number
  source?: string
  title?: string
  snippet?: string
  metadata?: Record<string, unknown>
}

export default async function IntelligencePage() {
  let summary:         EvidenceItem[] = []
  let recommendations: EvidenceItem[] = []
  let evidence:        EvidenceItem[] = []
  let error: string | null = null
  try {
    const data = await fetchIntelligence()
    summary         = data.summary         ?? []
    recommendations = data.recommendations ?? []
    evidence        = data.evidence        ?? []
    error           = data.error ?? null
  } catch (e: unknown) {
    error = e instanceof Error ? e.message : "Failed to load"
  }

  function renderItem(item: EvidenceItem): React.ReactNode {
    if (typeof item === "string") return item
    if (item == null) return ""
    // Evidence object: {score, source, title, snippet, metadata}
    const obj = item as Exclude<EvidenceItem, string>
    const title = obj.title || obj.source || "Evidence"
    const score = obj.score != null ? ` (${(obj.score * 100).toFixed(0)}%)` : ""
    return (
      <div>
        <div style={{ fontWeight: 600, marginBottom: 4 }}>{title}{score}</div>
        {obj.snippet && (
          <div style={{ color: "var(--txd)", fontSize: ".92em" }}>{obj.snippet}</div>
        )}
        {obj.source && obj.source !== title && (
          <div style={{ color: "var(--txs)", fontSize: ".75em", marginTop: 4 }}>
            source: {obj.source}
          </div>
        )}
      </div>
    )
  }

  function Section({ title, items, color }: { title: string; items: EvidenceItem[]; color: string }) {
    if (!items.length) return null
    return (
      <section style={{ marginBottom: 20 }}>
        <div style={{ fontSize: ".72em", fontWeight: 700, textTransform: "uppercase", letterSpacing: ".1em", color, marginBottom: 10 }}>
          {title}
        </div>
        <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
          {items.map((item, i) => (
            <div key={i} style={{
              background: "var(--c1)", border: "1px solid var(--bd)",
              borderLeft: `3px solid ${color}`, borderRadius: 6,
              padding: "8px 14px", fontSize: ".82em", color: "var(--tx)", lineHeight: 1.5,
            }}>
              {renderItem(item)}
            </div>
          ))}
        </div>
      </section>
    )
  }

  return (
    <div>
      <div className="secHdr">
        <div className="secDot" style={{ background: "#a78bfa" }} />
        <div className="secTitle">RAG Intelligence</div>
      </div>

      {error && (
        <div style={{ background: "rgba(255,61,94,.08)", border: "1px solid rgba(255,61,94,.2)", borderRadius: 6, padding: "8px 14px", fontSize: ".8em", color: "#ff3d5e", marginBottom: 14 }}>
          {error}
        </div>
      )}

      {summary.length === 0 && recommendations.length === 0 && !error && (
        <p style={{ color: "var(--txd)", fontSize: ".82em" }}>No intelligence data. RAG engine needs signal history to analyze.</p>
      )}

      <Section title="Market Brief"       items={summary}         color="#38b2f0" />
      <Section title="Recommendations"    items={recommendations} color="#00c896" />
      <Section title="Supporting Evidence" items={evidence}        color="#6b84a0" />
    </div>
  )
}
