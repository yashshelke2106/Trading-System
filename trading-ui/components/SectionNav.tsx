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
  id: "fund" | "swing" | "live"
  href: string
  label: string
  blurb: string
  tabs: { href: string; label: string }[]
}

export const SECTIONS: Section[] = [
  {
    // FUND leads (2026-09-06). The desk's active book is the paper macro fund
    // under H-022 — the first hypothesis in the programme to clear a matched
    // null. Swing stays because its research is still worth reading, but it
    // is no longer what the desk is running, and a nav that opened on it kept
    // implying otherwise.
    id: "fund",
    href: "/fund",
    label: "Fund",
    blurb: "paper, live market",
    tabs: [
      { href: "/fund",       label: "Book"       },
      { href: "/allocation", label: "Allocation" },
    ],
  },
  {
    id: "swing",
    href: "/swing",
    label: "Swing",
    blurb: "no API needed",
    tabs: [
      { href: "/swing",      label: "Overview"   },
      { href: "/verdict",    label: "Verdict"    },
      { href: "/accuracy",   label: "Accuracy"   },
      { href: "/journal",    label: "Journal"    },
      { href: "/portfolio",  label: "Portfolio"  },
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

// Explicit route -> section maps. Swing is the fallback, so a new no-API
// research page is picked up without touching either map. /allocation moved
// to Fund on 2026-09-06: it is where capital is actually deployed, which is a
// fund question, not a swing-research one. A route must appear in ONE
// section's tabs or the highlight is ambiguous.
const FUND_ROUTES = ["/fund", "/allocation"]
const LIVE_ROUTES = ["/live"]

const matches = (pathname: string, routes: string[]) =>
  routes.some(r => pathname === r || pathname.startsWith(r + "/"))

export function sectionFor(pathname: string): Section {
  const byId = (id: Section["id"]) => SECTIONS.find(s => s.id === id)!
  if (matches(pathname, FUND_ROUTES)) return byId("fund")
  if (matches(pathname, LIVE_ROUTES)) return byId("live")
  return byId("swing")
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
