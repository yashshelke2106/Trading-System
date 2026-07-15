"use client"

import Link from "next/link"
import { usePathname } from "next/navigation"

// No-API workflow only. Legacy Dhan-token tabs (Signals, Option Chain,
// Volume, Positions, Intelligence, Config) were removed 2026-07-15.
const TABS = [
  { href: "/swing",      label: "Swing"      },
  { href: "/allocation", label: "Allocation" },
  { href: "/accuracy",   label: "Accuracy"   },
  { href: "/journal",    label: "Journal"    },
  { href: "/trades",     label: "P&L"        },
]

export default function NavBar() {
  const pathname = usePathname()
  return (
    <nav className="navWrap" aria-label="Primary">
      {TABS.map(t => {
        const active = pathname === t.href || pathname.startsWith(t.href)
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
      })}
    </nav>
  )
}
