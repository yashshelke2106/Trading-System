import { redirect } from "next/navigation"

// Landing page now goes straight to the Swing dashboard (no-API workflow).
// The old Dhan-token Signals scanner was removed 2026-07-15.
export default function Home() {
  redirect("/swing")
}
