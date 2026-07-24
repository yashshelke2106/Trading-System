<!-- BEGIN:nextjs-agent-rules -->
# This is NOT the Next.js you know

This version has breaking changes — APIs, conventions, and file structure may all differ from your training data. Read the relevant guide in `node_modules/next/dist/docs/` before writing any code. Heed deprecation notices.
<!-- END:nextjs-agent-rules -->

## CSS changes do not hot-reload (turbopack dev)

TSX edits Fast-Refresh normally, but edits to `app/globals.css` are **not**
picked up — the served chunk (`/_next/static/chunks/app_globals_*.css`) keeps
its old contents, and `touch` does not invalidate it. Restarting `npm run dev`
usually rebuilds it; when even that fails, the `.next` build cache is stale and
must be removed:

```bash
rm -rf trading-ui/.next
```

Verify a CSS change actually shipped before trusting the browser — the page can
render new markup against an old stylesheet, which looks like "the old version"
but is really unstyled new markup:

```bash
css=$(curl -s http://localhost:3000/swing | grep -oE '/_next/static/chunks/app_globals[^"]*\.css' | head -1)
curl -s "http://localhost:3000$css" | grep -c 'YOUR_NEW_CLASS_OR_VALUE'
```

This cost real time on 2026-07-24 twice: once when the section nav rendered as
raw unstyled text, once when a contrast fix silently did not apply.
