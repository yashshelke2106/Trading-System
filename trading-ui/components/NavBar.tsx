"use client"

import Link from "next/link"
import { usePathname } from "next/navigation"

const TABS = [
  { href: "/",             label: "Signals"      },
  { href: "/chain",        label: "Option Chain" },
  { href: "/volume",       label: "Volume"       },
  { href: "/positions",    label: "Positions"    },
  { href: "/accuracy",     label: "Accuracy"     },
  { href: "/journal",      label: "Journal"      },
  { href: "/trades",       label: "P&L"          },
  { href: "/intelligence", label: "Intelligence" },
  { href: "/config",       label: "Config"       },
]

export default function NavBar() {
  const pathname = usePathname()
  return (
    <nav className="navWrap" aria-label="Primary">
      {TABS.map(t => {
        const active = t.href === "/" ? pathname === "/" : pathname.startsWith(t.href)
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
