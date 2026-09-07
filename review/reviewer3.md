# Reviewer 3: the accounting model. Adjudicate findings 1 to 6.

Read `review/README.md` first. Write your report to `feedback3.md` in the repo root.

`feedback2.md` claims six P1 defects in how a transaction becomes flows and how flows become positions.
They are the expensive ones: if they hold, the recommended fix is a rework of the action model rather than a
patch, and that decision costs days. Your job is to settle each one, not to restate it.

## The six claims

1. **Transaction-wide netting deletes economically real actions.** `_collect` reduces the whole transaction
   to one signed quantity per wallet and token, and `net_transaction` drops zero deltas, so a buy of 100 and
   a sell of 100 in the same transaction produces no flow at all.
   (`core/ledger/netflow.py` around lines 457 and 645.)
2. **Opposite-sign movements are taken as proof of a purchase.** A gift of tokens from one wallet plus an
   unrelated quote payment to another becomes an `observed` buy with a cost, with no venue event or trace
   supporting it. (`core/ledger/netflow.py` around 503 and 645; emitter trust in `core/ledger/engine.py:239`.)
3. **An unresolved sale invents zero proceeds.** `_quote_wei` returns zero, `_apply_sell` releases basis and
   books `0 - cost` as an estimated loss, so a disposal with unknown proceeds realizes a fabricated loss
   rather than being held as unresolved. (`core/ledger/fold.py` around 87 and 160.)
4. **Disposal flows lose the confirmed/estimated split.** A sale that releases estimated basis is still
   labelled `observed` because its own quote was observed, conflating quote confidence with realized-PnL
   confidence. (`core/ledger/fold.py:160`, `core/ledger/types.py:105`.)
5. **Basis vanishes when tokens change wallets.** The sender releases basis, the receiver inherits nothing,
   because `refold` rebuilds each wallet and token independently with no cross-wallet ordering. The plan
   (line 338) says a `transfer_in` from a ledger wallet inherits the sender's average cost.
   (`core/ledger/fold.py` around 180 and 185.)
6. **Flow identity is unstable.** `sub_index` enumerates the currently eligible legs of a transaction and is
   part of the primary key, so registering another token can renumber an existing token's rows. The review
   reports 357 duplicate `(txhash, wallet, token)` groups in the side database and names one transaction.
   (`core/ledger/netflow.py:753`, `core/ledger/schema.py:40`.)

## What to do with each

For every claim, return one of: **confirmed**, **confirmed but narrower than stated**, **refuted**, or
**cannot determine**, with the evidence that decides it.

- Reproduce it. A failing test against the current code is the strongest form. `tests/test_ledger_netflow.py`
  and `tests/test_ledger_fold.py` show how to construct transactions and read the resulting flows.
- Then measure it against the side database. A defect that cannot occur in the three replayed tokens is a
  different priority from one that already corrupts them. For claim 6 in particular, the 357 duplicate groups
  are checkable: confirm the count, find what actually differs between the rows, and establish whether the
  cause is `sub_index` renumbering or the repeated scoped replays those tokens went through.
- For claim 5, the author already measured that 94.9% of JAMES's unresolved inflow came from senders holding
  observed buys. Confirm or correct that number, and say what the position rows would look like if the rule
  were implemented.

## The question the human actually needs answered

Findings 1 and 2 attack the net-per-transaction model itself, and finding 6 attacks the row identity that
model produces. If they hold as stated, patching them individually may not be possible.

So end your report with a direct recommendation on one question: **can the current model be repaired
incrementally, or does the action and identity model have to be rebuilt first?** Give the reasoning, the
cases that force the answer, and if it is "rebuild", say what specifically must change and what can be kept.
An answer of "repair, in this order" is equally valuable if that is what the evidence supports.

Note that the author's fixtures pass and the ledger's quantities reconcile to the wei against chain for all
three tokens, including a 9,375-wallet comparison with zero mismatches, and supply conservation with zero
unaccounted. Those results are quantity-level. Explain how they coexist with these findings, because whoever
reads both needs to understand which of the two pictures governs a decision to ship.
