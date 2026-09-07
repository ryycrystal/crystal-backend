# Reviewer 5: break it with real transactions.

Read `review/README.md` first. Write your report to `feedback5.md` in the repo root.

The other reviewers are adjudicating claims already on the table. You are not. Your job is to find what
nobody has found yet, using real on-chain transactions rather than reasoning about code.

Everything verified so far rests on three tokens, and two of the three are ordinary. Every accepted result is
a quantity check: balances reconcile against `balanceOf`. Almost nothing checks cost basis, realized PnL, or
trade counts against independently derived truth, and only two wallets in the entire effort have been
hand-verified.

## Method

Pick real transactions off chain, derive by hand what each wallet's position should become, then compare
against what the ledger produced. Where the ledger has not replayed the token, replay it into the side
database yourself (one at a time, never two concurrently) or construct the bundle directly the way
`tests/test_ledger_netflow.py` does.

Shapes worth hunting, roughly in order of how likely they are to be mishandled:

- **Aggregator splits**: one buy sourced from several pools plus an over-the-counter seller in one
  transaction. moncock has these; find more, and check the quote attribution rather than only the quantity.
- **ERC-4337 bundles**: several users' operations in one transaction, where `tx.from` is a bundler that must
  never hold a position, and the real senders come from `UserOperationEvent`.
- **Uniswap V4**: native-currency pools settle the quote leg with no token transfer, so the cost has to come
  from the swap event or a trace. Note that the V4 topics were only indexed from 2026-09-05, so the log cache
  has no PoolManager rows before that; a replay of earlier history imputes those legs.
- **Custody and order-book fills**: inventory changing without an ERC-20 transfer.
- **LP add and remove, vault deposit and withdraw**: where basis is parked and returned.
- **Rebasing, fee-on-transfer, or otherwise unusual tokens**, if any exist in the registry.
- **A token whose pool is not in any registry**, so the classifier meets it cold and must decide from the
  pair probe alone.

## Adversarial construction

Then try to build a transaction that makes the ledger book something false, and see whether it is reachable
in practice. Two specific attacks worth attempting, because the classifier is what protects the position
table from filling with pools and bots:

- **Make a wallet look like a venue.** The classifier promotes an unknown contract to `venue_pool` if it
  emits a swap or sync event, or if it answers `token0()` and `token1()` with two distinct addresses. A
  contract that answers those getters, or emits an event with the right topic, is excluded from positions
  entirely and its holdings vanish from the ledger. How cheap is that to do deliberately, and does any real
  address already do it accidentally?
- **Make a venue look like a wallet.** A pool with undecoded events that does not answer the pair interface
  stays a `contract_unknown` holder and accumulates a position. Find one on chain if it exists.

Also worth probing: what happens when the same address is a wallet on one token and a venue on another, and
whether classification can flip between replays and silently change history.

## What to report

Concrete failures with the transaction hash, the derived truth, and the ledger's output. If you cannot make
it fail, that is a real result too, and more valuable than a list of theoretical concerns: say precisely what
you tried and what held.

Rank what you find by how often it occurs in the side database or in production's trade history, so the human
can tell a corner case from a systematic error.
