# Crystal Backend — Architecture

Scope note: this document is accurate for the **native Crystal launchpad** path, which
was recently normalized. nad.fun still runs on its original code path and has **not**
been moved behind an adapter yet.

---

## 1. High-level architecture

### Components

| Component | Files | Responsibility |
|---|---|---|
| **Entrypoints** | `indexer_main.py`, `main.py` | Indexer process (`--mode resume/backfill`) and the FastAPI app |
| **Log sources** | `core/stream.py`, `backfill.py` | Live log streaming + historical `eth_getLogs` fetching. `stream.py` also runs `vault_sampler` (polls vault balances every 30s) |
| **Chain map** | `core/chain.py` | Contract addresses, `EVENT_SIGS` (topic0 → tag), `PARSERS` (tag → decoder), and address gating for which logs are accepted |
| **Decoders** | `modules/*.py` | Pure ABI decoding per protocol: `launchpad`, `markets`, `pools`, `vaults`, `nadfun`. No state, no I/O |
| **Sequencer** | `core/sequencer.py` | Orders logs within a block, detects reorgs, dispatches to state handlers, batches writes (`BatchAccumulator`), records processed blocks |
| **State** | `state.py` | In-memory world + every `apply_*` handler. This is the write path |
| **Lifecycle model** | `core/lifecycle.py` | Source-agnostic phase/progress rules. Knows nothing about any launchpad |
| **Adapters** | `core/adapters/` | Per-source geometry. `native.py` holds all Crystal curve constants |
| **Storage** | `core/storage/*.py` | SQL. `schema.py` owns DDL + idempotent migrations |
| **API** | `api/api.py`, `api/routes/*.py` | FastAPI routers: launchpad, markets, pools, vaults, system |
| **Oracle** | `core/oracle.py` | MON/USD price derived from V3 pool swaps |

### How they interact

`indexer_main` starts the sequencer loop and the vault sampler. Logs arrive from
`stream`/`backfill`, are tagged by `chain.EVENT_SIGS`, decoded by the matching
`modules` parser, then handed to `sequencer`, which drives `state.apply_*` inside a
single DB transaction per block. State mutates the in-memory model and queues writes
into a `BatchAccumulator`, flushed at block end.

The API is a **separate process**. It never talks to the indexer — it reads Postgres.
The one exception is `POST /vaults/{address}/refresh-balance`, which makes a live RPC
call for freshness but deliberately does **not** persist a sample.

### Data flow

```
Monad RPC ─► stream.py / backfill.py ─► chain.py (tag) ─► modules/*.py (decode)
   └─► sequencer.py (order, reorg guard, batch)
        └─► state.py apply_* ─► adapters/ (normalize) ─► lifecycle.py (phase)
             └─► storage/*.py ─► Postgres
                  └─► api/routes/*.py ─► frontend
```

---

## 2. Folder structure

```
core/
  chain.py         event signature table, parser table, contract addresses, log gating
  sequencer.py     per-block ordering, reorg detection/rollback, batched writes
  stream.py        live log follower + 30s vault balance sampler
  oracle.py        MON/USD from V3 swaps
  lifecycle.py     NORMALIZED lifecycle model (source-agnostic; no launchpad knowledge)
  adapters/
    base.py        LaunchpadAdapter protocol + registry
    native.py      Crystal curve geometry (1e27 supply, 2e26 reserved, V0 injected)
  storage/
    schema.py      DDL + ADD COLUMN IF NOT EXISTS migrations (idempotent)
    launchpad.py   launchpad tables: tokens, trades, positions, blocks, reorg helpers
    markets.py     crystal_markets
    pools.py       AMM pools
    vaults.py      vault tables
    base.py        connection pool, db_cursor()

modules/            pure decoders, one per protocol (no state, no I/O)
  launchpad.py      TokenCreated / LaunchpadTrade / Migrated
  markets.py        MarketCreated / Trade / Sync / Mint / Burn
  nadfun.py         nad.fun events (NOT yet behind an adapter)

api/
  api.py            shared serialization helpers, price helpers, app wiring
  routes/
    launchpad.py    token lists, token detail, stats, charts, search, leaderboard
    markets.py      market list
    pools.py        pool list/detail
    vaults.py       vault list/detail/history/refresh
    system.py       health, sync, debug

state.py            in-memory model + every apply_* handler (the write path)
models.py           dataclasses mirroring the DB rows
indexer_main.py     indexer entrypoint
main.py             FastAPI entrypoint
tests/              unit tests + DB-backed integration tests
```

---

## 3. Normalized model

**Token** (`launchpad_tokens`) — one row per launched token. Identity (creator, name,
symbol, metadata), `source` (0 = native, 1 = nad.fun), the live curve reserves
(`curve_native_reserve`, `curve_token_reserve`), rolled-up aggregates (volume, counts,
`circulating_supply`), and migration state (`migrated`, `market`).

**Trade** (`launchpad_trades`) — one immutable row per trade log. Carries the amounts,
price, and the **post-trade curve reserves**. `UNIQUE (txhash, log_index)` makes it the
**idempotency key** and the unit of reorg rollback. Trades are the source of truth:
token aggregates can always be recomputed from them.

**Holder / Position** (`launchpad_positions`) — one row per `(user, token)`. Token
balance plus cumulative flows (`token_bought/sold`, `native_spent/received`) and PnL
fields. "Holder" is not a separate table — a holder is a position with `balance > 0`.
Balances are maintained from ERC-20 `Transfer` events, not from trade amounts.

**Lifecycle** (`core/lifecycle.py`) — **derived, not stored**. `CurveState` carries
normalized `tokens_sold` / `curve_supply`; `resolve_phase()` maps that plus the
migrated/graduated flags onto `CREATED → ACTIVE → GRADUATING → GRADUATED [→ MIGRATED]`.
Terminal states come from observed events, never inferred from progress.

**Market** (`crystal_markets`) — the orderbook/AMM venue. A launchpad token gets one at
graduation; the link is `launchpad_tokens.market`, mirrored in memory by
`State.launchpad_market_to_token` so post-graduation trades can be attributed back.

### Relationships

```
launchpad_tokens 1 ──── N launchpad_trades      (trades.token → tokens.token)
launchpad_tokens 1 ──── N launchpad_positions   (positions.token → tokens.token)
launchpad_tokens 1 ──── 1 crystal_markets       (tokens.market → markets.market, post-graduation)
launchpad_positions N ── 1 launchpad_users      (by user_address)
```

⚠️ **There are no declared FOREIGN KEY constraints.** Every relationship above is
logical and enforced in application code only.

---

## 4. Native adapter walkthrough

### Create Token
- **Event:** `TokenCreated(address indexed token, address indexed creator, string ×8)`
- **Runs:** `parse_token_created` → `State.apply_token_created` → adapter
  `initial_price_native()` (reads `launchpadParams()` once, cached)
- **Writes:** `launchpad_tokens` (source=0, quote=WMON, initial price = V₀ / 1e27),
  `launchpad_users.tokens_created`
- **Phase:** `CREATED`

### Trade (each buy/sell)
- **Event:** `LaunchpadTrade(token, user, isBuy, amountIn, amountOut, virtualNativeReserve, virtualTokenReserve)`
- **Runs:** `parse_launchpad_trade` → `apply_launchpad_trade` →
  `storage.trade_exists()` **idempotency guard** → `NativeLaunchpadAdapter.curve_state()`
  → `lifecycle` progress
- **Writes:** `launchpad_trades` (+ post-trade reserves), `launchpad_tokens`
  (price, volumes, counts, `circulating_supply`, curve reserves), `launchpad_positions`,
  `launchpad_ohlcv`, `launchpad_snipers` (first 10 blocks)
- **Phase:** `ACTIVE`
- `circulating_supply` is **derived** as `1e27 − tokenReserve`, never accumulated

### 75%
- No event. `CurveState.progress_bps ≥ 7500` — i.e. **600,000,000 of 800,000,000**
  tokens sold
- **Writes:** `approaching_75`, `approaching_75_block`, `approaching_75_at`
- **Phase:** `GRADUATING`
- Threshold derives from the adapter's constants, so it is independent of V₀

### Graduation
- **Events, in order, same tx:** `LaunchpadTrade` → `Migrated(token)` →
  `MarketCreated(...)` → `Sync` → `Mint`
- **Runs:** `apply_migrated` (sets migrated; `market` guarded by `COALESCE` so a replay
  cannot null it) → `apply_market_created` (links the market, registers
  `launchpad_market_to_token`)
- **Writes:** `launchpad_tokens.migrated/migrated_block/migrated_at/market`,
  `crystal_markets`
- **Phase:** `GRADUATED`
- **After this**, trades arrive as `Trade` (topic `0x9adcf0ad…`) on the market and are
  handled by `apply_market_trade` → `_record_graduated_launchpad_trade_locked`, which
  keeps price **and** volume flowing. Gated on `launchpad_market_to_token`, so ordinary
  Crystal markets are untouched.

---

## 5. Event flow

```
RPC (eth_getLogs / WS)
  ↓  raw log { address, topics[], data, blockNumber, blockHash, txHash, logIndex }
chain.EVENT_SIGS[topic0] → tag        e.g. "TC" | "LT" | "MG" | "MC" | "TR"
  ↓
modules/*.py PARSERS[tag]             pure decode → plain dict
  ↓
core/sequencer.py                     order by logIndex; reorg guard; one txn per block
  ↓
state.py apply_*                      idempotency guard, mutate in-memory model
  ↓
core/adapters/native.py               raw reserves → normalized CurveState
  ↓
core/lifecycle.py                     progress_bps, phase
  ↓
core/storage/*.py                     BatchAccumulator flush → Postgres
  ↓
api/routes/*.py                       read-only queries
  ↓
frontend
```

---

## 6. Database

**Launchpad core**
- `launchpad_tokens` — one row per token; identity, curve reserves, aggregates, migration state
- `launchpad_trades` — immutable per-trade facts; **idempotency key + reorg rollback unit**
- `launchpad_positions` — per `(user, token)` balances and flows; holders are `balance > 0`
- `launchpad_users` — per-address rollups (tokens created/graduated, volume)
- `launchpad_blocks` — processed-block checkpoint **and `block_hash` for reorg detection**
- `launchpad_ohlcv` — candles, so charts don't aggregate raw trades per request
- `launchpad_snipers` — early buyers (first 10 blocks)
- `launchpad_pools`, `nadfun_v2_tokens`, `holder_denylist` — auxiliary/nad.fun

**Crystal protocol**
- `crystal_markets` — orderbook/AMM markets; graduation target
- `crystal_pools`, `crystal_pool_sync_events`, `crystal_pool_tvl_samples`, `crystal_pool_lp_users`
- `crystal_vaults`, `crystal_vault_users`, `crystal_vault_deposits`, `crystal_vault_withdrawals`, `crystal_vault_balance_samples`

**Why the aggregate columns exist:** recomputing volume from `launchpad_trades` on every
request is too slow for list endpoints, so totals are maintained on `launchpad_tokens`.
The tradeoff is that they are running sums — which is exactly why the idempotency guard
and reorg recompute exist.

---

## 7. APIs

All read-only except one POST.

| Endpoint | Purpose | Response |
|---|---|---|
| `GET /tokens` | Home lists | `{recent_created[], recent_approaching[], recent_graduated[]}` with `graduationPercentageBps` |
| `GET /token/{addr}/{chartres}` | Token detail page | token core, `holders[]`, `topTraders[]`, `trades[]`, `devTokens[]`, `similarTokens[]`, chart bars |
| `GET /stats/{addr}` | Live stat tiles | `volume_usd_*`, `buy_volume_usd_*`, `sell_volume_usd_*`, `buy/sell_tx_count_*`, `change_pct_*` for 5m/1h/6h/24h |
| `GET /chart/{addr}/{chartres}` | Candles only | OHLCV bars |
| `GET /trades/{addresses}` | Trades for addresses | trade list |
| `GET /user/{addr}` | User summary | positions + totals |
| `GET /portfolio/{addr}` | Portfolio overview | holdings + value |
| `GET /portfolio/{addr}/positions` | Paginated positions | positions (`active_only` default true) |
| `GET /portfolio/{addr}/history` | Portfolio history | time series |
| `GET /volume/{addr}` | User volume | totals |
| `GET /leaderboard` | Rankings | ranked traders |
| `GET /search/query` | Token search | matching tokens |
| `GET /markets/list` | Markets | market list |
| `GET /pools/list`, `GET /pools/{address}` | AMM pools | pool list / detail |
| `GET /vaults/list` | Vault list | vaults + `latestBalance`, `snapshot`, `tvlUsd` |
| `GET /vaults/{addr}/{user}` | Vault detail | vault + user position, deposit/withdraw history, depositors |
| `GET /vaults/{addr}/history/{tf}` | Vault chart | `series.tvl[]`, `series.pnl[]` |
| `POST /vaults/{addr}/refresh-balance` | On-demand freshness | live `getBalances()`; **does not persist a sample** |
| `GET /health`, `GET /sync`, `GET /debug/mon_price` | Ops | health/sync/price |

---

## 8. Remaining TODOs

```
Launchpad Backend

COMPLETE
- Normalized source-agnostic lifecycle model (core/lifecycle.py)
- Native adapter boundary (core/adapters/) — all Crystal geometry isolated here
- Token creation indexing (TokenCreated)
- Buy/sell indexing (LaunchpadTrade), price + volume + supply
- Bonding/vAMM state, reserves persisted and restored
- 75% graduation criteria (600,000,000 of 800,000,000 sold), V0-independent
- Post-graduation Crystal market linkage; price AND volume stay continuous
- Idempotent event processing (trade row is the key; replay is a no-op)
- Restart recovery (rebuild_from_db)
- Reorg rollback + reconciliation (verified equal to a clean index)
- Migrations idempotent from a clean database (23 tables)
- 100 tests passing with a database; ruff + mypy clean on this surface

PARTIAL
- Lifecycle phase is computed but NOT persisted or exposed via any API
- state.py branch coverage 39% (much of it is vault/pool/nad.fun code)
- Lint/type gates run ad hoc; no config committed, not enforced repo-wide
- Sell path exercised only synthetically — no real on-chain sell yet
- Reorg detection only fires if a already-indexed block is re-delivered with a
  different hash; it does not scan backwards for a deeper reorg

NOT STARTED
- Integration against real deployed contracts (BLOCKED: zero TokenCreated /
  LaunchpadTrade / Migrated events exist on-chain — nobody has created a token)
- Fee correctness: 1% is hardcoded and is the CURVE rate. Post-graduation trades
  accrue no fees at all. The real rate is derivable per trade from the native
  reserve delta (buy: amountIn - delta; sell: |delta| - amountOut)
- Positions/PnL are not updated for post-graduation market trades (balances still
  work via Transfer events; cost-basis PnL does not exist anywhere)
- realized_pnl_native is cumulative net cash flow, not cost-basis realized PnL
- nad.fun has NOT been moved behind an adapter; it still uses its own branch
- launchpadInitialNativeSupply (V0) is still the 1000 MON TEST value → graduation
  market cap ≈ $565. Must be set via changeLaunchpadParams before launch
- CRYSTAL_ADDRESS must be updated when real contracts deploy, or the indexer
  silently indexes nothing
```
