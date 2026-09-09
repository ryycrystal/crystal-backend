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

Scan published `replay-jobs/ledger/all_blocks.json` (5,586,441 hot blocks over 32,221 tokens) and 34
partition lists (the launch-era range split in three). Every partition netted its range with
`ledger_replay.py --all --no-fold` on image `ledger-9ba4888` into a Postgres inside the container and
uploaded its tables as CSV: 9,372,608 flows in all, 862 MB gzipped, no partition failed. The merge loads
the CSVs, refolds every token, dumps the merged database to `out/ledger-9ba4888-all5/merged/`, then runs
the checks (moncock, JAMES, chipotle), verify, the sweeps and the acceptance query.

The first merge attempt taught two things about the merge container, both fixed: folding a token by
loading all of its flows at once was killed for memory 1,500 tokens into the registry (the largest tokens
carry millions of flows), so a full refold now folds in windows that end on block boundaries and carries
the state across (`store._refold_in_windows`, test-first, commit 8130a83); and verify's joins over nine
million flows filled the container's 20 GB disk before the dump was written, so the merge now dumps first,
caps temp files at 3 GB and drops the CSVs after loading. The second merge is running on `ledger-8130a83`;
its outputs replace this paragraph.

What that merge cannot fix by itself: the flows were netted by `ledger-9ba4888`, which predates the nad.fun
pair-liquidity rule (638fbec) and the position-token rule (769c8d0). Tokens with liquidity in nad.fun
pairs will show those legs as reference-priced trades until the registry is netted again on the newest
image, which is a rerun of the 34 partitions (about four hours on the two public nodes, two on a private
RPC).

## Acceptance

The owner's criterion, printed by `scripts/ledger_sweep.py` on every rebuilt token: fewer than one position
in ten carries any estimated cost, no such position is more than a tenth estimated, people's wallets match
chain to the wei, and any unclassified contract among the top 50 holders is listed for a decision. JAMES on
`ledger-7c2007a`: 2.4% of positions with any estimate, 32 over a tenth, every one of them a liquidity
position that the `lp_add` fix reclassifies; 13 unclassified contracts among the top 50 holders, all but
one passing tokens to nobody (lockers, vesting, multisigs).

## Open

- One bot contract on JAMES sits at -6 wei: it sent 6 wei more to the pool manager than it received in the
  same transaction, so it held dust from somewhere the ledger did not see. Dust, on a contract, unexplained.
- Cross-token OTC-like swaps between two contracts (JAMES against USDC between bot contracts) produce a few
  absurd prices on contract positions; people's wallets are not affected.
- Serving: positions exist for contracts that keep tokens so that quantities stay exact; the serving layer
  should filter them by kind so a bot's contract never appears as a user.
- A private RPC endpoint for rebuilds.
- The live path stays gated until the full-registry grading is read.

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
