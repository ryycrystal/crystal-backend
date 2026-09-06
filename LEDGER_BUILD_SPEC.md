# Ledger build spec — contracts for the `accounting-fix` branch

This is the implementation contract for POSITION_LEDGER_PLAN.md (read it first, especially
§4, §6, §7). Several agents build modules in parallel against this file. Every signature,
table and constant named here is binding; anything not named here is the implementer's call
as long as the tests and the fixture check pass.

## Ground rules

- Work only in the worktree `C:/Users/ryanl/OneDrive/Desktop/projects/crystal-backend-accounting-fix`
  (branch `accounting-fix`). Never touch `C:/Users/ryanl/OneDrive/Desktop/projects/crystal-backend`.
- Python 3.12, psycopg2, `Decimal` for money. `ruff check` and `ruff format --check` must pass
  (`python -m ruff ...` from the worktree root). **Zero code comments** — the repo rule; write
  self-explanatory code. Tests go in `tests/`, named `test_ledger_*.py`.
- Existing tables and modules are not modified except where this spec says so. New code lives
  under `core/ledger/` (package with `__init__.py`).
- Never write to prod. Prod is read-only through the tunnel (below). The side database is where
  everything is written.
- Commit your own files with explicit paths and a lowercase one-sentence message, no
  co-author trailer. Do not `git add -A`. Do not push.
- Run scripts from the worktree root with `PYTHONPATH=.`. Set `DATABASE_URL` to the side
  database for anything that writes.

## Data access

- **Side database** (writes): DSN in
  `C:/Users/ryanl/AppData/Local/Temp/claude/C--Users-ryanl-OneDrive-Desktop-projects/79e4cc35-2274-4196-b460-6db123110bd1/scratchpad/side_dsn.txt`
  (local Postgres 17, database `crystal_ledger`). Export it as `DATABASE_URL`. `core.storage.init_pool()`
  then works as in the rest of the codebase; `init_db()` may be run against it to create the
  existing schema (it is empty until you do).
- **Prod database** (read-only): the CONNECT tunnel listens on `127.0.0.1:15433`. Connect with
  `host=<PGHOST from .env> hostaddr=127.0.0.1 port=15433 dbname=<PGDATABASE> user=<PGUSER>
  password=<PGPASSWORD> sslmode=require`, reading those four from the worktree `.env` (copy it from
  the main checkout if missing; never print its values). Always
  `conn.set_session(readonly=True, autocommit=True)`. `scripts/replay_side.py` shows the
  `PROD_PG*` convention and how the raw log cache is read (`launchpad_block_logs(number, logs jsonb)`).
- **RPC**: `https://rpc.monad.xyz`. `eth_getLogs` accepts `address` + `fromBlock/toBlock`
  (≤100 blocks) but rejects a `topics` filter — filter client-side. `debug_traceTransaction`
  with `{"tracer":"callTracer"}` works for recent transactions and returns nothing useful beyond
  the archive edge (~600k–800k blocks back). `eth_getTransactionByHash` and
  `eth_getBlockByNumber(n, true)` work everywhere. Respect `RPC_MAX_RPS` (default 20).
- **Block timestamps**: cached logs carry `blockTimestamp` (hex) once
  `backfill.ensure_block_timestamps(logs_by_block)` has run; the replay script already does this.

## Existing code you plug into

- `core/chain.py`: `EVENT_SIGS` (topic0 → tag), `PARSERS` (tag → parser fn taking
  `(address, topics, data_no0x)` and returning a dict or None), `TOPICS`, address constants
  (`CRYSTAL_ADDR`, `NADFUN_ADDRS`, `PASSTHROUGH_ADDRS`, `VAULT_FACTORY_ADDRS`,
  `UNIV4_POOL_MANAGER_ADDR`), `accepts_log_for_indexing`.
- Parsed event keys (confirm in the parser source before relying on them):
  - `TF` (`core/chain.py:_parse_transfer`): `token`, `from`, `to`, `amount`.
  - `LT` (`modules/launchpad.py:parse_launchpad_trade`): `user`, `token`, `is_buy`,
    `amount_in`, `amount_out`, `native_reserve`, `token_reserve`, `source`. Read the parser to
    learn which of `amount_in`/`amount_out` is native and which is token for buys and sells.
  - `NFB`/`NFS` (`modules/nadfun.py`): nad.fun curve buy/sell, same shape family.
  - `V3SWAP` (`parse_v3_trade`): `pool`, `sender`, `user`, `amount0`, `amount1`, `sqrt_price_x96`.
  - `V2SWAP` (`parse_v2_pair_swap`), `V4SWAP` (`modules/univ4.py`): `pool_id`, `sender`,
    `amount0`, `amount1` (V4 sign convention is inverted vs V3, see CLAUDE.md).
  - `TR` (spot fill): `market`, `user`, `is_buy`, `amount_in`, `amount_out`, `start_price`, `end_price`.
  - `OBF` order fill, `IBD`/`IBW` internal balance deposit/withdraw (`modules/proto.py`).
- `core/sequencer.py`: `Sequencer._process_block(blk, logs, cur, counts_out, batch, record_processed)`
  and `_process_block_inner`; `_build_transfer_maps(logs)`; `_tx_index_of(log)`;
  `BatchAccumulator`. The ledger hooks in at the **end** of `_process_block_inner` (after the
  existing handlers, same cursor), guarded by `LEDGER_ENABLED` — one call, nothing else changes.
- `core/storage`: `db_cursor()`, `init_pool()`, `get_meta/set_meta`, `init_db()` in
  `core/storage/schema.py`. `launchpad_tokens(token, source, created_block, migrated_block, quote_token, ...)`,
  `launchpad_pools`, `univ4_pools`, `crystal_markets(market, base_address, quote_address, ...)`,
  `nadfun_v2_tokens`, `launchpad_positions` (old engine, untouched).
- Oracle series: `storage.get_mon_price_usd()` is the live rate; the historical series is the
  MON/USD samples the API's `_mon_usd_window` reads (find the table in `core/storage/launchpad.py`,
  search `mon_usd`). Seeding it into the side DB is the replay script's job.

## Constants

```python
WMON  = "0x3bd359c1119da7da1d913d1c4d2b7c461115433a"
LVMON = "0x91b81bfbe3a747230f0529aa28d8b2bc898e6d56"
USDC  = "0x754704bc059f8c67012fed69bc8a327a5aafb603"
AUSD  = "0x00000000efe302beaa2b3e6e1b18d08d69a9012a"
NATIVE = "native"
QUOTE_ASSETS = {NATIVE, WMON, LVMON, USDC, AUSD}
ZERO = "0x0000000000000000000000000000000000000000"
ENTRYPOINT_V06 = "0x5ff137d4b0fdcd49dca30c7cf57e578a026d2789"
ENTRYPOINT_V07 = "0x0000000071727de22e5e9d8baf0edac6f37da032"
USEROP_EVENT_TOPIC = "0x49628fd1471006c1482da88028e9ce4dbb080b815c9b0344d39e5a8e6ec1419f"
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
DUST_WEI = 10**15
```

## Modules and contracts

### `core/ledger/schema.py` — owner: schema/store agent

`init_ledger_schema(cur)`: idempotent DDL (CREATE TABLE IF NOT EXISTS, statement by statement),
never touching existing tables.

```
wallet_flows(
  block_number BIGINT, tx_index INT, log_index INT, sub_index INT,
  txhash TEXT NOT NULL, timestamp BIGINT NOT NULL,
  wallet TEXT NOT NULL, token TEXT NOT NULL,
  token_delta NUMERIC(78,0) NOT NULL,
  quote_asset TEXT, quote_delta NUMERIC(78,0),
  mon_value NUMERIC(50,18) NOT NULL DEFAULT 0, usd_value NUMERIC(50,18) NOT NULL DEFAULT 0,
  kind TEXT NOT NULL, venue TEXT, counterparty TEXT, origin TEXT,
  source TEXT NOT NULL, basis_state TEXT NOT NULL,
  price_native NUMERIC(50,18),
  basis_delta NUMERIC(78,0) NOT NULL DEFAULT 0, realized_delta NUMERIC(78,0) NOT NULL DEFAULT 0,
  PRIMARY KEY (block_number, tx_index, log_index, sub_index))
  + INDEX (wallet, token, block_number, tx_index, log_index, sub_index)
  + INDEX (token, block_number)
positions_v2(
  wallet TEXT, token TEXT,
  balance_token NUMERIC(78,0), custody_balance NUMERIC(78,0),
  token_bought NUMERIC(78,0), token_sold NUMERIC(78,0),
  native_spent NUMERIC(78,0), native_received NUMERIC(78,0),
  cost_basis_native NUMERIC(78,0), realized_pnl_native NUMERIC(78,0),
  basis_estimated_native NUMERIC(78,0), realized_estimated_native NUMERIC(78,0),
  unresolved_tokens NUMERIC(78,0), unresolved_proceeds_native NUMERIC(78,0),
  trade_count INT, buy_count INT, sell_count INT,
  first_flow_ts BIGINT, last_flow_ts BIGINT, last_flow_block BIGINT, flow_count INT,
  PRIMARY KEY (wallet, token))
address_kinds(address TEXT PRIMARY KEY, kind TEXT NOT NULL, source TEXT NOT NULL,
  first_seen_block BIGINT, evidence JSONB)
venues(address TEXT PRIMARY KEY, kind TEXT NOT NULL, token0 TEXT, token1 TEXT,
  discovered BOOLEAN NOT NULL DEFAULT FALSE, reviewed_at BIGINT, evidence JSONB)
token_registry(token TEXT PRIMARY KEY, source TEXT NOT NULL, registered_block BIGINT,
  quote_token TEXT, decimals INT NOT NULL DEFAULT 18, active BOOLEAN NOT NULL DEFAULT TRUE)
tx_meta(txhash TEXT PRIMARY KEY, block_number BIGINT, tx_index INT, from_addr TEXT,
  to_addr TEXT, value NUMERIC(78,0), selector TEXT)
tx_traces(txhash TEXT PRIMARY KEY, available BOOLEAN NOT NULL, transfers JSONB)
ledger_meta(key TEXT PRIMARY KEY, value TEXT)
```

`kind` values: `buy sell transfer_in transfer_out mint burn airdrop swap_leg lp_add lp_remove
vault_deposit vault_withdraw custody_deposit custody_withdraw`. `basis_state`: `observed
estimated unresolved`. `source`: `transfer_net venue_event trace reconcile`.
Address kinds: `eoa eoa_7702 wallet_4337 venue_pool venue_router venue_curve venue_custody
token contract_unknown zero`. Wallet kinds (may hold positions): `eoa eoa_7702 wallet_4337
contract_unknown`.

### `core/ledger/store.py` — owner: schema/store agent

```python
def insert_flows(cur, flows: list[Flow]) -> int            # ON CONFLICT DO NOTHING on the PK; returns inserted count
def load_flows(cur, keys: list[tuple[str, str]]) -> dict[tuple[str,str], list[Flow]]   # ordered by chain position
def upsert_positions(cur, rows: list[PositionRow]) -> None  # full replace per (wallet, token)
def refold(cur, keys: list[tuple[str, str]], fold_fn) -> int   # load_flows → fold_fn → upsert; returns rows written
def get_tx_meta(cur, txhashes) -> dict[str, TxMeta]; def put_tx_meta(cur, metas)
def get_trace(cur, txhash) -> TraceResult | None; def put_trace(cur, txhash, result)
def get_kinds(cur, addrs) -> dict[str, str]; def put_kind(cur, addr, kind, source, block, evidence)
def upsert_venue(cur, addr, kind, token0=None, token1=None, discovered=False, evidence=None)
def registry(cur) -> dict[str, TokenReg]                    # cached in-process, refresh on demand
def register_token(cur, token, source, block, quote_token, decimals)
```

Dataclasses `Flow`, `PositionRow`, `TxMeta`, `TraceResult`, `TokenReg` live in
`core/ledger/types.py` (owned by the netflow agent, created first; others import from it).
`Flow` has exactly the `wallet_flows` columns; `PositionRow` exactly the `positions_v2` columns.

### `core/ledger/types.py` — owner: netflow agent (write this first, within the first minutes)

Frozen dataclasses for `Flow`, `PositionRow`, `TxMeta` (`txhash, block_number, tx_index,
from_addr, to_addr, value, selector`), `TraceResult` (`available: bool, transfers:
list[tuple[str,str,int]]`), `TokenReg`, `VenueEvent` (`tag, log_index, parsed: dict,
address`), `TxBundle` (`txhash, block_number, tx_index, timestamp, transfers:
list[TransferLeg], venue_events: list[VenueEvent], meta: TxMeta | None, trace: TraceResult |
None, userop_sender: str | None`), `TransferLeg` (`log_index, token, from_addr, to_addr, amount`).

### `core/ledger/txmeta.py` — owner: rpc agent

```python
class TxMetaStore:
    def __init__(self, cur_factory, rpc_url: str | None = None)
    def get_many(self, txhashes: list[str]) -> dict[str, TxMeta]     # DB cache first, then batched eth_getTransactionByHash, persisted
    def note_block(self, block: dict) -> None                        # ingest a full block (eth_getBlockByNumber(n, True)) into the cache
class TraceStore:
    def native_transfers(self, txhash: str) -> TraceResult           # cached; available=False when the RPC has no trace
```
Batched JSON-RPC (one HTTP call with a list of requests), retries with backoff, rate limited by
`RPC_MAX_RPS`. Unit tests with a fake RPC.

### `core/ledger/kinds.py` — owner: classification agent

```python
class AddressKinds:
    def __init__(self, cur_factory, rpc_url: str | None = None)
    def load_known(self, cur) -> None            # constants, launchpad_pools, univ4_pools, crystal_markets, vaults, nadfun pairs → venues + kinds
    def kind(self, addr: str, cur) -> str        # cached; getCode on first sight; 7702 designator (0xef0100 + 40 hex) → eoa_7702
    def is_wallet(self, kind: str) -> bool
    def observe_tx(self, bundle: TxBundle, registry) -> list[str]   # heuristic: contracts that both receive and send a registered token (or take token / give quote) in one tx and are never tx.from, across ≥2 txs → venue_pool candidate (persist to venues with discovered=True); returns newly discovered addresses
    def userop_sender(self, bundle: TxBundle) -> str | None         # EntryPoint UserOperationEvent → sender (topic[2] is sender for v0.6/v0.7: verify the ABI and document which topic)
```
Known lists (see core/chain.py) are `venue_*`/`token`/`zero` kinds; the crystal core and nad.fun
curves are `venue_curve`; our order book (`CRYSTAL_ADDR`) is `venue_custody`; V4 PoolManager and
every pool `venue_pool`; passthroughs `venue_router`.

### `core/ledger/netflow.py` — owner: netflow agent

```python
def net_transaction(bundle: TxBundle, registry: dict[str, TokenReg], kind_of, quote_assets=QUOTE_ASSETS) -> list[Flow]
```
Pure function, implements plan §6.1–§6.6:
1. per-address token deltas for registered tokens; per-address quote deltas for quote assets
   (WMON/LVMON/USDC/AUSD transfers; `tx.value` from `meta` as outflow of `from_addr` and inflow of
   `to_addr`; trace transfers when `trace.available`); WMON deposit/withdraw are identity.
2. collapse non-wallet addresses (kind not in wallet kinds); they only label `venue`/`counterparty`.
3. per (wallet, token) with non-zero delta, classify per §6.3.
4. quote attribution order per §6.4: own quote deltas → venue event whose `user` matches → venue
   event whose token amount equals the wallet's |token_delta| (amount match; tolerate ±1 wei
   and, for fee-on-transfer, prefer exact) → trace → pro-rata by token share across wallets with
   `basis_state=estimated`.
5. `price_native = |quote|/|token|` when observed; else venue event price; else None.
6. `basis_state`: `observed` when the quote leg came from steps 1–4 with real amounts,
   `estimated` for pro-rata/reference-priced, `unresolved` for transfers from non-ledger
   sources, airdrops, mints without payment. `source` records which step produced the quote.
7. `sub_index` deterministic: sorted by (wallet, token) after the tx's own ordering.
8. quote in **native units**: LVMON converted with the LVMON rate (pass a `rates` object:
   `mon_usd`, `lvmon_rate`, `usdc_per_mon`); USDC/AUSD flows keep `quote_asset` and set
   `usd_value` directly, `mon_value` via the rate.

Unit tests (synthetic bundles) must cover: curve buy (LT, native via tx.value), curve sell via
0x Settler (tokens wallet→settler→core, core event names settler, amount match resolves native),
routed buy via router (core event names router, tokens core→router→wallet), pool swap with WMON,
multi-wallet batch (pro-rata estimated), plain transfer between wallets (unresolved for receiver
if sender unknown; carry basis handled by fold), airdrop from zero, custody deposit to
`CRYSTAL_ADDR`, token-to-token swap (two swap_legs), 4337 bundle (origin = userop sender),
7702 wallet, fee-on-transfer mismatch.

### `core/ledger/fold.py` — owner: fold agent

```python
@dataclass class PositionState: ...   # everything in PositionRow plus the three basis buckets in wei
def fold(prev: PositionState | None, flows: list[Flow]) -> tuple[PositionState, list[Flow]]
```
Returns the new state and the same flows with `basis_delta`/`realized_delta` filled. Rules per
plan §6.7 and the three-state basis: average cost; sells release basis proportionally from the
observed and estimated buckets pro-rata to their share of open tokens, and from unresolved tokens
as "proceeds without gain" (`unresolved_proceeds_native`); `transfer_out` moves basis
proportionally (basis_delta negative); `transfer_in` with `basis_state=unresolved` adds to
`unresolved_tokens`; `custody_deposit/withdraw` moves between `balance_token` and
`custody_balance` without touching basis; `lp_add`/`vault_deposit` park basis (record in
`basis_delta`, restore on the reverse pro-rata); `burn` realizes at zero proceeds; `swap_leg`
closes/opens at `mon_value`. `trade_count/buy_count/sell_count` count distinct transactions.
Property test: for any random sequence that ends fully sold, `realized_pnl_native +
realized_estimated_native == native_received − native_spent` and both basis buckets are 0.

### `core/ledger/engine.py` — owner: engine agent

```python
class LedgerEngine:
    def __init__(self, cur_factory, rpc_url=None, enabled=None)   # enabled defaults to env LEDGER_ENABLED in {"1","true"}
    def process_block(self, blk: int, ts: int, logs: list[dict], cur) -> int   # groups logs by tx, builds TxBundles (parsed venue events via h.EVENT_SIGS/h.PARSERS, transfers via TF parser), fetches tx_meta for txs that moved a registered token, traces only when a native leg is unresolved and the block is within the archive window, runs net_transaction, inserts flows, records affected (wallet, token) keys; returns flows inserted
    def flush(self, cur) -> int    # refold every affected key from the ledger (store.refold with fold.fold); clears the set
```
Registry: `token_registry` seeded from `launchpad_tokens` (+ `nadfun_v2_tokens` for the v2 source)
and `crystal_markets` base/quote on first use; `refresh_registry(cur)`.
Sequencer wiring (the only change outside `core/ledger/`): at the end of
`Sequencer._process_block_inner`, `if LEDGER.enabled: LEDGER.process_block(blk, ts, logs, cur)`;
in `BatchAccumulator.flush` (or right after it in the chunk loop) `LEDGER.flush(cur)`. Keep the
old engine's behaviour byte-identical when the flag is off. Add `LEDGER_ENABLED` to the env
documentation in README.md in one line.

### `scripts/ledger_replay.py` and `scripts/ledger_check.py` — owner: replay agent

`ledger_replay.py --token 0x... [--token ...] [--from-block N] [--blocks-file f] [--wipe]`:
- seeds the side DB (run `init_db()` + `init_ledger_schema`, copy `launchpad_tokens`,
  `launchpad_pools`, `univ4_pools`, `crystal_markets`, `nadfun_v2_tokens`, the MON/USD sample
  table and the LVMON rate meta from prod — read-only prod connection as in `replay_side.py`);
- finds hot blocks in prod's `launchpad_block_logs` for the token **and its pools/curves** (the
  token address alone misses nothing for transfers since TF logs carry the token address; venue
  events for its pools carry the pool address — include `launchpad_pools.pool` and `univ4` ids'
  currencies where relevant), bounded below by `created_block`; caches the block list to a file;
- fetches those blocks' logs from prod in chunks, ensures timestamps, and runs
  `LedgerEngine.process_block` + `flush` per chunk into the side DB with `LEDGER_ENABLED=1` and
  the old engine **not** run (call the engine directly, not the sequencer);
- prints progress and a final summary (flows, positions, estimated/unresolved shares).

`ledger_check.py`: runs the fixture assertions against the side DB and prints a PASS/FAIL table,
exit code 1 on any failure:

| fixture | assertion |
|---|---|
| CHIPOTLE `0x8e74f6e943a7a28605ddd59945bec63a8919f5e2`, wallet `0x25afd36012fa25336cc56a1b26c56e92dd77f0f3` | `trade_count == 20` (18 tx of the wallet's own + 2 settler sells, counting the two-leg tx once → verify exact count from the ledger and document it), `token_bought == token_sold == 3173915918.78 ± 0.01` tokens, `native_spent == 20724.081 ± 0.01` MON, `native_received == 34574.254 ± 0.01`, `realized_pnl_native == 13850.173 ± 0.01` MON with `basis_state=observed` for every one of its trades, `balance_token ≤ DUST_WEI`, `unresolved_tokens == 0` |
| moncock `0x405b6330e213ded490240cbcdd64790806827777`, wallet `0xb9e37df144f7e6a86da69642a1f01bec7d2035d2` | `token_bought ≈ 25,719,120.30`, `cost ≈ 477,018 MON` (±0.5%), fully sold (`balance ≤ dust`), `realized ≈ −193,957 MON` (±0.5%) |
| JAMES `0x43cf5407bda1400498b8064d50a7e17528d87777` | for every wallet with a position: `balance_token + custody_balance == chain balanceOf(wallet)` at the replay's head block (multicall or per-call `eth_call`), zero mismatches; the 59 wallets from prod's holders must all be present |
| invariants | no `positions_v2` row whose wallet kind is a venue; per-token estimated share and unresolved share printed |

Known CHIPOTLE trade list for the wallet (block, side, tokens, MON): see the session notes in
POSITION_LEDGER_PLAN.md §0; the two settler sells are tx
`0xcb3461b56e39f9741e974df5f4a7d0e487d9c042371cd91501a3e6c0130dca5b` (162,232,261.7263 tokens,
6,968.9069 MON) and `0x222182027db3846107aa51edc65c4f025fac4c8428e249dabe03a75b54a57b28`
(63,270,582.0733 tokens, 1,034.9064 MON). Core event topic `0x9adcf0ad…` in those txs carries
`[0, token_amount, native_amount, ...]`.

## Definition of done for the branch

1. `python -m ruff check . && python -m ruff format --check .` clean.
2. `python -m pytest -q` green (existing suite untouched + the new `test_ledger_*` tests).
3. `scripts/ledger_replay.py` run for the three fixture tokens into the side DB, and
   `scripts/ledger_check.py` prints all PASS.
4. `COMPLETE.md` at the worktree root containing the word `REVIEW`, the fixture table with the
   numbers actually produced, the estimated/unresolved shares, runtime of the replay, and any
   deviation from the plan with its reason.
