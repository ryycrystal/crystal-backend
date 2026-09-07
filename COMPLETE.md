REVIEW

# accounting-fix: net-flow position ledger

Generated 2026-09-07 11:28 from the side database `crystal_ledger` after replaying the three fixture tokens from prod's log cache.

## Fixture checks

### chipotle

```
ledger_meta replay_head_block: 101923730 (each token is compared at its own last folded block)
fixture    | check                        | expected               | actual            | result
-----------+------------------------------+------------------------+-------------------+-------
CHIPOTLE   | trade_count                  | 20                     | 20                | PASS  
CHIPOTLE   | token_bought                 | 3173915918.78 +- 0.01  | 3173915918.775686 | PASS  
CHIPOTLE   | token_sold                   | 3173915918.78 +- 0.01  | 3173915918.774538 | PASS  
CHIPOTLE   | native_spent                 | 20724.081 +- 0.01      | 20724.080700      | PASS  
CHIPOTLE   | native_received              | 34574.254 +- 0.01      | 34574.253543      | PASS  
CHIPOTLE   | realized_pnl_native          | 13850.173 +- 0.01      | 13850.172843      | PASS  
CHIPOTLE   | trade flows observed         | all observed           | {"observed": 20}  | PASS  
CHIPOTLE   | balance_token                | <= 3173915918775685977 | 1147748982595449  | PASS  
CHIPOTLE   | unresolved_tokens            | 0                      | 0                 | PASS  
invariants | positions on venue addresses | 0                      | 0                 | PASS  
invariants | 0x8e74f6e9 estimated share   | reported               | 0.000%            | PASS  
invariants | 0x8e74f6e9 unresolved share  | reported               | 0.000%            | PASS  
12/12 checks passed
```

### moncock

```
ledger_meta replay_head_block: 101923730 (each token is compared at its own last folded block)
fixture    | check                                | expected             | actual                   | result
-----------+--------------------------------------+----------------------+--------------------------+-------
moncock    | token_bought                         | 25719120.30 +- 0.01  | 25719120.300077          | PASS  
moncock    | native_spent (confirmed + estimated) | 477018 +- 0.5%       | 477018.491679            | PASS  
moncock    | realized (confirmed + estimated)     | -193957 +- 0.5%      | -194863.895521           | PASS  
moncock    | realized split confirmed / estimated | reported             | -124565.883 / -70298.012 | PASS  
moncock    | balance_token                        | <= 25719120300077260 | 0                        | PASS  
invariants | positions on venue addresses         | 0                    | 0                        | PASS  
invariants | 0x405b6330 estimated share           | reported             | 7.249%                   | PASS  
invariants | 0x405b6330 unresolved share          | reported             | 6.823%                   | PASS  
8/8 checks passed
```

### james

```
ledger_meta replay_head_block: 101923730 (each token is compared at its own last folded block)
fixture    | check                                                                    | expected     | actual                                                                                                                                       | result
-----------+--------------------------------------------------------------------------+--------------+----------------------------------------------------------------------------------------------------------------------------------------------+-------
JAMES      | positions present                                                        | True         | True                                                                                                                                         | PASS  
JAMES      | prod holder rows that are venues                                         | reported     | 6 ['0x43cf5407bda1400498b8064d50a7e17528d87777', '0x4a92f8d91b1facec94cbaa6cdc3ef4100313f3d6', '0x4b6ce40b0869ff8aa93432439dd0ef8f39657f37'] | PASS  
JAMES      | prod holders present (2489, holding on chain at block 102,523,914)       | 0 missing    | 0 missing []; 5 of prod's holders bought after the head                                                                                      | PASS  
JAMES      | chain balanceOf == balance + custody (4936 wallets at block 102,523,914) | 0 mismatches | 0 mismatches []                                                                                                                              | PASS  
JAMES      | chain reads that never answered                                          | 0            | 0 []                                                                                                                                         | PASS  
invariants | positions on venue addresses                                             | 0            | 0                                                                                                                                            | PASS  
invariants | 0x43cf5407 estimated share                                               | reported     | 7.372%                                                                                                                                       | PASS  
invariants | 0x43cf5407 unresolved share                                              | reported     | 44.617%                                                                                                                                      | PASS  
8/8 checks passed
```

## Replay runtimes

- CHIPOTLE: chipotle_replay_1.log: 29 flows in 0 min; chipotle_replay_2.log: 29 flows in 0 min; chipotle_replay_3.log: 29 flows in 0 min; chipotle_replay_4.log: 29 flows in 0 min; chipotle_replay_6.log: 29 flows in 0 min
- moncock: moncock_replay_9.log: 56,822 flows in 61 min; moncock_replay_10.log: 113,567 flows in 30 min; moncock_replay_12.log: 115,013 flows in 66 min
- JAMES: james_replay_3.log: 47,565 flows in 61 min; james_replay_6.log: 7,839 flows in 4 min

## Test suite

tests/: 705 passed, 4 skipped (pytest -q at 7b5b2e7)

## Commits on the branch

```
460b809 fall back to the side database's own registry when prod no longer lists a token, so a generation prod has purged can still be replayed from the log cache
1fe2f87 compare each fixture token against chain at its own last folded block, since the shared replay_head_block records only the token that replayed most recently
9bcd7c0 record fixture results after the pair-probe venue classification
210d84b document that the ledger must carry every contract generation because the indexer's live address pointers are not history
cfab649 treat every generation of the crystal core as custody, not just the address chain.py currently points at, so a historical replay never books positions on a retired core after the relaunch
77eb8b3 correct the chain-log scanner's premise: prod's cache is filtered by topic and not by address, so it carries every token's transfers from its first block and the real gaps are topics added later
166ddd2 classify prod's holder list before filtering venues out of it, so pools that prod wrongly holds positions for are reported rather than counted as missing ledger wallets
1b77a63 open the prod log connection inside the fetch retry loop so a tunnel drop during connect or session setup is retried instead of ending the replay
7b5b2e7 count a prod holder as missing from the ledger only when it holds tokens on chain at the replay head, since prod's holder list is live and the replay is pinned
a6befe6 recognise out-of-gas and other execution failures as definitive probe answers, leave unanswered calls unknown instead of failing the chunk, and batch the chain-log scanner ten windows per request
36afe4b fetch a late-registered token's pre-registration logs from the chain (ledger_chain_logs.py) and merge them into the replay with --chain-logs-file, lowering the token's registration block accordingly
5d375f0 treat an eth_call revert as a definitive empty answer in the batch client instead of a transport failure, so probing a contract without token0() no longer fails the chunk
305bdbd replace the pool-shape heuristic with a deterministic pair-interface probe: an unknown contract a registered token touches is asked once for token0() and token1(), and only a pair or a contract emitting pool events becomes a venue, so trading bots and pass-through contracts keep their positions
dfd28d2 add --reset-discovered to the replay so heuristically discovered venues are forgotten and re-derived under the current classification rules
5269fd2 document the venue discovery rule as built: pool events promote on sight, silent pool-shaped contracts need three sightings and must never be a direct target
a20a519 classify a contract as a venue when it emits swap or sync events, or is pool shaped across three transactions and was never called directly, so trading bots keep their positions; compare james balances at the replay head block with batched, retried reads
3fb44b6 take the reference price from a median of recent sized observed trades so a dust leg cannot poison later estimates, and book an estimate that rounds to nothing as unresolved
f050e84 retry a refused prod connection through the tunnel a few times before giving up, so a transient proxy reset does not abort a replay
560b4ca measure the estimated and unresolved shares over trade flows rather than remaining basis so a token whose observed positions have closed does not read as fully estimated
be9018c retry truncated http reads from the rpc like any other transport error, and let a replay resume from the ledger's last block instead of starting over after a crash
5d8f7ab fetch a chunk's receipts and transaction metadata concurrently so neither waits for the other
9653a47 count every getcode call against the rps budget, retry only the calls the endpoint rejected, and retry a replay chunk after a transient rpc or database error instead of dying
0986369 prepare the next chunk's receipts and transaction metadata in a background thread while the current chunk is netted so rpc waits overlap the fold
cc2a31b fetch receipts only for transactions where a registered token touched the pool manager, prefetch address kinds only for registered and quote token transfers, and send small json-rpc batches so the public endpoint's per-second cap is not tripped
b4db1a8 match a pool event's quote asset by the quote transfer at the venue so a v4 pool is not labelled with another pool's currency that moved through the singleton in the same transaction
56deb64 complete pool manager logs from receipts during replays, corroborate pool event hints with the venue's own token transfers, scale a partially covered leg at the venue price as estimated cost, net pool-shaped contracts as venues from their first sighting and purge the rows a discovered venue earned as a wallet, recognise the v0.8 entrypoint, and grade the moncock fixture on confirmed plus estimated realized pnl
034f3c9 let address classification write through the caller's cursor so venue discovery cannot deadlock against the chunk's open transaction
59c882d book routed trades at the venue event amount so router fees stay out of cost and proceeds, add a per-token ledger wipe and a fixture selector, and grade dust balances relative to the bought amount
aa48e9c expect the graduated fill's venue label to follow the transfer graph like the curve case
1479e75 resolve graduated token fills from the core market event by mapping the market to its base and quote so routed sells through the settler are observed, not estimated
91ba999 lint the ledger package and scripts through the ruff include list, run the ledger store tests on a scratch database instead of truncating the shared side database, and commit the ledger build spec
2cdb45c add the ledger replay that seeds the side database from prod and folds hot blocks through the ledger engine, and the fixture check that grades it
d1559b5 add the ledger engine that nets each block's transactions into wallet flows and wire it into the sequencer behind LEDGER_ENABLED
8ecc7fa add address kind classification with known lists, getcode with the 7702 designator rule, venue discovery and userop sender detection
1aecee9 add net-flow transaction netting for the position ledger with synthetic bundle tests
1af6764 add the ledger transaction metadata and trace stores with batched json-rpc, db cache first, backoff retries and rps limiting
bf5c861 add the ledger schema and store with idempotent ddl, conflict-safe flow inserts, chain-ordered loads, refold with fold delta write-back, tx meta, trace, kind, venue and cached registry access
48beff3 add the ledger fold with three basis buckets, proportional release, parked basis for lp and vaults, custody moves and distinct transaction trade counts
c8c270d add ledger types package with flow dataclasses and constants
```

## Known gaps (implemented behaviour differs from the plan)

### 1. Transfers do not carry cost basis yet (plan §6.4, line 338)

The plan says `transfer_out` releases basis proportionally and `transfer_in` **from a ledger wallet inherits the sender's average cost**; only inflows from unknown senders stay unresolved. The fold currently marks every `transfer_in` unresolved, which is the single largest driver of incomplete PnL and the reason JAMES reports a 44.6% unresolved share. Measured on the JAMES ledger (1,287,202,079 tokens of unresolved inflow):

| source of the inflow | tokens | share |
|---|---:|---:|
| sender has observed buys in the ledger | 1,222,010,728 | 94.9% |
| sender in the ledger, no observed buys | 38,079,484 | 3.0% |
| sender not in the ledger (genuinely unknown) | 27,111,867 | 2.1% |

The clearest case is the distributor `0x8258cf2e72bf`: one observed buy of 884,810,278 tokens, then 2,577 transfers out. Its cost is fully known, yet every recipient reads as unresolved. Implementing the inheritance rule should take the unresolved share to roughly 2%. It is not implemented here because it introduces a cross-wallet ordering dependency in the fold (a receiver's basis depends on the sender's basis at that moment), which is a design change the review round should weigh rather than a bug fix. It also matches existing product behaviour: CLAUDE.md records that the current engine already carries basis across transfers.

### 2. Prod purged the crystal generation while these fixtures were running

Crystal was redeployed on 2026-09-06 and `scripts/purge_crystal_generation.py` deleted 545,686 rows: every crystal token, trade and position. Prod's `launchpad_tokens` now holds nad.fun rows only, so the CHIPOTLE fixture token no longer exists there and the replay refused it. The history itself was never lost, because the log cache is keyed by topic rather than by registry, so `token_scope` now falls back to the side database's own registry and rebuilds the token from logs. Two consequences worth carrying into the full replay: the ledger's token universe must not be taken from prod's live registry, which is now provably lossy, and any wallet's crystal-era positions can only be restored by a replay of this kind.

### 3. Everything else

## Deviations from POSITION_LEDGER_PLAN.md

- Dust threshold in the check is relative, `max(1e15 wei, token_bought / 1e9)`, matching crystal.fun's rule; the spec's absolute 1e15 cannot pass against the real chain balance of the CHIPOTLE wallet.
- Routed trades are booked at the venue event amount when the wallet's own quote delta is within 10% of it, so router fees stay out of cost and proceeds; the wallet's own delta is kept when it is far from the venue amount.
- The estimated and unresolved shares are measured over trade flows (value-weighted), not over remaining basis on open positions, which degenerates once observed positions close.
- Replays resume from the ledger's last block for a token (`--resume`); the ledger's primary key makes re-processing idempotent.
- The JAMES holder check compares prod's live holder list against chain at the replay head block: a prod holder absent from the ledger counts as missing only if it holds tokens at that block. One holder (10,203 tokens) bought after the head and is reported, not failed.
- Prod's JAMES holder list contains pool contracts (two WMON/JAMES pairs holding 175k and 3.9k JAMES as reserves) because the old engine treats pools as holders; the check classifies prod's list before comparing and reports those rows instead of counting them as missing wallets.
- The classifier carries every crystal core generation (CRYSTAL_CORE_ADDRS), not just the live CRYSTAL_ADDR. Main relaunched the core on 09-07 and left the retired one in no list, which would have made a historical replay book positions on it after the merge. Any env-replaceable address list has the same hazard.
- The estimated share is inherent to routed trading, not a history gap: on JAMES it is sourced from venue events (2,446 flows) and reconciliation legs (550), and it sits between 2.7% and 12.4% in every block range across the token's life rather than clustering before the 09-05 V4 topic addition.
- Venue discovery (plan §7, rewritten 09-07): there is no shape heuristic. A contract emitting pool events is a venue on sight; every other unknown contract a registered token touches is probed once for token0()/token1() and is a venue only when it answers as a pair. The earlier two-sighting shape rule had promoted 259 trading bots with prod positions to venues, which silently dropped their JAMES flows; the probe agreed with every event emitter and rejected the bots.
- Traces are requested only within ~500k blocks of head, where the public RPC still serves them; older history resolves from logs, value and venue events, and is marked estimated only when none of those apply.

## Review pointers

- `core/sequencer.py` changes are all guarded by `LEDGER.enabled` (env `LEDGER_ENABLED`, default off); no other old-engine file is touched, so the deployed behaviour is byte-identical with the flag unset.
- With the flag on, `LedgerEngine.process_block` runs inside the indexer's per-block transaction and issues RPC calls (transaction metadata, getCode, occasional traces) from there. That is acceptable for shadow mode but is the reason Phase 1 stays shadow-only: before cutover the RPC work should move off the hot path (prefetch per chunk the way `scripts/ledger_replay.py` does).
- Positions are refolded from the full flow list per (wallet, token) on every flush; cheap at fixture scale, worth a checkpoint before a full-history run.

## Operational notes

- Never point `LEDGER_TEST_DATABASE_URL` at the side database: the engine tests truncate the ledger and seed tables. Use `TEST_DATABASE_URL` with a unique `SCRATCH_DB_NAME`.
- Prod is read only through the tunnel on 127.0.0.1:15433; `prod_conn` retries transient proxy resets.
