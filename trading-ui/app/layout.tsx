import type { Metadata } from "next"
import "./globals.css"
import IndexBar from "@/components/IndexBar"
import MarketStatusBar from "@/components/MarketStatus"
import NavBar from "@/components/NavBar"
import SectionNav from "@/components/SectionNav"
import VersionStamp from "@/components/VersionStamp"

export const metadata: Metadata = {
  title: "F&O Signal Terminal",
  description: "Live multi-timeframe F&O signals — NSE India",
}

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" className="h-full">
      <body className="min-h-full flex flex-col">
        {/* ── Sticky shell: brand + primary nav ── */}
        <header className="appShell">
          <div className="appBar">
            <div className="brand">
              <span className="brandMark" aria-hidden />
              <span className="appName">F&amp;O Signal Terminal</span>
              <span className="appTag">NSE · India</span>
            </div>
          </div>
          <SectionNav />
          <NavBar />
        </header>

        <main className="container viewEnter">
          {/* live market context */}
          <div style={{ display: "grid", gap: 10, marginBottom: 16 }}>
            <IndexBar />
            <MarketStatusBar />
          </div>
          {children}
          <VersionStamp />
        </main>
      </body>
    </html>
  )
}
