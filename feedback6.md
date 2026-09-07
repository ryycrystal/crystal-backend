# Review 6: is this the right thing, and will shipping it break what works?

Scope: product fit, consumer contract, the plan itself, cutover risk. Not a line-by-line code audit —
three other reviewers are inside the code. Branch `accounting-fix` at `f913de9`, read 2026-09-07.

## Verdict

The netting model is the right idea and its balance results are the best evidence on this branch, but
**as built it must not be pointed at any consumer**: through the compatibility view the plan itself
proposes, the ledger's `unresolved` state renders as *maximum possible profit*, turning 67.6% of JAMES
holders into fabricated gains where the current engine fabricates 8.3%. Separately, and independently of
any ledger logic, **this branch is 22 commits behind `main` and reverts the relaunched crystal core and
the 9/13 vault factory** — deploying it at all, even with the flag off, breaks the launch.

---

## What I verified independently

| check | method | result |
|---|---|---|
| full suite | `SCRATCH_DB_NAME=crystal_rev6_itest REWARDS_WORKER=0` | **706 passed, 4 skipped**, 101.9s — matches both prior reviews |
| branch-only diff is additive | `git diff $(git merge-base origin/main HEAD)..HEAD --stat` | 10,154 insertions / 32 deletions; only `core/sequencer.py` (+10), `scripts/replay_side.py`, `pyproject.toml` touch existing code. feedback.md's flag-off claim holds **for the branch's own changes** |
| branch vs `main` | `git rev-list --left-right --count` | **22 behind**, 43 ahead. The `core/chain.py` delta reverts `CRYSTAL_ADDR` and the vault factory (P1-1) |
| prod registry | read-only via the tunnel | `launchpad_tokens`: 30,521 source 1, 1,599 source 2, **0 source 0**. 1,264,003 position rows |
| ledger tables in prod | `to_regclass('positions_v2')`, `('wallet_flows')` | **both NULL** — they do not exist (P1-3) |
| `crystal_unrealized_pnl` on ledger rows, JAMES | ran the real function over `positions_v2` at `last_price_native` | **1,715 of 2,537 holders (67.6%)** get no cost subtracted; **26,839,067 MON** of market value books as pure unrealized gain (16.2% of held value). Same computation on the side DB's old-engine rows: 208 holders (8.3%), 142,224 MON (0.1%) |
| same on moncock | as above | ledger 288 of 2,949 holders (9.8%), 2,128,031 MON (19.3% of value); old engine 405 rows, 424,680 MON (3.9%) |
| `insider_holding` (`api/api.py:467`) | ran the exact predicate both ways, JAMES | **6.1% → 90.3% of held supply**; 520 → 2,570 rows. Prod-wide baseline today: 20,051 of 534,875 holder rows (3.7%) |
| crystal.fun dust rule | read `crystal-fun/src/components/MemePortfolio/MemePortfolio.tsx:61-62,121-131` | `held = remaining > max(1e-6, token_bought*1e-9)` confirmed verbatim; `pnlPct = total_pnl_native / native_spent` |
| JAMES per-holder unresolved share | `positions_v2` | **1,538 holders (60.6%) are 100% unresolved; 1,656 (65.3%) are ≥50%** — the "PnL incomplete" population. moncock: 5.7% |
| burn address | `address_kinds` + `positions_v2` | `0x…dead` classified `eoa`/`getcode`, holds 80,675,269 JAMES (**8.07%**), ranks **#2 by balance and #2 on the PnL leaderboard at +13,615,963 MON**. It also carries **103 flows of `kind='buy'`, `basis_state='observed'`** |
| rewards paid path | read `core/rewards.py` | reads `launchpad_trades.usd_amount/user_address`, `crystal_market_trades`, `crystal_orderbook_fills`, `crystal_vault_*`. **Never reads any position table or any basis column** |
| reconciliation volume | prod, last 7 days | 136 trades / **$6,977 of $3,759,025 (0.19%)** carry `venue='reconciliation'` |
| DEX Screener adapter | read `api/routes/dexscreener.py`, prod query | reads only `launchpad_trades`/`launchpad_tokens`/`launchpad_kv`/`launchpad_blocks`. Its `venue IS NULL` fallback matches **0 source-0 rows** in the last 7 days (all 8,412 null-venue rows are source 1/2). Undisturbed |
| feedback2's data claims | side DB, read-only | **357** duplicate `(txhash,wallet,token)` groups and **389** negative balance/custody rows reproduced exactly — **0 of each inside the three fixtures**. Unresolved disposals booking a loss: **325 flows, −1,029.6049 MON, all moncock**, concentrated in one flow on `0xfb6ad172a80416c9ed2d06b37179d84228c7fb88` (not the fixture wallet, so it does *not* explain moncock's 0.47% residual) |
| plan §11 integrity | `grep venue_leak\|estimated_share\|positions_v2 core/integrity.py` | **nothing wired**. No runtime signal exists for any of the four continuous checks |

Caveat on the A/B: the side database's `launchpad_positions` is an earlier engine replay of the same
tokens and may sit at a slightly different head. Every headline number above is intrinsic to
`positions_v2` alone; the old-engine column is corroboration, not load-bearing.

---

## Findings

### P1-1 — The branch reverts the relaunched crystal core and the 9/13 vault factory

`core/chain.py:63`, `:68`, `:111`. From `git diff origin/main -- core/chain.py`:

```
-CRYSTAL_ADDR       = "0x8e42afa92A8B0ED3eE23Db6B108419Aae47aD61F"   (main, commit fe0b24d)
+CRYSTAL_ADDR       = "0x6eb2aF5FC575689053Ac9b413220CaBfd01A2F9A"   (this branch, retired gen-3 core)
-VAULT_FACTORY_ADDR = "0x2388208C8F39e1E5A7FfbF8a2B30c73C7009cc00"   (main, the relaunched factory)
+VAULT_FACTORY_ADDR = "0xe35937f2c0E01589a9E8E9A2C9223b7B816247E7"   (this branch, now the retired gen-2)
```

`0x2388208C…` appears **nowhere** in this branch (`grep -rin 2388208c --include=*.py .` → no hits), so
the relaunched vault factory is not in `VAULT_FACTORY_ADDRS` and its events would not pass
`accepts_log_for_indexing`. Cost: deposits on 9/13 accrue nothing, silently, and the only symptom is
`crystal_rewards_vault_gaps` filling with `no_sample` rows — exactly the scenario CLAUDE.md's
TIME-CRITICAL section says cannot be repaired retroactively. Live crystal-core events are dropped the
same way, and blocks are processed exactly once.

The ledger's own classifier *does* know the relaunched core (`core/ledger/kinds.py:46`), so this is
stale-branch hygiene, not a design error. **Rebase onto `main` before anything, including a shadow
deploy.** Also arriving with those 22 commits: AUSD floating on its own USDC book
(`9b057fa`/`7960102`/`38abf6c`), the routed order-book fill attribution fix (`1a28b58`), and the DEX
Screener zero-reserve hotfix (`3d434df`) — all of which this branch would undo.

### P1-2 — The promised "no frontend contract change" inverts the meaning of unrealized PnL

Plan §12 Phase 3 says "point the API at `positions_v2` through a view with the old column names", and
§6.7 says "Unrealized stays a read-time fold: `crystal_unrealized_pnl(...)`, unchanged". Those two
sentences are incompatible. `core/storage/schema.py:310`:

```sql
SELECT GREATEST(hold,0) * COALESCE(price,0)
     - CASE WHEN GREATEST(bought - sold, 0) > 0 THEN ROUND(GREATEST(basis,0) * ...) ELSE 0 END
```

The function has two states, not three: cost known, or **cost is zero**. Ledger `unresolved` and
`estimated` inventory both arrive with `cost_basis_native = 0` and (for unresolved) `token_bought = 0`,
so the `ELSE 0` branch fires and the wallet's **entire market value is booked as unrealized profit**.

Measured on JAMES using `launchpad_positions_live`'s exact expression:

| | rows with no cost subtracted | market value booked as pure gain |
|---|---|---|
| current engine | 208 of 2,496 holders (8.3%) | 142,224 MON (0.1% of held value) |
| ledger via the same view | **1,715 of 2,537 holders (67.6%)** | **26,839,067 MON (16.2%)** |

Concrete case: `0x167ee78660ddca546b07afa4928563e617675203` holds 9,667,990 JAMES. Today its screen says
**+36,071 MON** unrealized. Under the ledger through the same view it says **+1,631,716 MON** — 45× — and
the ledger arrived there by deliberately recording *no* cost. The engine built to stop manufacturing
phantom profit manufactures 189× more of it than the engine it replaces.

This is the silent-meaning-change the brief asked for: `unrealized_pnl_native` keeps its name, its type
and its plausibility. Nothing 500s. It propagates to `/user/{addr}` (`api/routes/launchpad.py:1425-1440`,
which sums it into `total_pnl`), `/fun/user`, the WS `positions` and `user_positions` channels
(`api/ws_data.py:170-178`, `:265-280`), and the per-token PnL leaderboard — where **11 of the ledger's
top 20 JAMES wallets have no cost subtracted at all**, including `0x…dead` at #2.

**Fix:** the view is not a compatibility layer. Either `crystal_unrealized_pnl` gains an explicit
"no basis" arm returning NULL (loud) instead of `hold*price` (silent), or the cutover ports every
consumer deliberately. Do not ship the view as written.

### P1-3 — `LEDGER_ENABLED=1` against prod is an indexer outage, not shadow mode

`init_ledger_schema` (`core/ledger/schema.py:141`) has exactly one non-test caller,
`scripts/ledger_replay.py:85`. `core/storage/schema.py`'s `init_db()` — which the indexer owns — does not
create the ledger tables, and I confirmed read-only against prod that `positions_v2` and `wallet_flows`
**do not exist**. `core/sequencer.py:1036-1037` calls `LEDGER.process_block(..., cur)` on the sequencer's
own cursor inside the per-block transaction, so the first block raises `relation "wallet_flows" does not
exist` and aborts the whole block transaction. Container Apps restarts it; it fails again.

Compounding it, the same call issues RPC (tx metadata, `getCode`, traces) from inside that open
transaction. CLAUDE.md's "never hold a transaction open across slow work" rule exists because this
pattern took the production API down twice, and the symptom is API 500s on plain selects while cached
endpoints keep serving. feedback.md flagged the RPC half; the missing tables are what make the flag
unusable at all today.

Also `core/sequencer.py:11` and `:1130` import and construct `LedgerEngine` at module import time
regardless of the flag, so an import-time error in the ledger package takes the indexer down with the
feature off. That is feedback.md finding 4 — I agree and rate it higher, because it is the only thing
standing between "flag off" and "byte-identical".

### P1-4 — `token_bought` collapsing silently rewrites four numbers users act on

`core/ledger/fold.py:150-152`: an unresolved acquisition returns before touching `token_bought`,
`native_spent`, `trade_txs` or `buy_txs`. `:185-188`: every `transfer_in` is unresolved. `:162-163`: a
sell always increments `token_sold` and `native_received`. Today `state.apply_token_transfer`
(`state.py:1439-1443`) moves `bought_delta` **and** basis with the tokens. So `token_bought` changes
meaning from "quantity you acquired" to "quantity you acquired *and we could price*", with no rename.

Measured on JAMES — 2,257 of 4,931 rows (45.8%) get `token_bought = 0`, 1,514 of 2,537 holders (59.7%)
get both `token_bought = 0` and `native_spent = 0`:

| consumer | code | today | ledger |
|---|---|---|---|
| token page "insider holding" | `api/api.py:467` `balance > (bought-sold)+1e18` | 6.1% of held supply | **90.3%** |
| crystal.fun open/closed tab | `MemePortfolio.tsx:126` `remaining > max(1e-6, bought*1e-9)` | relative floor scales with position size | floor collapses to 1e-6 tokens; dust remainders flip from *closed* to *active* and begin contributing `remaining*price` to account value |
| stats channel buyers / sellers | `api/ws_data.py:421`, `:424` | 2,265 / 2,632 | 2,674 / 2,997 (net-flow finds routed trades the old engine missed — this direction is an improvement) |
| "pro traders" | `api/api.py:472` `realized>0 AND trade_count>=10` | 225 | 202 |
| crystal.fun PnL % | `MemePortfolio.tsx:130-131` `total_pnl_native / native_spent` | a ratio | numerator now excludes estimated cost while the denominator includes it (P2-2); reads 0% wherever `native_spent = 0` — 1,514 JAMES holders |

"90% insider holding" on a memecoin page is a rug signal. It would appear on every token, on the same
day, with no error anywhere. This is the most user-visible of the silent changes.

### P1-5 — The 44.6% unresolved figure is an unimplemented feature, not epistemics

The brief asks whether three parallel figure sets are more honest or merely less useful. On JAMES,
**1,538 of 2,537 holders (60.6%) have a 100% unresolved position and 1,656 (65.3%) are ≥50%
unresolved**. For two thirds of the token's holders the ledger's answer is "we don't know".

But COMPLETE.md's own table says **94.9%** of that unresolved inflow came from senders with observed buys
already in the ledger, and 3.0% more from senders in the ledger. Only **2.1%** is genuinely unknown. The
number is therefore not a measurement of what is unknowable; it is a measurement of `fold.py:185` not
implementing plan §6.7. Presenting it as honesty is the wrong frame: it is a regression against shipped
behaviour — basis travels with transfers today, and ~175K rows were repaired to make that true —
dressed as epistemic caution.

Until inheritance lands, the three-state model cannot be evaluated as a product, because the state
distribution it produces is an artifact of a missing rule. This is what settles the two reviews' tone
dispute (P3-2).

### P2-1 — `positions_v2` has one index; all five serving indexes are missing

`launchpad_positions` carries `idx_positions_token_balance`, `idx_positions_token_total_pnl`,
`idx_positions_user`, `idx_positions_user_balance_keyset` and `idx_positions_user_pnl_keyset`.
`positions_v2` has only its primary key (`core/ledger/schema.py:52`; confirmed via `pg_indexes` on the
side DB). A view cannot restore them. Prod holds 1,264,003 position rows, 534,875 with a balance, so
`/holders`, the WS `top_holders`, the six token-page LATERALs at `api/api.py:433-472` and both user
keysets would seq-scan. CLAUDE.md documents an endpoint timing out past 60s and a lock cascade that
parked the whole API; this is the same shape. Adding the indexes later, at indexer startup, against a
full table is itself a risk CLAUDE.md tells you not to take casually.

### P2-2 — Estimated basis is recorded and then invisible to every consumer

`0x78f96ea45fcf3761317c8f13abf695fe4c406c0e` on JAMES made two priced buys totalling **53,298.16 MON**,
both booked `basis_state='estimated'` from `venue_event` (blocks 86,035,524 and 87,500,064). Its
`basis_estimated_native` is 53,298.16 and its `cost_basis_native` is **0**. The view reads
`cost_basis_native`, so the screen shows **+186,035 MON** unrealized on a position whose known cost is
53,298 — while `pnlPct` divides that by `native_spent`, which *does* include the estimated 53,298. The
numerator and denominator of the same percentage use different confidence rules. That is a contract
inconsistency introduced by "additive" columns, not by a pending product decision.

### P2-3 — The persisted row cannot answer the question the model exists to answer

`core/ledger/types.py:122` — `PositionRow` carries `unresolved_tokens` but **not** `observed_tokens`,
`estimated_tokens`, or any parked (LP/vault) quantity or basis, all of which `PositionState`
(`fold.py:59-65`) tracks and then discards at `to_row()`. So from a served row you cannot compute
observed-basis unrealized separately from estimated-basis unrealized — exactly what plan §4 promises
three parallel figure sets for — and a wallet that LP'd or vaulted its tokens shows a reduced
`balance_token` with no indication of where they went. Corroborates feedback2 findings 10 and 12.

### P2-4 — Burn addresses are ordinary wallets, and netting gives one 103 purchases

`core/ledger/types.py:13` recognises only `ZERO = 0x0…0`; `0x…dead` gets `eth_getCode` → empty →
`eoa` → a position row. On JAMES it holds 80,675,269 tokens (8.07% of supply), ranks **#2 by balance**
(behind a 61% distributor, so the brief's "largest holder" is not quite right) and **#2 on the PnL
leaderboard at +13,615,963 MON** — entirely fabricated by P1-2.

It also carries **103 flows with `kind='buy'` and `basis_state='observed'`** (27.23 MON of attributed
quote, sourced `venue_event`). A burn address cannot buy. This is feedback2's finding 2 — opposite-sign
movements taken as proof of a purchase — occurring in real replayed output, where feedback2 could only
demonstrate it synthetically. The ledger's own invariant suite does not catch it.

**Decision on the burn question: no, it is not right.** Burned supply is not a holding. Add a
`burn_sink` kind, seeded from a known list, that records flows but writes no position row and no PnL, and
surface burned supply as a token-level field so it can be excluded from circulating supply and holder
percentages. Do not solve it with `holder_denylist`: that is per-address config nobody will remember to
seed, and CLAUDE.md already records two "empty list fails open" incidents.

### P2-5 — Plan §11's continuous verification is not wired, so nothing would page

`core/integrity.py` contains no reference to `venue_leak`, estimated share, unresolved share,
unclassified contracts, `positions_v2` or `wallet_flows`. During shadow mode there would be no signal at
all — not that the ledger diverged from chain, not that a venue acquired a position, not that a token's
unresolved share moved. Plan §13.4's threshold ("estimated basis share < 1% per token") is enforced
nowhere, and `scripts/ledger_check.py` reports both shares with `ok=True` unconditionally, which is
consistent with COMPLETE.md printing "reported" as the expected value against a 44.617% actual.

### P3-1 — Rewards and DEX Screener are genuinely safe, and this is worth stating plainly

The brief asks whether the three states can be kept out of the paid path. **They already are, by
construction, and the plan overstates the problem.** `core/rewards.py:255-258` accrues from
`launchpad_trades.usd_amount` keyed on `t.id`; spot from `crystal_market_trades`; maker from
`crystal_orderbook_fills`; vaults from `crystal_vault_*` and balance samples. Nothing in the paid path
reads a position row, a cost basis, or a PnL column, and the ledger writes none of those tables (it only
*reads* `launchpad_trades` at `core/ledger/engine.py:382`, `:443`).

**A wallet whose basis is entirely unresolved earns exactly what it earns today**, because rewards price
notional volume and unresolved-ness is a statement about cost, not about volume. The only path that could
change it is plan Phase 4's deletion of `apply_reconciliation_trade`, which today writes
`venue='reconciliation'` trade rows: on prod over the last 7 days that is **136 trades / $6,977 of
$3,759,025 — 0.19% of rewards volume**. Not material; but losing it silently on a different day might be,
so gate Phase 4 behind a volume diff.

The DEX Screener adapter (`api/routes/dexscreener.py`) reads only trades and tokens and is undisturbed; I
confirmed its `venue IS NULL` fallback still matches zero source-0 rows over the last 7 days, as
CLAUDE.md claims (all 8,412 null-venue rows are source 1/2). Two things to carry forward: `/events`
serves `t.user_address`, which is `_resolve_trade_user`'s output, so **Phase 4's removal of
`_resolve_trade_user` would revert DEX Screener's `maker` attribution to routers** unless trades are
re-attributed from flows first; and `api/spot_data.py` / `api/spot_graph.py` read `crystal_markets`,
chain balances and sync events and touch no position table at all — meaning plan §13.7 ("spot positions
and PnL exist for order-book trading, unified with launchpad positions") is entirely unbuilt, not
partially built.

### P3-2 — Settling the two prior reviews

They are not really in conflict once you separate the fixtures from the rest of the side database.

- feedback2's findings 6 and 7 (357 duplicate flow groups, 389 negative rows) reproduce exactly — and
  **all 746 fall outside the three fixture tokens**, in the 159 partially-replayed leftovers. They are
  strong evidence that *scoped replays produce unusable rows*, which is a cutover finding, not evidence
  that the fixtures are wrong. feedback.md is right that the fixtures are real.
- feedback2's finding 3 (an unresolved disposal invents a loss) lands **inside a fixture**: 325 moncock
  flows, −1,029.6049 MON, concentrated in one flow on `0xfb6ad172a80416c9ed2d06b37179d84228c7fb88`. That
  wallet would see a 1,029.6 MON realized loss that never happened. It is not the moncock fixture wallet,
  so it does *not* explain the 0.47% residual feedback.md worried about — but it does show the acceptance
  suite passing over a real invented number. feedback2 is right that the checks are not acceptance.
- The disagreement about the *model* is the real one, and P1-5 resolves it: the state distribution both
  reviewers are arguing about is produced by an unimplemented rule, so neither is judging the design that
  was actually proposed.

Practical reading: feedback.md's verdict ("design right, evidence real, not ready to cut over") and
feedback2's ("shadow prototype; the marker is not acceptance") are the same verdict at different volume.
Adopt feedback2's blocking list and feedback.md's assessment of the fixtures.

---

## Part two: what the portfolio screen should say

Today's engine gives a memecoin trader one confident number that is *sometimes* wrong. The ledger as
built gives two thirds of JAMES holders no number, and — through the view — a very confident wrong one.
Neither is the answer.

Do not ship "PnL incomplete" as the primary treatment. A degen trader is not asking an accounting
question. They are asking three things, and two of them never need a cost basis:

1. **"What is it worth?"** `balance_token × last_price_native`. Always known. Make it the headline.
2. **"Have I got my money back?"** `native_received − native_spent` over observed flows. Exactly known
   for every trade we saw, needs no basis at all, and is the number this audience actually uses. The
   ledger has both halves and no surface exposes the difference. Add `net_cash_native` as a first-class
   column.
3. **"Am I up?"** — the only one that needs basis. Show it **only over the covered fraction, with the
   fraction stated**: "+1,240 MON on the 34% of this position we can price". Never show a bare percentage
   whose numerator and denominator use different confidence rules (P2-2).

Sequencing matters more than presentation. **Implement transfer inheritance first** (plan §6.7,
`fold.py:185`). It is the difference between 65.3% of holders incomplete and roughly 2%, per the author's
own measurement. At 2%, "PnL incomplete" is a rare honest badge and every recommendation above is cheap.
At 65%, it is the default state of the product and no presentation saves it.

For genuinely unknown receipts (the residual 2.1%), unresolved is the right default and mark-to-market at
receipt is the wrong one: marking to market at receipt converts an airdrop into a realized gain the
moment it is sold, which is the phantom profit this project exists to remove. Show the quantity, show the
value, omit the PnL, label the reason.

---

## Part three: the plan, re-examined

**The three basis states put the boundary in the wrong place — feedback2 is right, and there is a third
axis neither review names.** `basis_state` is a property of a *flow*, but PnL confidence is a property of
a *position's inventory*, and the code proves the mismatch: `fold.py` tracks `observed_tokens` /
`estimated_tokens` / `unresolved_tokens` and then throws two of the three away at `types.py:122`.
Collapse the enum into two independent booleans per lot — **cost known** and **proceeds known** — and
demote `estimated` from a confidence tier to a *provenance* tag (how we learned the number), which is
what it actually is. Then add the axis the plan never mentions: **valuation confidence**. A position in a
token with no live price has unknown unrealized PnL regardless of basis, and CLAUDE.md records a real
incident where one bad trade zeroed `last_price_native` and 66 positions showed no value. Three axes, two
of them booleans, is simpler than one three-valued enum that has to mean three different things.

**Per-address classification is the right default and the wrong primitive to make load-bearing.** The
asymmetry decides it: a wrong *wallet* classification adds a junk row; a wrong *venue* classification
silently deletes a real user's entire position. COMPLETE.md documents that happening — the old shape
heuristic "promoted 259 trading bots with prod positions to venues, which silently dropped their JAMES
flows". 259 real holders erased by one rule change. Vaults, routers and market-maker contracts are all
venues for their own pair and ordinary holders of anything else that reaches them, so yes, an address
should be allowed to be a venue for one token and a wallet for another — but the cheaper, safer form is a
per-`(address, token)` **override** table with a recorded reason, keeping the global kind as the default.

The harder constraint is that classification must not be able to change *identity*, and today it can:
`sub_index` enumerates currently-eligible legs, so reclassifying an address renumbers an existing flow's
primary key and `ON CONFLICT DO NOTHING` then inserts a duplicate instead of deduplicating (feedback2
finding 6; 357 such groups, which I reproduced). Whatever the classification primitive, the ledger key
must be **provenance-only** — `(txhash, log_index, transfer_ordinal)` — with classification a mutable
interpretation layered on top. That single change also turns reclassification into a refold rather than a
corruption event, which is the property the plan needs and does not have.

**The token universe cannot be the registry, and the side database proves it.** Prod now holds 0 source-0
tokens; the side database's `token_registry` holds 30,518 `nadfun_v1`, 2,159 `nadfun_v2`, 3 `spot_base`
and **6 `crystal`** — the hand-seeded fixtures. A full replay driven off either registry would rebuild
nad.fun and essentially none of the crystal generation, i.e. precisely the tokens crystal.fun serves. The
universe should be **the log cache's own creation events** (`TC`, `NFC`, `MC`), with `token_registry` made
append-only: rows are never deleted, retirement is a `retired_at` timestamp, and `active` gates
*serving*, never *folding*. A purge of derived state must not be able to lose a token, which is exactly
what happened on 09-06. This belongs in §8 as a hard rule.

**Things a portfolio product needs that the plan never mentions at all:**

- **USD basis and USD PnL.** `wallet_flows` stores `usd_value` per flow; `positions_v2` has no USD column
  at all. Users think in dollars, and CLAUDE.md documents five distinct USD pricing paths whose
  disagreements have already produced visible bugs.
- **A wallet-level rollup.** `positions_v2` is per `(wallet, token)`. There is no account value, no cash
  (quote-asset) position, and no answer to "am I up across everything" except summing whatever the client
  happened to fetch. `launchpad_users` exists for this today and the plan does not say what becomes of it.
- **Historical position value.** The plan deletes the basis overlay and keeps unrealized as a read-time
  fold, so nothing produces a position's value at a past time — yet §13.7 requires the portfolio graph and
  the live total to agree, and `/portfolio/{addr}/daily` and `api/spot_graph.py` still have no
  ledger-derived source.
- **Fees as a line item.** feedback2 showed router fees erased to match venue amounts. A portfolio that
  observes a fee and then hides it is worse than one that never saw it.
- **A server-side open/closed rule.** The dust rule lives in the frontend and depends on `token_bought`,
  which this change redefines. If the backend owns positions it should own `is_open`.
- **Spam/airdrop suppression.** Making `unresolved` receipts first-class makes every spam airdrop a
  portfolio row with a headline "profit". There is a per-address `holder_denylist` and no token-scoped one.
- **A burn sink kind** (P2-4).

---

## Part four: cutover risk, bluntly

**Step 1 — full-history replay.** Largest risk: it produces an authoritative table containing silently
*incomplete* token histories. Measured in the side data: 389 negative balance rows and 357 duplicate flow
groups, none in the scoped fixtures, all in tokens that were only incidentally touched. `--resume` uses
`MAX(block_number)` over the requested tokens, so a later incidental flow can skip another token's missing
earlier history. **How you would notice: you would not.** There is no per-token coverage record and no
integrity counter (P2-5). Prerequisite: per-token contiguous coverage ranges committed atomically, plus a
hard refusal to serve any token whose coverage is incomplete. Second risk: the fold refolds the full flow
list per `(wallet, token)` on every flush — fine at 113k flows, quadratic at prod scale (1.26M positions).
Both prior reviews flagged it; nobody has measured it, including me.

**Step 2 — shadow mode behind `LEDGER_ENABLED`.** Largest risk is P1-3: the tables do not exist in prod
and the call runs on the sequencer's own cursor, so turning the flag on stops indexing entirely. Second
largest is RPC inside the open block transaction; that failure presents as API 500s on uncached endpoints
while cached ones keep serving, which reads like an application bug rather than a lock cascade. **How you
would notice:** `[SQ]` log lines stop, and `SELECT COUNT(*) FROM pg_locks WHERE NOT granted` goes nonzero.
Prerequisites: an idempotent schema step inside `init_db()`, a preflight that refuses to enable when the
tables are absent, RPC prefetched per chunk the way `scripts/ledger_replay.py` already does, and the lazy
import.

**Step 3 — switching API reads.** Largest risk is P1-2 and P1-4: numbers change meaning while staying
plausible. **How you would notice: you would not, and that is the point.** No exception, no null, no shape
change — a token page reading 90% insider holding and a portfolio reading +45× unrealized both render
perfectly. The only detection that works is a *differential*: run both engines and alert on per-wallet PnL
and per-token holder-stat divergence above a threshold, for at least a week, before switching anything.
Secondary risk is P2-1: the missing indexes turn `/holders` and the token-page LATERALs into seq scans
over 534k rows, and CLAUDE.md's history says that is how the launchpad goes down.

### Minimum version safe to ship by 2026-09-13

Ship the branch **only after a rebase onto `main`**, with the ledger entirely inert:

1. **Rebase, or at minimum cherry-pick `fe0b24d`**, and take the other 21 commits. Non-negotiable and
   independent of everything else (P1-1). Verify with
   `python -c "from core import chain as h; print(h.CRYSTAL_ADDR, h.VAULT_FACTORY_ADDRS)"` before the
   image is built, not after — and remember `az acr build` ships the working tree, not HEAD.
2. **Make `LEDGER_ENABLED` impossible to turn on by accident**: lazy-import `LedgerEngine` inside the
   guard (`core/sequencer.py:11`, `:1130`) and add a preflight that logs and refuses when the ledger
   tables are absent. Under an hour of work.
3. **Ship `core/ledger/*` and `scripts/ledger_*` as dead code.** They are genuinely additive (10,154
   insertions against 32 deletions, only the sequencer touched) and carry zero launch risk with the flag
   off. The suite is green at 706 on this tree.
4. Run the replay tooling **against the side database only**, from a laptop, on whichever tokens matter.
   It needs no flag and no deploy.

**Cut, explicitly, all of it:** the shadow write on the live indexer; the compatibility view; any change
to `launchpad_positions`; the transfer-inheritance rewrite; the cost/proceeds confidence matrix;
cross-wallet folding; custody, LP and vault accounting; spot positions; and — hardest to give up — the
merge of the corrected balances into prod. That last one is the strongest result on this branch (moncock
9,375/9,375 and JAMES 4,936/4,936 exact against chain, and 213–0 against prod where the two engines
disagree, per COMPLETE.md) and it addresses CLAUDE.md's largest open item, the 17,889 wrong
`balance_token` values. **Cut it anyway.** It is a bulk write to prod's position tables during launch
week, its only rollback is PITR, and its benefit is invisible to the vault launch.

If there is appetite for more than the rebase in the remaining week, the single highest-value item is
**wiring plan §11's four counters into `core/integrity.py` against the existing tables**. It is about a
day, it is the only thing that would make a later cutover detectable at all, and it is useful whether or
not the ledger ever ships.

---

## What I did not check

- **No line-by-line audit** of `netflow.py`, `kinds.py`, `engine.py`, `store.py` or `txmeta.py`. Three
  other reviewers are inside them; I read `fold.py`, `types.py` and `schema.py` in full and the rest only
  where a consumer question led me there.
- **No replay was run** (brief instruction), so I did not verify the fixtures myself, did not rebuild the
  side database from clean inputs, and cannot attribute every stored anomaly to this commit rather than to
  an earlier iteration.
- **The old-engine baseline is the side database's `launchpad_positions`**, an earlier replay of the same
  tokens that may sit at a different head block. I did not verify its head. Every headline number is
  intrinsic to `positions_v2`; the old-engine column is corroboration only.
- **No chain calls.** I did not independently verify any `balanceOf`, so COMPLETE.md's supply-conservation
  result and its 9,375- and 4,936-wallet comparisons are unverified by me. feedback2 independently
  verified 100 JAMES wallets.
- **No throughput or scale measurement.** The quadratic refold, the trace budget, `tx_meta` fetch cost at
  400 ms blocks and full-history replay runtime are unmeasured by me and, as far as I can tell, by anyone.
- **CHIPOTLE** has no `positions_v2` rows under the address I queried, so all quantitative work here is
  JAMES and moncock only.
- **I did not read the main interface repo** (`crystal-interface`); the frontend evidence is crystal.fun
  plus the backend payloads. The interface reads the same `launchpad_positions_live` columns, so I expect
  the same exposure, but I did not confirm which components render them.
- **`api/spot_data.py` and `api/spot_graph.py`** I checked only for position-table dependencies (there are
  none). I did not review their own correctness.
- **I did not test the compatibility view**, because none exists yet. P1-2 is computed by running
  `crystal_unrealized_pnl` — the real production function, present in the side database — over real
  `positions_v2` rows at the real `last_price_native`.
- I did not re-derive feedback2's findings 1, 4, 8, 9, 11 or 14, or feedback.md's moncock tolerance
  analysis beyond establishing that the invented loss does not explain it.
