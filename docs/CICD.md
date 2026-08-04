# CI/CD

Two GitHub Actions workflows. Both are self-contained — no external accounts,
no secrets, no live deploy target (by design: this is a local, `PAPER_TRADE`
trading system).

## CI — `.github/workflows/ci.yml`

Runs on every push (all branches), every PR, and manual dispatch. Two parallel
jobs:

| Job | Steps |
|-----|-------|
| **python** (engine + API) | install deps → `config.example.py`→`config.py` → ruff lint (blocking: broken-code rules only; informational: full set) → `pytest tests/` → safety rails |
| **frontend** (trading-ui) | `npm ci` → `next build` (type-checks + compiles) |

**Safety rails** (the reason CI exists for a paper-trading system): asserts
`config.PAPER_TRADE is True` and that a real `config.py` is never committed.
A green build is a *proof the engine cannot place a live order*, not just that
tests pass.

**Lint policy:** the blocking step runs only `E9,F63,F7,F82` (syntax errors,
undefined names, f-string bugs) so legacy style never fails a build; the full
`E/F/W/I` set (see `pyproject.toml`) runs as non-blocking annotations for
cleanup.

## CD — `.github/workflows/release.yml`

Runs on a `v*` tag (or manual dispatch). There is **no live deployment** — the
engine runs on the operator's machine. CD here means *packaging*:

1. Re-assert `PAPER_TRADE is True` before shipping anything.
2. Build the Next.js UI so the bundle runs without a Node build step.
3. `tar.gz` the deployable (code + prebuilt UI + `.bat` launchers + the example
   config), **excluding** `logs/` (runtime data), `node_modules`, and scratch.
4. Publish a checksummed GitHub Release with auto-generated notes.

`config.py` is gitignored and is never present in the bundle — only
`config.example.py` ships. To run a release: unpack, copy the example to
`config.py`, add credentials, `start_trading.bat`.

### Cutting a release

```bash
git tag v0.1.0
git push origin v0.1.0
```

## Local parity

CI reads the same `pyproject.toml` a developer does, so these match CI:

```bash
ruff check .
pytest tests/
cd trading-ui && npm run build   # next build type-checks by default
```
