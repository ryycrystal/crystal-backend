# Reviewer 4: replay, operations, and whether the tests prove anything.

Read `review/README.md` first. Write your report to `feedback4.md` in the repo root.

Two jobs. First adjudicate `feedback2.md` findings 7 to 14. Second, and more important, audit the
verification machinery itself, because the author wrote both the code and the checks that pass it.

## Part one: adjudicate findings 7 to 14

Same verdicts as reviewer 3 uses: confirmed, confirmed but narrower, refuted, or cannot determine, each with
evidence.

7. **Scoped replays write partial histories for other tokens.** A replay selects transactions involving the
   requested token, then nets every registered token in those transactions, so unrelated tokens acquire
   incidental partial positions. Also: `--resume` uses the last recorded activity as the coverage mark, which
   is not the same as coverage. (`scripts/ledger_replay.py` around 390 and 526.)
   This one has a visible footprint: 159 tokens in the side database have flows starting after their creation
   block, holding 389 negative balances. Establish whether that is incidental netting, deliberate one-off
   experiments, or both, and what it implies for the full-registry run.
8. **Mixed quote payments are dropped and router fees erased.** `_own_quote` picks either the MON family or
   the USD family and discards the other, so a purchase funded with both records only part of the cost.
   `_prefer_venue_quote` rewrites the wallet's own outflow to the venue amount when they are within 10%.
   (`core/ledger/netflow.py` around 222 and 340.)
9. **Trades entirely inside custody are never processed.** Only a registered token `Transfer` marks a
   transaction as moved, so an order-book fill that changes inventory without an ERC-20 transfer is invisible.
   (`core/ledger/engine.py` around 249 and 302.)
10. **LP and vault basis is pooled across destinations.** Parked basis is one aggregate per wallet and token
    with no vault, pool or share identifier. (`core/ledger/fold.py` around 198 and 208.)
11. **Startup and retry are not ready for the live flag.** `init_ledger_schema` is only called by the replay
    script, so enabling `LEDGER_ENABLED` on a normal backend database meets missing tables. In-memory
    registry and classification state is mutated before commit while chunk retries reuse the engine.
    (`core/sequencer.py:1036`, `core/ledger/engine.py:182`.)
12. **Three-state inventory is not persisted, and replay and live valuation differ.** Observed and estimated
    token quantities live only in transient state; the row stores unresolved quantity only. Replay seeds the
    last trade per minute and selects by bucket without checking the block.
13. **Completion checks report uncertainty as PASS**, and the plan's threshold of under 1% estimated basis
    per token is neither enforced nor replaced. (`scripts/ledger_check.py` around 163 and 316.)
14. **`classify_code(None)` becomes EOA and is cached forever**, so absent bytecode evidence is recorded as a
    positive classification. (`core/ledger/kinds.py` around 75 and 199.)

Finding 11 gates the shadow run that is scheduled next, so treat it as the most urgent to settle.

## Part two: audit the verification machinery

`COMPLETE.md` presents seven passing results: three fixture tables, plus supply conservation, twelve SQL
invariants, a determinism replay, a 9,375-wallet chain comparison, and a differential against production's
engine where the chain sided with the ledger 213 times and with production zero times.

For each, answer: **does the check prove what the document says it proves?** Look for checks that are
tautological, that measure the author's own assumptions, that pass by construction, or whose failure mode is
silence. Specific things to test rather than assume:

- The duplicate-flow invariant in `scripts/ledger_verify.py` keys on the table's primary key, so it cannot
  detect the 357 coarser duplicates `feedback2.md` reports. Confirm that, and find any other invariant with
  the same shape: a check whose key or scope guarantees it passes.
- `chain_balances` compares `balance_token + custody_balance` against `balanceOf`. Establish what this cannot
  catch. Quantities can reconcile while cost basis, realized PnL, buy and sell counts are all wrong, and none
  of those are covered by any automated check.
- The supply conservation test subtracts balances the ledger already counts. Verify that exclusion is right
  and that it cannot mask a missing holder.
- The determinism test replays 29 flows across roughly 30 blocks. Say whether that generalizes.
- The differential excluded addresses the ledger classifies as venues from production's side. Check that this
  is not circular: the ledger deciding which comparisons it is graded on.

Then state plainly what an unqualified `REVIEW` marker in `COMPLETE.md` is currently entitled to claim, and
what it should say instead if that is narrower.

## Deliverable

Beyond the findings, give the human two lists: **what must be true before `LEDGER_ENABLED` is turned on in
the live indexer**, and **what must be true before any API reads from these tables**. Concrete and checkable,
in the order they should be done.
