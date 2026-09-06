# Position ledger plan — net-flow accounting for every wallet, token and venue

Status: proposal, 2026-09-06, revised after review the same day. Owner: backend. Target:
built and validated in shadow before the vault launch on 2026-09-13, cut over after it.
Companion to CLAUDE.md §"Trade attribution" and §"Cost basis".

---

## 0. Summary

Today a wallet's holdings come from ERC-20 transfers, but its buys, sells, cost basis and
PnL come from **venue events** (the launchpad core, nad.fun curves, pool swaps) that name
whoever *called* the venue. Through any aggregator that caller is a router or the 0x
Settler, so the position engine credits the wrong address or drops the trade. Every
attribution patch so far (`PASSTHROUGH_ADDRS`, `_resolve_trade_user`, per-tx count dedupe,
V4 legs, OTC reconcile) narrows the gap for one venue shape and leaves the next one open.

This plan replaces venue attribution with what every serious portfolio product does:
**net flow per transaction.** For each transaction, each address's change in each registered
token is measured from transfers; its change in quote assets (MON, WMON, LVMON, USDC, AUSD)
is measured the same way, plus the transaction's native value and internal calls. An address
that sent 162M CHIPOTLE and received 6,969 MON in one transaction sold 162M for 6,969 MON,
whatever venues sat in between. Venue events are kept for **price and volume only**.

Everything becomes an append-only **ledger** (`wallet_flows`). Positions and PnL are a
deterministic fold of the ledger, so they are rebuildable, auditable and testable against
chain truth. Addresses are classified so venues, routers, curves and pools never receive a
position row, including venues we have never seen before.

Verified fixtures the new engine must reproduce exactly:

| case | correct answer | what the engine said before the 09-06 merge |
|---|---|---|
| `0x25afd360…f0f3` on CHIPOTLE (`0x8e74…f5e2`) | 20 trades, bought 3,173,915,918.78, sold the same, realized **13,850.173 MON**, dust balance | 15 trades, realized 7,529.053 MON |
| `0xb9e37df1…35d2` on moncock (`0x405b…7777`) | bought 25,719,120.30, cost ≈477,018 MON, realized **−193,957 MON**, fully sold | phantom profit before the hand fix |
| JAMES (`0x43cf…7777`) holders | all 59 balances equal chain `balanceOf` | 34 of 59 understated by 13.46M tokens in total |

---

## 1. What is wrong today, precisely

Four functions write positions, all through `BatchAccumulator.add_position_delta`
(`core/sequencer.py:99`):

| writer (`state.py`) | input | attribution |
|---|---|---|
| `apply_launchpad_trade` (907) | crystal curve `LT`, nad.fun `NFB`/`NFS` | event `user`, patched by `_resolve_trade_user` |
| `_record_graduated_launchpad_trade_locked` (2054) | pool swaps `V2SWAP`/`V3SWAP`/`V4SWAP` | transfer-graph walk |
| `apply_token_transfer` (1352) | `TF` | balance legs unconditional; basis only when not in a trade tx |
| `apply_reconciliation_trade` (1642) | OTC/unattributed token deltas | reconcile pass |

Consequences, all observed in production this week:

- **Balance right, PnL wrong.** `balance_token` is transfer-derived and matches chain;
  `token_bought/sold`, `native_spent/received` and basis are event-derived and miss any
  leg the event did not name. 0x25af/CHIPOTLE: five routed trades missing, realized PnL
  understated by 6,321 MON.
- **Passthrough guard drops deltas.** `add_position_delta` early-returns for
  `PASSTHROUGH_ADDRS`. A sell the event attributes to the Settler is not credited to
  anyone; the wallet's tokens left by transfer, so it looks like a gift.
- **No ledger.** Positions are mutated in place. There is no per-event record, so a wrong
  row cannot be explained or rebuilt except by replaying the chain.
- **Spot writes no positions.** `apply_market_trade` records `crystal_market_trades` for
  charts; no per-wallet position or PnL exists for order-book trading.
- **Unknown venues.** `apply_token_transfer` only guards *unknown tokens*. A registered
  token traded on an unknown pool has its transfer legs applied to both the wallet **and
  the pool**, so pools accumulate position rows (`repair_router_positions.py` exists to
  delete them after the fact).
- **Chunk-boundary basis.** The basis overlay exists because writes are batched per chunk
  and mid-chunk reads see stale rows (commit `6b9fb7a`). A ledger fold within the batch
  removes the class.


### Edge cases the fold must reproduce (all observed on prod, 2026-09-05/06)

- **Routed buy split across venues in one transaction**: a tracked V3 pool, the V4
  PoolManager and an untracked V3 pool, delivered through two hops (executor, router).
  Moncock wallet `0xb9e37df1…35d2`, tx `0x09131fec…`: 7,038,992.72 tokens for 161,704 MON
  across three legs, one wallet row expected.
- **V4 singleton**: every V4 pool's tokens leave one address (the PoolManager) and the pool
  is a bytes32 id, not a graph node. Resolving through the id credited the executor
  (fixed in `31f9d9d`).
- **Untracked pools**: a swap on a V3 pool the indexer never registered is ignored today and
  the leg imputed at another pool's price (4,042.60 MON actual vs 4,188.22 imputed on the
  transaction above).
- **OTC fills**: quantity exact from transfers, native never observable without a trace.
- **ERC-4337**: `tx.from` is the bundler; the smart account is the trader and its own
  wallet (product rule: never union an EOA with its subwallets).
- **Arbitrage contracts**: the signer never touches the token; the contract is the holder.
- **Passthrough executors** (0x Settler, AllowanceHolder, executors such as
  `0xfb78fc…`): net delta zero within the transaction, must never hold a position.
- **Transfers carry basis**: `token_sold > token_bought` is legitimate; a settler's
  same-transaction transfer leg once drained basis on 1CT sells (175k rows repaired).
- **Same-block ordering**: 2–3 Monad blocks share a timestamp; order is (block, tx index,
  log index), never timestamp.
- **Cache topic set**: the log cache stores only topics indexed at ingest, so a new event
  type has no history (V4 logs before 2026-09-05 had to be backfilled by RPC for 130k
  blocks); the fold must not depend on any venue log existing for old blocks.
- **Counting**: a routed trade counts once per (tx, token, wallet) however many legs it has.
- **USD**: every flow is priced at the MON/USD rate of its block; a replay must carry
  prod's series or history is priced at today's rate.
- **Prod keeps trading during any rebuild**: merging a refold needs a cutoff block per
  token and defers wallets that traded after it.
- **Known answers beyond §0**: the 841 never-sold positions repaired on 2026-09-05 must be
  unchanged and the 8,584 sold-cohort positions must move to the ledger answer.

---

## 2. How the reference products do it

| product | holdings | trades | trader identity | basis |
|---|---|---|---|---|
| Zerion, DeBank | transfer-derived, contract balances read for custody | net flow per tx: token delta vs quote delta | address whose balance changed; `tx.from` as origin | average cost; inbound transfers at market value |
| Dune `dex.trades` | n/a | per venue event, then netted per tx for aggregator hops | `tx_from` as taker, `tx_to` as router | n/a |
| GeckoTerminal, DexScreener | n/a | venue events | `tx.from` ("maker/origin"), recipient fallback | n/a |
| Nansen, Arkham | transfer-derived | net flow | labels: router, pool, CEX, contract vs EOA | average cost |
| tax engines (Koinly, Rotki) | ledger of flows | flows classified trade/transfer/income | wallet | lots: FIFO/average/HIFO, configurable |

Common ground: **a ledger of per-transaction flows is the primary record; venues are
labels, not the source of attribution; contracts are classified so pools and routers are
never treated as users; basis is average cost with explicit rules for transfers, airdrops
and swaps that lack a quote leg.**

---

## 3. Design principles

1. **The ledger is the truth.** Every position number is a fold of `wallet_flows`. If the
   fold and the row ever disagree, the row is wrong.
2. **The holder is the address whose balance changed.** Not the event's `user`, not the
   router, not the bundler. `tx.from` is recorded as *origin* for display and analytics.
3. **Venue-agnostic by construction.** A trade is "token delta with an opposite quote
   delta in the same transaction". The engine never needs to know the venue to book it.
4. **First-party custody uses first-party events.** Our order book holds internal balances
   (`Crystal.deposit/withdraw`, tags `IBD`/`IBW`; fills `TR`/`OBF` name maker and taker).
   Vaults and pools have their own ledgers (CLAUDE.md "two seams"). Those events are
   trusted inputs; transfer legs to and from our own custody contracts are custody moves,
   never trades.
5. **Never write a position for a venue.** Address classification gates the single write
   seam. Unknown contracts that behave like pools are quarantined as discovered venues.
6. **Idempotent and replayable.** Ledger rows are keyed by chain position. Re-processing a
   block is a no-op. History is rebuilt by folding, never by hand.
7. **Quote normalisation.** Every flow stores its quote asset and amount, plus `mon_value`
   and `usd_value` at block time from the oracle series, so MON-denominated and
   USD-denominated PnL are both first-class and consistent.
8. **Basis has three states, and money only moves on confirmed.** A flow's cost is
   `observed` (the quote leg was seen on chain), `estimated` (a trade shape with an
   unobservable quote leg, valued at the reference price) or `unresolved` (tokens moved
   with no cost evidence at all; no number is invented). Confirmed PnL is computed from
   observed basis only. Estimated and unresolved are reported beside it, never blended
   in. Leaderboards, rewards, referral USD and anything ranked or paid use confirmed
   figures only, because estimates can be manufactured by routing through a pool we
   cannot observe and confirmed basis cannot.
9. **Chain truth is checked continuously.** Sampled `balanceOf` at a pinned block must
   equal the ledger fold. Drift is a paged alert, not a quarterly discovery.
10. **No frontend contract change.** `launchpad_positions` columns keep their names and
    meaning; new information is added, not substituted.

---

## 4. Data model

### `wallet_flows` (append-only ledger)

| column | notes |
|---|---|
| `block_number`, `tx_index`, `log_index`, `sub_index` | chain position; `sub_index` orders synthetic rows inside one tx (e.g. two tokens in one swap). Primary key. |
| `txhash`, `timestamp` | |
| `wallet` | address whose balance changed (classified as a wallet, see §7) |
| `token` | registered token address |
| `token_delta` | signed, wei |
| `quote_asset` | `native`, WMON, LVMON, USDC, AUSD, or NULL for pure transfers |
| `quote_delta` | signed, in quote units; opposite sign to `token_delta` for trades |
| `mon_value`, `usd_value` | absolute value of the flow at block time |
| `kind` | `buy`, `sell`, `transfer_in`, `transfer_out`, `mint`, `burn`, `airdrop`, `swap_leg`, `lp_add`, `lp_remove`, `vault_deposit`, `vault_withdraw`, `custody_deposit`, `custody_withdraw` |
| `venue` | venue address the tokens came from / went to, if classified; NULL for wallet-to-wallet |
| `counterparty` | the other address on the transfer leg |
| `origin` | `tx.from` (or the ERC-4337 sender when the tx is a bundle) |
| `source` | `transfer_net`, `venue_event`, `trace`, `reconcile` — how the quote leg was observed |
| `basis_state` | `observed`, `estimated` or `unresolved`, see §6.6 |
| `basis_delta` | signed basis moved by this row after the fold (filled by the folder, kept for audit) |
| `realized_delta` | realized PnL booked by this row after the fold |

### `positions` (fold; today's `launchpad_positions`, same columns plus)

Three sets of figures, one per basis state, so no consumer has to guess what a number
contains:

- **confirmed**: `cost_basis_native` (exists), `realized_pnl_native` (exists), unrealized
  from the SQL fold — all computed from observed basis only;
- **estimated**: `basis_estimated_native`, `realized_estimated_native`, and the
  corresponding unrealized;
- **unresolved**: `unresolved_tokens` (quantity held or sold with no cost evidence) and
  `unresolved_proceeds_native` (proceeds already taken on such tokens — confirmed money,
  unconfirmable gain).

Plus `custody_balance` (tokens held for the wallet inside our order book),
`first_flow_ts`, `last_flow_ts`, `flow_count`. `trade_count/buy_count/sell_count` count
**transactions**, not legs, matching the #12 dedupe semantics. The frontends keep reading
the confirmed columns under their existing names; a position that is mostly unresolved
should render as "PnL incomplete" rather than as a small confident number.

### `address_kinds`

| column | notes |
|---|---|
| `address` | |
| `kind` | `eoa`, `eoa_7702`, `wallet_4337`, `venue_pool`, `venue_router`, `venue_curve`, `venue_custody`, `token`, `contract_unknown`, `zero` |
| `source` | `factory_event`, `known_list`, `getcode`, `heuristic`, `manual` |
| `first_seen_block`, `evidence` | evidence is JSON: which events/heuristics fired |

### `venues`

Pools, curves, routers, settlers, our own core/custody, with `kind`, `token0/1` where
known, `discovered` flag and `reviewed_at`. Seeded from factory/initialise events we
already index (`V4INIT`, nad.fun pair creation, crystal migrations), the passthrough list,
the 0x contracts, and heuristics (§7).

### `token_registry`

What "registered" means: `source` (crystal launchpad, nad.fun v1, nad.fun v2, spot base,
spot quote), `registered_block`, `quote_token`, `decimals`, `active`. Replaces the implicit
"is it in `launchpad_tokens` or `token_to_v3_pool`" test in the sequencer.

Kept unchanged: `launchpad_trades`, `crystal_market_trades`, `launchpad_ohlcv`,
`crystal_pool_*`, `crystal_vault_*`. They are venue/price/volume tables and the frontends
read them.

---

## 5. Inputs per block

1. **Logs by topic** (exists): `TF` for every ERC-20 transfer; venue events for price and
   volume; first-party custody and fill events; factory/initialise events for venue
   discovery.
2. **Transaction metadata** (new): `from`, `to`, `value` and the `input` selector for
   every transaction that moved a registered token, cached in a `tx_meta` table next to
   the log cache the way logs are, so history rebuilds never refetch it. Live it is filled
   from one `eth_getBlockByNumber(n, true)` per block we already process. During a replay
   the log cache already says which transactions matter, so only those are fetched
   per-hash — a small fraction of one call per block over millions of blocks.
3. **Traces on demand** (new, gated): `debug_traceTransaction` with `callTracer` is
   available on `rpc.monad.xyz` (verified 2026-09-06 on tx `0xcb3461…`). Requested only for
   transactions where a registered token moved **and** a native quote leg is unresolved
   after logs, value and venue events. Cached by txhash. Budgeted under the existing
   `RPC_MAX_RPS` limiter; if the budget is exceeded the flow is booked
   `basis_state = estimated` and queued for a backfill worker. Traces do not exist beyond
   the archive edge (met around 600k–800k blocks back on 2026-09-06), so old history
   resolves from logs, value and venue events or not at all — see §6.4 and Phase 0.
4. **Oracle series** (exists): MON/USD and LVMON rate at block time for `mon_value` and
   `usd_value`.

---

## 6. The netting algorithm

Per block, per transaction, in chain order:

### 6.1 Group and net

- Group logs by transaction. For every registered token, sum `TF` legs into
  `token_delta[address]`. Sum quote-asset `TF` legs into `quote_delta[address][asset]`.
- Add native legs: `tx.value` is a native outflow for `origin` and inflow for `tx.to`; the
  crystal core's `LT`/`NFB`/`NFS` events carry the native amount for the named user; a
  trace, when fetched, contributes every internal `value` transfer.
- Treat WMON deposit/withdraw as identity: a wallet wrapping MON has no quote delta.

### 6.2 Collapse venues and passthroughs

- Drop every address classified as venue, router, passthrough, custody, token contract or
  zero from the *wallet* set. Their deltas are used only to label `venue` and
  `counterparty` on the wallet rows.
- A registered token that flows wallet → router → pool inside one tx nets to a single
  wallet row with `venue = pool`.

### 6.3 Classify each (wallet, token) with `token_delta ≠ 0`

| observation | kind |
|---|---|
| opposite-sign quote delta present | `buy` / `sell` (quote leg = that delta) |
| no quote delta, counterparty is a wallet | `transfer_in` / `transfer_out` |
| counterparty is the zero address | `mint` (from curve/launch) or `airdrop` (unsolicited) / `burn` |
| tokens out, LP token in (pool mint) | `lp_add`; tokens in, LP token out | `lp_remove` |
| tokens out to a vault, shares in | `vault_deposit`; reverse | `vault_withdraw` |
| tokens to/from our order-book custody | `custody_deposit` / `custody_withdraw` |
| two registered tokens with opposite signs and no quote delta | two `swap_leg` rows, valued at reference price (§6.6) |

### 6.4 Multi-wallet transactions

Token deltas are exact per address. Quote attribution per address uses, in order:

1. the address's own quote transfers and `tx.value`;
2. a venue event in the same transaction whose `user` is the address;
3. a venue event whose **token amount equals the address's token delta** — this is the
   routed curve buy or sell through 0x or a router, where the event names the router but
   the amount identifies the wallet. Single-wallet transactions, which is nearly all of
   them, resolve here from logs alone with no trace;
4. a trace, when available and within budget;
5. otherwise, for batchers that settle several users in one transaction, the transaction's
   total quote leg **pro-rata by token share**, booked `basis_state = estimated`.

Phase 0 measures how much of the routed cohort stops at step 3; that number, not an
assumption, sets the §13 thresholds.

### 6.5 Price

`price_native` for a trade row is `|quote_delta| / |token_delta|` when the quote leg is
observed; otherwise the venue event's price in that transaction; otherwise the token's last
known price. Venue events remain the source for OHLCV.

### 6.6 Estimated and unresolved quotes

Two different situations hide behind "we could not observe the cost", and they are kept
apart:

- **Estimated.** The movement has a trade shape — tokens came from or went to a venue, a
  known counterparty pattern, a venue event exists — but the quote leg itself is
  unobservable (third-party curve paying native through an internal call with no trace;
  a batcher's pro-rata share). The row is booked at the **reference price** (last venue
  price at or before that block) with `basis_state = estimated`. The position keeps
  `basis_estimated_native` so the UI can say "includes estimated cost" and a backfill can
  replace the estimate with a trace later.
- **Unresolved.** Tokens arrived with no cost evidence at all: unknown sender, airdrop, a
  venue we cannot price. No number is invented. The quantity is tracked as
  `unresolved_tokens`; when those tokens are sold the proceeds are confirmed money but the
  gain is not, and they accumulate in `unresolved_proceeds_native`. This replaces today's
  zero-basis behaviour, which is how phantom profit is manufactured.

Estimates are attackable — route a token through a pool we cannot observe and the
reference price implies whatever basis you like — so nothing ranked or paid reads them
(principle 8).

### 6.7 Basis and PnL (unchanged semantics, applied to the ledger)

- Average cost. Buys add `quote` to basis. Sells release `basis × sold / open` and book
  `proceeds − released` as realized. Fully closed positions therefore satisfy
  `realized = Σ proceeds − Σ cost` exactly.
- `transfer_out` carries basis proportionally; `transfer_in` from a ledger wallet inherits
  the sender's average cost; `transfer_in` from an unknown source, `airdrop` and `mint`
  without payment are `unresolved` (never zero basis) unless the product decision in §14
  chooses mark-to-market, in which case they are `estimated`.
- `burn` realizes at zero proceeds. `swap_leg` realizes/opens at reference value.
- `lp_add`, `vault_deposit`, `custody_deposit` park basis in the destination ledger (pool,
  vault, custody sub-balance); they are not sells. The reverse restores it.
- Gas is recorded per transaction (`gas_native`) but excluded from PnL by default; a view
  can include it. Protocol fees are inside the observed quote legs and need no handling.
- Unrealized stays a read-time fold: `crystal_unrealized_pnl(hold, bought, sold, basis,
  price)`, unchanged.

---

## 7. Address classification and venue discovery

Order of evidence, first match wins, all cached in `address_kinds`:

1. **Known lists**: crystal core, launchpad curves, our order book (custody), vault
   factories and vaults, `PASSTHROUGH_ADDRS`, 0x Settler / AllowanceHolder, known routers,
   V4 PoolManager, WMON/USDC/AUSD/LVMON token contracts, the zero address.
2. **Factory and initialise events we already index**: V2 pair creation, V3 pool creation,
   `V4INIT`, nad.fun pair creation, crystal migrations (`MG`) → `venue_pool`.
3. **ERC-4337**: a transaction to the EntryPoint carrying `UserOperationEvent` → the
   `sender` is `wallet_4337`; `tx.from` (the bundler) is never a holder.
4. **`eth_getCode`** on first sight, cached forever: empty → `eoa`; `0xef0100 + 20 bytes`
   (48 hex chars) → `eoa_7702` (a wallet, CLAUDE.md); anything else → contract, pending
   the heuristic below.
5. **Pool events**: an unknown contract that emits a pool event we already decode (V2/V3
   `Swap`, `Sync`, nad.fun / PancakeSwap sync) is `venue_pool` on first sight
   (`emits_venue_event`), for that transaction and from then on.
6. **Pair interface probe for silent contracts**: every other unknown contract that a
   registered token touches is asked once, in the same batched RPC path as `eth_getCode`,
   for `token0()` and `token1()`. Two distinct addresses back means a pair: `venue_pool`
   (`pair_interface`), with the pair's tokens recorded on the `venues` row. A contract that
   answers neither is remembered as probed (`address_kinds.source = 'pair_probe'`) and never
   asked again; a call that never answered is not remembered, so a throttled RPC cannot
   hide a pool. There is no shape heuristic any more. The 09-07 JAMES replay is why: of the
   addresses the old "pool shaped across transactions" rule promoted, roughly nine in ten
   were relayed arbitrage bots or zero-balance pass-throughs (an arbitrage bot receives and
   sends the token inside one transaction, which is exactly the shape a pool has), while
   every contract that emitted a swap event, and only six of the silent ones, answered
   `token0()`. Silent pools that are not pairs (a custom AMM with undecoded events) stay
   holders and surface through the `venue_leak` integrity count for manual listing.
7. **Bot contracts** (a contract that trades without emitting pool events and is not a
   pair) keep positions as `contract_unknown` holders, matching the CLAUDE.md decision
   that a bot's own contract is a real distinct actor, whether or not it is called directly.

The write seam checksThe write seam checks `kind ∈ {eoa, eoa_7702, wallet_4337, contract_unknown}`; every other
kind is refused. `integrity` reports `venue_leak` = number of position rows whose address
is classified as a venue; the healthy value is zero.

---

## 8. Token registry

Registered: crystal launchpad tokens (from `TC`), nad.fun v1/v2 tokens (from `NFC`),
spot market base and quote tokens (from `MC`/`crystal_markets`). Everything else is
ignored, with a counter of unregistered tokens seen in transactions that also moved a
registered token, so third-party launchpads become visible for a product decision without
being indexed by accident. Registration is retroactive-safe: registering a token later
means replaying its history through the same fold, which the side-database replay does.

---

## 9. Venue coverage

| venue | token legs | quote legs | notes |
|---|---|---|---|
| crystal curve (`LT`) | `TF` | native via `tx.value` (buy) and core event (sell) | event `user` used only as a hint |
| graduated pools (V2/V3) | `TF` | WMON `TF` | swaps via routers net correctly |
| Uniswap V4 PoolManager | `TF` | WMON `TF` for WMON-paired pools; **native-currency pools settle the quote leg with no token transfer**, so the `V4SWAP` log or a trace carries the cost | `V4SWAP` kept for price, and for cost on native-currency pools — do not delete the decoder |
| nad.fun v1/v2 curves and pairs | `TF` | native/WMON; LVMON-quoted tokens use the LVMON rate | generations are venue detail, invisible to the fold |
| 0x Settler, AllowanceHolder, other routers | collapsed | collapsed | passthrough by classification, not by list |
| unknown third-party pools/launchpads | `TF` | trace or estimated | discovered into `venues` |
| OTC / EOA-to-EOA | `TF` | trace if native, estimated otherwise | flagged |
| our order book | `TR`/`OBF` events | in the event | custody via `IBD`/`IBW`; USDC-quoted markets carry `usd_value = quote` |
| vaults, LP pools | `VD`/`VDP`/`VWD`, `PMINT`/`PBURN` | in the event | basis parked, not sold |
| multi-venue routed buy (V3 + V4 + untracked V3 in one tx) | `TF` per leg | WMON `TF` per leg, or the venue event matched by amount | the moncock fixture: legs net to one wallet row per token, `venue` = the largest leg, per-leg price from the events |
| ERC-4337 bundles (EntryPoint) | `TF` | the account's WMON `TF`, or native via trace | `origin` = the UserOperation sender, never the bundler |
| tracked token on a pool from another factory | `TF` | WMON `TF` | today ignored and imputed; must be discovered into `venues` on first sight |

---

## 10. Ordering, batching and reorgs

- Ledger keys are chain positions; the fold is deterministic given the same ledger.
- The sequencer keeps chunked processing. Inside a chunk the fold runs on the in-memory
  ledger rows of that chunk on top of the last persisted position, so no mid-chunk row
  read is needed and the basis overlay is deleted.
- A reorg (rare on Monad, but the log cache handles holes) is handled by deleting ledger
  rows at or above the reorged block and re-folding affected positions; positions carry
  `last_flow_block` so the affected set is cheap to find.

---

## 11. Continuous verification

- **Per transaction**: Σ `token_delta` over all addresses = mint − burn. Violations are
  logged with the txhash; they indicate a missed leg, never silently absorbed.
- **Per wallet, sampled**: `balanceOf` at the indexer's pinned head block (multicall, the
  reconciler already does this) must equal the fold plus custody balance. Drift → alert.
- **Closed positions**: `realized = Σ proceeds − Σ cost` for every position with
  `balance ≤ dust`. This is exact and cheap to assert nightly.
- **Venue leak** = 0; **estimated share** (estimated basis ÷ total basis) and
  **unresolved share** (unresolved tokens ÷ tokens held or sold) tracked **per token** and
  overall, because one thin token can hide a bad rule inside a healthy average;
  **unclassified contracts** count.
- All four surface in `integrity_last` next to the existing lag/gap checks.

---

## 12. Rollout

**Phase 0 — spikes (1 day).** Measure full-block fetch cost at 400 ms blocks; measure trace
latency and how often a trace is actually needed on a day of real blocks; run the
classification heuristic over the last week and eyeball the discovered venues.
Two more measurements, whose numbers decide the estimated-quote strategy and the §13
threshold: **trace availability by block age** (sample transactions at 10k, 100k, 300k,
600k, 1M and 5M blocks back and record which return a `callTracer` result; the archive
edge was met around 600k–800k blocks back on 2026-09-06) and **log-only resolvability of
routed legs** (over the 162-token cohort's routed transactions, the share whose quote leg
resolves from `tx.value`, WMON transfers or a venue event, by user or by amount, without a
trace).

**Phase 1 — build behind a flag (3 days).** New tables; netting + classification module;
the fold; the write seam gate. Shadow mode: the indexer writes `wallet_flows` and
`positions_v2` alongside the current tables, no API change. Unit tests from fixtures; the
eight existing position test files are ported to feed the fold instead of the old handlers.

**Phase 2 — replay and diff (1–2 days).** Before folding, seed the side database with
prod's oracle series (MON/USD samples and the LVMON rate) and the `tx_meta` cache, or
history gets priced at today's rate and the replay refetches every block. Then replay the
affected token set using the replay tooling built for #12, fold, and diff against: the
fixtures above; chain `balanceOf` for a 1,000-position sample; the 841 positions repaired
on 09-05 (must be unchanged); the 8,584 already-sold cohort (must move to the ledger
answer). Shadow mode stays on across the vault launch.

**Phase 3 — cutover (half a day, after the vault launch).** Point the API at
`positions_v2` through a view with the old column names. Keep the old tables for a week.
Kill switch is the flag. Landing a cutover of the position seam in the days before
2026-09-13 is the one risk this plan refuses; shadow across the launch costs nothing.

**Phase 4 — delete (half a day).** Remove `_resolve_trade_user`, the passthrough
early-return, `apply_reconciliation_trade`, the basis overlay, and the repair scripts they
made necessary. Update CLAUDE.md.

Coordination: freeze the position seam in `state.py`/`core/sequencer.py` for other
agents during Phases 1–3, and share the side-database replay with the #12 owner rather
than running two replays.

---

## 13. Acceptance

1. Fixtures reproduce exactly (§0 table).
2. Sampled balances match chain to the wei at the pinned block for ≥ 99.9% of sampled
   positions; the remainder are explained by a listed cause.
3. Zero position rows on venue-classified addresses.
4. Estimated basis share < 1% **per token** and overall; unresolved share reported per
   token with every unresolved row carrying its cause; every estimated row carries a
   txhash a backfill can trace. If Phase 0 shows routed legs resolve worse than expected
   from logs alone, the threshold is revised with the number, not waived.
5. Indexer throughput ≥ 2× block rate during replay of a busy day, with the trace budget
   under the limiter.
6. Frontends need no change: every field crystal.fun and the interface read keeps its
   name and meaning; new fields are additive.
7. Spot positions and PnL exist for order-book trading, unified per (wallet, token) with
   launchpad positions, and the portfolio graph and live total agree.

---

## 14. Open decisions (need a product answer)

- **Inbound transfers from unknown senders**: unresolved (no invented number) vs
  mark-to-market at receipt (Zerion/DeBank). Zero basis, today's behaviour, is off the
  table. Recommendation: unresolved by default, since it is the honest statement; if the
  product wants a number, mark-to-market as `estimated` — but that changes realized PnL
  users can already see, so it ships with a frontend "includes estimated cost" treatment
  and a support note, not only a backend flag.
- **Gas in PnL**: excluded by default, available in a view. Recommendation: keep excluded on
  the token PnL row; show it on the wallet summary.
- **nad.fun scope**: positions for nad.fun tokens can be gated by `token_registry.active`
  per source, which is how to shed load without dropping discovery (see the 09-06
  discussion).
- **Pro-rata quote attribution in batch transactions**: acceptable as estimated, or should
  such rows require a trace before booking? Recommendation: estimate, flag, trace in the
  backfill worker.

---

## 15. Work breakdown

| item | files | days |
|---|---|---|
| schema: ledger, kinds, venues, registry, positions_v2, integrity fields | `core/storage/schema.py`, new `core/storage/ledger.py` | 0.5 |
| block fetch with transactions; trace client with cache and budget | `core/backfill.py`, `core/stream.py`, new `core/trace.py` | 0.5 |
| classification module + heuristic + `getCode` cache | new `core/address_kinds.py` | 0.5 |
| netting per transaction | new `core/netflow.py` | 1 |
| fold (basis rules, custody, parked basis) | new `core/position_fold.py` | 1 |
| sequencer wiring behind flag; shadow write; remove overlay when flag on | `core/sequencer.py`, `state.py` | 0.5 |
| tests: fixtures (0x25af/CHIPOTLE, moncock, JAMES), invariants, property tests on netting | `tests/` | 0.5 |
| replay + diff harness on the side DB | `scripts/` | 0.5 |
| cutover view, integrity wiring, CLAUDE.md | `core/integrity.py`, `api/`, docs | 0.5 |

Total ≈ 5.5 focused days. Phases 0–2 fit before 2026-09-13 with a day of margin for the
unknowns in Phase 0; Phase 3 waits for the launch to pass.
