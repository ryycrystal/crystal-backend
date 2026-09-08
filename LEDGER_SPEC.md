# The position ledger, in plain language

Branch `accounting-fix`, 2026-09-08. This is the design as it runs today, written for a discussion
away from the code. The status of the rebuild that grades it is at the end.

## The one-paragraph version

Token balances come from the transfer graph and are exact to the wei for every address; nothing in
this document can move a balance. Cost and proceeds come from three rules applied per transaction, per
real wallet, in order: the wallet's own payment, else what the venues at the far end of the token's path
were paid for exactly those tokens, else it is a transfer. The first rule covers every direct trade on
our terminal, nad.fun's terminal, Uniswap and our market. The second rule covers aggregators, solvers,
gmgn-style terminals and 1CT, and it is the only place the difficulty lives, because "what the venues
were paid for this wallet's tokens" is spread over up to a dozen transfers among contracts that have to
be looked through. Traces are an optional fourth tier for what is still unpriced after that.

## The rules

1. **The wallet's own payment prices the trade.** MON it attached to the transaction or received from
   it, WMON or LVMON that left or arrived, in the same transaction. Fees a router took are inside this
   number because the wallet paid them. Where the tokens came from a person's wallet directly, the payment
   must have reached that person, directly or through one contract that forwarded the same amount;
   otherwise any payment to a non-person qualifies, because a routed trade pays one hop and receives from
   another.
2. **If nothing left the wallet, follow the tokens.** Walk backwards (for a buy) or forwards (for a sale)
   through every contract that only passed the tokens on, until a pool, curve, market or another wallet
   is reached. The cost is what those venues were paid for exactly those tokens, taken from the venue's
   own fill event, pro-rata when one fill served several wallets, summed across venues and converted to
   MON when part of it was USDC. A seller paid by one of the contracts on the path counts the same way.
3. **Otherwise it is a transfer.** Cost travels with the tokens between a person's wallets, so funding a
   1CT wallet from an EOA does not turn its later sale into pure profit. Tokens from the zero address are
   a mint or an airdrop at no cost. Tokens into a vault or custody contract are parked, not sold. OTC and
   airdrops have no design of their own; they fall out of this rule.
4. **Optional: the trace.** `debug_traceTransaction` shows native MON moved by internal calls, which
   emit no log. It is fetched only for a movement still unpriced or estimated after rules 1 and 2, once
   per transaction, cached in the database, and the RPC serves it for all history. It matters in two
   places: a native curve payout that went through a router when you want the wallet's exact receipt
   rather than the venue's amount, and venues whose event is not in the cache. If the product accepts the
   venue's amount as the wallet's proceeds, fees ignored, this tier can be dropped.

Which addresses get rows: EOAs, EIP-7702 wallets, ERC-4337 accounts, and a contract that keeps tokens.
A contract that ends the transaction holding exactly what it started with is transparent and gets
nothing; a classified router that keeps up to a tenth as a fee is still looked through, and that fee is
priced to nobody.

## Balances are exact. Here is the fine print.

- The transfer graph is `balanceOf`. Every ERC-20 change emits `Transfer`, the token contract is the only
  authority on its balances, and 5,066 JAMES wallets reconcile to chain to the wei.
- Uniswap v4 can leave a swap's output inside the pool manager as a claim instead of sending the ERC-20
  out. The person's ERC-20 balance genuinely does not change, so the graph and the chain agree. What we
  lose is the price of that swap unless its event is available.
- Our log cache did not store the v4 swap topic before 2026-09-05. That is an indexer configuration gap,
  not a chain fact; those blocks can be backfilled with `scripts/backfill_v4_logs.py`, or priced through
  the trace. Balances were never affected.
- Two halves of one transfer share a log index. Keyed on the log index alone, one half is dropped on
  insert; the ledger lost 65,791 rows that way before adding a side ordinal. A bug, not a chain fact.
- Full block coverage is a precondition. A block never read has no transfers; the 18.4-million-block hole
  in the log cache once made tokens vanish. Coverage is recorded per token and a position is only served
  for a token covered from its creation.

## moncock, in plain English

Wallet `0xb9e37df144f7e6a86da69642a1f01bec7d2035d2` bought moncock four times and sold once, all through
an aggregator. It never signed those transactions: a solver's EOA, `0xf70da97812…`, did. The solver paid a
shared router in USDC, the router paid an executor, the executor bought from pools and over-the-counter
sellers and handed the tokens back up the chain to the wallet. On the sale the tokens went the other way
and the USDC came back to a fourth address.

The row we want is: trader `0xb9e3…`, received 8.15 million moncock, cost 161,773 MON. The buyer is found
by following the tokens to the wallet where they stopped, not by who signed. The cost is found by following
the same tokens backwards to the four places they were bought from and summing what those places were
paid. The 3,986 USDC the solver paid the router is the solver's business and appears nowhere in the
wallet's row. Whatever the wallet paid the aggregator is not in these transactions at all, and does not
need to be: the market cost of what it received is on chain.

| transaction | what the venues were paid for the wallet's tokens |
|---|---|
| buy 101979019 | 44,731.864 + 3,143.979 WMON = 47,875.844 MON |
| buy 101979065 | 99,663.027 + 8,092.435 WMON = 107,755.462 MON |
| buy 101979106 | 121,135.109 + 7,966.891 WMON, 801.707716 USDC at 0.024539 = 161,773.015 MON |
| buy 101979140 | 4,042.605 + 88,936.902 WMON, 68,724.686 MON settled natively to the v4 pool manager = 161,704.192 MON |
| sell 102243456 | 261,831.999 + 20,322.597 WMON = 282,154.596 MON |

Total cost 479,108.51 MON, proceeds 282,154.60, realized loss 196,953.92. The earlier hand-derived
477,018.49 and -193,957 imputed the v4 leg at a tracked pool's price, 2,090 MON under what the trace
shows was paid, and put the sale 906 MON above what the two pools paid. The fourth buy stays an estimate
in the ledger because that v4 swap's event is not in the cache and only the trace shows the payment.

The fourth buy also shows why the trace exists at all: without it the pool manager looked as if it had
been paid 99.66 USDC for 2.9 million tokens, sixteen times cheaper per token than the other pools in the
same transaction.

## Why rule 2 is the hard part

"What the venues were paid for this wallet's tokens" is not a field. In the third moncock buy it is
spread over 21 transfers among 12 addresses in three currencies, with the tokens changing hands four times
before reaching the wallet. Following them is a graph walk, and every item below is a way the walk picks
the wrong fill or the wrong amount. All of them happened on real data; six occur inside that one buy.

Money not where the tokens are:

- the payer is not the receiver: 79% of 7702-wallet buys on JAMES had no payment at the wallet;
- proceeds sent to a third address;
- native MON paid by an internal call, no log, about a third of all trades;
- wrapping: pay MON to the WMON contract, receive WMON from the zero address, pay WMON to the pool;
- USDC converted to WMON inside the route, one payment appearing as two legs;
- a payment forwarded through an escrow versus an unrelated refund passing through the same contract;
- a payment for one token attaching to a nearer airdrop of another in the same transaction;
- router fees in tokens or in quote;
- one delivery assembled from several venues, in two currencies with different decimals;
- one executor serving two wallets from one pool purchase;
- a router that bought part and was handed part, then delivered the lot;
- unspent tokens returned to the wallet by the contract it handed them to;
- cycles through two contracts;
- fills recorded under a market address while the tokens sit with the core contract;
- forged events, and a pair reporting a quote nothing backed (554 trillion MON, once).

Kind ambiguity:

- tokens in and nothing out: a gift, or a purchase someone else paid for;
- a wallet sending to itself through a contract, read as a sale plus a purchase;
- an EOA funding its 1CT wallet;
- hand-off halves at different log positions, one sender to many receivers, a contract that forwards
  before it is funded;
- a pass-through, a fee-keeping router, a bot holding inventory and an unrecognised smart account look the
  same in the graph: 24,033 same-transaction round trips on JAMES were all contracts and none were people;
- vaults, custody, LP shares, mints, burns, and token-for-token swaps with no quote asset anywhere.

## How much each tier covers

On JAMES, 95,074 trades: about half were priced by the wallet's own payment, about a third from a venue
event because nothing moved at the wallet, and under the previous model 9,480 were left estimated and
would have needed a trace. The number under the current model comes out of the rebuild below.

## Open product choices

- **Fees.** Rule 1 books what the wallet paid, fees included. Rule 2 books what the venue was paid, fees
  excluded, because the wallet's own payment is not visible. Either can be made uniform.
- **Traces.** Keep them as the completion tier, or drop them and accept the venue's amount everywhere,
  with pre-Sept-5 v4 history estimated or backfilled.
- **Contracts that keep tokens.** They get rows today so that quantities stay exact for every address and
  so that an unrecognised smart account keeps its history. They need not be served.
- **Which reference price** fills an unresolved movement when neither rule applies: the ledger uses the
  token's recent observed trades, labelled as an estimate.

## Where it is

- Netting: `core/ledger/netflow.py`. Fold and hand-offs: `core/ledger/fold.py`. Engine and traces:
  `core/ledger/engine.py`, `core/ledger/txmeta.py`. Kinds: `core/ledger/kinds.py`.
- Known wallets and invariants: `scripts/ledger_check.py`, `scripts/ledger_verify.py`. Replay:
  `scripts/ledger_replay.py`.
- Tests: `tests/test_ledger_netflow.py`, `tests/test_ledger_fold.py`, `tests/test_ledger_check.py`,
  `tests/test_ledger_engine.py`. Every defect above was written as a test that failed first.

## Status of the grading run

An Azure Container Apps job, `ledger-rebuild` in `crystal-prod-rg`, execution `ledger-rebuild-614uc1x`,
started 2026-09-08 05:31 UTC from image `crystal-backend:ledger-9d40761`. It reads prod's log cache
directly, replays moncock from creation and then JAMES into a Postgres inside the container, runs the
known-wallet checks with the chain comparison over every holder, the verify invariants and the per-token
sweep, and uploads the logs plus a dump of the rebuilt tables to blob storage under
`replay-jobs/out/ledger-9d40761/`. It writes nothing to prod. It stops on its own within eight hours.

Pass means: moncock native_spent 479,108.51 and realized -196,953.92 within 0.5%, JAMES nine of nine
including balance == chain balanceOf for every holder, zero mismatches.

## Glossary

- **holder**: an address that can hold a position: EOA, 7702 wallet, smart account, or a contract that keeps tokens.
- **pass-through**: a contract that ends a transaction holding exactly what it started with; looked through, no rows.
- **venue**: a pool, curve or market; where a price is made.
- **far end**: the venue or wallet a movement traces to once the pass-throughs are looked through.
- **own payment**: quote asset that left or reached the wallet itself in the same transaction.
- **trace**: the transaction re-executed to list native MON moved by internal calls.
- **observed / estimated / unresolved**: priced from the wallet's payment or a venue's fill; priced pro-rata or from a reference; not priced.
