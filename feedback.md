# Review: accounting-fix (net-flow position ledger)

Reviewed 2026-09-07 against `origin/main` at `214cef2`, 44 commits, 9,658 insertions.
Reviewer ran the suite, the lint gate, and independent prod checks rather than reading
COMPLETE.md alone.

## Verdict

The design is right and the evidence is real. This is a better engine than the one in
production: on the one fixture where both can be compared it is exactly correct where the
patched engine is not. **It is not ready to cut over**, and one gap must close before it
can be, because it would regress a behaviour users can already see. Shadow mode as
proposed is the correct next step.

## Verified independently

| check | result |
|---|---|
| full suite, own scratch database | 706 passed, 4 skipped |
| `ruff check` / `format --check` | clean, 138 files |
| behaviour with the flag off | only `core/sequencer.py` touched (+10 lines), every call site guarded by `LEDGER.enabled` |
| CHIPOTLE against the patched engine | ledger 20 trades / 13,850.173 MON realized; patched engine 21 / 13,786.82, and it over-books `token_bought` by 5,123,708.88 (a double-counted reconciliation leg) |
| moncock `native_spent` | ledger 477,018.49 against the hand-derived 477,018.49; the patched engine in prod reads 477,000.57 |
| prod registry claim | confirmed: `launchpad_tokens` now has zero `source = 0` rows (30,518 at source 1, 1,596 at source 2) |

The CHIPOTLE comparison is the strongest result in the report and deserves to be the
headline: the net-flow fold reproduces the fixture exactly, including the trade count,
where the venue-attribution engine cannot even after two attribution fixes.

## Blocking before cutover

**1. `transfer_in` does not inherit the sender's basis** (`core/ledger/fold.py:185`).

You flagged this yourself and measured it (JAMES 44.6% unresolved, 94.9% of it from
senders with observed buys). Raising it here because it is not merely incomplete, it is a
**regression against shipped behaviour**: basis already travels with transfers in the live
engine, and ~175K rows were repaired to make that true. Cutting over with every inbound
transfer unresolved would change PnL for every wallet that ever received tokens, in the
direction of "we no longer know your cost".

The cross-wallet ordering objection is real but smaller than it looks: flows are already
keyed `(block, tx_index, log_index, sub_index)`, so folding **per token across all its
wallets in chain order**, holding per-wallet state in memory for the pass, gives a
deterministic sender basis at the moment of transfer with no extra passes. The current
per-`(wallet, token)` fold is what makes it awkward, not the data model.

**2. An unresolved buy is not counted as a buy** (`core/ledger/fold.py:147-152`).

When the cost cannot be observed, `_apply_buy` returns before touching `token_bought`,
`native_spent` or `trade_txs`, so the tokens land in the unresolved bucket while the trade
disappears from `trade_count`, `buy_count` and `token_bought`. `balance_token` still moves,
so the row stops satisfying `token_bought - token_sold ≈ balance`, and a user sees a
purchase they made missing from their trade count. Suggest counting the trade and the
quantity, and leaving only the *cost* unresolved. That keeps the honest-labelling property
while preserving the quantity identity.

**3. RPC inside the indexer's open transaction** (`core/ledger/engine.py`, your own review
pointer).

`LEDGER.process_block` runs inside the per-block transaction and issues chain calls from
there. CLAUDE.md's "never hold a transaction open across slow work" rule exists because
this pattern took the production API down twice, and a `getCode` or trace stall would hold
the transaction for as long as the RPC hangs. Recommend the flag stay off on the live
indexer entirely until the RPC work is prefetched per chunk the way `scripts/ledger_replay.py`
already does; shadow-validate through the replay script instead, which needs no flag.

## Should fix

**4. Unguarded import** (`core/sequencer.py:11`, `:1130`). `from core.ledger.engine import
LedgerEngine` and `LEDGER = LedgerEngine(db_cursor)` run at import time regardless of the
flag, so any import-time or constructor error in the ledger package takes the indexer down
even with the feature off. The "byte-identical with the flag unset" claim holds for
behaviour but not for failure modes. A lazy import inside the guard restores it.

**5. Realized PnL will read systematically lower than production.** Proceeds attributable
to unresolved tokens go to `unresolved_proceeds_native` (`fold.py:171`) instead of realized.
That is the correct conservative choice and I would keep it, but it is a visible product
change: today those sells book the full proceeds as profit. It needs a UI treatment and a
one-line support note, not just a column. Pair it with finding 1, which is what shrinks the
unresolved pool in the first place.

**6. The moncock realized check passes at 0.47% against a 0.5% tolerance.** Thin. The
residual is the estimated V4/OTC legs, which is understood, but a tolerance that a fixture
only just clears will not survive a data change. Either tighten the estimate or state the
expected residual explicitly so a future regression is distinguishable from drift.

## Notes and agreements

- **Venue write gate.** Single enforcement point (`netflow.py:694`, `:722`, gated on
  `WALLET_KINDS`) is exactly the plan's §7 requirement, and `CONTRACT_UNKNOWN` staying a
  wallet keeps the standing decision that a bot's own contract is a real actor. Replacing
  the shape heuristic with the `token0()`/`token1()` probe was the right call, and finding
  that the heuristic had promoted 259 trading bots is the kind of evidence that should go
  in the plan.
- **Prod's registry is lossy.** Independently confirmed. The purge removed every `source = 0`
  row while I was merging into that generation, and your side-registry fallback is what let
  the CHIPOTLE fixture run at all. This belongs in `POSITION_LEDGER_PLAN.md` §8 as a hard
  rule: the token universe comes from logs plus a durable registry, never from the live
  table.
- **`scripts/replay_side.py`.** Your change fixes a genuine bug of mine: the connection was
  opened outside the retry block, so a tunnel drop during connect escaped the loop and
  ended the replay. That failure cost me several runs last night. Adopted.
- **Scale is unproven.** 113,567 flows in 30 minutes and 47,565 in 61 minutes are fixture
  scale; full history is three orders of magnitude larger, and the per-flush refold from
  the complete flow list per `(wallet, token)` is quadratic in the worst case. The
  checkpoint you mention is a prerequisite for any full run, not an optimisation.
- **Timing.** Vaults launch inside this window. Shadow across the launch, cut over after,
  as the plan now says.

## Open questions for the human

1. Which engine is authoritative where they disagree? They will disagree on any wallet
   holding transferred tokens, and the ledger is the more defensible answer.
2. Inbound transfers from genuinely unknown senders: zero basis (today) or mark-to-market
   at receipt (plan recommendation)? This is a product decision and it changes visible PnL.
3. Does the crystal generation's history get rebuilt into the new registry, or does the
   relaunch intentionally start that generation empty? The ledger can restore it from the
   log cache; nothing else can.
