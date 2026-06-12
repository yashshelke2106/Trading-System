"use client"
import type { Signal } from "@/lib/types"

interface Props { signal: Signal }

const tfLabel = { "1d": "1D", "15m": "15m", "5m": "5m" } as const
type TFKey = keyof typeof tfLabel

export default function MTFBadges({ signal }: Props) {
  return (
    <div className="flex gap-1">
      {(["1d", "15m", "5m"] as TFKey[]).map((tf) => {
        const data = signal.per_tf?.[tf]
        if (!data) {
          return (
            <span key={tf} className="px-1.5 py-0.5 rounded text-[10px] bg-[#1f2937] text-[#6b7280]">
              {tfLabel[tf]} —
            </span>
          )
        }
        const aligned = data.direction === signal.direction
        return (
          <span
            key={tf}
            className={`px-1.5 py-0.5 rounded text-[10px] font-medium ${
              aligned
                ? "bg-[#064e3b] text-[#10b981]"
                : "bg-[#7f1d1d] text-[#ef4444]"
            }`}
            title={`${tf}: ${data.direction} | vol ${data.vol_ratio?.toFixed(1)}x | str ${data.strength?.toFixed(0)}`}
          >
            {tfLabel[tf]} {aligned ? "✓" : "✗"}
          </span>
        )
      })}
    </div>
  )
}
