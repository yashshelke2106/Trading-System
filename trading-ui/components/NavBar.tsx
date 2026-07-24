"use client"

import Link from "next/link"
import { usePathname } from "next/navigation"
import { sectionFor } from "@/components/SectionNav"

// Sub-navigation for whichever SECTION is active (see SectionNav.tsx).
// Deliberately shows ONLY the current section's tabs — that is what makes
// the sections feel like sections rather than one flat tab strip with a
// divider in it. Legacy Dhan-token tabs (Option Chain, Volume, Positions,
// Intelligence, Config) were removed 2026-07-15; /live supersedes them.
export default function NavBar() {
  const pathname = usePathname()
  const section = sectionFor(pathname)

  return (
    <nav className="navWrap" aria-label={`${section.label} pages`}>
      {section.tabs.map(t => {
        const active = pathname === t.href
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
