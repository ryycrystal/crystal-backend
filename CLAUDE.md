# CLAUDE.md — everything an agent needs to know about this repo

This file is the collected working knowledge of the agents that built and operated this
backend. It is deliberately blunt about traps. Read it top to bottom before touching
prod. Deeper docs: `README.md` (operator guide), `ARCHITECTURE.md`, `STARTUP_MODES.md`
(indexer start modes), `API.md`, `DESIGN.md`.

---

## What this is

One Python codebase, two processes built from one Docker image:

- **Indexer** (`indexer_main.py`) — follows Monad, writes derived state to Postgres.
  Exactly one replica, guarded by a Postgres advisory lock. Owns `init_db()` (all
  schema migrations).
- **API** (`main.py`, uvicorn) — reads Postgres, serves REST + WebSocket. 1–3 replicas.
  Never talks to the indexer.

It serves TWO frontends: the main interface (app.crystal.exchange) and **crystal.fun**
(the crystal-only launchpad fork). They hit different, near-duplicate route handlers —
see "The two token-overview handlers" below, this has burned us more than once.

Chain: Monad mainnet. Blocks ~400ms and heading to 300ms. Prod DB host is
**crystal-prod-db-r3** (Azure Postgres Flexible) — an older host named crystal-prod-db
may still answer; it is stale and misleading, never use it.

---

## Deploying (and the traps)

Day-to-day flow is in `README.md` §6. The short version: ruff check + format --check +
full pytest green, `az acr build` to `crystalprodacr`, then `az containerapp update` —
**indexer first if the change touches schema**, then API. Resource group
`crystal-prod-rg`. Image refs must use the FULL login server
`crystalprodacr-c3dbbeh2exdec8av.azurecr.io`, not the short form.

There is now also CI/CD: lint runs on `dev` and PRs, and pushes to `main` deploy to
Azure with **the commit SHA as the image tag**, behind a production-environment
approval gate. So the deployed image tag == the commit it runs. Check what's live with:
`az containerapp revision list -n crystal-api -g crystal-prod-rg` and compare the image
tag to `git rev-parse origin/main`.

Traps, all hit for real:

- `az acr build` on Windows reliably CRASHES with a `UnicodeEncodeError` while
  streaming build logs. **The build itself still runs.** Never re-trigger blindly —
  poll `az acr task list-runs --registry crystalprodacr --top 1` for status instead.
  Prefix every `az` call with `PYTHONIOENCODING=utf-8`; it reduces but does not
  eliminate the crashes.
- Registry uploads can get connection-reset if another agent is building
  concurrently. Retry; check `list-runs` to see whose build is in the queue.
- The `/tokens?since_block=1` `as_of_block` reads through
  `ttl_cache("tokens:list", ttl=3s, serve_stale=30s)`, so it can look FROZEN for ~30s
  while the indexer is advancing fine. Confirm indexer progress from its `[SQ] <block>`
  log lines, never from the cached API.

### The indexer restart deadlock (incident 2026-09-01, ~27 min stalled)

`init_db()` used to run its whole DDL in one transaction. `ALTER TABLE ... ADD COLUMN
IF NOT EXISTS` and `CREATE OR REPLACE VIEW launchpad_positions_live` take
AccessExclusiveLock **even when they change nothing**, so restarting the indexer while
the API serves reads is a deadlock lottery. When it loses, the process dies,
Container Apps restarts it, and it deadlocks again — indexing stops entirely.

Fixed: `init_db()` now runs **statement-by-statement in autocommit**
(`db_autocommit_cursor`), with `lock_timeout='5s'` and up to 20 retries with backoff
(`core/storage/schema.py`). Proven: a later deploy restarted the indexer under full
API traffic and it went straight to streaming blocks.

If it ever still loses the race (log line `init_db contended with live readers`):
scale the API down briefly —
`az containerapp update -n crystal-api -g crystal-prod-rg --min-replicas 0 --max-replicas 1`
(max must be ≥1, 0 is rejected), wait for `[SQ]` lines to appear, then restore
`--min-replicas 1 --max-replicas 3`. ~1–2 min of API downtime beats stalled indexing.

---

## The two token-overview handlers (the #1 recurring trap)

There are TWO near-duplicate implementations of the token overview:

- `api/routes/fun.py` → `GET /fun/token/{addr}/{chartres}` — **what crystal.fun calls**
- `api/routes/launchpad.py` → `GET /token/{addr}/{chartres}` (main interface) and
  `GET /chart/{addr}/{chartres}`

Each carries its own copy of the chart-resolution guard and other logic. Three
separate commits once "fixed" chart behavior for crystal.fun by editing only
`launchpad.py` — they had zero effect until `fun.py` was patched too. **When changing
token-overview/chart behavior, grep every route file**
(`grep -rn "chartres not in" api/`) and fix all copies. Shared helpers in `api/api.py`
(`_build_ohlcv_from_db`, `_scaled_price`, `_apply_live_pool_reserves`) do apply to both.

`/fun/token` response notes:
- `monUsd` is the global MON/USD rate window `{resolution, from, to, before,
  points:[{t, rate}]}` — `t` in seconds, `before` a plain float rate or null.
- The payload has **NO `holders` key**. crystal.fun's holders come from the WS
  `holders` channel, with `/holders/{token}` as the REST fallback.
- Trades' `"block"` field is misnamed — it carries the unix **timestamp**, not a block
  number. Both frontends rely on this; do not "fix" it.

---

## Units and scaling (memorize these)

- Wei everywhere for token/native amounts: divide by 1e18.
- `_scaled_price()` multiplies price by **1e9**. Because total supply is 1e9,
  `lastPriceNativePerTokenWad`, `marketcap_native_raw`, `athMarketcap`, and
  `price_ref_*` are all numerically the MON market cap. Frontends divide by 1e9 to get
  a per-token price.
- `graduationPercentageBps` is misnamed: it is a **0–1 fraction**, not basis points.
  `progressBps` is real bps (0–10000), server-clamped, forced to 10000 once
  migrated/graduated. Prefer `progressBps`.
- `circulating_supply` counts curve-sold tokens; graduation moves 800,000,000 of the
  1e9 for crystal tokens (793,100,000 for nad.fun).
- Curve generations: `CRYSTAL_LAUNCHPAD_GEN` env (1 or 2) selects supplies in
  `core/adapters/native.py`. Gen 2 adds a virtual supply V=ceil(2e26/3):
  initial 1e27+V, graduated 2e26+V. The flag is set on both container apps.

## Stats windows semantics

`token_stats` (`api/routes/launchpad.py`) serves 5m/1h/6h/24h windows.
`base_price = refs[window] or first_price`: for a token **younger than the window**,
`change_pct_X` silently equals change-since-launch (all windows show the same number).
This was audited and deliberately KEPT — it matches what every DEX screener shows.
`price_ref_*` carry forward through quiet periods, so a quiet token doesn't lose its
baseline. Frontends gate on `price_ref_X > 0` before trusting `change_pct_X`.

## OHLCV / chart contract

- Stored intervals (written by `state.py` INTERVALS): 1s,5s,15s,1m,5m,15m,1h,4h,1d.
- 12h (43200) and 1w (604800) are AGGREGATED on read in `_build_ohlcv_from_db` from
  the nearest stored divisor. All three resolution guards accept them.
- **Buckets exist only for intervals that contained a trade.** There are NO seed
  candles anymore (removed deliberately): a never-traded token returns an empty klines
  array, a one-trade token returns exactly one candle. Clients render sparse series
  themselves. Do not reintroduce `_initial_price_kline` fallbacks — it is dead code.
- `_mon_usd_window` is token-independent (global rate series). It is snapped to
  resolution boundaries and cached (`ttl_cache("mon_usd:window")`) so every token/poll
  shares one computation. Never call it with a raw `now` end again — that made the key
  unique per second and uncacheable.

---

## Caching and the pool

- `TTLCache` in `api/api.py` is lock-guarded (it once had a concurrent-expiry
  `KeyError` → 500 race — do not remove the lock).
- Edge cache: the middleware in `api/api.py` sets
  `public, s-maxage=1, stale-while-revalidate=2` for prefixes in `_EDGE_CACHEABLE`
  (includes `/fun/token/`), `no-store` otherwise. **Never add `/fun/user/` or any
  per-wallet endpoint to that list** — a public CDN cache would serve one user's
  portfolio to another.
- The psycopg2 pool (`core/storage/base.py`) waits up to `DB_POOL_WAIT_SECONDS`
  (default 10) for a connection instead of instantly raising `PoolError` at the cap
  (`DB_MAX_CONN`, default 25). Overload now shows up as latency, not 500s.
- Snipers queries: never write `WHERE LOWER(token) = %s` — every writer lowercases
  already, and `LOWER()` defeats the index and forces a sequential scan (this was live
  on every token page load once).

## Holders endpoints

- `/holders/{token}` and the WS `top_holders` both use `balance_token > 1` (wei) so the
  count matches the list. The WS list is capped at 50 (`ws_data.py HOLDERS_LIMIT`).
- Curve/pool addresses and the denylist are excluded via `_sql_not_internal` — so a
  bonded token's supply held by the curve is INVISIBLE in holders and percentages
  won't sum to 100. Known and accepted for now.
- `/user/{addr}?include_native=1` returns all-time per-token rows: `total_pnl_native`
  (wei) = `realized + unrealized`, `token_bought/sold`, `balance_token`,
  `native_spent/received`, plus `native_balance` (3s RPC cache). Max-sells leave
  wei-dust remainders; crystal.fun classifies sold-vs-active with a RELATIVE dust test
  (`remaining > max(1e-6, bought×1e-9)`), so don't "fix" tiny nonzero balances.

---

## WebSocket contract (api/ws.py)

- Channels per token: `token`, `stats`, `trades`, `holders`, `top_traders`,
  `dev_tokens`, plus board-level channels. `seq` increments per (token, channel) per
  subscriber; a gap means the client must rebaseline via REST.
- Payload row keys (`upserts` / `added` / `devTokens`) are load-bearing for the
  frontends' `applyRows` reducers.
- Trade ids are `{txhash}-{log_index}` on both REST and WS — that's the dedupe key.

## Testing

- `TEST_DATABASE_URL="postgresql://postgres:<pw>@localhost:5432/postgres?sslmode=disable"
  python -m pytest -q` — green suite is the deploy bar (~470 tests). Local dev DB
  setup: `dev-env.ps1` (points DATABASE_URL at local `crystal_dev`).
- The integration harness (`tests/test_launchpad_integration.py`) creates/drops a
  scratch DB per module and truncates `LAUNCHPAD_TABLES` between tests via the
  autouse `clean` fixture. That list MUST include every table tests write through —
  `launchpad_ohlcv` and `spot_graph_buckets` were missing once and caused two
  long-standing "flaky" cross-test failures. If a new table is written by
  `state.py` paths, add it to the list.
- Tests that import `db`/`clean` from the integration module must list `clean` in
  their signature to get isolation; many only take `db`.
- ruff is the gate: `python -m ruff check .` and `format --check .`. CI pins the ruff
  version; keep local matching.

---

## Security (standing, unresolved)

- **This repo (`ryycrystal/crystal-backend`) is PUBLIC.** A hardcoded twitterapi.io key
  existed in history (no key literal remains in tracked source as of 2026-09-02 — it is
  read from env now, but history still has it). Flip the repo private.
- **`bhealthyfences/points-backend` is not a separate repo — it REDIRECTS to
  `ryycrystal/crystal-backend`.** It was renamed/transferred, and GitHub still resolves
  the old path. Verified 2026-09-02: `gh api repos/bhealthyfences/points-backend` returns
  `full_name: ryycrystal/crystal-backend`.

  This matters because the local folder **`nadfun backend`** still has that old URL as its
  `origin`. It holds 174 commits of unrelated work, **none of them pushed**, and
  `git ls-remote origin` from that folder returns *this* repo's branches (`main`, `dev`,
  `block-scoped-clear`). So `git push` from `nadfun backend` aims 174 unrelated commits at
  this public repo. A plain push is rejected as non-fast-forward; a forced one would be
  destructive. **Repoint that remote before anyone pushes from it.** Do not describe it as
  "a stale copy you shouldn't push to" — it is the same repo under an old name.
- The API has no auth, open CORS, and no rate limits.
- The R2 image uploader worker (`launchpad-api.bhealthyfences.workers.dev`, outside
  this repo) accepts unauthenticated PUTs from any origin with derivable keys
  (`img/{TICKER}-{ms}.{ext}`) — anyone can overwrite any coin's artwork.

## Known-open items (audited, reported, deliberately not yet fixed)

- The frontends' 3-second receipt race (in the interface repos, not here) reports
  slow-but-landed transactions as failures — duplicate-trade/launch hazard.
- `token_overview_graph` in `launchpad.py` pulls every holder row into Python for one
  creator balance (`_holders_for_token`, no LIMIT) — `fun.py` has the cheap version;
  the launchpad handler was never updated.
- `/holders?sort=pnl` computes `crystal_unrealized_pnl` for every row then sorts —
  the pnl index is dead because the live-view column shadows it.
- Launch gas is a hardcoded 15M limit and Monad charges the LIMIT, not usage: every
  launch overpays ~0.15 MON, and an over-limit tx burns the full 1.5 MON.

## Frontends this backend must not break

- **crystal.fun** (repo `crystal-fun`, github CrystalExch/Crystal-Fun, PRIVATE,
  auto-deploys from main): only `source == 0` tokens, calls `/fun/*`, `/holders`,
  `/stats`, `/user`, `/tokens/feeds?source=0`. Chart = eight pure resolutions
  1m…1w, MON/USD toggle converts per-candle via the `monUsd` window.
- Main interface (repo `crystal interface`, app.crystal.exchange, auto-deploys):
  calls `/token/*`, `/chart/*`, plus vaults/pools/orderbook/rewards surfaces.

## Rewards / vaults (adjacent systems living in this repo)

- Points engine is backend-only by design (rates in `core/rewards.py`, weekly ^0.8
  close Wednesday 12am PT, kv/campaign config seams). Accrual is serialized on an
  advisory lock. Launch target was 9/8 with vaults.
- Vault APY is strategy-yield at constant prices for stable-quote vaults; the
  reconciliation report cross-checks flow ledger vs indexer tables vs chain.
- Graduated (V3) pools never emit V2 Sync — live reserves come from a virtual-reserve
  reconciler, `reservesFrom: "crystal_pool"` in payloads.

## Working conventions

- Commit style: lowercase, one descriptive sentence, no co-author trailers. Commit and
  push freely; `dev` should track `main` (PR-merge them together).
- **Zero code comments anywhere** — the repo owner removes them on sight. Write
  self-explanatory code instead. (The rare existing comments predate the rule or
  document non-obvious constraints; don't add more.)
- Multiple agents share these working trees concurrently. `git status` before staging,
  stage explicit paths, never `git add -A` with unknown dirt, split shared-file diffs
  by hunk if needed, `git pull --rebase --autostash` before pushing.

---

## 5. Indexer architecture

`core/sequencer.py` pulls logs and dispatches them; `state.py` holds in-memory state and applies the effects.

**Logs are fetched by topic, with no address filter** (`backfill.py`, `fetch_logs_http`). If you are wondering "does the indexer even see event X", check whether X's topic is in `h.TOPICS` — do not assume an address filter is excluding it.

**Event tags are abbreviated.** The ERC-20 `Transfer` handler is tag **`TF`**, not `"TRANSFER"`. Grepping `EVENT_SIGS` values for `"TRANSFER"` returns nothing and will lead you to the false conclusion that transfers are not indexed. They are: `TF` is the standard `0xddf252ad…b3ef` topic and it is in `h.TOPICS`.

### Chunked processing and the BatchAccumulator

The sequencer processes a **chunk of blocks** at a time. Position writes inside a chunk accumulate in a single `BatchAccumulator` and are flushed **once at the end** of the chunk:

```python
batch = BatchAccumulator()
for blk in range(chunk_start, chunk_end + 1):
    self._process_block(blk, logs, cur=cur, counts_out=counts, batch=batch, record_processed=False)
batch.flush(cur)
self._state.basis_clear_overlay()
```

**This is the single most important structural fact in the indexer**, because anything that re-reads `launchpad_positions` mid-chunk sees pre-chunk data. That is exactly what caused the cost-basis bug below.

### Sequencer/state filter mismatch (latent, currently harmless)

`core/sequencer.py` admits a transfer if the token is in `launchpad_tokens` **or** `token_to_v3_pool` **or** is an LP token. But `state.py` `apply_token_transfer` then does a hard `return` when `launchpad_tokens.get(token) is None`. So a token qualifying *only* via the pool branch would have **both legs of every transfer silently dropped**.

Measured 2026-09-02: **0 orphan pools out of 30,515** (`launchpad_pools` rows whose token is absent from `launchpad_tokens`), so this explains none of the observed drift. It is a real one-line defect worth fixing, but do not chase it as the cause of a balance problem — it currently has no footprint. Note also that `load_tokens_for_state` has no `WHERE` clause, so graduated tokens *are* loaded into `launchpad_tokens`.

---

## 6. Cost basis and PnL — the part that has been wrong most often

PnL here is **always cost-basis PnL**. `crystal_unrealized_pnl(hold, bought, sold, basis, price)` is a SQL function; unrealized and realized must agree with the position row's `cost_basis_native`.

### The chunk-boundary basis bug (fixed, commit `6b9fb7a`)

`State._basis_reset_if_new_block` used to clear the cost-basis overlay on **every block** and re-seed it from `launchpad_positions`. Because the sequencer batches an entire chunk before flushing (section 5), a buy in block N and a sell in block N+k of the *same chunk* caused the sell to re-read a positions row that **did not yet contain the buy**. Zero basis was released, so the sell booked **the entire proceeds as realized profit**.

The fix lets the overlay survive the chunk when a batch is carrying the writes, and has the sequencer clear it explicitly after the flush:

```python
def _basis_reset_if_new_block(self, blk: int, batched: bool = False) -> None:
    if batched:
        self._basis_block = blk
        return
    if blk != self._basis_block:
        self._basis_block = blk
        self._basis_overlay.clear()
```

Regression tests: `tests/test_basis_batch_boundary.py`. They fail without the fix — keep them.

**Finding the real footprint of a basis bug: use a tight query, not a loose one.** The loose signal "any sell where `realized == native_amount` and the wallet ever bought that token" gave ~12,000 rows / 8.3M MON and was a bad overcount — it sweeps in legitimate zero-basis sells of tokens that arrived by transfer. The true signature also requires a buy of **at least the sold quantity shortly before the sell**: 211 sells / 122k MON within 100 blocks, 557 / 330k within 500, 954 / 484k within 2000.

### `in_trade_tx` does not affect balances

In `apply_token_transfer`, `in_trade_tx` gates only `move_tokens` / `released` — i.e. **cost basis**. The balance legs `adjust(from, -amount, …)` / `adjust(to, +amount, …)` run unconditionally. If you are debugging a *balance* discrepancy, `in_trade_tx` is not your cause.

### `apply_token_transfer` does not write `launchpad_transfers`

**`launchpad_transfers` is populated only by `scripts/extract_transfers.py`.** The indexer's transfer handler adjusts positions and never inserts into that table. So comparing a position's balance against `launchpad_transfers` measures that script's coverage as much as the indexer's behaviour — it is **not** a clean test of the balance path. This cost real debugging time; do not repeat it.

---

## 7. Repair scripts — dangerous, read before running

These all write derived rows directly:

`rebuild_basis_with_transfers.py`, `rebuild_positions_pnl.py`, `repair_drained_positions.py`, `repair_trade_traders.py`, `repair_router_positions.py`, `reconcile_position_balances.py`, `vault_reconcile.py`.

**If two of them run at once they last-writer-win on the same rows and neither result is trustworthy.** Coordinate before starting one — there are frequently other agent sessions active. (Deploying backend images never conflicts with a running repair.)

- **`reconcile_position_balances.py`** — sweeps `balance_token` to chain truth. Reads `balanceOf` in 300-position multicall3 batches **pinned to the indexer head block**, so a trade landing mid-sweep cannot write a balance newer than the deltas the indexer will apply. Keyset-paginated (5,000/transaction). ~190 positions/sec across 6 read workers with serial writes; a full sweep is ~38 min. Resumable via `launchpad_kv` key `reconcile_balances_at`. Last full run: 450,522 checked, 17,889 corrected, 12,085 zeroed.
- **`rebuild_basis_with_transfers.py`** — re-folds a token's whole trade+transfer history. **Impractical at full scope: ~0.4 tokens/sec over ~32k tokens is roughly 24 hours.** Scope it with `--tokens-file`. Defaults to dry-run; `--apply` is opt-in. **Trap: its resume key `rebuild_basis_transfers_at` can hold a stale near-end-of-alphabet value from a previous run, in which case it silently skips almost everything — pass `--restart`.**

Benchmark honestly: an early runtime estimate here was off by 16x because it was measured on reads without the writes.

---

## 8. Domain systems living in this repo (quick map + facts that cost time to learn)

### Contract generations and the migration history
- Gen-3 core router is `0x6eb2aF5FC575689053Ac9b413220CaBfd01A2F9A` (Aug 28 migration).
  Event topics changed at that migration and the `Migrated` event was REMOVED — old
  topic assumptions silently match nothing.
- nad.fun has two curve generations with different emitters (v1 `0xA728…/0x6F6B…`,
  v2 `0x9f38…/0x8986…`); as of late Aug 2026 create activity shifted BACK to the v1
  emitter. The `source` column is written correctly per generation — trust it.
- **`--clean` on the indexer is a FULL derived-state wipe.** It once destroyed prod
  derived data and required PITR into what is now crystal-prod-db-r3. Never run it
  against prod casually; read STARTUP_MODES.md first.

### Transfers carry cost basis
Token transfers move basis with the tokens (live + history pipeline). Consequence:
`token_sold > token_bought` on a position row is LEGITIMATE (tokens received by
transfer then sold) — it was once "repaired" as corruption; it is not. The 8/30
settler-transfer-leg bug that drained basis on 1ct sells is fixed and its 175K-row
repair is done.

### Fee accounting
`fees_usd` on trades/tokens is the CURVE fee, not Crystal's 1% frontend cut — the
frontend cut is not indexed at all. So fees will never reconcile to 1% of volume;
that's expected, not data loss.

### X (twitter) tracker
Lives in the backend since 8/28: `/x/track`, `/x/tweets`, `/x/ws`, with a
leader-elected poller across API replicas. The old localhost tracker is redundant.
The twitterapi.io key it uses was once hardcoded in this public repo (see Security).

### Volume fee tiers
Basic/Bronze/Silver/Gold/Diamond by launchpad USD volume. The `volume_tiers` table is
deliberately SQL-editable config; `/tiers` endpoints serve it; the referrals page
renders it. Referrals otherwise: the CONTRACT fee mechanics exist, but there are zero
live backend data services behind most of the referrals UI — known rebuild gap.

### Rewards launch hardening (pre-9/8 review, since fixed in code)
Week-close and leader election had two concurrency bugs (now locked), the predeposit
boost fails CLOSED, and the rewards denylist + bonus-vault list are managed over
authenticated-ish admin HTTP endpoints instead of SQL against prod. An EMPTY denylist
is a trap: it silently includes internal wallets in rewards — seed it on any fresh
environment.

### DB capacity
crystal-prod-db-r3 is the ONLY server. Storage grows ~831 bytes per cached block;
autogrow is on. The log cache once had 18.4M holes which made tokens vanish from
derived state — there is a guard now; if tokens "disappear", check log-cache coverage
before anything else.

### Endpoint inventory crystal.fun actually calls (break these, break prod)
`/fun/token/{addr}/{res}` (+`?tracked=`, `?series=false` on polls), `/fun/user/{addr}`
and `/fun/user?addresses=`, `/user/{addr}?include_native=1`, `/holders/{token}`,
`/stats/{token}`, `/tokens`, `/tokens/feeds?source=0&limit=100`, `/price/mon`, and the
WS channels listed above. `/tokens?since_block=` is also the indexer-liveness probe.
