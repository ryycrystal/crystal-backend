# accounting-fix: the four seams, built and rebuilt on real data

Status at 2026-09-08 02:35. **Not ready for review yet:** JAMES is rebuilt and clean, moncock and chipotle
are still replaying. This document is written as they land, and the header changes to REVIEW only when all
three fixtures pass their own checks. Nothing here has been written to production, and `LEDGER_ENABLED` must
stay off.

The previous version of this file was corrected by six external reviews, which established that its green
table proved the **quantity** layer and nothing about the **value** layer. That distinction still governs
everything below: every defect found in this work conserves net token quantity per wallet, which is exactly
why balance and supply checks stayed green through all of them.

---

## What was built

Four seams and a prerequisite, each with fixtures written to fail on the defect before the fix existed.

**Step 0, coverage.** A replay records, in the same transaction as the flows it writes, which block range it
actually read for each token. A position is served only for a token whose coverage reaches back to its
registration. A token seen only because another token's transaction moved it keeps its flows as evidence and
gets no position, which is where the leftovers' 389 negative balances came from. A cached block list
certifies coverage only from its own first block, so a list built for a later window cannot claim a history
it never read.

**Seam 1, the movement.** Flows are built from individual movements matched to the payment that funded them,
rather than one netted row per wallet and token per transaction. A venue event counts as evidence only from
a classified venue. A swap that settles against Uniswap V4's claim balances produces movements from the
event and the pool's own registration. Two consequences worth knowing:

- Router fees stay in cost. A wallet paying 8,684.93 for tokens the pool received 8,598.08 for used to have
  the 86.85 difference deleted. Cost is now what the wallet paid and proceeds are what it kept, so cost
  basis rises slightly on every routed trade and realized falls by the same amount.
- A movement's identity carries its side. Keyed on the log index alone, the two halves of one transfer
  collided on the primary key and one was dropped on insert.

**Seam 2, the disposal shape.** Each flow stores the whole vector the fold computed rather than two totals,
so a sale drawing on observed, estimated and unresolved inventory at once is recoverable from its row. An
unpriced disposal no longer books the released basis as a loss: it leaves the running average and waits in
its own bucket until proceeds are known. Every field the fold carries is persisted, so a stored position is
a checkpoint a later fold resumes from.

**Seam 3, the ordered fold.** A token is folded as one pass across all its wallets in chain order, so a
transfer hands its cost to the receiver instead of destroying it. The fold resumes from the persisted
checkpoint and loads only the wallets in the new flows; a flow landing at or below the watermark folds the
whole token again, which is the correction path and stays off the common one.

**Seam 4, parked basis.** Basis parked in a pool or vault is recorded against that pool or vault. Pooled
into one aggregate, withdrawing 100 from a vault that held it at 100 returned 550, the average of every
vault the wallet had used.

**Also shipped.** The sequencer reaches the ledger through a gate that imports nothing from `core.ledger`
while the flag is off, and refuses to start against a database without the ledger tables rather than raising
inside the block transaction. The replay and the live engine share one rule for what a MON was worth, where
they previously differed in five ways.

---

## What the rebuilt data found that 774 passing tests did not

Each of these was found by rebuilding JAMES and asking the stored rows a question no test had asked. Each is
now a test. The suite was green before every one of them.

| what was wrong | how it showed | why no test saw it |
|---|---|---|
| both halves of a transfer shared a primary key | 65,791 of 72,690 wallet-to-wallet transfer rows had lost their counterparty half | the netting tests never wrote to a database, and the collision only happens on insert |
| parked basis was deleted and never rewritten on a full fold | no parked rows survived any replay's first flush | an early return that read as "nothing to do" |
| a movement took its counterparty from the wallet's largest neighbour | 2,751 rows named the trading partner instead of who received a 0.2% fee | correct while a leg was a netted position, wrong once a leg became one movement |
| a wallet sending tokens to itself was read as a token swap | three transactions booked a sale and a purchase that cancelled | the pair is opposite-signed and unpriced, which is exactly a swap's shape |
| a pair swap with no transfer of its own invented a second disposal | one wallet sold the same tokens twice, into a negative balance | the rule was written for V4, which really can settle with no transfer |
| a pair's reported quote was taken as a price with nothing backing it | two flows booked 554 trillion MON against a token priced at 0.06 | the amount matched the token side; nothing checked the quote side |
| a seller's implied price came from its net position, not from what it sent | a relayer forwarding everything netted to wei of dust, and its whole payment divided by that dust priced the movement that passed through it | the arithmetic is right for the quantity it was given; only the quantity was wrong |

---

## JAMES, rebuilt from its creation block

153,389 flows across 5,051 wallets, blocks 85,819,844 to 102,523,914, coverage complete from creation.

| measure | before this work | now |
|---|---|---|
| inflow arriving with no price of its own | 44.617% | 35.698% |
| inflow that still has no cost at all | not measurable | 3.424% |
| positions holding any unresolved tokens | not measurable | 101 of 5,051 |
| estimated share of traded value | 7.372% | 6.387% |

The first row counts movements that arrive without a price of their own, which a transfer never has. The
second is the one that matters after seam 3, because a transfer now delivers the sender's cost: it is the
share of all inflow that ends up in the unresolved bucket. Reviewer 7 predicted this would fall to roughly
2%; it is 3.4% of inflow and 2.0% of positions.

Fifteen structural checks hold on the rebuilt data, including every defect listed above: no negative
balance, no negative inventory, every position's inventory equal to the sum of its flows' effects, every
transfer's other half present, every transfer's cost travelling with its tokens, parked totals equal to the
per-venue rows, and no position served without coverage from creation.

### The one thing that does not hold, and its size

Observed prices span more than a thousandfold from the token's median on 433 of 84,966 trade flows. Those
flows carry **0.010% of traded value**. The cause is that a wallet making several movements of one token in
one transaction has its receipts attached to the nearest movement of opposite sign, and proximity is
sometimes wrong: on transaction `0xf8f9f66354` a 0.48-token movement was given 5,729 MON while a
33,605-token movement in the same sale was given a fraction of a wei.

Under average cost this does not reach the position: realized comes from the wallet's totals, and those are
conserved. What it distorts is `price_native` on the individual flow, which a later flow can read as a
reference price. It is pinned as a known limitation with a test asserting the conservation that does hold.

---

## Fixture checks

Pending. moncock and chipotle are replaying; `ledger_check.py` and `ledger_verify.py` run when they finish.

---

## Known gaps, deliberately

- **The fee is not exposed as its own field.** Cost is what the wallet paid, which is the conservation the
  reviews asked for, but a consumer cannot recover what the venue received separately from what the router
  took.
- **Nothing has been replayed beyond these three tokens.** The value layer is proven on fixtures and on one
  fully swept token, not across the registry.
- **A replay from China runs at about 50 blocks a second**, almost all of it round trips. The same code was
  measured at 160 to 640 blocks a second running next to the database in Azure, which is where any
  registry-wide replay has to happen.
- **`LEDGER_ENABLED` stays off.** The gate now makes turning it on safe rather than fatal, but no consumer
  reads these tables and none should until the value layer is proven more widely.
