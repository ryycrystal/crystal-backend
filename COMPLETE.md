# accounting-fix: status

Status 2026-09-08 evening. **The ledger was rewritten to the wallet-boundary model, graded once on real data,
and is being graded a second time with the router list and chipotle.** The design is in
[LEDGER_SPEC.md](LEDGER_SPEC.md); this file is the state of the evidence. Nothing has been written to
production and `LEDGER_ENABLED` stays off.

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

The rewrite replaced the pricing half of `core/ledger/netflow.py` and re-keyed the fold's hand-offs by
sender and receiver. An adversarial review of it found eleven defects in the first draft, each now a test
that failed before its fix: tokens returned through a pass-through booked as a purchase, a fee router
inflating proceeds, a seller paid by a later hop leaving the buyer unpriced, a v4 settle paid by a real
transfer counted twice, mixed WMON and USDC fills summed in raw units, a payment attaching to a nearer
airdrop of another token, order-book fills never pooling, a missing USDC rate dropping legs silently, an
observed purchase taking up cost its seller released to someone else, a pass-through forwarding before it
was funded losing the cost, and the checker exempting contracts. The suite is green at 804.

## Grading, first execution

Azure Container Apps job `ledger-rebuild`, execution `ledger-rebuild-614uc1x`, image `ledger-9d40761`,
before the router list. Moncock and JAMES replayed from creation into a Postgres inside the container,
reading prod's log cache directly, in 75 minutes.

| check | result |
|---|---|
| moncock token_bought / token_sold | 25,719,120.30, exact |
| moncock trade_count | 5 |
| moncock native_spent (confirmed + estimated) | 478,878.24 against 479,108.51 chain-derived, within 0.5% |
| moncock realized (confirmed + estimated) | -196,723.65 against -196,953.92, within 0.5%, all of it observed |
| moncock balance | 0 |
| JAMES prod holders present | 2,470 checked, 0 missing |
| JAMES chain balanceOf == balance + custody | 4,949 wallets, every person's wallet exact, 10 unclassified contracts differ |
| moncock estimated share / inflow with no cost | 3.85% / 0.12% |
| JAMES estimated share / inflow with no cost | 3.52% / 0.00% |

The moncock figures are derived from the chain transaction by transaction in the checker's docstring. The
old hand-derived 477,018.49 imputed the v4 pool manager leg 2,090 MON low; the trace shows 68,724.686 MON
settled natively. Under the previous model the same token read 11.4% estimated and 31.5% of inflow without
a price of its own.

Verify held 20 of 24 invariants. Of the four that did not: one was written before a movement could be
split into two legs at one chain position and has been rewritten to key on the party each leg names; three
rows lack the other half of a transfer and 80 hand-offs released more cost than was taken up, both under
investigation with the second execution's dump; and moncock's supply conservation shows 2.4% of supply
held by addresses that are neither positions nor probed venues, also to be read from the dump. Observed
prices more than a thousandfold from the median: 27 of 25,347 on JAMES, 164 of 92,916 on moncock, the
largest an absurd 1.1e21 MON per token on what is almost certainly a dust-sized leg.

The ten contracts that differ from chain are `pair_probe` negatives: other venues' pools and fee sinks
holding between 2 wei and 2,349 JAMES. They are now reported for review rather than failing the run, and
the sweep lists any unclassified contract among a token's top 50 holders.

## Grading, second execution

`ledger-rebuild-c9s97hq`, image `ledger-3b18bd8`, started 15:33 UTC: the router list, the checker and
verify changes above, chipotle added (337,308 hot blocks from creation, target 20 trades and 13,850.173
MON realized), and a dump of the rebuilt tables uploaded to blob `replay-jobs/out/ledger-3b18bd8/` for
local inspection. Results replace the table above when it finishes.

## Acceptance

The owner's criterion, printed by `scripts/ledger_sweep.py` on every rebuilt token: fewer than one position
in ten carries any estimated cost, no such position is more than a tenth estimated, people's wallets match
chain to the wei, and any unclassified contract among the top 50 holders is listed for a decision. On the
first execution JAMES had 17 of 3,268 positions with any estimate, 12 of them over a tenth, on the stale
local copy; the job's own sweep numbers come with the second execution.

## Open

- The three missing halves and 80 hand-offs with unmatched cost, from the dump.
- Moncock's 2.4% of supply outside positions and probed venues, from the dump.
- The dust-leg price outliers: a leg of a few wei priced pro-rata should be labelled, not counted.
- Serving: positions exist for contracts that keep tokens so that quantities stay exact; the serving layer
  should filter them by kind so a bot's contract never appears as a user.
- Product choices, listed at the end of the spec: fees under rule 2, traces or venue amounts, and
  which contracts to serve.
- The live path stays gated until the second grading passes.

## Running it

The job is defined by `make_job_yaml.py` in the session scratchpad and runs `ledger_runner.sh` from blob
`replay-jobs/ledger/`; it needs the prod read-only credentials, a container SAS, and an image built from
the branch with `az acr build --no-logs -r crystalprodacr -t crystal-backend:ledger-<sha> .`. Locally,
`scripts/ledger_replay.py --token <addr> --blocks-file <list> --wipe-token` rebuilds one token into the
side database through the log cache, `scripts/ledger_check.py --fixture <name>` grades the known wallets,
`scripts/ledger_verify.py --dsn <file>` runs the invariants, and `scripts/ledger_sweep.py <token>` prints
the acceptance lines. The scratchpad's `explain_tx.py <block> <txhash>` re-nets one transaction, trace
included, and prints every movement the engine saw and every row it produced.
