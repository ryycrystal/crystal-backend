# Reviewer 7: review the repair design before it is built.

Read `review/README.md` first for setup and constraints. Write your report to `feedback7.md` in the repo root.

You are reviewing a **design**, not code. `REPAIR_PLAN.md` proposes how to fix the fourteen findings from
`feedback2.md`, which `feedback3.md` confirmed and `feedback5.md` added to. Nothing in it is implemented.
Catching a wrong seam here costs an afternoon; catching it after implementation costs the week before the
9/13 vault launch.

Read, in this order: `REPAIR_PLAN.md`, then `feedback3.md` (which proposed a different ordering), then
`feedback2.md` and `feedback5.md` for the underlying findings, then `POSITION_LEDGER_PLAN.md` §6 and §7 for
the original design, then the code the plan proposes to change: `core/ledger/netflow.py`, `fold.py`,
`engine.py`, `schema.py`, `types.py`.

## The standing rule this is judged against

The repo owner's instruction, given today: fix the design seam, never patch at the boundary. A change that
converts, coerces or special-cases at a call site is a layer, and layers are what made the current engine
hard to reason about. The same domain fact living in three or more files is the tell that a seam is missing.

So the question for every step is not only "does this work" but **"is this the seam, or is it a layer wearing
a seam's clothes?"** Say so plainly where you think the plan fails that test. Step D is where I am least
confident, and the plan admits it.

## What the plan asks you to settle

Four questions are marked open in `REPAIR_PLAN.md`. They are the reason this review exists.

1. **Ordering: A before B.** The plan claims the new flow key `(txhash, wallet, token, anchor_log_index)` is
   stable whether or not a flow later becomes per-action, so identity can be fixed first and everything else
   validated by re-replay afterwards. `feedback3.md` also puts identity first. If that claim is wrong, the
   whole sequence inverts and there is no way to validate any intermediate state. Check it.
2. **Ordering: C before B**, which departs from `feedback3.md`. The plan argues that admitting non-transfer
   movements changes which transactions exist, so grouping them into actions afterwards avoids doing the
   grouping twice. `feedback3.md` did not consider C at all, because `feedback5.md` had not been written yet.
   Say which order is right.
3. **The definition of an action** (step B): a maximal set of movements connected through the transfer graph
   and venue events, between one wallet-facing start and end. This is the load-bearing definition in the
   whole plan. Attack it with concrete cases: batched multi-user transactions, ERC-4337 bundles,
   self-transfers, multi-hop cycles returning to the payer, a wallet that legitimately trades the same token
   twice in one transaction, and a transaction mixing a genuine trade with an unrelated gift.
4. **Wallet attribution for claim-settled swaps** (step C): with no ERC-20 transfer there is no transfer
   graph to walk, and the plan proposes falling back to the transaction origin or the ERC-4337 sender, marked
   explicitly. Is that sound, or should such movements be recorded as unattributed?

Plus two you should raise independently if the plan has them wrong:

5. **Step D's stored shape.** Two confidences on a disposal, and three-state inventory persisted in quantity
   as well as money. Is that the right model, or is there a cleaner one? `feedback2.md` finding 4 argues cost
   and proceeds are separate axes; check whether the plan's version actually delivers that or just adds a
   second flag.
6. **Step E's cost.** Folding per token across all wallets in chain order, when `feedback6.md` says the
   current per-pair refold is already quadratic at 1.26M positions. Is ordering per token affordable, and if
   not, what is the checkpointing model?

## What else to look for

- **Anything the five seams do not cover.** The plan claims fourteen findings reduce to five seams and
  everything else is a consequence. Test that claim: find a finding in `feedback2.md` that is not actually
  addressed by A through E.
- **Sequencing against 9/13.** The plan explicitly excludes the compatibility view, the missing indexes, the
  integrity counters, and anything touching the live indexer. Is that the right cut, given `feedback6.md`
  recommends shipping the branch inert and doing nothing else?
- **Whether repair is still the right call.** `feedback3.md` said repair rather than rebuild because the
  evidence each fix needs is already collected and then discarded. Steps B and C both change what is
  collected. Does that reasoning still hold, or has enough changed that a rebuild of `netflow.py` is now
  cheaper than four sequenced repairs to it?
- **Acceptance.** The plan says quantity checks cannot validate any of this and that value-level fixtures are
  needed. Say what those fixtures should be, concretely enough to build: which wallets, which shapes, and
  what number each must produce.

## Deliverable

A verdict on each of the six questions, a judgement on the ordering as a whole, and — if you disagree with
the plan — the sequence you would run instead, with the reasoning. If you think a step is a layer rather than
a seam, say which and what the seam would be.

You may prototype against the code to test a claim. Do not implement the plan.
