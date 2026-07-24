"use client"

import Link from "next/link"
import { usePathname } from "next/navigation"

// Groww-style top-level SECTIONS (user decision 2026-07-24).
//
// Groww splits its app into Stocks / F&O / Mutual Funds / Pay — you pick a
// section first, and the whole sub-navigation below changes. Same idea here,
// with two sections that correspond to the only real split in this system:
//
//   SWING     works with zero credentials. Reads recorded history: the
//             strategy verdict, the capture archives, the paper journal.
//             Nothing here can be taken down by an expired subscription.
//   LIVE API  needs a working Dhan subscription. Connection probe,
//             credential entry, and the live scanner's breakout / pattern /
//             news-action setups.
//
// The separation is not cosmetic: it keeps a dead live feed from making the
// recorded-history side look broken, and keeps the credential surface in one
// place instead of smeared across every tab.

export interface Section {
  id: "swing" | "live"
  href: string
  label: string
  blurb: string
  tabs: { href: string; label: string }[]
}

export const SECTIONS: Section[] = [
  {
    id: "swing",
    href: "/swing",
    label: "Swing",
    blurb: "no API needed",
    tabs: [
      { href: "/swing",      label: "Overview"   },
      { href: "/verdict",    label: "Verdict"    },
      { href: "/allocation", label: "Allocation" },
      { href: "/accuracy",   label: "Accuracy"   },
      { href: "/journal",    label: "Journal"    },
      { href: "/trades",     label: "P&L"        },
    ],
  },
  {
    id: "live",
    href: "/live",
    label: "Live API",
    blurb: "needs Dhan",
    tabs: [
      { href: "/live", label: "Scanner" },
    ],
  },
]

// Routes that belong to the Live section. Everything else falls to Swing,
// so a new no-API page is picked up without touching this map.
const LIVE_ROUTES = ["/live"]

export function sectionFor(pathname: string): Section {
  const isLive = LIVE_ROUTES.some(r => pathname === r || pathname.startsWith(r + "/"))
  return SECTIONS[isLive ? 1 : 0]
}

export default function SectionNav() {
  const pathname = usePathname()
  const active = sectionFor(pathname)

  return (
    <div className="sectionNav" role="navigation" aria-label="Sections">
      {SECTIONS.map(s => {
        const on = s.id === active.id
        return (
          <Link
            key={s.id}
            href={s.href}
            className={`sectionTab${on ? " active" : ""}`}
            aria-current={on ? "page" : undefined}
          >
            <span className="sectionLabel">{s.label}</span>
            <span className="sectionBlurb">{s.blurb}</span>
          </Link>
        )
      })}
    </div>
  )
}
