import { redirect } from "next/navigation"

// The app is organised into two top-level SECTIONS (see components/SectionNav.tsx):
// Swing (no API needed) and Live API (needs a working Dhan subscription).
// "/" is not a section of its own — it drops you into the default one, the
// half that always works regardless of credential state.
export default function Home() {
  redirect("/swing")
}
