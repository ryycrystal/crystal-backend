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

## Cutover

Measured on 2026-09-09 before starting: prod holds **none** of the ledger tables, so the load is purely
additive. It creates `wallet_flows`, `positions_v2`, `ledger_token_coverage`, `address_kinds`, `venues`,
`parked_entitlements`, `token_fold_state`, `tx_meta`, `tx_traces` and `ledger_receipt_logs`, and touches
no `launchpad_*` or `crystal_*` table. It is reversible by dropping them.

The gap between the registry's last block, 103,098,148, and the live head is 189,717 blocks of which only
**2,393 are hot**, about three minutes of replay, growing by roughly 2,700 hot blocks a day. Catch-up is
therefore not the obstacle it looked like.

Order of operations, all but the last step invisible to users:

1. Load the rebuilt registry into prod's new ledger tables from the partition CSVs already in blob, with
   the re-net applied on top (`scratchpad/ledger_prodload.sh`, an Azure job so the data never crosses the
   China link). It refuses to run twice unless forced.
2. Refold in prod, sharded, about 25 minutes.
3. Replay the catch-up blocks, three minutes.
4. Set `LEDGER_ENABLED=1` on `crystal-indexer` so the live path keeps the ledger current. Overlap with
   step 3 is harmless: a scoped block is deleted and rewritten, so replaying a block the live path has
   already seen is idempotent.
5. Point the API at `positions_v2`, filtering by address kind so a bot's contract never appears as a user.

Step 4 is the one with a production cost worth watching: netting each block fetches transaction metadata,
sometimes receipts, and occasionally a trace, so the indexer's RPC load rises. The chain produces about
2.5 blocks a second and the replay sustained 8 to 27, so it should keep up, but watch the `[SQ]` lag after
enabling it. Step 5 is the only step users can see, and it is the one that still needs writing: nothing in
`api/` reads `positions_v2` today.

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
