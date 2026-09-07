# accounting-fix: the four seams, built and rebuilt on real data

Status 2026-09-08 06:45. **Ready for the next review round on two of three fixtures.** JAMES and moncock are
rebuilt from their creation blocks on this exact engine, all 24 verify invariants hold across them, and
JAMES passes all nine of its own checks including a wallet-by-wallet comparison against chain. chipotle is
replaying and has not finished. Nothing has been written to production and `LEDGER_ENABLED` stays off.

The previous version of this file was corrected by six external reviews, which established that its green
table proved the **quantity** layer and said nothing about the **value** layer. That distinction governs
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
it never read. In the current database 164 tokens hold flows this way and are correctly served no positions.

**Seam 1, the movement.** Flows are built from individual movements matched to the payment that funded them,
rather than one netted row per wallet and token per transaction. A venue event counts as evidence only from
a classified venue. A swap that settles against Uniswap V4's claim balances produces movements from the
event and the pool's own registration. Two consequences worth knowing:

- Router fees stay in cost. A wallet paying 8,684.93 for tokens the pool received 8,598.08 for used to have
  the 86.85 difference deleted. Cost is now what the wallet paid and proceeds are what it kept.
- A movement's identity carries its side, because keyed on the log index alone the two halves of one
  transfer collided on the primary key and one was dropped on insert.

**Seam 2, the disposal shape.** Each flow stores the whole vector the fold computed rather than two totals,
so a sale drawing on observed, estimated and unresolved inventory at once is recoverable from its row. An
unpriced disposal no longer books the released basis as a loss: it leaves the running average and waits in
its own bucket. Every field the fold carries is persisted, so a stored position is a resumable checkpoint.

**Seam 3, the ordered fold.** A token is folded as one pass across all its wallets in chain order, so a
transfer hands its cost to the receiver instead of destroying it. The fold resumes from the persisted
checkpoint and loads only the wallets in the new flows; a flow landing at or below the watermark folds the
whole token again, which is the correction path and stays off the common one.

**Seam 4, parked basis.** Basis parked in a pool or vault is recorded against that pool or vault. Pooled
into one aggregate, withdrawing 100 from a vault that held it at 100 returned 550.

**Also shipped.** The sequencer reaches the ledger through a gate that imports nothing from `core.ledger`
while the flag is off and refuses to start against a database without the ledger tables. The replay and the
live engine share one rule for what a MON was worth, where they previously differed in five ways.

---

## What the rebuilt data found that a green suite did not

Nine defects, each found by rebuilding a token and asking the stored rows a question no test had asked, each
now a test. The suite was green before every one of them.

| what was wrong | how it showed |
|---|---|
| both halves of a transfer shared a primary key | 65,791 of 72,690 wallet-to-wallet transfer rows had lost their counterparty half |
| parked basis was deleted and never rewritten on a full fold | no parked rows survived any replay's first flush |
| a movement took its counterparty from the wallet's largest neighbour | 2,751 rows named the trading partner instead of who received a 0.2% fee |
| a wallet sending tokens to itself was read as a token swap | three transactions booked a sale and a purchase that cancelled |
| a pair swap with no transfer of its own invented a second disposal | one wallet sold the same tokens twice, into a negative balance |
| a pair's reported quote was taken as a price with nothing backing it | two flows booked 554 trillion MON against a token priced at 0.06 |
| a seller's implied price came from its net position, not from what it sent | a relayer forwarding everything netted to dust, and its whole payment divided by that dust priced the movement passing through it |
| basis released by a transfer was taken up only when the receiving half was labelled a transfer | a dust refund turned one hand-off into a buy at 0.163 MON, destroying 47,875 MON; 306 more arrived labelled a swap leg |
| the invariant meant to catch that trusted the same label | it compared only pairs whose inbound half said transfer, so it passed while cost was destroyed |

The last two are worth dwelling on. A check written from the same assumption as the code cannot falsify it.

---

## The two rebuilt tokens

| | JAMES | moncock |
|---|---|---|
| blocks | 85,819,844 - 102,523,914 | 37,719,344 - 102,355,176 |
| flows | 153,389 | 339,693 |
| wallets | 5,051 | 9,681 |
| inflow arriving with no price of its own | 35.699% | 31.463% |
| inflow that still has no cost at all | 3.591% | 11.392% |
| estimated share of traded value | 6.391% | 11.612% |
| structural checks | 15 of 15 | 14 of 15 |

JAMES's uncosted inflow was 44.617% before this work. Reviewer 7 predicted the ordered fold would bring it
to roughly 2%; it is 3.6%. What remains is transfers from senders who never had a cost to pass on, across
708 counterparties on JAMES and 1,351 on moncock. No amount of replaying can price those.

**All 24 verify invariants hold** across both, including every one added for the defects above: both halves
of a transfer stored, cost travelling with the tokens, each position's inventory equal to the sum of its
flows' effects, parked totals equal to the per-venue rows, no position served without coverage from
creation, and every flow written by the current interpretation. Zero negative balances overall.

### JAMES fixture: 9 of 9

Including the check that matters most, because it can see a holder the replay never recorded at all:

```
chain balanceOf == balance + custody (5066 wallets at block 102,523,914) | 0 mismatches
prod holders present (2474, holding on chain)                           | 0 missing
```

### moncock fixture: 6 of 9, and the expectation is what is wrong

```
moncock | token_bought                         | 25719120.30 | 0.00 | FAIL
moncock | native_spent (confirmed + estimated) | 477018      | 0.00 | FAIL
moncock | realized (confirmed + estimated)     | -193957     | 0.00 | FAIL
```

That wallet neither buys nor sells moncock. It receives it from its own router contract and passes it on, so
under the movement model it is a conduit: the cost arrives as inherited basis and leaves with the tokens,
and it realizes nothing. The 477,018 figure was measured when a whole transaction collapsed to one row per
wallet and the router's purchases were attributed to the wallet it delivered to.

**This needs your decision, not a code change.** Either restate the fixture against inherited cost rather
than `native_spent`, or attribute a router's purchase to the wallet it delivers to. The second is what the
old engine did and it is what produced the phantom holders.

---

## Open, with measurements

**A venue's price is applied to a whole movement even when the event covers a fraction of it.** Right for a
wallet buying 600 from a pool and 200 over the counter, which is a tested case; wrong for a relayer. On
moncock transaction `0xbd8349ea12` it priced 138,034 tokens at 2,519,335 MON against a token trading at
0.013. Bounding the extension was tried and reverted because it breaks the legitimate case. The real fix is
to price only the covered portion and leave the rest unresolved, which seam 2 already has the inventory for
but which needs a movement to be splittable in two. **This is the first item for the next round.**

**Receipts are attached to the nearest movement of opposite sign, and proximity is sometimes wrong.** On
JAMES transaction `0xf8f9f66354` a 0.48-token movement was given 5,729 MON while a 33,605-token movement in
the same sale got a fraction of a wei. Under average cost this does not reach the position, because realized
comes from the wallet's totals and those are conserved; it distorts `price_native`, which a later flow can
read as a reference price.

Together these show up as the price spread, the one structural check that does not pass:

| token | observed prices more than a thousandfold from the median |
|---|---|
| JAMES | 441 of 84,906 |
| moncock | 673 of 187,853 |

**chipotle was not rebuilt, and cannot be from here.** Its own logs appear in 337,308 blocks against
41,799 for JAMES, and it is dense: 12.7 flows per hot block against JAMES's 3.7, with 56,547 wallets.
Measured over its first 500 blocks it replays at **5 blocks a second, an 18-hour projection**, because
almost all of that time is RPC round trips for transaction metadata and this link is in China. JAMES and
moncock managed about 50. It was started and stopped rather than left to grind.

This is the same constraint that governs any registry-wide replay: the code was measured at 160 to 640
blocks a second running next to the database in Azure. chipotle needs that path, not this one. Its
fixture covers a single wallet with 20 trades, so what it would add is one more hand-checked wallet,
not new coverage of the value layer.

**A position does not say how far forward its coverage runs.** Coverage from creation is what decides
whether a token is served at all, and it has a `to_block`, but nothing on the position row carries it.
The aborted chipotle replay made this concrete: it left 148,000 blocks of real history covered from
creation through block 38,146,392, which is honest and correct as of that block, and would have been
read as current. Those rows were deleted rather than left. A consumer needs the coverage end alongside
the position, and the invariant that a served position is current needs somewhere to look.

**The fee is not exposed as its own field.** Cost is what the wallet paid, which is the conservation the
reviews asked for, but a consumer cannot separate what the venue received from what the router took.
