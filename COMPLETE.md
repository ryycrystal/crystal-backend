# accounting-fix: status

Status 2026-09-09, 06:40 China time. **The wallet-boundary ledger is built and graded on three tokens; the
full-registry replay is running on a pool of public RPC nodes after the first attempts hit the public
endpoint's rate cap.** The design is in [LEDGER_SPEC.md](LEDGER_SPEC.md); this file is the state of the
evidence. Nothing has been written to production and `LEDGER_ENABLED` stays off.

## What changed today

The previous model priced every address, including executors and routers, by matching venue events to
movements across a transaction, and carried cost through intermediaries by inheritance. The owner ruled that
out of scope: only people's wallets buying and selling matter, whatever route they took. The netting now
does three things, in order, per transaction and per real wallet: the wallet's own payment prices its
movement; failing that, the tokens are followed through every contract that only passed them on and the
cost is what the venues and paid sellers at the far end received for exactly those tokens; failing that,
it is a transfer, a mint, a burn or a deposit. Traces are fetched for any movement still unpriced after
that, for any block. Intermediaries get no rows. Routers, aggregators, settlers and executors are a known
list, `core/ledger/routers.py`, 143 addresses from the Monad protocols registry and our own flows, and are
looked through even when they keep up to a tenth as a fee. Only pools are discovered.

Reading the first JAMES grading dump overnight found four more things, each a test that failed before its
fix, all on `accounting-fix` and pushed:

- **Liquidity is not a trade.** Every one of the 249 JAMES transactions that fell back to a reference
  price at the v4 pool manager was a liquidity add or removal, not a swap: the pool manager emits
  `ModifyLiquidity`, nothing else, and a v4 position is an NFT in the position manager, so no share token
  ever came back to mark it. Those legs are now `lp_add` / `lp_remove` with the basis parked, which is the
  whole source of the 32 JAMES positions that were more than a tenth estimated.
- **A pass-through forwards what it received first.** The transfer graph is walked FIFO by log index. The
  old walk split a router's outgoing leg pro rata over everything the router received in the transaction,
  so a wallet that bought 4.4 JAMES through a Relay executor was booked half as a purchase and half as a
  transfer from an arbitrage bot whose round trip the same executor handled later. The three "missing
  half" rows in verify were that. The bot itself passes everything through and has no rows.
- **Tokens sent to their own contract are burned**, realizing the loss, instead of releasing cost that
  nobody takes up (the one "cost did not travel" row).
- **Fees shrink the share, not the price.** A fee-on-transfer or fee-keeping hop now charges the venue price
  pro rata for the tokens that arrived; the old walk scaled the wallet's share up to the venue's fill.

The replay itself changed more than the engine. Transaction metadata and receipts are fetched per block
ahead of netting (`eth_getBlockByNumber` and `eth_getBlockReceipts` once per block that moved two or more
registered-token transactions), the RPC client yields to the node instead of dying (twelve attempts with a
jittered 30 s cap, and a limiter that halves on every refusal and earns its rate back one call per answered
batch), and metadata and receipts are spread over a pool of public nodes (`RPC_HTTP_POOL`) while traces
stay on `rpc.monad.xyz`, the only public node that serves them.

## What limits the replay

`rpc.monad.xyz` is a QuickNode public endpoint capped at 50 calls per second per client, counting every
item of a batch, and all Azure executions share one egress address. The first full-registry attempt, 48
partitions at 40 calls a second each, died in refusal storms; the three-token runs on the same endpoint
crawled at one to four blocks a second. Probing from Azure found two other public nodes that answer batches
of 50 with a browser user agent: `monad-mainnet.drpc.org` served 430 transactions a second with no
refusals and `rpc1.monad.xyz` about 300 with many; neither serves `debug_traceTransaction`. The registry
has 5,586,441 hot blocks over 32,221 tokens and needs roughly 1.5 calls per block, so the public pool is
the difference between days and hours. A private RPC key (QuickNode or Alchemy, a few hundred calls a
second) would make the full replay a two-hour job; the owner should get one before the next rebuild.

Two of the public nodes turned out to be worse than useless: `monad-mainnet.drpc.org` and `api.zan.top` answer null, not an error, for any transaction, receipt or block older than a few hours, and the first pool run took those nulls as answers and lost half its receipts (JAMES went from 2.4% to 11.9% of positions with an estimate on nothing but that). The client now treats a null answer for history as a node that has forgotten, cools it down and asks another; the pool in use is `rpc1.monad.xyz` and `monad.rpc.thirdweb.com`, both checked against blocks back to 37.8M. Every run that touched the two bad nodes was stopped and relaunched on image `ledger-9ba4888`.

The registry scan itself was rewritten: the first version's `EXISTS (... JOIN launchpad_tokens)` rescans
the 32k tokens for every one of 62 million block rows and would have run for days; a hashed `IN` subplan
scans 86,000 blocks a second per connection and four range connections finish in four minutes.

## Grading, three tokens

Image `ledger-638fbec` (every fix in this file), one execution per token on the two good public nodes, job
`ledger-rebuild`, runs `ledger-638fbec-moncock` and `ledger-638fbec-james` under blob `replay-jobs/out/`
with their check, verify and sweep logs and a dump of the rebuilt tables.

| check | result |
|---|---|
| moncock checks | 11 of 11: bought / sold 25,719,120.30 exact, 5 trades, native_spent and realized within 0.5% of the chain-derived 479,108.51 and -196,953.92, balance 0 |
| moncock verify | 22 of 24: two bot contracts a few hundred wei negative, and five hand-off rows where a 7702 wallet passed tokens through itself in the transaction it sold in |
| moncock positions with any estimate / over a tenth | 365 of 9,100 (4.0%) / 273, almost all of them liquidity in the nad.fun pair whose events the cache does not hold |
| moncock estimated share / inflow with no cost | 1.73% / 0.00% |
| JAMES checks | 10 of 10: 2,471 prod holders present, 4,948 wallets compared to chain, every person's wallet exact, 9 unclassified contracts reported |
| JAMES verify | 23 of 24: one bot contract at -6 wei |
| JAMES positions with any estimate / over a tenth | 87 of 4,676 (1.9%) / 9 |
| JAMES estimated share / inflow with no cost | 2.25% / 0.00% |
| chipotle | graded by the registry merge (its standalone run at 337k blocks is the registry's own work) |

The moncock figures are derived from the chain transaction by transaction in the checker's docstring. The
old hand-derived 477,018.49 imputed the v4 pool manager leg 2,090 MON low; the trace shows 68,724.686 MON
settled natively. The earlier image `ledger-7c2007a` graded moncock 11 of 11 and JAMES 10 of 10 as well;
what the night's fixes changed is the estimate counts (JAMES 32 positions over a tenth to 9, moncock 340 to
273) and the invariants (JAMES 21 of 24 to 23 of 24).

## Full registry

Scan published 5,586,441 hot blocks over 32,221 tokens, split into 34 partitions. Every partition netted
its range with `ledger_replay.py --all --no-fold` on image `ledger-9ba4888`, 9,372,608 flows in all, no
partition failed. Both attempts to merge inside an Azure job then died on the container's 20 GB disk, so
the merge runs on the owner's machine: load the partition CSVs, refold every token, grade. Two fixes came
out of those failures, both committed: a full refold now folds a token in block windows and carries the
state across, because loading a large token's flows at once was killed for memory, and the merge dumps
before it verifies.

The refold of 31,518 tokens took 90 minutes across four shards and produced 1,231,183 positions.

| registry check | result |
|---|---|
| fixture checks | 23 of 24 |
| moncock | bought and sold 25,719,120.30 exact, 5 trades, spent 478,878.24 and realized -196,723.65 against the chain-derived 479,108.51 and -196,953.92, balance 0 |
| JAMES | 2,468 prod holders all present, 4,967 wallets compared to chain, 0 mismatches on people's wallets, 0 unanswered |
| verify | 21 of 24 invariants |
| negative balances | 38 of 1,231,183: 34 bot contracts, 4 people's wallets at a few thousand wei |
| missing transfer halves | 4 of 9,372,608 flows |
| cost did not travel | 580 of 9,372,608 flows, 0.006% |

The one failing check is a stale fixture, not the ledger. The checker's CHIPOTLE is token `0x8e74f6e9…`,
which the 2026-09-06 crystal relaunch purged from `launchpad_tokens`, so the registry replay never had it
in scope; and the job scripts define `CHIPOTLE` as `0x350035555e…`, which is CHOG, a nad.fun token. Every
"chipotle" figure in earlier runs was CHOG. Grading the real Chipotle needs a token-scoped replay of an
address the registry no longer lists.

## Acceptance

The owner's criterion, counted over people's wallets only: fewer than one position in ten carries any
estimated cost, and none is more than a tenth estimated.

| | registry |
|---|---|
| people's positions with cost | 1,199,453 |
| carrying any estimate | 33,706, 2.81% |
| more than a tenth estimated | 30,178, 2.52% |

The first half passes with room. The second does not, and the cause is concentrated rather than diffuse:
85% of all estimated flows are reference-priced trades at pools, 170,848 flows over 373 venues, of which
35 venues carry 80%. Two factories account for 94% of them. `0x6b5f5643…` is a Uniswap V3 fork whose
pools answer `slot0`, `liquidity` and `fee` but emit their own event signatures, so we never decoded
their fills. `0x182a9271…` is plain Uniswap V2 pairs emitting the standard `Swap` and `Sync`, which we
already decode and throw away at ingest, because `accepts_log_for_indexing` admits V2 pair events only
from a hardcoded address list while V3 swaps are admitted from any address.

That gives two levels of repair. Level one is the transfer-graph rules already committed here, which
reclassify liquidity as liquidity without decoding anything; it needs only a re-net of the affected
116,349 blocks across 257 tokens, 2% of the registry. Level two collapses the double trust gate at ingest
and adds the fork's decoder, which makes those trades exactly priced; it is deferred until after launch
by the owner's decision. Neither is a hotfix on top of the other: level two removes a gate rather than
adding a case, and one re-net can carry both whenever it lands.

## Open

- One bot contract on JAMES sits at -6 wei: it sent 6 wei more to the pool manager than it received in the
  same transaction, so it held dust from somewhere the ledger did not see. Dust, on a contract, unexplained.
- Cross-token OTC-like swaps between two contracts (JAMES against USDC between bot contracts) produce a few
  absurd prices on contract positions; people's wallets are not affected.
- Serving: positions exist for contracts that keep tokens so that quantities stay exact; the serving layer
  should filter them by kind so a bot's contract never appears as a user.
- A private RPC endpoint for rebuilds.
- The live path stays gated until the full-registry grading is read.

## Level one, applied and graded

The 116,349 blocks carrying reference-priced flows, across 257 tokens, were re-netted on image
`ledger-8130a83` in ten Azure shards and applied to the merged registry: 229,941 flows replaced by exactly
229,941, and the 257 tokens refolded into 661,921 positions in seventeen minutes.

| | before | after |
|---|---|---|
| reference-priced flows | 170,848 | 131,289 |
| positions carrying any estimate | 33,706, 2.81% | 29,228, 2.44% |
| positions more than a tenth estimated | 30,178, 2.52% | 27,050, 2.26% |
| moncock over a tenth | 321 | 270 |
| JAMES over a tenth | 22 | 10 |
| CHOG over a tenth | 2,711 | 2,508 |

Nothing regressed. Verify holds the same 21 of 24 with the same three failures, 38 negative balances of
which 34 are bot contracts and 4 are people's wallets holding a few thousand wei, 4 missing transfer
halves and 582 cost-travel rows out of 9,372,608 flows. Moncock still answers its hand-derived targets
inside tolerance with every trade observed, and JAMES still has no missing holders and no mismatch on any
person's wallet.

So level one removes a quarter of the reference-priced flows but only a tenth of the badly-estimated
positions, because those flows are spread thinly rather than concentrated in the worst positions. The
prediction from the pilot shard, a 24.6% fall in flows, was accurate; extrapolating it to positions was
not.

**What is left, measured rather than guessed.** Of the positions more than a tenth estimated, 12.8% draw
their estimate from a venue that trades one launchpad token directly against another, where no MON price
exists on chain at all and no amount of decoding can produce one. The other 87.2% are trades against a
quote asset that level two would price exactly. That puts the floor at roughly 4,000 positions, 0.3%, and
means the criterion is better stated as no position more than a tenth estimated except where the trade
itself had no MON price.

## Ten thousand positions against prod, and the bug it found

Ten thousand random traded positions were compared with prod, with every balance settled against the
chain rather than against either system's opinion.

| balances at block 103,098,148 | |
|---|---|
| identical | 9,921 |
| the chain agrees with the ledger, not prod | 55 |
| the chain agrees with prod, not the ledger | 0 |

Every other difference falls into a known bucket: 3,803 differ only by the fee convention, where prod
books the venue's leg and the ledger books what the wallet actually paid; 383 carry one of prod's synthetic
`reconciliation` legs; 176 are sales prod never attributed to the wallet at all, its documented router
attribution bug, with the balances agreeing; 364 are identical; 24 are absent from the ledger; 2 traded
after the ledger's head.

Realized looks far worse than it is. Of 1,483 positions where both systems recorded a sale, 98.7% have
realized moving no more than its own components moved: the median absolute gap on proceeds is 6.84 MON and
on realized 7.30 MON, nearly the same MON, but proceeds is a large number and realized is the small
difference of two large ones, so the same gap reads as 1% against one and 34% against the other.

Ten positions moved more than their components could explain and each was traced: one where prod's realized
contradicts its own spend and proceeds, four where basis legitimately travelled with transferred tokens,
four differing under one percent through basis allocation on partial sales, and one where the ledger's
realized is exactly proceeds minus spend while prod's is not.

**The sweep found one real defect, now fixed.** `_less_conversions` cancelled any unmatched inbound leg of
the quote asset against the payment, testing only that it did not come from a person's wallet. So a wallet
that received 97 USDC from an unrelated contract and sent 100 USDC into a router had its purchase priced
at 2.75 USDC, about 95 MON, against a pool that was paid 3,480 MON, turning a 2,834 MON loss into a 550 MON
profit. The rule now cancels only a genuine conversion: the asset minted from the zero address or its own
contract, or change handed back by the party that was paid. Commit 54702ba, test-first.

That fix cannot be applied to a slice, because the cancelled leg is never written down, so the flows cannot
say which transactions it touched. Repairing history means re-netting the whole registry, about four hours.
Until then the tables loaded in prod carry the defect.

## What the reference-priced flows actually were

Level two was going to decode the nad.fun pair fork's own swap event so those trades could be priced
exactly. Reading the fork's logs against the transfers changed the diagnosis. At the largest such pool,
every normally priced transaction was a swap, one asset in and one out, and every reference-priced one
was a liquidity withdrawal, both assets out and nothing in. Followed to the end, the pool paid both assets
to a router, a second contract gave the token to one wallet and split the WMON between two other
addresses, and that wallet paid nobody. The engine had called it a purchase at 2.43 MON because the tokens
traced back to a pool, and `_classify` treated any venue-adjacent movement with no payment as a trade to
be priced at the token's market rate.

So the decoder was the wrong tool: there was no trade to price. The fix, commit a8ee6a9, is structural.
Before inventing a price, the engine asks whether the pool both took something in and paid something out
in that transaction, counting ERC-20 legs, the transaction's own value, and native movements in the trace.
A pool that moved assets one way only was handing liquidity back or accepting it, and the movement is
booked as `lp_remove` or `lp_add`: parked cost is restored where this wallet parked some, and otherwise the
tokens arrive with no cost, exactly as an airdrop does. The rule waits until the trace has been consulted,
because the v4 pool manager settles in native MON by internal call and would otherwise look one-way on a
real swap; the trace was already being fetched for every one of these flows, so nothing new is spent.

Together with the third-party-inflow fix (54702ba), this is what the next full re-net carries. Both are
netting changes, so the registry is re-netted from the log cache on image `ledger-a8ee6a9` rather than
patched: the cancelled leg of the first bug is never written down, so its reach cannot be read from the
flows.

## Cutover

Measured on 2026-09-09 before starting: prod holds **none** of the ledger tables, so the load is purely
additive. It creates `wallet_flows`, `positions_v2`, `ledger_token_coverage`, `address_kinds`, `venues`,
`parked_entitlements`, `token_fold_state`, `tx_meta`, `tx_traces` and `ledger_receipt_logs`, and touches
no `launchpad_*` or `crystal_*` table. It is reversible by dropping them.

The gap between the registry's last block, 103,098,148, and the live head is 189,717 blocks of which only
**2,393 are hot**, about three minutes of replay, growing by roughly 2,700 hot blocks a day. Catch-up is
therefore not the obstacle it looked like.

Order of operations, decided with the owner on 2026-09-10. The API keeps reading `launchpad_positions`;
what changes is who writes it.

1. Re-net the registry on `ledger-a8ee6a9`, which carries both pricing fixes, and merge it locally: load,
   refold in four shards, checks, verify, sweeps, and the ten-thousand-position sweep against prod. The
   residue must collapse to the fee convention and prod's own bugs before anything below runs.
2. Reload prod's ledger tables from those partitions (`ledger_prodload.sh MODE=replace`) and refold in
   prod on `ledger-8130a83`, whose fold is identical but predates the projection, so the refold fills
   `positions_v2` without touching the served table while the legacy accumulator is still live.
3. Replay the catch-up blocks, a few minutes.
4. Merge to `main`, build, roll `crystal-indexer` with the CLI, set `LEDGER_ENABLED=1`. From that block the
   fold writes `launchpad_positions` and `BatchAccumulator.flush` no longer does (commit 05209fb).
5. Immediately run `overwrite_positions.py` with `APPLY=1`: snapshot the table, upsert every ledger
   position onto it, remove the 37,724 router, pool and contract rows the ledger never saw move, leave the
   273 people's rows it lacks for the catch-up. Dry-run validated against prod.
6. Watch the `[SQ]` lag for a day. The V2-acceptance widening follows later on its own branch.

Step 4 is the one with a production cost worth watching: netting each block fetches transaction metadata,
sometimes receipts, and occasionally a trace, so the indexer's RPC load rises. The chain produces about
2.5 blocks a second and the replay sustained 8 to 27, so it should keep up.

## Serving-side fixes that ride with the flip

- `2dedb6b` the sequencer's attribution reconciler stands down once the ledger is on. It invented a trade
  for whatever a venue's amount and the wallet's transfers disagreed by, stamped 1970 when it lacked the
  block's timestamp, about a thousand rows an hour. 207,698 of them were removed from `launchpad_trades`
  (snapshot `launchpad_trades_reconciliation_20260910`); they never touched the ledger's balances.
- `ea34686` the portfolio's daily series is a slice of `wallet_flows`, so the graph and the headline are one
  source. Until the flip and catch-up the series ends at the ledger's head and says so in `as_of_block`.
- `ae28f3a` the spot graph ends on the live total the same response reports, marked `live`, instead of the
  last stored hourly bucket, which a background fill used to refresh only after the first response.

Still on the legacy trade rows: the activity feed (`portfolio_history`, `portfolio_last_trades`) and the
users-table aggregates behind the leaderboard.

## Interim overwrite, 2026-09-10 19:50 UTC

At the co-founder's request, relayed and confirmed by the owner, `launchpad_positions` was overwritten from
the ledger as it stood in prod before the two pricing fixes: the level-one netting with the third-party
inflow bug and the one-way pool prices still in it. Snapshot `launchpad_positions_pre_ledger_20260910a`,
1,231,183 rows upserted, 37,725 router, pool and contract rows the ledger never saw move removed, 299
people's rows the ledger lacks kept, 1,056 rows for uncovered tokens untouched. The indexer was not
flipped, so the legacy accumulator keeps adding onto these rows until the flip; the reload chain in Azure
is unaffected and the fixed data replaces this table in the same flip-then-overwrite step as before.

## Running it

Everything runs as executions of the Container Apps job `ledger-rebuild` in `crystal-prod-rg` with
per-execution overrides: `az containerapp job start --image <acr>/crystal-backend:ledger-<sha> --command
bash /app/scripts/ledger_job_entry.sh --env-vars ... RUNNER_URL=<blob url of the runner script>`. The
runners live in blob `replay-jobs/ledger/`: `ledger_runner_nofold.sh` for one token (`ONLY=<name>`),
`ledger_scan.sh`, `ledger_part.sh` (`PART=all_pNN`), `ledger_merge.sh`; `RPC_HTTP_POOL` names the pool
of public nodes and `RPC_MAX_RPS` the per-node cap. Images are built with
`az acr build --no-logs -r crystalprodacr -t crystal-backend:ledger-<sha> .` and confirmed with
`az acr repository show-tags`. Locally, `scripts/ledger_replay.py --token <addr> --blocks-file <list>
--wipe-token` rebuilds one token into the side database through the log cache, `scripts/ledger_check.py
--fixture <name>` grades the known wallets, `scripts/ledger_verify.py --dsn <file>` runs the invariants,
and `scripts/ledger_sweep.py <token>` prints the acceptance lines. The scratchpad's `explain_tx.py <block>
<txhash>` re-nets one transaction, trace included, and with `EXPLAIN_HINTS=1` prints the venue events and
hints the engine saw.
