"use client"

import { useEffect, useState } from "react"

// Light/dark switch. The entire design system reads from CSS variables, so
// switching themes is one attribute on <html> — no component needs to know a
// theme exists.
//
// Two ordering problems this has to solve:
//   1. FLASH — if React set the theme on mount, a light-theme user would see
//      a dark frame first. THEME_INIT below runs before first paint, inlined
//      in <head> by layout.tsx, and is the thing that actually applies the
//      saved choice. This component only handles clicks afterwards.
//   2. HYDRATION — the server cannot know the stored preference, so the
//      button renders a stable placeholder until mounted, otherwise React
//      warns about a server/client text mismatch.

export type Theme = "dark" | "light"
const KEY = "fo-theme"

// Stringified and injected into <head>. Must stay dependency-free and cheap.
export const THEME_INIT = `(function(){try{
var t=localStorage.getItem(${JSON.stringify(KEY)});
if(!t){t=window.matchMedia&&window.matchMedia('(prefers-color-scheme: light)').matches?'light':'dark';}
document.documentElement.setAttribute('data-theme',t);
}catch(e){document.documentElement.setAttribute('data-theme','dark');}})();`

function current(): Theme {
  if (typeof document === "undefined") return "dark"
  return document.documentElement.getAttribute("data-theme") === "light" ? "light" : "dark"
}

export default function ThemeToggle() {
  const [theme, setTheme] = useState<Theme>("dark")
  const [mounted, setMounted] = useState(false)

  useEffect(() => {
    setTheme(current())
    setMounted(true)
  }, [])

  function toggle() {
    const next: Theme = current() === "light" ? "dark" : "light"
    document.documentElement.setAttribute("data-theme", next)
    try { localStorage.setItem(KEY, next) } catch { /* private mode — session only */ }
    setTheme(next)
  }

  return (
    <button
      type="button"
      onClick={toggle}
      className="themeBtn"
      title={mounted ? `Switch to ${theme === "light" ? "dark" : "light"} theme` : "Toggle theme"}
      aria-label={mounted ? `Switch to ${theme === "light" ? "dark" : "light"} theme` : "Toggle theme"}
    >
      {/* suppressHydrationWarning: the glyph legitimately differs between the
          server render (no preference knowable) and the client. */}
      <span suppressHydrationWarning>{mounted && theme === "light" ? "☾" : "☀"}</span>
    </button>
  )
}
