# Reviewer 6: does this serve the product, and is the plan itself right?

Read `review/README.md` first. Write your report to `feedback6.md` in the repo root.

The other three reviewers are inside the code. You are the only one asked to step back and check that the
thing being built is the right thing, and that shipping it will not break what already works.

## Part one: the consumer contract

The plan promises no frontend contract change: `launchpad_positions` keeps its column names and meaning, and
new information is added rather than substituted. Test that promise against the actual consumers.

Read `CLAUDE.md` for who reads what, then check the real call sites: `api/routes/launchpad.py`,
`api/routes/fun.py`, `api/api.py`, `api/ws.py`, `api/routes/dexscreener.py`, `core/rewards.py`, and the
spot and portfolio paths in `api/spot_data.py` and `api/spot_graph.py`.

- Which consumers read holdings, cost basis, realized or unrealized PnL, and what would each see if it read
  `positions_v2` instead? Name any that would silently change meaning rather than break loudly.
- Rewards pays real money from USD volume. The plan says only confirmed figures may feed ranked or paid
  features. Confirm whether the ledger's three states can actually be kept out of the paid path, and what a
  wallet whose basis is mostly unresolved would earn compared with today.
- crystal.fun classifies a position as sold using a relative dust test. Check that still works.
- The DEX Screener adapter serves trade provenance. Check the ledger does not disturb it.

## Part two: does the model answer the user's question

A user opens the portfolio and wants to know what they hold and whether they are up or down. The ledger
answers with three parallel sets of figures: confirmed, estimated, and unresolved.

- On the current JAMES data, 44.6% of flow value is unresolved, driven by transfers whose sender's cost is
  known but not inherited. Work out what a real wallet's screen would say today, and whether it is more
  honest or merely less useful than the current engine's single confident number.
- The plan says a position that is mostly unresolved should render as "PnL incomplete" rather than a small
  confident number. Is that a good answer for a memecoin trader? Say what you would show instead if not.
- Burn addresses are treated as ordinary holders. One holds 8% of JAMES's supply and would rank as its
  largest holder. Decide whether that is right.

## Part three: the plan itself

`POSITION_LEDGER_PLAN.md` was reviewed once before the code existed. Now that an implementation and two code
reviews exist, re-examine the design decisions rather than the code.

- The three basis states, and the rule that confirmed PnL uses observed basis only. Is the boundary in the
  right place, given `feedback2.md` argues cost confidence and proceeds confidence are separate axes that the
  implementation conflates?
- Classification as the gate that keeps venues out of positions. Is a per-address kind the right primitive,
  or should the same address be allowed to be a venue for one token and a wallet for another?
- The plan assumes the token universe comes from the registry. Production purged an entire generation on
  2026-09-06, so that assumption is now provably lossy. What should the universe be?
- Anything the plan does not mention at all that a portfolio product needs.

## Part four: cutover risk

Vault launch is 2026-09-13. The intended sequence is a full-history replay, then shadow mode behind
`LEDGER_ENABLED`, then switching API reads.

Give the human a blunt answer to two questions. **What is the largest thing that could go wrong at each
step, and how would we notice?** And **what is the minimum version of this that is safe to ship by 9/13**,
if the full model is not ready. Being specific about what to cut matters more than being encouraging.
