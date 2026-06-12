"use client"
import type { Signal } from "@/lib/types"
import MTFBadges from "./MTFBadges"

interface Props { signal: Signal }

function fmt(n: number | undefined) {
  if (n == null) return "—"
  return n.toLocaleString("en-IN", { maximumFractionDigits: 2 })
}

function elapsed(ts: string) {
  const diff = Date.now() - new Date(ts).getTime()
  const m = Math.floor(diff / 60000)
  if (m < 1) return "<1m"
  if (m < 60) return `${m}m`
  return `${Math.floor(m / 60)}h${m % 60 ? (m % 60) + "m" : ""}`
}

export default function SignalRow({ signal }: Props) {
  const lng = signal.direction === "long"
  const dirColor  = lng ? "text-[#10b981]" : "text-[#ef4444]"
  const dirBg     = lng ? "bg-[#064e3b]"   : "bg-[#7f1d1d]"
  const riskPct   = signal.entry_price > 0
    ? Math.abs((signal.sl_price - signal.entry_price) / signal.entry_price * 100).toFixed(1)
    : "—"

  return (
    <tr className="border-b border-[#1f2937] hover:bg-[#111827] transition-colors">

      {/* Symbol + direction */}
      <td className="px-3 py-2 whitespace-nowrap">
        <div className="flex items-center gap-2">
          <span className={`text-xs font-bold px-1.5 py-0.5 rounded ${dirBg} ${dirColor}`}>
            {signal.direction.toUpperCase()}
          </span>
          <span className="font-bold text-white">{signal.symbol}</span>
        </div>
      </td>

      {/* Score */}
      <td className="px-3 py-2 text-center">
        <span className="text-[#f59e0b] font-bold">{signal.confluence_score}</span>
      </td>

      {/* Entry / SL / T1 / T2 */}
      <td className="px-3 py-2 text-right tabular-nums text-white">{fmt(signal.entry_price)}</td>
      <td className="px-3 py-2 text-right tabular-nums text-[#ef4444]">
        {fmt(signal.sl_tight)}
        <span className="text-[#6b7280] text-[10px] ml-1">({riskPct}%)</span>
      </td>
      <td className="px-3 py-2 text-right tabular-nums text-[#34d399]">{fmt(signal.target_1)}</td>
      <td className="px-3 py-2 text-right tabular-nums text-[#10b981] font-bold">{fmt(signal.target_price)}</td>

      {/* RR */}
      <td className="px-3 py-2 text-center text-[#9ca3af]">
        {signal.rr_ratio ? `1:${signal.rr_ratio}` : "—"}
      </td>

      {/* Option leg */}
      <td className="px-3 py-2 whitespace-nowrap text-[#9ca3af] text-[11px]">
        {signal.option_strike ? (
          <span className={signal.option_type === "CE" ? "text-[#60a5fa]" : "text-[#f97316]"}>
            {signal.option_strike} {signal.option_type}
            {signal.entry_prem ? ` @₹${signal.entry_prem}` : ""}
          </span>
        ) : "—"}
      </td>

      {/* MTF badges */}
      <td className="px-3 py-2">
        <MTFBadges signal={signal} />
      </td>

      {/* RSI + Vol */}
      <td className="px-3 py-2 text-[#9ca3af] text-[11px] whitespace-nowrap">
        RSI {signal.rsi?.toFixed(0) ?? "—"} · {signal.volume_ratio?.toFixed(1) ?? "—"}x
      </td>

      {/* Patterns */}
      <td className="px-3 py-2 text-[#6b7280] text-[11px] max-w-[220px] truncate"
          title={signal.reason}>
        {signal.reason?.substring(0, 60)}
      </td>

      {/* Age */}
      <td className="px-3 py-2 text-right text-[#6b7280] text-[11px]">
        {elapsed(signal.ts)}
      </td>
    </tr>
  )
}
