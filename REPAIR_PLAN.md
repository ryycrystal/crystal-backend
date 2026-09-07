# Repair plan: what changes, at which seam, and in what order

Draft for review, not yet implemented. Written 2026-09-07 against `accounting-fix` at `f9a6013`,
responding to `feedback2.md` (14 findings), `feedback3.md` (all six accounting findings confirmed, with an
ordering), `feedback5.md` (one new P1), `feedback6.md` (consumer contract and cutover).

The standing rule this plan is written under: fix the seam, do not patch the boundary. Where a step only
converts or special-cases at a call site, it is called out as such and is not proposed.

---

## The five defects, grouped by the seam they actually break

Fourteen findings reduce to five seams. Everything else is a consequence.

| # | seam | what is wrong | findings |
|---|---|---|---|
| A | **flow identity** | the row key contains a position that depends on registry membership | f2-6 |
| B | **what a flow represents** | one netted quantity per transaction, so actions inside it are erased | f2-1, f2-2 |
| C | **what evidence a movement needs** | only an ERC-20 `Transfer` counts as economic movement | f2-9, f5-1 |
| D | **how confidence is modelled** | one state per flow, conflating cost confidence with proceeds confidence | f2-3, f2-4, f2-12 |
| E | **fold ordering** | each `(wallet, token)` folds independently, so basis cannot cross wallets | f2-5 |

`feedback3.md` recommends repair rather than rebuild, on the grounds that the evidence each fix needs is
already collected and then discarded. This plan accepts that and proposes the order A, C, B, D, E.

That differs from `feedback3.md`'s A, B, D, E in one respect: **C moves before B**, because C changes which
transactions exist at all, and re-deriving action grouping afterwards would mean doing B twice.

---

## A. Flow identity comes from provenance, not position

**Now.** `sub_index` is the leg's ordinal in the transaction's list sorted by `(wallet, token)`
(`netflow.py:753`) and is part of the primary key (`schema.py:40`). Registering a token that sorts earlier
renumbers an existing token's rows, so `ON CONFLICT DO NOTHING` inserts a duplicate rather than recognising
the row. 357 duplicate `(txhash, wallet, token)` groups exist in the side database; in 37 of a 200-group
sample the two copies also disagree on `kind`.

**Change.** Key a flow by immutable transaction provenance:
`(txhash, wallet, token, anchor_log_index)`, where the anchor is the lowest log index among the legs that
composed the flow. All four components come from the chain and none depends on what is registered or how
addresses are classified. A `sub_index` remains only as a within-anchor discriminator for genuinely
repeated movements at the same anchor, defaulting to 0.

**Open question for review.** The anchor is only well defined once B decides what a flow is. If a flow stays
transaction-wide, the lowest log index is stable but arbitrary; if a flow becomes per-action, the anchor is
the action's own first leg and is meaningful. This plan takes the anchor as stable-but-arbitrary now, and
meaningful after B, with no key change between the two. **Reviewers should check that claim**, because if it
is wrong, A must be done after B and nothing can be validated by re-replay in the meantime.

**Cost.** Schema change plus one clean re-replay of the side database. No consumer reads flow keys.

---

## C. Economic movement is not the same as an ERC-20 transfer

**Now.** `process_block` marks a transaction interesting only when a registered token `Transfer` appears
(`engine.py:249`, `:302`). A Uniswap V4 swap settled against the PoolManager's ERC-6909 claim balances moves
no ERC-20, and an order-book fill changes custody inventory without one. Both are dropped before netting.
Verified: transaction `0xd786059a…` is three V4 swaps on moncock with zero moncock transfers, and the ledger
has no rows for it at all. 26 moncock and 130 JAMES wallet-transactions vanish this way.

**Change.** Admit a transaction on any of three kinds of evidence, not one: a registered token transfer, a
venue event naming a registered token (V2/V3/V4 swap, curve trade, core fill), or a custody event on our own
order book. Each becomes a *movement* with its own provenance, so a claim-settled swap is a first-class
movement whose quantity comes from the swap event rather than from a transfer that does not exist.

**Why before B.** B nets within an action. If C is done after B, every action boundary has to be recomputed
once the missing movements appear.

**Open question for review.** A venue event gives a quantity and a pool, but the wallet must still be
resolved, and for a claim-settled swap the transfer graph that `_resolve_trade_user` walks is empty. The
proposal is to fall back to the transaction origin (or the ERC-4337 sender), and to mark such attribution
explicitly rather than silently. Reviewers should say whether that is sound or whether these should be
recorded as unattributed movements.

---

## B. A flow is one action, not one transaction

**Now.** `_collect` reduces the whole transaction to one signed quantity per `(wallet, token)`, and
`net_transaction` drops zero deltas (`netflow.py:457`, `:645`). A buy of 100 and a sell of 100 in one
transaction produces nothing; a buy of 100 and a sell of 60 becomes a net buy of 40 with the sale, its
proceeds and its realized PnL erased. Separately, because netting is transaction-wide, an unrelated token
receipt and an unrelated quote payment in the same transaction are read as a purchase with an invented
price, at `observed` confidence (f2-2).

**Change.** Group the transaction's movements into actions, and net only within an action. An action is a
maximal set of movements connected through the transfer graph and the venue events between one wallet-facing
start and end. Aggregator hops stay collapsed, because they are connected; a gift and an unrelated payment
do not, because they are not. A purchase then requires a real link between a token movement and a quote
movement inside one action, which removes the coincidence inference as a side effect rather than as a
special case.

**Open question for review.** The definition of an action is the whole of this step and the part most likely
to be wrong. Reviewers should attack it directly: batched multi-user transactions, self-transfers,
multi-hop cycles that return to the payer, and transactions where the same wallet legitimately trades twice.

---

## D. Cost confidence and proceeds confidence are separate axes

**Now.** One `basis_state` per flow. `_quote_wei` returns 0 when proceeds are unknown, and `_apply_sell`
books `0 - cost` as a realized loss (`fold.py:87`, `:160`), so a disposal with unknown proceeds fabricates a
definite loss: 352 rows and −1,029.60 MON in the side data. A sale that releases estimated basis is still
labelled `observed` because its own quote was observed, so a consumer filtering flows on
`basis_state='observed'` reads an estimated gain as confirmed.

**Change.** Carry two independent confidences on a disposal, one for the basis released and one for the
proceeds received, and never let "no evidence" become a number. An unresolved disposal retains its released
basis and records unresolved proceeds; it does not realize. Positions persist the three-state inventory in
quantity as well as in money (f2-12), so observed, estimated and unresolved holdings can be reported apart.

**This is a stored-shape change, not a computation change.** It is the step where "is this the real seam"
matters most: adding a second flag to the existing row would be the layered version. The seam is that a
disposal is an event with two evidentiary sides, and the row should say so.

---

## E. The fold is ordered across wallets, per token

**Now.** `refold` rebuilds each `(wallet, token)` independently (`fold.py:180`, `:185`), so a receiver has no
sender state to inherit and every `transfer_in` is unresolved. Plan line 338 already specifies the opposite:
a `transfer_in` from a ledger wallet inherits the sender's average cost. On JAMES this is 1,287,202,079
tokens of unresolved inflow, of which 89.1% came from senders holding observed acquisitions.

**Change.** Fold a token's flows in chain order across all wallets at once, carrying basis along transfer
edges. Requires B first, because inheritance needs the individual transfer edge, and transaction-wide
netting has already merged it away.

**Open question for review.** This makes the fold's unit the token rather than the `(wallet, token)` pair,
which changes both memory profile and incremental-update strategy. `feedback6.md` notes the current refold
is already quadratic at production scale. Reviewers should say whether ordering per token is affordable at
1.26M positions, and if not, what the checkpointing model should be.

---

## Consequences that are not steps

- **Quantity checks cannot validate any of this.** Every defect above preserves net token quantity per
  wallet, which is why the wallet-by-wallet chain comparison and supply conservation pass while the value
  layer is wrong. Acceptance for the repair needs value-level fixtures: hand-derived cost and realized PnL
  for wallets in each of the shapes above, not more balance checks.
- **The verification tooling is unaudited.** `feedback4.md` was commissioned and never delivered. One hole
  is known: the duplicate invariant keys on the primary key and so cannot see A's duplicates.
- **`accepts_log_for_indexing` admits router events only from the live `CRYSTAL_ADDR`.** After the 09-07
  relaunch, token creations and trades from retired cores are rejected, so crystal-era history cannot be
  replayed until the gate takes the generation list. Needed before any full-history run, independent of the
  five seams.
- **Not in scope here:** the compatibility view (`feedback6.md` P1-2), the missing serving indexes, wiring
  plan §11's counters into `core/integrity.py`, and anything touching the live indexer before 9/13.

---

## Order, and what each step is validated by

| step | seam | validated by | needs re-replay |
|---|---|---|---|
| A | identity | re-replay twice, rows identical | yes, one clean rebuild |
| C | movement evidence | the 26 moncock and 130 JAMES vanished transactions appear | yes |
| B | action grouping | round-trip fixtures: buy+sell in one transaction books both | yes |
| D | confidence | an unresolved disposal realizes nothing; estimated basis never reads confirmed | no, fold only |
| E | fold ordering | JAMES unresolved share falls to roughly 2%; basis conserved across transfers | no, fold only |

A and C change what is stored, so each needs a clean rebuild of the side database before the next step is
measured. D and E are fold-only and can be iterated without re-replaying.
