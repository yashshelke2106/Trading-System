"use client"

import Link from "next/link"
import { usePathname } from "next/navigation"

// The dashboard is deliberately TWO PARTS (user decision 2026-07-24):
//
//   OFFLINE — works with zero credentials. Reads files the capture layer and
//             journal already wrote. This is the part that is always true:
//             no token can expire out from under it.
//   LIVE    — accepts Dhan credentials and scans the live market for
//             breakout / pattern / news-action setups. Useless without a
//             working subscription, and it says so rather than pretending.
//
// Legacy Dhan-token tabs (Signals, Option Chain, Volume, Positions,
// Intelligence, Config) were removed 2026-07-15; /live supersedes them with
// a single page that probes liveness instead of assuming it.
const HOME = [
  { href: "/", label: "Dashboard" },
]

const OFFLINE = [
  { href: "/verdict",    label: "Verdict"    },
  { href: "/swing",      label: "Swing"      },
  { href: "/allocation", label: "Allocation" },
  { href: "/accuracy",   label: "Accuracy"   },
  { href: "/journal",    label: "Journal"    },
  { href: "/trades",     label: "P&L"        },
]

const LIVE = [
  { href: "/live", label: "Live (Dhan)" },
]

const GROUP_LABEL: React.CSSProperties = {
  fontSize: ".58em", textTransform: "uppercase", letterSpacing: ".12em",
  color: "var(--txs, #6b7688)", alignSelf: "center", padding: "0 8px 0 2px",
  whiteSpace: "nowrap",
}
const DIVIDER: React.CSSProperties = {
  width: 1, alignSelf: "stretch", background: "var(--bd)", margin: "0 6px",
}

export default function NavBar() {
  const pathname = usePathname()

  const tab = (t: { href: string; label: string }) => {
    // "/" is a prefix of every route, so it must match exactly or the
    // Dashboard tab renders active on every page.
    const active = t.href === "/" ? pathname === "/"
      : pathname === t.href || pathname.startsWith(t.href)
    return (
      <Link
        key={t.href}
        href={t.href}
        className={`navTab${active ? " active" : ""}`}
        aria-current={active ? "page" : undefined}
      >
        {t.label}
      </Link>
    )
  }

  return (
    <nav className="navWrap" aria-label="Primary">
      {HOME.map(tab)}
      <span style={DIVIDER} aria-hidden="true" />
      <span style={GROUP_LABEL} aria-hidden="true">no&nbsp;api</span>
      {OFFLINE.map(tab)}
      <span style={DIVIDER} aria-hidden="true" />
      <span style={GROUP_LABEL} aria-hidden="true">live</span>
      {LIVE.map(tab)}
    </nav>
  )
}
