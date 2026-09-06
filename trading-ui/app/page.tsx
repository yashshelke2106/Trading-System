import { redirect } from "next/navigation"

// The app is organised into three top-level SECTIONS (components/SectionNav.tsx):
// Fund (the paper macro book), Swing (equity research, no API needed) and
// Live API (needs a working Dhan subscription). "/" is not a section of its
// own — it drops you into the one the desk is actually running.
//
// Changed from /swing to /fund on 2026-09-06. The active book is now the paper
// macro fund under H-022; landing on swing research implied the desk was still
// running a strategy it had measured as unfundable. Fund also needs no
// credentials, so this keeps the "always works" property the old default had.
export default function Home() {
  redirect("/fund")
}
