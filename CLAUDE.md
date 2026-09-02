# CLAUDE.md — operating knowledge for this repo

This file is the **hard-won knowledge layer**: traps, gotchas, and things you cannot
learn by reading the code. It is written by Claude sessions, for Claude sessions.

For the basics, read the existing docs first — they are good and current:

| Doc | Covers |
|---|---|
| `README.md` | mental model, local dev, test/lint gates, **the full deploy runbook** |
| `ARCHITECTURE.md` | folder structure, normalized model, event flow, DB, APIs |
| `API.md` | every endpoint and its row shape |
| `STARTUP_MODES.md` | indexer startup / rebuild / replay modes |
| `DESIGN.md` | target data architecture and accuracy rules |

**Multiple Claude sessions edit this file at once.** Add to the section that fits,
revise what is wrong, and commit + push in small increments. Pull/rebase before you push.

---

## 1. Read this before you touch anything

### The working tree is SHARED between sessions

Every Claude session on this machine works in **the same checkout**. Other agents are
editing files while you are.

- **Never run `git add -A` or `git add .`** — you will commit another agent's
  half-finished work under your message, and it is not cleanly reversible (reverting
  deletes their work off the shared disk). Stage explicit paths only:
  `git add path/one.py path/two.py`.
- Run `git status --short` before every commit and treat anything you did not edit as
  someone else's.
- A malformed shell `--query` can create junk files in the repo root (a file literally
  named `` `0`].{n `` has appeared this way). `git add -A` will happily commit those too.
- `az acr build` builds from the **working tree**, not from HEAD — so your image will
  contain other sessions' in-flight edits. That is normal and expected per the README
  flow, but run the lint + test gate immediately before building so you know what you
  are shipping.
- Files can go dirty and clean again underneath you within seconds. If a `git status`
  result surprises you, re-run it before concluding anything.

### Deploy collisions are real

A peer session can deploy an image built from an older tree and silently roll your work
out of prod. Nothing errors — the behavior just disappears.

- After **any** deploy, re-verify later. Check the live image:
  ```bash
  az containerapp show -n crystal-api -g crystal-prod-rg -o tsv --query 'properties.template.containers[0].image'
  az containerapp show -n crystal-indexer -g crystal-prod-rg -o tsv --query 'properties.template.containers[0].image'
  ```
- Never assume a verified deploy stays live.

### Conventions

- **No code comments.** None, anywhere. The codebase has essentially no docstrings
  either (only `core/rewards.py` uses them). Match that.
- **Commit messages**: lowercase, one descriptive sentence saying *why*, not `z` or `s`.
- **Never add Claude/Anthropic co-author trailers.**
- Commit and push freely; you do not need to ask.

---

## 2. Environment and access

`.env` in the repo root holds real production credentials (this repo is **public** —
see the security section). Load them with `env_loader.load_env()`.

| Var | Value / meaning |
|---|---|
| `PGHOST` | `crystal-prod-db-r3.postgres.database.azure.com` — **the live DB** |
| `PGDATABASE` | `crystal` |
| `RPC_HTTP` | `https://rpc.monad.xyz` |

- **`crystal-prod-db-r3` is the only live server.** An older `crystal-prod-db` may still
  answer but is stale and misleading. Always confirm the host.
- `dev-env.ps1` points `DATABASE_URL` at a **local** `crystal_dev` DB. That local DB is
  a very old snapshot (hundreds of days behind prod) — fine for schema and logic tests,
  useless for anything about current data. Do not draw conclusions about prod from it.
- Always open prod connections read-only unless you intend to write:
  ```python
  conn.set_session(readonly=True, autocommit=True)
  ```
- Scripts under `scripts/` need `PYTHONPATH=.` or they fail with
  `ModuleNotFoundError: No module named 'core'`.

---

## 3. Deploy and CI

Full runbook is in `README.md` §6. What that does not tell you:

- **Deploys from `main` are gated** on a `production` GitHub environment and sit in
  `waiting` until a human approves. A deploy can sit unapproved for hours and look
  "stuck". Check with `gh run list` and look for `waiting`.
- CI runs on `dev`; the gated deploy runs on `main`. Branch model is `dev` → PR → `main`.
- **`az acr build` crashes on Windows with `UnicodeEncodeError: cp1252`** even with
  `PYTHONIOENCODING=utf-8` set. The crash is in az's colorama log writer *after the
  build has already succeeded*. **Do not retry the build.** Verify instead:
  ```bash
  az acr task list-runs --registry crystalprodacr --top 3 -o table
  az acr repository show-tags --name crystalprodacr --repository crystal-backend --orderby time_desc --top 5 -o tsv
  ```
- ACR image references must use the **full login server**
  `crystalprodacr-c3dbbeh2exdec8av.azurecr.io`, not `crystalprodacr.azurecr.io`
  (the short form fails with `no such host`).
- CI-built images are tagged with the **full commit SHA**, so you can map a running
  container straight back to a commit.

---

## 4. Testing

```bash
TEST_DATABASE_URL="postgresql://postgres:PASSWORD@localhost:5432/postgres?sslmode=disable" python -m pytest -q
```

- **Two parallel test runs drop each other's scratch database mid-suite.** If you see a
  burst of `psycopg2` errors and failures across unrelated files, that is almost
  certainly another session running pytest at the same time — not your change. Re-run
  before you diagnose. (Suite was green at 462 passed / 2 skipped as of 2026-09-01.)
- Unit tests placed in `tests/test_stats_shape.py` are `skipif not TEST_DATABASE_URL`
  and will **silently skip**. Put real unit tests elsewhere.
- Lint gate: `python -m ruff check .` and `python -m ruff format --check .` must both
  be clean before a deploy.

---

## 5. Launchpad: generations, pools, and reserves

### nad.fun has two generations, and the API hides it

- `source = 1` → nad.fun **v1**, `source = 2` → nad.fun **v2**.
- **`_api_source()` (`api/api.py`) collapses BOTH generations to `1`.** So a client
  reading `source` can never tell v1 from v2. The raw value is exposed separately as
  **`sourceRaw`**, which is present on `/meta` *and* on list rows. Use `sourceRaw`.

### Graduated tokens live in Uniswap **V3** pools

This is the single most important non-obvious fact in the launchpad code.

- Of the migrated tokens on prod: **~144 are Uniswap V3, ~5 are V2 pairs** (the V2 ones
  are exactly the `source=2` set). A dev-DB sample will tell you "100% V3" — that sample
  is skewed; do not trust it.
- A V3 pool **never emits a `Sync` event**. `launchpad_pools.reserve_*` was originally
  only advanced by the `V2SYNC` handler, so **every graduated token's reserves were
  frozen forever**. Symptoms: stale reserves while the token trades actively, and sells
  reverting with `CallExecutionError: Execution reverted for an unknown reason` because
  the client's CPMM min-out was computed from wrong reserves.
- **Fix in place:** `core/pool_reserve_sync.py` — `pool_reserve_worker()`, a 120s loop
  registered in `indexer_main.py`. One Multicall3 `aggregate3` with **allowFailure=true**
  probes `getReserves` / `slot0` / `liquidity` per pool: if `getReserves` answers it is a
  V2 pair; else `slot0`+`liquidity` means V3. Kind detection and data read happen in the
  same round trip, so there is no `pool_kind` column to maintain and it self-corrects.

### V3 virtual reserves (the maths)

At the current tick a V3 pool behaves **exactly** like a constant-product pair with:

```
x_virtual = L · 2^96 / sqrtPriceX96
y_virtual = L · sqrtPriceX96 / 2^96
```

Feeding those into ordinary CPMM math is not an approximation — replaying 24 real
on-chain sells reproduced actual output with ratio **1.00000**, including a
1,945,528-token sell. Do **not** use `balanceOf(pool)` for a V3 pool: it includes
out-of-range liquidity and was 3% off in the one case measured, and arbitrarily wrong
in general.

**Price orientation trap:** `sqrtPriceX96` encodes `token1/token0`. Native price is
`p01` when `token_is_0` is true, else `1/p01`. Inverting this makes every healthy token
look catastrophically broken (it once "found" 144/145 tokens off by >100x — all noise).

### Pool fees are not all 1%

- V3 launchpad pools: `fee() = 10000` = **1.00%**.
- nad.fun **v2 pair** pools: the effective fee solved from real trades is **~1.607%**
  (factor 0.9832–0.9840). Clients that assume 1% over-predict output by ~0.6% and can
  revert at low slippage.

### Drained pools

Four migrated pools have `liquidity() == 0` (HAHA, MONGU, CHOG, FLOKI) — real V3
contracts with nothing in them. `_apply_live_pool_reserves` now serves explicit
`"0"` reserves for these rather than falling back to frozen curve numbers, so clients
can tell "no liquidity" from "unknown". **No swap can succeed on these regardless of
what you quote.** HAHA is the origin of the famous "59,478x wrong price" number — it is
one abandoned empty pool, not a systemic problem.

---

## 6. PnL and cost basis — the sharpest edges in the codebase

### Where the numbers come from

- **Realized PnL** = `SUM(launchpad_trades.realized_native)`. Per-trade, from trades only.
- **Unrealized 24h** (`storage.pnl_24h`) = `SUM(balance_token × (last_price_native −
  hourly_close_24h_ago))`. This is a **mark-to-market on the balance you hold** — a user
  can make zero trades and still show a large number. It is not derived from trades.
- The two footer/portfolio tiles are **trailing 24h**; the PnL *calendar* is
  **calendar-day in the viewer's timezone** (`/portfolio/{addr}/daily?days=&tz=`).
  They are supposed to disagree. Do not "fix" that.
- Known minor flaw in `pnl_24h`: it multiplies the *current* balance by a *24h* price
  delta, so a position opened an hour ago is credited with the whole day's move.
  Measured impact was negligible (0.3% of total, 80 of 533k positions) — do not chase it
  without new evidence.

### The basis ledger

`state.py` keeps an in-memory overlay (`_basis_for` / `_basis_apply_buy` /
`_basis_apply_sell` / `_basis_apply_transfer`) that is cleared every new block and reads
through to `storage.get_position_basis()`, which computes
`open_tokens = token_bought − token_sold` and `cost_basis_native` from
`launchpad_positions`.

**Basis travels with transfers, live.** `apply_token_transfer` moves basis from sender to
recipient on ordinary wallet→wallet moves, skipping venues, internal addresses, and legs
inside a trade tx. `launchpad_transfers` is only the **historical extraction table** used
by the one-time backfill — it is *not* the live mechanism, and its block coverage lagging
prod is **not** evidence that live transfer-basis is broken.

### The trader-repair trap (fixed 2026-09-01, but understand it)

`scripts/repair_trade_traders.py` reassigns trades that were credited to a router or
settler back to the real wallet. It used to do **only**:

```sql
UPDATE launchpad_trades SET user_address = %s WHERE txhash = %s AND log_index = %s
```

It moved the *trade* but never touched `launchpad_positions`. The buy's cost basis stayed
stranded on the router while the real wallet got a trade row with no basis behind it — so
the next sell subtracted nothing and **booked the entire sale proceeds as profit**. Then
`repair_router_positions.py` deleted the orphaned router rows, destroying the basis for
good. Every repair pass minted fresh phantom profit.

Commit `89e7401` added `rebuild_positions_for_user()` so the repair refolds both wallets
afterward. **Caveat: that helper uses the trades-only `_fold`,** so it can itself clobber
transfer-carried basis. It should be switched to the transfers-aware fold.

### Which rebuild script to use

| Script | Folds | Use when |
|---|---|---|
| `scripts/rebuild_basis_with_transfers.py` | trades **+ transfers** | **authoritative.** Any wallet that may have received tokens by transfer. |
| `scripts/rebuild_positions_pnl.py` | trades only | only when you know there are no transfers. Has `--user`, `--apply` (dry-run by default). |

Both rewrite per-trade `realized_native` as well as the position row.

**Do not run `rebuild_positions_pnl.py` broadly.** It was run across 673 wallets on
2026-09-01 and corrected 548 position rows, but ~1,400 of the positions it touched
involve transfers, where a trades-only fold can drop basis. No before-snapshot was taken.
Re-running those wallets through the transfers-aware script supersedes it.

**Detection query for the phantom-profit signature:**

```sql
SELECT * FROM launchpad_trades
WHERE is_buy = false AND realized_native > 0 AND realized_native = native_amount;
```

Be careful interpreting it: a sell where the wallet **sold more than it ever bought** is a
legitimate zero-basis sale (tokens arrived by transfer/airdrop), not a bug. Only rows where
the wallet still had open *bought* tokens are real defects. On 2026-09-01 the split was
8,437 legitimate vs **4** real.

### Performance note

Per-wallet repair via a subprocess per wallet runs at ~0.6 wallets/min (Python startup +
Azure SSL connect each time) — 673 wallets projected to **17 hours**. Batching all wallets
into one process with a single connection and `WHERE user_address = ANY(%s)` did the same
work in **165 seconds**. Always batch.

---

## 7. Security posture (know this before you publish anything)

- **This backend repo is public**, and `.env` with live production DB credentials is in
  the tree. Treat every credential you see here as already exposed, and never widen the
  blast radius (do not paste them into new files, issues, or external services).
- There is no API authentication, CORS is open, and there are no rate limits.
- The twitterapi.io key was exposed publicly and the account has been returning 402 since
  2026-08-29, so the X/socials pipeline is frozen.

---

## 8. Useful verification recipes

**Is the reserve reconciler alive?** Actively-traded migrated pools should have
`last_sync_at` within ~5 minutes:

```sql
SELECT COUNT(*) FILTER (WHERE extract(epoch from now())::bigint - p.last_sync_at < 900),
       COUNT(*)
FROM launchpad_tokens t JOIN launchpad_pools p ON p.token_addr = t.token
WHERE t.migrated;
```

**Is anything rewriting the DB right now?**

```sql
SELECT pid, state, now() - query_start AS runtime, LEFT(query, 120)
FROM pg_stat_activity
WHERE datname = current_database() AND state <> 'idle' AND pid <> pg_backend_pid()
ORDER BY query_start;
```

**Replay a real trade to validate quoting math** — read pool state at `block - 1`, apply
the client formula, compare to the actual amounts in the trade row. Two traps:
1. If **two trades landed in the same block**, the `block-1` snapshot is stale for the
   second one and it will look like a mismatch. Check
   `COUNT(*) ... WHERE block_number = X` first.
2. A genuine V3 **tick crossing** (liquidity differs between `blk-1` and `blk`) makes
   virtual-reserve CPMM deviate. That is structural, not a bug; worst observed was 0.62%.

The public RPC is **not a deep archive** — state reads fail beyond a few hundred thousand
blocks back, so historical replay is limited to recent activity.

---

## 9. Open items / known gaps

- `rebuild_positions_for_user()` in `scripts/repair_trade_traders.py` uses the trades-only
  fold and should use the transfers-aware one.
- The 673 wallets rebuilt on 2026-09-01 with the trades-only fold should be re-run through
  `rebuild_basis_with_transfers.py`.
- `origin/block-scoped-clear` is 249 ahead / 446 behind `main`, last touched 2026-08-05.
  Long abandoned; merging it is a project, not a chore.
- `launchpad_transfers` historical coverage lags prod by a few hundred thousand blocks.
  This limits the *backfill* script only, not live basis carrying.
