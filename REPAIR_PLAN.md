# Repair plan v2: three seams, one rebuild

Revision of the v1 plan, which was reviewed in `feedback7.md` and found wrong in specific ways: its five
steps were really three, its flow key could not dedupe, its action definition failed in both directions,
its step D was a layer, its step E was 28x too slow, and it dropped four confirmed P1s while claiming their
audit had never been delivered. All six criticisms are accepted. This version follows the sequence
`feedback7.md` proposed, with the four dropped findings folded back in.

Sources: `feedback2.md` (14 findings), `feedback3.md` (six confirmed, ordering), `feedback4.md` and
`feedback8.md` (findings 7-14 confirmed, verification machinery audited), `feedback5.md` (claim-settled V4),
`feedback6.md` (consumer contract, cutover), `feedback7.md` (this plan's predecessor, rejected).

## The rule this is built under

Fix the seam, never patch the boundary. Duplicated domain knowledge across files is the tell that a seam is
missing. And, from `feedback8.md`, the acceptance rule that should have applied from the start: **a check
must be shown to fail on the defect it targets before it is allowed to pass on the fix.**

---

## Step 0: coverage, before anything else

Everything else is measured through it, and today nothing records what a replay actually covered.
`--resume` uses the last recorded activity as if it were coverage, which on the leftover tokens would skip a
median of 13.4 million blocks. 129 of the 159 leftovers hold flows that are purely incidental to some other
token's replay, and 389 positions carry negative balances as a result.

Per-token contiguous coverage ranges, committed with the flows in the same transaction. A token whose
coverage is incomplete is excluded from positions rather than served partially. Until this exists the
leftovers cannot be used as evidence and no fixture can demonstrate the identity fix working.

Addresses finding 7.

## Step 1: the movement seam

The single largest change, and the one v1 split into three. `_Leg`, one netted record per wallet and token
per transaction, is replaced by a `Movement`:

```
(txhash, evidence_log_index, side, wallet, token, quantity,
 evidence_kind, counterparty, attribution_evidence)
evidence_kind in {erc20_transfer, venue_swap, curve_trade, core_fill, custody}
```

Identity is that tuple plus an interpretation version. No ordinal, no anchor, and `side` gives the ordered
fold its ordering for free. `ON CONFLICT` becomes replace-if-newer-version rather than do-nothing, so a
re-replay corrects rather than duplicates.

Three things ship together here because they are one seam:

- **Actions are a matching over movements, not a connected component.** v1 proposed connectivity, which
  provably fails in both directions: a wallet buying and selling one token through one pool is a single
  component, so grouping degenerates to netting, and a gift plus an unrelated payment is also a single
  component, so the coincidence inference survives. Matching a token movement to a quote movement is what
  distinguishes them.
- **Evidence other than an ERC-20 transfer counts.** A V4 swap settled in claim balances, a curve trade and
  a core fill are movements in their own right. Attribution comes from the event's own actor field, not from
  the transaction origin, which `feedback7.md` showed is wrong on the very transaction v1 cited.
- **The emitter gate, widened to the generation list.** Without a gate, admitting venue events is a
  regression: any address can emit a swap topic. With the live-address-only gate we have today, retired-core
  history cannot be replayed at all.

One schema change, one clean rebuild. Addresses findings 1, 2, 6, 9 and `feedback5.md` finding 1.

## Step 2: the disposal shape

Persist the vector the fold already computes, rather than labelling it. A single sale can draw on observed,
estimated and unresolved inventory at once and split its proceeds four ways; two labels cannot round-trip a
three-by-three outcome, which is why v1's step D was a layer. Persist the full position inventory too: the
three-state open quantities, the parked fields, and a resumable substitute for the transient transaction-hash
sets.

Fold-only, no rebuild. One change unblocks both the confidence model and the checkpoint the ordered fold
needs, which is why they are not two steps. Addresses findings 3, 4, 12's quantity half.

## Step 3: the ordered fold, with a watermark

Fold per token in movement order to a persisted checkpoint, apply new flows incrementally, and refold fully
only when a flow lands below the watermark. Measured, a naive per-token ordered refold per flush is 33 hours
against today's 72 minutes, so the checkpoint is not optional. It is possible only after step 2 persists the
state it resumes from. Addresses finding 5.

## Step 4: the two the previous plan dropped

Quote conservation (finding 8): a purchase funded with 10 WMON and 20 USDC records 20 MON today, understated
by a third and labelled observed, and router fees inside a 10% band are erased. Per-entitlement parked basis
(finding 10): basis parked in vaults and pools is one aggregate per wallet and token with no destination.

## Not in scope, deliberately

The compatibility view, the missing serving indexes, wiring the integrity counters, the full-history replay,
and any bulk write to production. One exception: making the ledger import lazy in `core/sequencer.py` is the
only path by which this branch can break production with the flag off, and it should ship regardless.

---

## Status

All four seams and step 0 are built, and the full suite is green at 756 tests. What each one changed:

**Step 0, coverage.** A replay now records, in the same transaction as the flows, which block range it
actually read for each token, and a position is only served for a token whose coverage reaches back to
its registration. A token seen in passing keeps its flows as evidence and gets no position, which is
where the leftovers' 389 negative balances came from. A cached block list only certifies coverage from
its own first block, so a list built for a later window cannot claim a history it never read.

**Seam 1, the movement.** Flows are built from individual movements matched to the payment that funded
them, a venue event counts only from a classified venue, and a claim-settled swap produces movements
from the event and the pool's own registration. Two consequences worth knowing:

- Router fees are no longer removed from cost. A wallet paying 8,684.93 for tokens the pool received
  8,598.08 for used to have the 86.85 deleted. Cost is now what the wallet paid and proceeds are what it
  kept, so cost basis rises slightly on every routed trade and realized falls by the same amount.
- A movement's identity now carries its side. Keyed on the log index alone, the two halves of one
  transfer collided on the primary key and one was dropped on insert: 65,791 of 72,690 wallet-to-wallet
  transfer rows in the side database had lost their counterparty half. The netflow tests never wrote to a
  database, so only a test that stored its result caught it. Ordering the outgoing half first is also what
  lets seam 3 release a sender's basis before the receiver inherits it.

**Seam 2, the disposal shape.** Each flow stores the whole vector the fold computed rather than two
totals, so a sale drawing on observed, estimated and unresolved inventory at once is recoverable from its
row. An unpriced disposal no longer books the released basis as a loss: it leaves the running average and
waits in its own bucket. Every field the fold carries is now persisted, so a stored position is a
checkpoint a later fold can resume from.

**Seam 3, the ordered fold.** A token is folded as one pass across all its wallets in chain order, so a
transfer hands its cost to the receiver instead of destroying it. The fold resumes from the persisted
checkpoint and only loads the wallets in the new flows; a flow landing at or below the watermark folds
the whole token again, which is the correction path and stays off the common one.

**Seam 4, parked basis.** Basis parked in a pool or vault is recorded against that pool or vault. Pooled
into one aggregate, withdrawing 100 from a vault that held it at 100 returned 550, the average of every
vault the wallet had used.

**Also shipped:** the sequencer reaches the ledger through a gate that imports nothing from `core.ledger`
while the flag is off, and refuses to start against a database without the ledger tables instead of
raising inside the block transaction.

## What the rebuilt data found that the tests did not

Every seam above was green across the whole suite before any of these was known. They were found by
rebuilding JAMES and asking the stored rows questions no test had asked, and each is now a test.

| what was wrong | how it showed | why no test saw it |
|---|---|---|
| the two halves of a transfer shared a primary key, so one was dropped on insert | 65,791 of 72,690 wallet-to-wallet transfer rows had no counterparty half | the netflow tests never wrote to a database, and the collision only happens on insert |
| parked basis was deleted and never rewritten on a full fold | no `parked_entitlements` rows after any replay's first flush | an early return that read as 'nothing to do' |
| a movement took its counterparty from the wallet's largest neighbour in the transaction | 2,751 of JAMES's transfer rows named the trading partner instead of who received a 0.2% fee | correct while a leg was a netted position; wrong once a leg became one movement |
| a wallet sending tokens to itself was read as a token swap | three transactions booked a sale and a purchase that cancelled | the pair is opposite-signed and unpriced, which is exactly a swap's shape |
| a pair swap with no transfer of its own invented a second disposal | one wallet sold the same tokens twice, into a negative balance | the rule was written for Uniswap V4, which really can settle with no transfer |
| a pair's reported quote was taken as the price with nothing backing it | two flows booked 554 trillion MON against a token priced at 0.06 | the amount matched the token side, and nothing checked the quote side |

The common shape: all six conserve net token quantity per wallet, which is why the balance, supply and
coverage checks stayed green through every one of them.

Next: rebuild the three fixtures on this code and re-run `ledger_check` and `ledger_verify`, then the
next review round.

## Acceptance

Twelve value-level fixtures, from `feedback7.md`, each with the number it must produce. Quantity checks
cannot see any of these defects, which is why the existing green table coexists with all of them.

| # | shape | must produce |
|---|---|---|
| 1 | claim-settled V4 arbitrage cycle (`0xd786059a…`) | two movements, plus and minus 320.019831901713613752, attributed to the actor |
| 2 | unequal round trip in one transaction | buy 100 at 100 **and** sell 60 at 72; unit cost 1.00 |
| 3 | sell then buy, log order reversed | two flows, distinct keys; the row anchored at log 2 is the sell |
| 4 | gift plus unrelated payment | `transfer_in`, unresolved, no quote |
| 5 | cross-wallet basis, both address orders | identical either way |
| 6 | unpriced disposal | realized 0, basis held out of the running average |
| 7 | mixed-inventory sale | released 50 observed / 150 estimated; realized 150 / 50 / 200 |
| 8 | forged emitter | `transfer_in`, unresolved |
| 9 | mixed quote | cost 30 MON |
| 10 | router fee | 109.90 MON, or 100.00 with 9.90 recorded as fee |
| 11 | identity under registry change | flow count unchanged, no duplicate group |
| 12 | replay against live valuation | identical values from both paths |

Each is written as a failing test first, against the current code, and only then made to pass.

**Case 1 forces a product decision before the code is written.** Under average-cost accounting that
arbitrage cycle books roughly +27,666 MON of realized profit on moncock, for a transaction whose real profit
was 645.88 USDC. Decide whether that is the intended answer at the fixture, not after.
