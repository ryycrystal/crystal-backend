# CLAUDE.md — everything an agent needs to know about this repo

This file is the collected working knowledge of the agents that built and operated this
backend. It is deliberately blunt about traps. Read it top to bottom before touching
prod. Deeper docs: `README.md` (operator guide), `ARCHITECTURE.md`, `STARTUP_MODES.md`
(indexer start modes), `API.md`, `DESIGN.md`.

---

## Current state snapshot (2026-09-02, update me when it changes)

- Deployed: both container apps run the image tagged with origin/main's commit SHA via
  the new CI pipeline (approval-gated). Verify with `az containerapp revision list`
  vs `git rev-parse origin/main` rather than trusting this line.
- Branches: `dev` == `main` (kept in sync by PR). `block-scoped-clear` is a stale
  Aug-28-migration-era branch, 400+ commits behind — historical, don't build on it.
- crystal.fun v1 shipped 9/1 (repo `crystal-fun`, auto-deploys from main). Vaults +
  rewards launch was targeted for 9/8 07:00 with the interface work in
  `crystal interface`.
- Recent full audits (9/1–9/2) of crystal.fun + these fun/launchpad routes fixed:
  reverted-trade-as-success, MAX-buy gas shortfall, edge-cache exclusion of
  `/fun/token/`, the snipers LOWER() seq scan, TTLCache race, mon-usd window
  caching, holders threshold mismatch, and the init_db restart deadlock. Known-open
  items are listed in their own section below — they are reported, not forgotten.
- A stale PUBLIC copy of old backend history lives at bhealthyfences/points-backend;
  a local clone of it ("nadfun backend" folder) holds ~170 obsolete commits that must
  never be pushed.

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

## Watched contract addresses — the failure mode that keeps recurring

**A wrong or incomplete watched-contract address fails silently, and fixing the code does
not repair history.** Blocks are processed exactly once. If the indexer was not watching an
address when its events went by, those events are gone from the derived tables forever —
no error, no gap, no retry. You find out when a number looks wrong downstream, often days
later.

It has caused at least four separate incidents:

- The backend indexed the **wrong referral manager**, so `referral_bindings` sat empty.
- Two vault withdrawals were dropped because they came from the **legacy factory** before
  the acceptance rule included it. The ledger stayed permanently ahead of chain, surfacing
  as a phantom **-$14,948** PnL on a vault that never held that much.
- A local `.env` pinned a **stale** `VAULT_FACTORY_ADDRESS`, so a replay script silently
  skipped every event from the current factory and reported a confident false mismatch.
- The vault contract **closes a vault with no event at all**, so nothing was watching for a
  state change that genuinely happens on-chain (see the vault section).

Rules that follow:

1. Set contract addresses **before** the contract's first block, never after.
2. Prefer the **committed defaults** in `core/chain.py` over Azure env vars. Env
   `VAULT_FACTORY_ADDRESSES` **replaces** the whole list — it does not append — so a
   partial env value silently stops indexing older generations. Prod deliberately sets no
   vault env at all and relies on the code defaults.
3. `accepts_log_for_indexing(tag, addr)` in `core/chain.py` is the single gate every
   dropped event passes through. Read it before changing any address or event tag.
4. When you change an acceptance rule, ask "what history is now wrong?" and repair it
   explicitly. The integrity sweep exists to make that damage visible; it does not fix it.
5. Verify which addresses a process actually resolved before trusting its output:
   `python -c "from core import chain as h; print(h.VAULT_FACTORY_ADDRS, h.CRYSTAL_ADDR)"`.

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
  Exact gen-2 values, worth having when you compare against a chain read:
  `V = 66666666666666666666666667`, `initial_curve_supply = 1066666666666666666666666667`
  (note the trailing **667**, not 666 — an off-by-one here produced a wrong root-cause
  diagnosis once; see below), `CURVE_SUPPLY = 8e26`.

### `curve_state()` returning None silently blanks reserves, permanently

`NativeLaunchpadAdapter.curve_state(ev)` (and the nad.fun equivalent) returns `None` when
the event has no usable reserves — including the `int(ev.get("token_reserve") or 0)` case
where the field is simply **absent**, which lands in the `token_reserve <= 0` branch. When
it returns `None`, `state.py` never assigns `lp.curve_native_reserve` /
`lp.curve_token_reserve`, so the row keeps its `0` default. There is **no repair path and
no log line** — the row stays blank until some later event for that token happens to carry
reserves. A fully-traded token can therefore sit at `reserveQuote: 0, reserveBase: 0`
indefinitely, which reads downstream as "never traded".

Symptom to recognise: a token with `tx.total > 0` but both reserves `0` in
`/tokens/feeds`. Verify against chain before theorising — the launchpad core answers
`0x7b06271a(address)` returning `(native_reserve, token_reserve)`:

```bash
curl -s -X POST -H 'content-type: application/json' https://rpc.monad.xyz \
 -d '{"jsonrpc":"2.0","id":1,"method":"eth_call","params":[{"to":"0x6eb2af5fc575689053ac9b413220cabfd01a2f9a","data":"0x7b06271a000000000000000000000000<TOKEN_NO_0x>"},"latest"]}'
```

Word 0 is native, word 1 is token. Cross-check the decode against a token whose DB row is
already correct before trusting it.

#### Correction: commit `d11e7b5`'s message is wrong

That commit says it keeps reserves "when a fully sold back token overshoots its initial
supply by a wei". **That is not what happened and the guard was not the cause.** The token
in question (CHIRP) had an on-chain `token_reserve` of `1066666666666666666666666667`,
which is *exactly* `initial_curve_supply` under gen 2 — the old guard `token_reserve >
initial_curve_supply` evaluates false and **accepted** it. The "+1 wei" came from an agent
hardcoding the constant as `…666` while checking. The real cause was almost certainly
absent reserve fields on that token's events (the `<= 0` branch above); it was never
proven. The row was fixed by a manual `UPDATE`, not by the code change.

The code change in `d11e7b5` is still worth keeping — it widens the ceiling to `* 2` and
clamps `tokens_sold` to `max(..., 0)` in the nad.fun adapter, which fixes a genuine
negative-value path — but **do not cite it as the fix for blank reserves.** Four tests
across `tests/test_launchpad_gen2.py` and `tests/test_lifecycle.py` had asserted
`IC + 1 -> None`; they were updated to assert `tokens_sold == 0` for a small overshoot and
`None` only for absurd values (`IC * 3`).

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

### The ttl_cache rule that matters most: serve_stale must exceed the query runtime

`ttl_cache(prefix, ttl_seconds, serve_stale_seconds)` (`api/routes/…`) serves a fresh
value inside `ttl`, serves the **stale** value and spawns one background refresh while
within `serve_stale` past expiry, and otherwise **blocks the request on a full
recompute**. `_spawn_refresh` de-dupes per cache key, so concurrent callers cannot
stampede — one refresh runs and everyone else gets the stale value instantly.

**Therefore: if a query takes longer than `ttl + serve_stale`, the stale window closes
before the background refresh ever lands, and every single request pays the full cold
cost.** The cache stops working exactly when you need it most. Measured on
`/tokens/feeds` (2026-09-02) with `ttl=3s, serve_stale=30s` against a query taking
17–52s:

| request | before (`serve_stale=30`) | after (`serve_stale=180`) |
|---|---|---|
| `source=1&limit=10` | 16.8s | — |
| `source=1&limit=30` | **>60s (timed out)** | — |
| `source=1&limit=100` | 52.1s | 6.7s / 10.1s / 2.9s |

Raising only `serve_stale` fixes the blocking **without touching freshness** — `ttl`
stays 3s, so the board's 5s poll still gets 3-second-fresh data. When you meet a slow
cached endpoint, check this ratio before optimising anything else.

### `/tokens/feeds` is query-bound, not serialization-bound

`source=0` (crystal, what crystal.fun calls) is ~1.6s. `source=1` (`source <> 0`,
nad.fun) was pathological. Note it was **16.8s even at `limit=10`** — cost barely moves
with `limit`, so it is the queries, not the row serialization.

Prime suspect, unconfirmed: `launchpad_trades` carries **only** `idx_launchpad_trades_block`
— there is no index on `timestamp` — while the endpoint's `volume_24h` CTE does
`WHERE timestamp >= cutoff GROUP BY token`. For the tiny `source = 0` driving set the
planner can avoid materialising it; for `source <> 0` it cannot. **This was not verified
with EXPLAIN against prod, and no index was added** — adding one runs at indexer startup
against a large table, which is a bigger risk than the slow endpoint. Get an EXPLAIN
first.

### Do not "fix" the source filter

`_normalize_source_filter` accepts **only** `0` or `1` and returns HTTP 400 for anything
else, with a clear message. `source=2`/`source=3` returning 400 is **correct behaviour,
not a bug** — `1` already means "everything that is not crystal" (`source <> 0`). This
was mistakenly filed as a defect once.

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

### Set `SCRATCH_DB_NAME` or you WILL fight the other agents

The integration harness creates and drops a scratch database. The name used to be a
constant, so two agents running the suite at the same time **dropped each other's
database mid-run** — which surfaces as `connection ... server closed unexpectedly`,
`database "crystal_lp_itest" does not exist`, or a test that passes alone and fails in
a full run. It looks exactly like a flaky test and it is not one.

It now reads `SCRATCH_DB_NAME` (default `crystal_lp_itest`). Pick a name unique to you:

```bash
TEST_DATABASE_URL="postgresql://postgres:<pw>@localhost:5432/postgres?sslmode=disable" SCRATCH_DB_NAME="crystal_<yourname>_itest" REWARDS_WORKER=0 python -m pytest -q
```

Also set **`REWARDS_WORKER=0`** so the background rewards thread does not accrue
underneath your assertions.

Two known ordering flakes in `spot_graph`/`stats_shape` fail on an untouched baseline
too — confirm against `git stash` before spending time on them.

**When you add a guard, prove it has teeth:** disable it and watch the test fail. Both
the rewards advisory lock and the vault-gap close guard were verified that way, and the
gap-upsert bug was only found because the test refused to pass for the right reason.
- **A large number of `s` (skipped) means you are not testing anything.** The
  rewards/vault suites are `pytest.mark.skipif` on `TEST_DATABASE_URL`; without it ~158
  tests silently no-op and the suite still looks green.
- **Import order breaks scripts.** `api/api.py` and `api/routes/*.py` import each other,
  so importing a route module first raises
  `ImportError: cannot import name 'router' ... (most likely due to a circular import)`.
  In any script, `import api.api` **before** `from api.routes import ...`, and call
  `storage.init_pool()` or you get `RuntimeError: [DB] Uninitialized connection pool`.
  This is why the test suite passes in-suite but a single module can fail standalone.

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

### Vault contract semantics that are not obvious from the ABI

Source: `crystal contracts-dev/contracts/vaults/CrystalVault.sol` + `CrystalVaultFactory.sol`.

- **Deposits are ratio-matched and refund the excess.** The vault takes the optimal
  amounts for its current reserve ratio, so a user depositing into a differently-weighted
  vault may have only part accepted. Shares are
  `min(quote·supply/quoteBal, base·supply/baseBal)`; the first deposit is `sqrt(quote·base)`.
- **The owner must hold > 1/20 of supply**, enforced on deposit *and* on partial owner
  withdrawal (`require(balanceOf[owner] * 20 > totalSupply)`). An owner can always exit
  **fully**, but cannot sit below 5% while the vault is open.
- **A full owner exit closes the vault and emits NOTHING.** Inside `withdraw()`, when the
  owner's balance hits 0 the contract calls `cancelAll()` and sets `closed = true,
  locked = true` with no event — the factory's `Closed` event only fires for the explicit
  `close()` path. The indexer therefore *derives* this in `state.apply_vault_withdraw`.
  Keep that derivation if you touch the function; without it a dead vault lists as Active
  forever with a deposit button that always reverts.
- **A closed vault is still fully withdrawable.** `withdraw()` gates only on shares and
  lockup — there is **no** `closed`/`locked` check. Closing stops deposits and trading;
  depositors exit whenever. Never treat closed as "funds gone", and never hide a closed
  vault from a user holding shares in it (that made a depositor's funds unreachable in the
  UI while perfectly withdrawable on-chain).
- **`lockup = 0` means the factory maximum**, not "no lockup" — `_createVault` turns 0 into
  `maxLockup()`. Any caller passing 0 silently gets the harshest lockup available.
- **`unlockTimestamp[user]` is stamped at deposit time** with the lockup then in force.
  Recomputing `last_deposit + current_lockup` diverges the moment an owner calls
  `changeLockup` — measured **three days** off on prod. Read `unlockTimestamp(address)`
  (`0x28f0e093`) from chain for anything gating a withdrawal.
- **`getBalances()` returns `(quote, base, availableQuote, availableBase)`** — the total
  includes capital in resting orders. NAV uses totals; the difference is deployed capital.
- **NAV never reads `token.balanceOf(vault)`**, it reads accounted balances — which is what
  makes the vault immune to donation/direct-transfer manipulation. Keep it that way.

### Writing vault data is TWO seams, not one

```
insert_crystal_vault_deposit / insert_crystal_vault_withdrawal   # ledger row only
upsert_crystal_vault_user_delta                                  # shares + counters
```

The live indexer calls both. **A backfill that calls only the first leaves
`crystal_vault_users` behind and nothing notices** — `shares` self-heals against
`balanceOf` in the sampler, so the row looks fine while `withdraws`/`last_withdraw` stay 0
and the UI renders an exited depositor as one who never withdrew. Recompute deltas *from
the ledger* so re-runs are idempotent; use `shares_delta=0` when shares are already
chain-correct.

### Vault invariants — `scripts/vault_reconcile.py`

Asserts five invariants and exits non-zero on failure, so it can gate a deploy:

| # | Invariant |
| --- | --- |
| I1 | chain `totalSupply` == Σ `balanceOf(holder)` |
| I2 | chain `totalSupply` == stored `circulating_shares` |
| I3 | stored `circulating_shares` == Σ deposits − Σ withdrawals |
| I4 | Σ `crystal_vault_users.shares` == chain Σ `balanceOf` |
| I5 | net assets − net contributed == MM PnL (value conservation) |

```bash
python scripts/vault_reconcile.py [0xVAULT] [--json]
```

**Trap — reconciliation that heals to zero.** `state.reconcile_vault_user_shares` sets a
holder's stored shares to whatever a multicall `balanceOf` returns, *including a transient
0*, and then skips that user (they look empty) so it never self-corrects. A real holder can
be silently zeroed. Do not heal a non-zero holder **down** to 0 unless the flow ledger also
nets 0. I4 catches it after the fact; nothing prevents it yet.

### Integrity sweep also watches the vaults

`core/integrity.py` runs in the **indexer** every `INTEGRITY_INTERVAL` (300s) and writes
`integrity_last` to `launchpad_kv`. Beyond lag/gaps/holes it reports
`vault_ledger_divergences` (dropped events), `vault_user_counter_drift` (the two-seam bug
above) and `vault_status_drift` (a vault still open whose owner holds nothing — the silent
close). Healthy is `ok: true` with all three at 0. Check it before concluding anything
downstream is broken.

### Rewards config lives in kv, and the boost is fail-closed

- Timing is **config, not code**: `rewards_vault_start`, `rewards_program_start`,
  `rewards_predeposit_start`, `rewards_predeposit_cutoff`,
  `rewards_predeposit_multiplier` are `launchpad_kv` metas with code defaults. Shifting
  launch dates needs no deploy.
- **The pre-deposit boost applies only to vaults listed in
  `crystal_rewards_predeposit_vaults`.** It used to be fail-*open* — an empty table meant
  every vault qualified, so forgetting to seed it would have handed 3× to the whole
  programme. Seeding the table is now what *enables* the boost.
- `_close_week` takes the same advisory lock the accrual paths take; its `finalized` check
  is check-then-act and would otherwise let two nodes interleave a permanent distribution.
- A week **refuses to close** if vault hours were unvalued by a sampling outage, unless the
  gap is acknowledged. Deliberate: distributions are permanent.
- The **denylist** (`crystal_rewards_denylist`) excludes wallets from accrual *and* the
  close. House/MM/test wallets belong there or they compete with users for the pool.

### Rewards engine in depth (`core/rewards.py`)

Runs as a worker thread inside `crystal-api`, leader-elected via
`crystal_rewards_leader` so only one replica accrues. Raw USD per category is stored
**separately from points** in `crystal_rewards_contrib`, so the inputs stay auditable
after the fact.

Rates are points per USD, or per USD-hour for vaults:

| category | rate | | category | rate |
|---|---|---|---|---|
| `pregrad` | 1.0 | | `stable_taker` | 0.01 |
| `grad` | 0.10 | | `stable_maker` | 0.002 |
| `spot_taker` | 0.05 | | `vault_hour` | 0.05 |
| `spot_maker` | 0.01 | | | |

Weekly close: `adjusted = raw ** 0.8`, then `crystals = pool * adjusted / sum(adjusted)`.
The 1,000,000 pool is **fully distributed by construction** — the exponent redistributes
shares between wallets, it does not shrink the payout. Competition ranking (ties share a
rank), percentile badges off `STATUS_LADDER`, self-cross earns zero.

**The schedule is entirely kv/meta driven — moving a date needs NO redeploy:**

| meta key | default | meaning |
|---|---|---|
| `rewards_program_start` | 9/16 00:00 LA | main accrual begins |
| `rewards_vault_start` | 9/8 07:00 LA | vault accrual may begin |
| `rewards_predeposit_start` | falls back to vault_start | boost window **opens** |
| `rewards_predeposit_cutoff` | falls back to program_start | boost window **closes** |
| `rewards_predeposit_multiplier` | 3.0 | the boost |
| `rewards_max_gap_hours` | 6 | vault-gap close tolerance |

**The cutoff bounds which deposits QUALIFY, not how long the boost lasts** — qualifying
shares keep the multiplier for the whole season. If the open date slips, push the cutoff
out by the same amount or the qualifying window collapses and the incentive evaporates.

**Boost accounting is an ordered replay, not a net-at-cutoff snapshot.** Deposits inside
the window go in a boosted book, deposits outside in an unboosted one, and a withdrawal
spends **unboosted shares first** — so ordinary trimming preserves the boost, but dipping
below what you pre-deposited burns it permanently. Pre-window deposits earning 3x was a
real bug; it is now pinned by tests.

**Concurrency, the subtle one.** Every accrual transaction takes
`pg_advisory_xact_lock(782301944117)` before reading its watermark **through its own
cursor**. `storage.get_meta()` reads on a *separate connection*, so without the lock the
read-modify-write spans two transactions and concurrent workers double-count even with
leader election. `test_concurrent_accrual_cannot_double_count` fails if you remove it.

**Admin surface** — prefix is `REWARDS_PATH_PREFIX` (default `results`), key is env
`REWARDS_ADMIN_KEY` sent as header `x-admin-key`. Reads are deliberately unauthenticated
and nothing in the frontend fetches them.

```
GET  /results/status            schedule, watermarks, vault gaps
GET  /results/gaps              unvalued vault-hours
GET  /results/wallet/{addr}     balances, contrib, grants, badges
GET  /results/volumes/{addr}    raw USD per category
POST /results/denylist          {"address":"0x.."}         admin
POST /results/bonus-vaults      {"vault":"0x.."}           admin
POST /results/acknowledge-gaps  {"weekStart": N}           admin
POST /results/run               force a cycle              admin
```

Two standing decisions people keep trying to reverse:
- **`bonus-vaults` empty means EVERY vault earns the boost.** Anyone can spin up a junk
  vault and farm it. Scoping it is the main outstanding pre-launch config action.
- **The team depositor is deliberately NOT denylisted** (decided 9/1) — the team locks
  its own capital and earns like anyone else. Do not "helpfully" add it. Note it ranks
  #1 in every modelled scenario and rank/status are publicly readable.

### The silent-zero vault guard

If a vault holds shares but cannot be valued for an hour, **everyone in it earns nothing
and nothing downstream says so.** Every other failure mode here is loud — a stalled
indexer just makes the week close *wait*, which is safe. This one pays out quietly wrong,
and a finalized week is permanent.

So: each unvalued vault-hour is recorded in `crystal_rewards_vault_gaps` with a reason
(`no_sample`, `stale_sample`, `zero_value`, `no_supply`) and logged as
`[REWARDS] vault ... unvalued`. A successfully valued hour deletes its own gap row, so a
backfill self-heals. `_close_week` then **refuses to finalize** (returns `None`, week
stays open and retries) if any single vault exceeds `rewards_max_gap_hours` of
`no_sample`/`stale_sample`.

**Only missing/stale samples block.** A fresh sample reading `$0` is a genuinely empty
vault — real data, correctly earning nothing — and must never hold up a payout, which is
why `zero_value` is recorded but not counted. Escape hatch when a gap is real and
accepted: `POST /results/acknowledge-gaps {"weekStart": N}`, scoped per week.

### Vault accounting traps

- TVL comes from `crystal_vault_balance_samples`; a sample older than
  `VAULT_SAMPLE_STALENESS` (24h) does not count.
- **Value a flow from one sample**, never by dividing USD by a rebuilt supply — that
  inflated NAV 28x across a withdraw/redeposit and invented a $15k realized loss.
- The flow ledger and the depositor counters are **two separate write seams**. A backfill
  that calls only `insert_*` leaves counters stale and the drift is invisible. Write both.
- PnL series, list snapshot and APY must share one constant-price basis, or a falling
  base asset reads as strategy performance.

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

## Indexer architecture

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

## Cost basis and PnL — the part that has been wrong most often

PnL here is **always cost-basis PnL**. `crystal_unrealized_pnl(hold, bought, sold, basis, price)` is a SQL function; unrealized and realized must agree with the position row's `cost_basis_native`.

### The chunk-boundary basis bug (fixed, commit `6b9fb7a`)

`State._basis_reset_if_new_block` used to clear the cost-basis overlay on **every block** and re-seed it from `launchpad_positions`. Because the sequencer batches an entire chunk before flushing (see the chunking section above), a buy in block N and a sell in block N+k of the *same chunk* caused the sell to re-read a positions row that **did not yet contain the buy**. Zero basis was released, so the sell booked **the entire proceeds as realized profit**.

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

## Repair scripts — dangerous, read before running

These all write derived rows directly:

`rebuild_basis_with_transfers.py`, `rebuild_positions_pnl.py`, `repair_drained_positions.py`, `repair_trade_traders.py`, `repair_router_positions.py`, `reconcile_position_balances.py`, `vault_reconcile.py`.

**If two of them run at once they last-writer-win on the same rows and neither result is trustworthy.** Coordinate before starting one — there are frequently other agent sessions active. (Deploying backend images never conflicts with a running repair.)

- **`reconcile_position_balances.py`** — sweeps `balance_token` to chain truth. Reads `balanceOf` in 300-position multicall3 batches **pinned to the indexer head block**, so a trade landing mid-sweep cannot write a balance newer than the deltas the indexer will apply. Keyset-paginated (5,000/transaction). ~190 positions/sec across 6 read workers with serial writes; a full sweep is ~38 min. Resumable via `launchpad_kv` key `reconcile_balances_at`. Last full run: 450,522 checked, 17,889 corrected, 12,085 zeroed.
- **`rebuild_basis_with_transfers.py`** — re-folds a token's whole trade+transfer history. **Impractical at full scope: ~0.4 tokens/sec over ~32k tokens is roughly 24 hours.** Scope it with `--tokens-file`. Defaults to dry-run; `--apply` is opt-in. **Trap: its resume key `rebuild_basis_transfers_at` can hold a stale near-end-of-alphabet value from a previous run, in which case it silently skips almost everything — pass `--restart`.**

Benchmark honestly: an early runtime estimate here was off by 16x because it was measured on reads without the writes.

---

## 8. Trade attribution — who gets credited for a trade

A trade event names **whoever called the core**. For a routed trade that is a router,
an aggregator, or the 0x Settler — not the trader. Everything below exists to fix that.

### `_resolve_trade_user` (core/sequencer.py)

Walks the transaction's ERC-20 transfer graph to find the real wallet. Called from
three places: the `TR` (spot) branch and both launchpad branches (`LT`/`NFB`/`NFS`
and the graduated-pool path).

- **Buy**: address with the largest positive net token retention, preferring leaf
  nodes, then deepest-from-pool.
- **Sell**: first sender that is not the pool, the zero address, or a known passthrough.
- Falls back to the event's original `user` when it cannot do better.

### `PASSTHROUGH_ADDRS` — the router list (core/chain.py)

Stateless execution contracts that forward someone else's trade and never hold a
position of their own. Currently the 0x Settler, the 0x AllowanceHolder, and the
referral manager.

```python
PASSTHROUGH_ADDRS = _addrs_from_env([...], "PASSTHROUGH_ADDRESSES")
```

**To add a router:** append to the list, or set the `PASSTHROUGH_ADDRESSES` env var
(comma-separated) — the env route needs no code change and no redeploy. Then clean
history with `python scripts/repair_router_positions.py --apply --force` (with no
`--traders` it defaults to this same list).

Two enforcement points, and the second is the important one:

1. `_resolve_trade_user` treats them like `pool`/`zero_addr` and walks *past* them.
2. **`BatchAccumulator.add_position_delta` early-returns for them.** Every position
   write in the codebase funnels through that single method — launchpad trades,
   graduated trades, and `apply_token_transfer` — so guarding there covers all three
   paths and any future one automatically. Do not scatter equivalent checks at call
   sites.

### The limitation you cannot fix — do not try

Arbitrage bots trade through **their own contracts**. The EOA that signed the
transaction never touches the token, so it is not in the transfer graph at all.

Verified on-chain 2026-09-01 (tx `0x0b93489149a2…`, token LAST): tokens flowed
curve → settler → bot contract → back to curve, with the signer
`0x256efafe…` absent from every transfer. No graph walk can recover a human here.

The only thing naming a person is `tx.from`, which (a) costs an extra RPC per trade
in the indexer hot path and (b) is **wrong for ERC-4337**, where it returns the
bundler. Leave these attributed to the bot's own contract. That is the honest answer
and it is strictly better than crediting shared 0x infrastructure, because the bot
contract is a real distinct actor while the Settler is shared by every user.

If a bot contract gets big enough to pollute a leaderboard, add *it* to
`PASSTHROUGH_ADDRS` and `--force` clears its history. That is the intended escape
hatch. `tests/test_passthrough_positions.py` covers this mechanism.

### EIP-7702 delegated wallets break `eth_getCode` contract checks

A 7702-delegated EOA **has code**, so `getCode(addr) != "0x"` is not a valid
"is this a contract" test — it misclassifies ordinary user wallets. This silently
made a repair script fix **zero** rows.

A designator is exactly `0xef0100` + 20-byte address = **48 hex chars**. Exclude that
shape explicitly:

```python
code.startswith("0xef0100") and len(code) == 48   # this is a WALLET, not a contract
```

Gas follows the same trap: a plain MON transfer to a delegated EOA needs ~**21,212**
gas, not 21,000. Hardcoding 21,000 produces out-of-gas. Estimate per destination.

### `crystal_market_trades` has no `token` column

It keys off `market`. Join `crystal_markets` to reach the base token. A repair script
crashed on this. `launchpad_trades` *does* have `token`, so code that works on one
table does not transfer to the other.

---

## 9. A note on the two knowledge files

`CLAUDE.md` (this file) is the one Claude Code auto-loads into context — treat it as
the primary. `SUMMARY.md` was started in parallel by another session and overlaps
heavily; it has a fuller operational runbook and incident history. If they disagree,
verify against the code rather than trusting either. Ideally they get merged.

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

---

## Working in a shared tree with other agents (read first)

Several agent sessions edit these repos **at the same time**. This is the single
easiest way to destroy work that is not yours:

- **Never `git add -A` or `git add .`** — stage explicit paths only. Otherwise
  you will commit another session's half-finished edits along with your own.
- **Never `git stash`, and never switch branches.** A stash here once swept up
  another session's uncommitted work; it had to be recovered by hand out of the
  stash object. If you genuinely need a branch, use `git worktree add`.
- **Check a file's mtime before assuming it is yours.** A file written seconds
  ago belongs to somebody who is still typing. `stat -c '%y' <file>`.
- Committing a file that another session is mid-edit in is *non-destructive*
  (the bytes on disk are untouched), so when in doubt, commit rather than revert.
  It is `checkout`, `stash`, and `reset --hard` that lose work.

The frontend repo (`crystal interface`) is shared the same way, and there the
stakes are higher: see the deploy note below.

## The deploy approval gate, and why not to route around it

`main` auto-deploys, but the deploy job **pauses for manual approval from
`ryycrystal` and waits indefinitely**. That is deliberate: other people hold
write access to this repo, and the gate is what stops them moving production.

**The gate is enforced by Azure, not by the YAML.** The federated credential is
bound to the `production` *environment*, so a job that drops the `environment:`
line cannot obtain an Azure token at all. Editing the workflow does not bypass
approval — it breaks authentication. Do not "temporarily" remove it.

Two things that will confuse you:

- **`AADSTS700213: No matching federated identity record found`.** GitHub
  sometimes presents an **ID-based OIDC subject**
  (`repo:ryycrystal@171206695/crystal-backend@972947090:environment:production`)
  rather than the plain-name form. Credentials for both forms are registered
  now. If it recurs, read the exact subject out of the job log under "Federated
  token details" instead of guessing. It fails **closed** — there is never a
  partial deploy.
- **Re-runs need re-approval.** `gh run rerun --failed` returns the job to
  `waiting`; the earlier approval is not inherited.

A deploy can therefore sit unnoticed for hours. If production looks like it is
missing a commit, **check for a run in `waiting` before debugging anything else.**

## Production topology

- **The live database is `crystal-prod-db-r3`.** An older `crystal-prod-db` host
  still accepts connections but is **stale and will mislead you**. Confirm
  `PGHOST` before trusting any number you pull.
- The API sits behind Cloudflare at `api.crystal.exchange`, which requires an
  **origin/SNI override to the Container Apps FQDN** — there is no ACA custom
  domain configured. Getting this wrong produces a **522**, which reads like an
  outage but is a routing misconfiguration.
- Verify what is actually deployed rather than trusting the working tree; images
  are tagged with the commit SHA precisely so this is checkable:

```bash
az containerapp revision list -n crystal-api -g crystal-prod-rg \
  --query "[?properties.active] | [-1].properties.template.containers[0].image" -o tsv
```

## Lock cascades — the failure mode that takes the whole API down

Postgres queues lock requests. **An `ACCESS EXCLUSIVE` request blocks every
reader that arrives after it**, including readers that would not otherwise
conflict. A long-running transaction plus one waiting `ALTER TABLE` is enough to
park the entire API; this has happened, with 67 queries stacked behind a single
migration.

Consequences:

- The app sets a **5s `lock_timeout` with retry** (commit `b5c603c`). A running
  image older than that commit does not have this safety net — one of the two
  causes of the outage above was exactly that.
- **Compute outside the transaction.** Maintenance scripts must read, close the
  transaction, do the Python work, then reopen to write. Holding a read open
  keeps `ACCESS SHARE` on the table, which alone is enough to park a waiting
  migration behind it.
- Diagnose before blaming the DB — longest open transaction and blocked count:

```bash
psql -c "SELECT pid, state, now()-xact_start AS age, left(query,80) FROM pg_stat_activity WHERE state<>'idle' ORDER BY xact_start;"
psql -c "SELECT count(*) FROM pg_stat_activity WHERE wait_event_type='Lock';"
```

Zero blocked queries means the problem is somewhere else — go look elsewhere
rather than re-fixing the database.

## WebSocket frame kinds are per-channel — do not generalize

Only the **per-subscriber wallet channels** (`user_positions`, `positions`)
label their first frame `snapshot`. There, `prev is None` genuinely means "this
subscriber's first frame".

**Do not apply that to the other channels.** `trades`, `holders` and
`top_traders` are **broadcast** and keyed `(token, channel)`, so `prev is None`
fires once per server process per token — not per subscriber. Worse, the client
explicitly **discards** `trades` frames labeled `snapshot`, so mislabeling that
channel silently drops trade data with no error anywhere. **Read the client
handler before changing any channel's `kind`.**

The bug this originally fixed: the client only clears its map on a `snapshot`,
so when every frame said `delta` the map could only grow, and anything that left
the set while the client was disconnected was never removed.

**Related trap:** `user_positions` rows carry live unrealized PnL recomputed
from price, so an open position's row changes **on every price tick**. Anything
that infers "a trade happened" from a row changing must fingerprint only
trade-driven fields — `trade_count`, `balance_token`, `token_bought`,
`token_sold`, `native_spent`, `native_received` — or it will mark every position
as freshly traded on every tick.

## Numbers that look wrong but are correct

Before "fixing" any of these, confirm it is actually a bug. Each has been
re-reported more than once.

- **`fees_usd` is the bonding-curve fee, not Crystal's cut.** Crystal's ~1%
  frontend fee is **not indexed at all**, so `fees_usd` will never equal 1% of
  volume.
- **Graduated pools are Uniswap V3, so `V2SYNC` never fires** for them and
  reserve-derived prices do not update from sync events. A reconciler handles
  this and is **already deployed**. Stale graduated prices are not a live
  problem.
- **~126K zero-balance positions still carry `cost_basis_native > 0`.** Benign:
  `crystal_unrealized_pnl` zeroes the basis term when the holding is zero.
- **Positions with `sold > bought`** are legitimate after the transfer-basis
  repair — they are not corruption.

## Cost basis: proportional release, and it travels with transfers

Realized PnL releases basis **in proportion to the fraction sold**:
`released = basis * sold / open`. Anything computing PnL as
"native received − native spent" is wrong and produces absurd gains on
partially-sold positions. **Every PnL surface in launchpad must use this
method** — there is no carve-out for one widget or one endpoint.

Basis also **carries across transfers**: moving tokens to another wallet moves
the proportional basis with it instead of materializing a fake gain. A bug where
a swap's transfer leg drained cost basis on sells was fixed and ~175K rows were
repaired.

## Vault ledger writes are two seams, not one

Writing a flow row and updating the depositor counters are **separate
operations**. A backfill that calls only the insert path leaves the counters
stale, and the drift is **invisible** until somebody reads them. Always do both,
and re-check the counters after any vault backfill.

## nad.fun has two curve generations

v1 and v2 use different emitters, different contracts and different sell paths
(`sellToNative` vs `sell()`). As of Aug 2026, create activity **shifted back to
the v1 emitter**, so "it must be v2 because it is recent" is a bad assumption —
check the source column, which is now written correctly per generation.

A small number of v2 tokens are **LVMON-quoted**. Those break the settler's WMON
unwrap and need raw router actions composed by hand; a sell that reverts on such
a token is not a self-match or a liquidity problem.

## Dead integration: X / twitterapi.io

The provider has returned **402 Payment Required since 2026-08-29**. Socials are
frozen and Alerts are permanently empty. **This is not a code bug** — the
account needs funding. Do not debug the poller.

The key is also **hardcoded in this public repo** across dozens of commits, so
it needs *rotating*, not merely deleting from HEAD; the history still carries it.

## Frontend deploys are ungated — be more careful there than here

`crystal interface` deploys to **`app.crystal.exchange`** (not
`crystal.exchange`), and pushes to its `main` **auto-deploy with no approval
step at all**. A push there is an immediate production release. Typecheck before
pushing, and remember the tree is shared — confirm the diff is yours.

## Verify that your change actually took effect

Several fixes here were reported as shipped while the code path never ran. Real
examples, all of which looked correct in the diff:

- a REST payload that never arrived because a WebSocket freshness check returned
  early before using it;
- a sort that was silently overwritten by a pre-existing sort later in the same
  chain;
- a CSS rule that lost to an equal-specificity selector in another file;
- a `title` tooltip on a `disabled` button, which never renders.

Reproduce the symptom, apply the fix, then reproduce again. **Do not add a poll
to paper over a data-flow bug** — find out why the data is not arriving. And
prefer reading the database or the running container over trusting the local
working tree: the deployed image has repeatedly been older than local `main`.

---

## Writing to prod Postgres: the two lock rules

The `init_db` deadlock above is one instance of a broader hazard. **Once an
`ACCESS EXCLUSIVE` request is waiting, Postgres queues every later reader behind it.**
The API then starts 500ing on plain selects while cached endpoints keep serving fine —
that asymmetry is the tell for lock contention rather than a dead app.

Two rules, both learned by taking the production API down, twice in one night:

1. **Never hold a transaction open across slow work.** Read what you need, close the
   transaction, do the slow in-Python work, then open a short write transaction. A
   repair script once wrapped a multi-minute fold inside its read transaction (321s
   idle-in-transaction) and parked a migration behind it.
2. **Never fetch a huge result set in one read.** A single `SELECT` over 532k positions
   sat idle-in-transaction for 131s and parked a migration. Use keyset pagination, a
   few thousand rows per short transaction.

**Killing the python process does not close the Postgres backend.** After stopping any
long-running script — including via a tool-level task stop — check `pg_stat_activity`
for `idle in transaction` and `pg_terminate_backend` your orphan. One was observed
holding a transaction for 203s after its process was gone.

First thing to run when prod looks weird:

```sql
SELECT COUNT(*) FROM pg_locks WHERE NOT granted;
```

Nonzero means something is queued behind a lock. Zero means look elsewhere.

---

## RPC limits that shape what you can debug

- **`eth_getLogs` is capped at a 100-block range.** Replaying a wallet's or token's full
  history over millions of blocks is therefore not possible with a naive scan. If you
  need to explain a historical discrepancy, either drive it from stored rows, or detect
  the problem in a *recent* window (sample, compare against chain at head) so the block
  range you need logs for is small. Several investigations have dead-ended here.
- Archive `eth_call` pinned to an old block does work, so `balanceOf`-at-block is a
  viable truth source where `getLogs` is not. `reconcile_position_balances.py` relies on
  this, batching through multicall3.
- There is a client-side rate limiter (`RPC_MAX_RPS`, default 20). Bursty parallel read
  scripts will be throttled rather than 429'd; budget wall-clock accordingly.

---

## `last_price_native` can be zeroed by a single bad trade (fixed 2026-09-02)

A token's `last_price_native` is taken from its most recent trade. If that trade lands
with `price_native = 0`, the token's price becomes 0 and **every position in it shows no
value**, even though hundreds of earlier trades carry a real price.

Found via 66 positions with `balance_token > 0` and no price. It was 3 graduated tokens
(CHOG, MONGU, FLOKI): 200–420 trades each, all but the last correctly priced. They also
have no `crystal_markets` row and no pool sync events, so the graduated-reserves
reconciler structurally could never re-price them.

Fixed by restoring `last_price_native` from the most recent trade with `price_native > 0`
(66 → 0 verified). Scope check afterwards: **0** tokens remain in that state, only **6**
such trades exist in all of history, and **none after block 101,000,000** — so this is
historical, not live, and needed no code change. If it reappears, look at why a trade
was written with a zero price rather than patching the token row again.

---

## Fee accounting: `fees_usd` is not Crystal's revenue

`fees_usd` is the **bonding-curve fee**, not Crystal's 1% frontend cut. The frontend cut
is unindexed, so `fees_usd` will never equal 1% of volume and should not be reported as
Crystal's earnings. This has been misread before.

---

## OPEN: outbound transfers and stale `balance_token`

**Status as of 2026-09-02: root cause NOT identified.** A full reconciler sweep found
**17,889 positions with a wrong `balance_token` and 12,085 that had to be zeroed** —
wallets that had transferred tokens away while the position row kept the old balance.
The reconciler fixes the symptom to chain truth at its pinned block; the underlying
indexing path is still unexplained, so **drift will recur.**

Eliminated — do **not** re-investigate these:

- **Transfer logs are fetched.** Tag `TF`, standard `0xddf252ad…b3ef` topic, present in
  `h.TOPICS`, and `fetch_logs_http` applies no address filter.
- **`in_trade_tx` is not the cause.** It gates only cost basis, never the balance delta.
- **The sequencer/state filter mismatch is not the cause.** 0 orphan pools of 30,515.
- **Do not measure this with `launchpad_transfers`.** That table is written only by
  `scripts/extract_transfers.py`, never by the indexer's transfer handler, so comparing
  balances against it measures that script's coverage too. This wasted real time.

Empirical: of the top 300 positions by balance, **40 disagree** with the DB's own event
history — 31 where the chain balance *exceeds* what recorded events explain (missing
inbound credits), 9 the other way.

**Next step, and why the obvious approach fails:** full historical replay is impossible
because `eth_getLogs` is capped at 100 blocks. Instead detect drift in a *recent* window
— sample positions, multicall `balanceOf` at head, diff against `balance_token` — which
yields cases whose blocks are recent enough to actually pull logs for, then trace those.

**Interim remedy:** re-run `scripts/reconcile_position_balances.py` (~38 min, resumable
via `launchpad_kv` key `reconcile_balances_at`).

---

## Spot graph pricing: use the in-window median, not the boundary sync

`api/spot_graph.py` used to price a bucket from the **last sync before the bucket
boundary**, which let a single transient reserve reading define a whole bucket and put
visible spikes in the portfolio value series. It now takes the **median of the syncs that
fall inside the bucket window**, falling back to the last known sync only for a quiet
token that had none:

```sql
WHERE LOWER(m.base_address)=%s AND LOWER(m.quote_address)=%s
  AND e.timestamp > %s AND e.timestamp <= %s
```

Bump `VALUE_VERSION` whenever you change how a bucket's value is computed — it is the
cache-invalidation key for stored buckets. It went 3 → 4 with this change. Forgetting to
bump it means old, differently-computed buckets are served alongside new ones.

The portfolio graph and the spot portfolio graph **must agree**; they had diverged, and
the fix was this pricing change plus pinning the series tail to the live total rather
than patching one graph to match the other. If they disagree again, fix the shared
source, not the presentation.

---

## Query shapes that have taken the launchpad down

- A **~30,000-address exclusion array** passed as a literal `NOT IN (...)`, combined with
  a holder-stats sort, was the cause of general launchpad slowness. Rewriting it as an
  **anti-join** was verified in prod at **4–25x** faster. If you find yourself
  interpolating a large address list into SQL, make it a join instead.
- The vault summary endpoint ran **6.7–12.1s**; reducing the number of balance samples
  taken in SQL brought it to **~0.9s**. The statistics were proven equivalent — the
  series differs only in which of several equal-valued samples represents a bucket.

---

## Two client-side facts that explain "the backend is wrong" reports

- **Mainland China cannot reach the RPC.** Any surface that reads only from chain renders
  as `$0` there. This is why LP reads `/pools/positions` from the backend first and only
  then falls back to chain. When someone reports zeros that nobody else can reproduce,
  ask where they are before debugging the backend.
- **1CT limit orders execute from an ERC-4337 smart account, not the user's EOA.** Funds
  sitting in the Rabby EOA are not spendable by it, and the failure surfaces as
  `TRANSFER_FROM_FAILED` — which reads like a backend or contract bug and is not one.

---

## Wallet data is scoped to a single address — never union wallet sets

A user's EOA and their 1CT (one-click-trading) subwallets are **separate
accounts for every read**. Positions, holders, trades, balances and PnL for an
address must return **only that address's data**, never the union of the EOA and
its subwallets.

This is a product rule, not an optimization. Unioning produced a visible bug
where a portfolio flickered between 11 and 17 positions as different code paths
disagreed about which set they were showing. If you add a new wallet-scoped
endpoint or WebSocket channel, scope it by the single address and resist the
temptation to "helpfully" merge in subwallets.

## 1CT limit orders execute from the smart account, not the EOA

One-click trading uses an **ERC-4337 smart account**. Limit orders execute
*from that smart account*, but users fund their **Rabby EOA** — so the smart
account is frequently empty while the user is certain they have money.

The symptom is a `TRANSFER_FROM_FAILED` revert, or a limit order that silently
never fills. Before debugging matching logic, **check the smart account's
balance, not the EOA's.**

## Vault dry-run harness — revert the frontend before prod

Dry runs against the test contracts use an **isolated dryrun database** plus a
**local API on :8011**, with the frontend repointed at it.

**`crystal interface/src/settings.ts` carries the repointing, and it must be
reverted before anything ships to production.** A dry-run `settings.ts` pushed
to the frontend's `main` would auto-deploy a production app pointed at a
developer's laptop. When you see that file modified, read the diff before
assuming it is a normal change — and confirm which it is rather than guessing.

## Adjacent systems that live in or near this backend

- **Volume fee tiers** — Basic / Bronze / Silver / Gold / Diamond, assigned by
  launchpad USD volume, served from `/tiers`. The thresholds live in the
  `volume_tiers` table and are **editable in SQL** — do not hardcode them in
  application code.
- **Rewards / points engine** — shipped backend-only and **deliberately has no
  frontend**. Weekly scoring uses an exponent of `0.8` and the week **closes
  Wednesday 00:00 PT**. Rates and campaigns are configured through kv/campaign
  seams rather than code.

  Known-open risks flagged in review and **not yet fixed**: two concurrency bugs
  in the week-close/leader-election path, a pre-deposit boost that can be gamed,
  and an **empty denylist that fails open**. Treat these as live before launch.

## Mainland China cannot reach the RPC

Chain-only reads render as `$0` for users in mainland China because the RPC
endpoint is unreachable there — the data is fine, the fetch simply fails.

The fix pattern is to **read from the backend first and fall back to chain**,
which the LP surface now does via `/pools/positions`. Other chain-first paths
remain exposed. If a Chinese user reports zeros, this is the first hypothesis,
not a data bug.

## The app is translated into 9 locales by an AST rewrite

Translation is applied by an **AST rewrite pipeline**, not by hand. The trap:
the rewrite can inject a translation hook into a position that **violates the
rules of hooks**, producing a runtime error that looks nothing like an i18n
problem. Some strings are deliberately left in English.

## Performance: the exclusion-array pattern is the usual culprit

Launchpad slowness traced to a **~30,000-address exclusion array** passed into
queries, combined with a holder-stats sort. Rewriting it as an **anti-join** was
**4–25x faster** in production.

If a launchpad endpoint is slow, look for a large `NOT IN (...)` / `<> ANY(...)`
array before optimizing anything else.

## Log-cache holes silently vanish tokens

A gap in the log cache (once **18.4M blocks** worth) does not raise an error —
tokens simply **disappear** from listings, because the events that created them
were never read. It was resolved with an overnight refill plus a clean reindex,
and a guard now exists.

The lesson generalizes: **missing rows here look like a product bug, not an
ingestion bug.** If something is absent rather than wrong, suspect coverage
before suspecting query logic.

## Pre-launch gaps that are known and still open

These are deliberate, documented, and not yet done. Do not treat them as
discoveries:

1. **Migrations run on indexer startup.** A bad migration takes the indexer down
   with it. Splitting them into a separate step is the largest remaining gap.
2. **Container apps run in single-revision mode** — no traffic splitting, so
   there is **no instant rollback**. The only way back is redeploying an older
   SHA, which must pass through the approval gate.
3. **No staging environment.** `main` is the first place code meets real data.
4. **No API authentication, open CORS, no rate limits.** The frontend also ships
   provider keys to the browser.

## 1CT key storage (frontend, but you will be asked about it)

1CT private keys are **AES-GCM encrypted under a non-extractable IndexedDB
key**. Two invariants: **never persist plaintext**, and **never write while the
vault is locked**. If you touch that path, preserve both.
