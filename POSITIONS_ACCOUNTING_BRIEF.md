# Positions accounting: brief for the redesign

Written 2026-09-06 after the #12 routed-trade repair. Purpose: give whoever designs
the next positions ledger every fact, invariant and edge case learned the hard way,
so the design is judged against them instead of rediscovering them. Nothing here is
a proposal yet; the "Candidate direction" section is a starting point for the
discussion, not a decision.

## What the ledger must answer

Per (wallet, token): tokens bought, tokens sold, native spent, native received,
cost basis, realized PnL, balance. Per trade row: who, which side, how many tokens,
how much native, at which price, through which venue. Per token: volumes and counts.
Everything else (users, candles, leaderboards) derives from those.

## How it works today, and why it accreted patches

Positions are derived from **venue events**: bonding-curve trades, nad.fun v1/v2
trades, Uniswap V2/V3 swaps, and since 2026-09-05 Uniswap V4 swaps. A venue event
names its *caller*, which for anything routed is a router, an aggregator, the 0x
Settler, or an ERC-4337 bundler. The code then guesses the person:

1. `_resolve_trade_user` walks the transaction's ERC-20 transfer graph for the token
   (buy: largest positive net retention, then leaf, then depth from the pool; sell:
   first sender that is not the pool, zero, or a passthrough).
2. `PASSTHROUGH_ADDRS` lists stateless executors to walk past.
3. `_verify_attribution` compares what the venue events credited a wallet with
   against its net transfers in the same transaction and, on a gap, books a
   **reconciliation leg** for the missing quantity, pricing it at the credited leg's
   unit price.
4. Transfers outside a trade transaction move tokens *and proportional basis*
   between wallets.

Each routing innovation broke step 1 (settlers, multi-hop aggregators, 4337 smart
accounts, V4's singleton PoolManager) and each break got a patch. The quantities are
now sound because step 3 makes the transfer graph the invariant; the remaining
fragility is entirely in *who* a venue leg belongs to and *what native it cost* when
no venue event covers it.

## Ground truths, ranked by how exact they are

| fact | source | exactness |
|---|---|---|
| tokens moved per wallet per tx | ERC-20 `Transfer` logs of the token | exact, complete |
| native paid on a curve leg | curve trade event (`LT`, `NFB`/`NFS`) | exact |
| native paid on a pool leg | V2/V3/V4 swap event amounts | exact when the pool is tracked and the log is cached |
| WMON moved per wallet per tx | WMON `Transfer` logs | exact, complete (cached, currently *dropped* by the filter) |
| native MON moved by internal calls | nothing in logs | invisible without traces |
| who signed the transaction | `tx.from` | one RPC per trade, and wrong under 4337 (bundler) |

Consequence: token quantities can always be exact. Native cost is exact whenever the
wallet's MON left as WMON or through a curve/pool event, and inferable in every other
case except a native-MON OTC fill.

## Edge cases the design must handle (all observed on prod)

- **Routed buy split across venues** in one transaction: tracked V3 pool + V4
  PoolManager + an untracked V3 pool, all delivered to the wallet through two hops
  (executor, router). Moncock wallet `0xb9e3…35d2`, tx `0x09131fec…`.
- **V4 singleton**: every V4 pool's tokens move from one address (the PoolManager);
  the pool is a bytes32 id, not a graph node. Resolving through the id credited the
  executor (fixed in `31f9d9d`).
- **Untracked pools**: swaps on a V3 pool the indexer never registered are ignored;
  the leg is imputed at another pool's price (4,042 MON actual vs 4,188 imputed).
- **OTC fills** with no event at all: quantity known from transfers, native unknown.
- **ERC-4337**: `tx.from` is the bundler; the smart account is the real trader and
  must be treated as its own wallet (product rule: never union EOA and subwallets).
- **Arbitrage contracts**: the signer never touches the token; credit the contract.
- **Passthrough executors** (0x Settler, AllowanceHolder): must hold no position;
  enforced today in `BatchAccumulator.add_position_delta`.
- **Transfers carry basis**; `token_sold > token_bought` is legitimate (airdrop or
  transfer then sell). A settler's same-transaction transfer leg once drained basis
  on 1CT sells (fixed; 175k rows repaired).
- **Chunked processing**: a chunk's positions are flushed once; anything re-reading
  the table mid-chunk sees stale rows (the chunk-boundary basis bug, `6b9fb7a`).
- **Same-block ordering**: 2–3 Monad blocks share a timestamp; ordering is
  (block, log index), never timestamp.
- **Cache topic set**: the log cache stores only topics indexed at ingest; a new
  event type has no history. V4 logs before 2026-09-05 had to be backfilled by RPC.
- **RPC limits**: `eth_getLogs` is capped at 100 blocks, state reads work only a few
  hundred thousand blocks back, ~20–40 requests/s.
- **Counting**: a routed trade must count once per (tx, token, wallet) however many
  legs it has (`_counted_trade_keys`).
- **USD**: priced at the MON/USD rate of the moment; a replay that never sees the
  oracle pool must not overwrite prod's USD figures.
- **Prod keeps trading during any repair**: cutoff-aware merges, deferred wallets.

## Constraints on any redesign

- Zero code comments in this repo; self-explanatory code.
- The indexer is one replica processing chunks; per-chunk batch writes; no
  transactions held across slow work.
- Repairs must be replayable from empty into a side database and merged per token
  (`scripts/replay_side.py`, `merge_side.py`, `report_side.py`, `compare_side.py`
  exist and are the validation harness for any new ledger).
- Frontends depend on payload shapes (`token_bought`, `token_sold`, `native_spent`,
  `native_received`, `cost_basis_native`, `realized_pnl_native`, trade ids
  `{txhash}-{log_index}`); the new ledger can change internals, not the contract,
  without a coordinated frontend change.
- Proportional basis release is the PnL method everywhere; no carve-outs.

## Candidate direction (to be debated, not decided)

A **transfer-first, per-transaction double-entry ledger**:

1. For each transaction and token, take every wallet's token delta from `Transfer`
   logs. This is the quantity ledger; venue events never change it.
2. For each transaction, collect the wallet's native outflows and inflows from
   observable sources: curve events, pool swap events (any pool, tracked or not,
   as long as the swap's counter-asset is a known quote), WMON transfers, and
   transaction value. Allocate the wallet's net native cost across the legs it
   received in that transaction, proportionally to token quantity or by venue price
   where a venue event covers the leg exactly.
3. Venue events become **price and venue annotations** on legs, and the only source
   for legs of wallets that are pools or curves themselves.
4. Passthrough executors are simply wallets whose net delta is zero and therefore
   never get a leg; no list needed except for exotic contracts that retain dust.
5. A leg with quantity but no observable native (native-MON OTC) is recorded with
   an explicit `native_source = 'imputed'` flag and priced at the block's reference
   price, so the estimate is visible instead of silent.

Known open questions for that direction: native MON paid by internal call to a
curve is only visible through the curve event (fine) but native MON paid to an OTC
counterparty is not (imputed by design); 4337 smart accounts fund gas from the
account (exclude gas from cost); multi-token transactions (token A sold for token B)
need the cost split by leg, which the WMON leg in the middle usually makes explicit.

## How to validate any candidate

Refold the 162-token cohort plus the tail batch from empty through the candidate,
compare against the pass-2 result and against the hand-derived known answers
(moncock wallet: 25,719,120.30 bought / 477,018.49 spent / −193,956.87 realized),
and diff position by position. The harness runs in ~5 hours end to end through the
tunnel; faster from inside Azure.
