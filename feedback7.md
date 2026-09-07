# Reviewer 7: the repair design, judged against the seam rule

Reviewed 2026-09-07 against `accounting-fix` at `f9a6013`. `REPAIR_PLAN.md` is a design; nothing in it is
implemented. I prototyped against `core/ledger/*` to test specific claims and implemented no step.

## Verdict

The plan cuts in four of the right places — A, B, C and E are seams — but **three of them are
under-specified in ways a reviewer can falsify today, and D is a layer**. The ordering is wrong where it
matters: **C before B recovers nothing at all**, because the 156 transactions C is meant to rescue all net to
zero and transaction-wide netting deletes them again; I measured that on the plan's own evidence. And A, B and
C are not three seams. They are one seam — what a *movement* is — split three ways, and splitting it is what
produces the ordering question, three clean rebuilds instead of one, and a key in A that step E cannot use.

Separately: **the plan says `feedback4.md` "was commissioned and never delivered". It is in the repo root, 43 KB,
written an hour before the plan, and it confirms with blast radius exactly the four P1s the plan drops.**

---

## What I verified independently

| check | method | result |
|---|---|---|
| branch state | `git rev-list --left-right --count origin/main...HEAD` | **0 behind, 45 ahead**; `0x2388208C…` present at `core/chain.py:69`. `feedback6.md` P1-1 (reverted core and vault factory) **is resolved at `f9a6013`** |
| exact round trip vanishes | `net_transaction`, buy 100 / sell 100 through one pool | **0 flows** — reproduces `feedback2.md` finding 1 |
| unequal round trip | buy 100 @ 100 WMON, sell 60 @ 72 WMON, one tx | one flow: `+40 tokens, −28 WMON, observed`. The sale is erased **and the surviving buy's unit cost becomes 0.70 instead of 1.00** |
| the anchor moves | same wallet, sell at log 2 then buy at log 7 | today's single flow carries `log_index = 2` (the *sell's*) with `kind = buy`. Under B the same key holds a sell |
| the proposed key does not dedupe | side DB, `group by txhash, wallet, token, log_index` | **all 357** duplicate groups also collide on the proposed key. Dedupe becomes first-writer-wins under `ON CONFLICT DO NOTHING` (`store.py:69`) |
| the fold loses its tiebreaker | side DB | **13,263** chain positions carry >1 flow (26,535 flows, 15.2%); **12,819** are the two halves of one transfer; in **7,280 of those (56.8%) the receiver currently sorts before the sender** |
| coincidence inference survives B | union-find over the transfer graph, "gift + unrelated payment" | **one connected component** — they share the wallet node. The plan's claim that B removes this "as a side effect" is false under its own definition |
| action definition merges a real round trip | union-find, buy and sell of one token through one pool | **one connected component**. "Maximal connected set" does not split the case B exists to split |
| forged emitter | `LT` from `0x…dede`, which `accepts_log_for_indexing` rejects | `kind=buy, quote=−10 MON, basis_state=observed, source=venue_event`. `engine.py:240` applies **no emitter gate** |
| mixed quote leg dropped | 10 WMON + 20 USDC for 100 tokens at 1 USD/MON | recorded as **20 MON, `observed`**; truth 30. `netflow.py:222` |
| D cannot be two labels | fold 100 observed@100 + 100 estimated@300 + 100 unresolved, sell 150 @ 600 | fold computes released_observed 50, released_estimated 150, realized 150 / estimated 50 / unresolved_proceeds 200. **The row stores `basis_delta=−200, realized_delta=200, basis_state='observed'`** |
| E's cost, measured | loaded moncock's 114,623 flows, timed the real `fold.fold` | one ordered pass **3.35 s (34,189 flows/s)**; per-token refold on every 500-block flush **4.1 billion flow-applications ≈ 33 h of pure fold CPU**, against 72 min for today's per-pair refold — **28×** |
| checkpointing is blocked | `PositionState` vs `PositionRow` | `observed_tokens`, `estimated_tokens`, all five `parked_*` fields and the three tx-hash sets are **carried and never persisted**, so `fold(prev, …)` cannot resume |
| **the 26 + 130 are two populations** | prod `launchpad_trades` + `launchpad_block_logs`, all 147 blocks cached | moncock **16 with no ERC-20 transfer (C class) / 10 with transfers netting to zero (B class)**; JAMES **62 / 68**. All 156 net to zero, so **neither step alone recovers any of them** |
| the plan's cited transaction | receipt + `eth_getCode` on `0xd786059a…` | `tx.from` = `0x6807af96…` (**EOA, in no transfer, receives nothing**); `tx.to` = `0x9d54c129fb…` (**contract, and the `sender` on all three V4 Swaps**); the 645.878067 USDC profit lands on `0xd28832cf…`, whose code is `0xef0100…` — **a 7702-delegated wallet** by CLAUDE.md's own test |
| side DB fixture hygiene | side DB, read-only | **0** negative rows and **0** duplicate groups inside CHIPOTLE/moncock/JAMES; all 389 and all 357 sit in the 159 leftovers (1,276 position rows, 10,501 flows) |
| `feedback4.md` exists | `ls`, `head` | delivered, 43 KB, mtime 16:39 vs `REPAIR_PLAN.md` 17:33; confirms findings 7, 8, 9, 10, 12, 13, 14 with measurements |
| prod scale for E | prod, read-only | 1,264,050 position rows; largest token **793,773 trades** → ≈23 s of CPU for one whole-token fold pass |

Side database and prod were opened `readonly=True`. I ran no replay and wrote nothing anywhere.

---

## The six questions

### 1. Is the flow key stable across B? No — but the fix is to widen A, not to move it

The key stays *unique*. It does not stay attached to the same movement, and one of the three failures is
already visible in stored data.

- **Content flips under an unchanged key.** The anchor today is `first_log[(wallet, token)]`, the minimum log
  index over *all* of that pair's transfers in the transaction (`netflow.py:457`). For a wallet that sells at
  log 2 and buys at log 7, the single netted flow carries `log_index = 2` with `kind = buy`. Under B, log 2 is
  the *sell* action's anchor. Same key, opposite sign. With `ON CONFLICT DO NOTHING` (`store.py:69`) an
  overlapping re-replay keeps the pre-B `+40` row and inserts the post-B `+100` row beside it: the wallet ends
  at +140. That corrupts the balance layer, which is the only layer the author's evidence covers.
- **The key does not dedupe what it was designed to dedupe.** All 357 duplicate `(txhash, wallet, token)`
  groups in the side database *also* collide on `(txhash, wallet, token, log_index)` — my count matches
  `feedback4.md`'s 357 groups / 360 extra rows. So A converts 357 visible duplicates into 357 silent
  first-writer-wins conflicts, including the 37-in-200 where the two copies disagree on `kind`
  (`feedback3.md`). Today you can at least see the disagreement.
- **A's own acceptance is vacuous.** "Re-replay twice, rows identical" is guaranteed to pass by construction
  once the key is stable and the write is `DO NOTHING`, whether or not the second replay would have produced
  the same rows. And the 357 duplicates live entirely in the 159 leftover tokens, which `feedback2.md`
  finding 7 says are structurally incomplete; the three fixtures already show zero duplicates, so a clean
  fixture rebuild proves nothing about A either way.

**The sequence does not invert.** A stays first. What must change is what A does: identity has to carry the
movement's *side*, and the write has to become a versioned replace rather than `DO NOTHING`.

### 2. C before B, or B before C? Neither — they are one change, and C-before-B recovers zero

This is the plan's clearest error, and it is measurable on the plan's own evidence.

All 26 moncock and 130 JAMES vanished wallet-transactions were selected because they **net to exactly zero**.
`net_transaction` builds a leg only when `delta != 0` (`netflow.py:745`). So under transaction-wide netting,
admitting the missing evidence changes nothing: the movements are added and then cancelled. **C before B
recovers 0 of 156.**

I also split the population, which neither prior review did:

| | no ERC-20 transfer at all (needs C) | transfers present, net to zero (needs B) |
|---|---|---|
| moncock | 16 | 10 |
| JAMES | 62 | 68 |

The plan's C section says all 26 and 130 "vanish this way". Half of them do not — they are B's, and
`feedback3.md` already counted the same 156 under finding 1. **The plan's acceptance criterion for C
("the 26 moncock and 130 JAMES vanished transactions appear") cannot distinguish C's effect from B's, and
cannot be met by C alone.** B before C recovers 78 of 156. Both together recover 156.

The plan's stated reason for C-first — "avoids doing the grouping twice" — is an implementation-effort
argument, and it is answered by doing them once, together.

### 3. The definition of an action: wrong, and provably wrong in both directions

"A maximal set of movements connected through the transfer graph and venue events, between one wallet-facing
start and end" is two definitions in one sentence and they disagree. Read as a connected component — which is
what "maximal set … connected" means — it fails the cases B exists for:

- **A wallet trading the same token twice through the same pool.** Buy at log 5, sell at log 9, both touching
  `{wallet, pool}`. One component. B degenerates into exactly the transaction-wide netting it replaces.
  Verified by union-find over the real leg set.
- **The plan's own claimed side effect.** "A gift and an unrelated payment do not [connect]." They do — both
  touch the receiving wallet. One component. The coincidence inference of `feedback2.md` finding 2 survives B
  unchanged. Verified.
- **Batched multi-user transactions.** Repairing the definition to "connected through non-wallet nodes only"
  fixes the gift case and breaks this one: two users buying through one router from one pool are connected
  through the router and the pool, so they become a single action.
- **ERC-4337 bundles** are the same shape one level up: several senders, one EntryPoint node.
- **Self-transfers** produce two movements at one log index for one wallet; under netting they vanish, under
  components they are a singleton netting to zero, and the plan's "`sub_index` as a within-anchor
  discriminator" is the only thing between them and a primary-key collision.
- **Multi-hop cycles returning to the payer** — the plan's headline C transaction — are one component by
  construction. They must not be one action, or the arbitrage collapses to nothing again.

**Connectivity is the wrong primitive; matching is the right one.** A trade is a token movement *matched* to a
quote movement through shared evidence: the same venue event, a direct transfer counterparty, or a trace edge.
Movements that match nothing stay unmatched — a gift is unmatched, an unrelated payment is unmatched, and
neither is upgraded by proximity. Under matching, every case above falls out with no special case: aggregator
hops collapse because the hops share one venue event; two trades of one token in one transaction stay apart
because they match two different venue events; a batch splits per settlement event; a cycle becomes a chain of
matched pairs. Matching is also what makes finding 2 disappear "as a side effect", which is what the plan
claims for B and only gets under this definition.

### 4. Wallet attribution for claim-settled swaps: the proposed fallback is wrong on the plan's own transaction, and unnecessary

`0xd786059afa5e0b48…`, block 45,905,959, decoded from the receipt:

```
tx.from  0x6807af96b344cdf39f17cd1e1eb96385af57619d   EOA, in no transfer, receives nothing
tx.to    0x9d54c129fb5ce5e4480845392c1dcd6689a8b76e   contract (78,790 hex of code)
                                                       — and the `sender` field on all three V4 Swap events
0xd28832cfafa551b984cccca1fee30d7ed6674873             code = 0xef0100… -> a 7702-delegated WALLET;
                                                       receives the 645.878067 USDC profit
```

**Falling back to the transaction origin credits `0x6807af96…`, which appears in no transfer and holds
nothing.** That is exactly the phantom-holder class the classifier exists to remove (`feedback5.md` finding 3),
and it contradicts a documented, incident-derived rule in CLAUDE.md: "`tx.from` … is wrong for ERC-4337, where
it returns the bundler … Leave these attributed to the bot's own contract."

The fallback is also not needed here: the V4 `Swap` event carries `sender` = `0x9d54c129fb…`, which
`netflow.py:184` already parses. So: **prefer the venue event's own actor field.** Where a venue event has no
such field, record the movement with **no wallet**, keep the origin in the existing `Flow.origin` column, and
account the other side against the venue's own custody balance so quantity still conserves — the PoolManager's
ERC-6909 claim balance is a real custody location, not a hole. Do not invent a holder to keep a column
non-null. "Marked explicitly" does not help: `venue_leak`-style counters watch addresses classified as venues,
and an origin fallback creates a position on an address classified as a *wallet*, which nothing counts.

One further thing this transaction shows that nothing in A–E addresses. The full cycle is
`1.0 USDC -> 320.0198 moncock -> 27,667.17 WMON -> 646.878 USDC` (the middle asset confirmed on chain as WMON,
`0x3bd359c1…`). Under B+C the ledger would book this contract a **buy of 320 moncock for 1 USDC and a sale for
27,667.17 WMON — roughly +27,666 MON realized — against a real economic profit of 645.88 USDC.** It is
arithmetically correct under average-cost accounting and economically nonsense, because one of the three pools
is mispriced; that is what arbitrage *is*. **C admits `observed` quotes from venues with no credibility gate.**
`POSITION_LEDGER_PLAN.md` §6.6 already worries about this for *estimated* prices; C extends the same attack
surface to the strongest confidence the system has.

### 5. Step D's stored shape: a layer, and it does not deliver finding 4's separate axes

Two confidences on a disposal cannot express what the fold already computes. Measured, on a position holding
100 observed @100, 100 estimated @300 and 100 unresolved, selling 150 for 600:

```
fold computes   released_observed 50   released_estimated 150   unresolved qty consumed 50
                realized_pnl 150       realized_estimated 50    unresolved_proceeds 200
row stores      basis_delta -200       realized_delta 200       basis_state 'observed'
```

`_take_open` (`fold.py:122`) draws proportionally from **all three** inventory states in one call and
`_apportion` splits proceeds **four** ways. A "basis confidence" scalar plus a "proceeds confidence" scalar
cannot round-trip a 3×3 outcome. Adding two labels to this row is the layered version of the fix; the plan
names that trap in its own text and then walks into it.

**The seam is that `basis_delta` and `realized_delta` are lossy projections of a vector the fold has in hand.**
Store the vector — `released_observed`, `released_estimated`, `qty_from_unresolved`, and proceeds split
`observed / estimated / unresolved`. Then `basis_state` on a disposal stops being a label and becomes
derivable, and finding 4's consumer filter (`basis_state='observed'`) has something real to filter on.

Two things D leaves undefined that decide whether it works at all:

- **Where does the retained basis of an unresolved disposal live?** If it stays in `cost_basis_native` while
  the tokens have gone, average cost per remaining token inflates and every later sale over-releases. It needs
  a fourth bucket — disposed-but-unpriced basis, held outside the running average — and the plan does not say
  so.
- **How does an estimate become observed later?** D's premise is that proceeds may be learned later. That is a
  correction path, and corrections need the versioned-replace semantics A dropped. **D depends on the half of
  A the plan cut.**

The quantity half of D is right and is a real gap: `observed_tokens`, `estimated_tokens` and all five
`parked_*` fields exist in `PositionState` and are absent from `PositionRow` (`types.py:122`). Verified,
matching `feedback4.md` 12a.

### 6. Step E's cost: not affordable as written, 28× measured, and missing two prerequisites

I loaded moncock's 114,623 flows from the side database and timed the real `fold.fold`:

```
one ordered pass over the whole token                      3.35 s   (34,189 flows/s)
today's per-(wallet,token) refold, 500-block chunks   148,492,092 applications   ~72 min
step E's per-token refold, same chunking            4,105,968,910 applications   ~33 h     (28x)
incremental fold(prev, new)                               175,102 applications   ~5 s
```

Extrapolating: prod's largest token by `launchpad_trades` has **793,773 trades**, so one whole-token fold pass
is ~23 s of CPU. Refolding it on every chunk flush does not fit a 400 ms block cadence; one hot token would
consume a large fraction of a chunk's wall clock on its own. (This is pure Python fold time on in-memory
flows — real cost adds `load_flows` and the delta writes, so it is a floor.)

The checkpointing model the plan asks for already exists in the signature — `fold(prev, flows)` — and three
things block using it, none of which the plan mentions:

1. **No persistable checkpoint.** `observed_tokens`, `estimated_tokens`, the five `parked_*` fields and the
   three tx-hash sets (`trade_txs`/`buy_txs`/`sell_txs`) live only in `PositionState`. Resuming from a stored
   `PositionRow` loses the inventory split and double-counts `trade_count` for any transaction spanning a
   chunk boundary. This is the same schema change as D's quantity half — **do them together**.
2. **No total order at a chain position.** `load_flows` orders by `(block, tx_index, log_index, sub_index)`
   (`store.py:74`). E requires the sender's release before the receiver's inheritance, and both halves of a
   transfer share a log index. Today the tiebreaker is `sub_index`, derived from sorting by wallet address —
   so **in 7,280 of 12,819 same-position transfer pairs (56.8%) the receiver folds first**, which is exactly
   backwards for E. Under A's proposal (`sub_index` defaults to 0) there is no tiebreaker at all.
3. **Retroactive insertion.** Scoped replays and the V4 log backfill insert flows *before* existing ones,
   invalidating any checkpoint. `feedback2.md` finding 7 is therefore a prerequisite for E, not an unrelated
   item.

The affordable shape: fold each token once in movement order to a persisted checkpoint per `(wallet, token)`
at a block watermark; apply new flows incrementally with `fold(prev, …)`; full refold only when a flow lands
below the watermark. Common case O(new flows); correction case rare and explicit.

---

## Seam or layer, step by step

| step | verdict | what the real seam is |
|---|---|---|
| **A** identity | **half a seam** | Right direction, half the change. `feedback2.md` finding 6 asked for immutable provenance **and** "version derived interpretations and support atomic replacement". The plan takes the key and leaves `ON CONFLICT DO NOTHING`, so the first interpretation wins forever. The seam: *a flow row is a versioned interpretation of immutable evidence* — evidence key + interpretation version + replace-on-newer. The key must also carry the movement's **side**, or E has no order. |
| **B** grouping | **right seam, wrong definition** | Grouping is the seam. Connectivity is the wrong primitive; **matching** is right — a trade is a token movement matched to a quote movement through shared evidence, and unmatched movements stay unmatched. |
| **C** evidence | **right seam, wrong plane, and it widens an open hole** | Written as a *transaction admission* rule. The seam is a **movement record**: `(asset, quantity, side, holder-evidence, provenance)`, of which an ERC-20 `Transfer` is one producer among several. As a transaction gate it has no emitter authentication (`engine.py:240`), so it promotes today's forged-emitter defect from "can mis-price a real transfer" to "can create a movement from nothing". |
| **D** confidence | **layer** | Two labels on a row is the second flag the plan itself warns against. The seam: `basis_delta`/`realized_delta` are lossy projections — **store the vector the fold already computes**, and let confidence be derived rather than declared. |
| **E** ordering | **right seam, unaffordable, missing prerequisites** | Ordering across wallets per token is correct for finding 5. It needs a persisted fold checkpoint, a total order including movement side, and finding 7's coverage records — none of which are in the plan. |

**A, B and C are one seam.** All three are statements about what a movement is: A about its identity, B about
how movements group, C about what evidences one. Splitting them creates the ordering question, forces three
clean rebuilds instead of one, and produces a key in A that E cannot use. The same domain fact — "a movement" —
currently lives implicitly in `_Leg`, in `first_log`, in `_hints` and in the bundle's `moved` flag. Four places
is the tell the brief describes.

---

## Findings

### P1-1. C before B recovers nothing; the ordering is wrong

`REPAIR_PLAN.md` "Why before B"; `netflow.py:745`. All 156 cited wallet-transactions net to exactly zero, so
netting deletes them whether or not the evidence is admitted. Measured split: 78 need C, 78 need B, none is
recovered by either alone. **Cost if built in this order:** C ships, its acceptance criterion reports zero
recovered, and the natural conclusion is that C is broken.

### P1-2. A's key does not dedupe, and A's acceptance cannot fail

`store.py:69`, `schema.py:40`. All 357 duplicate groups also collide on the proposed key, so `DO NOTHING`
keeps whichever row landed first — including 37-in-200 with a disagreeing `kind`. "Re-replay twice, rows
identical" passes by construction. **Instead:** make the write a version-checked replace, and make A's
acceptance a deliberate construction — replay range R for token T, register a token that sorts earlier, replay
R again, assert flow count unchanged and no `(txhash, wallet, token)` group above 1. The fixtures already show
zero duplicates and cannot test this.

### P1-3. A drops the discriminator E needs

`netflow.py:753-754`, `store.py:74`, `fold.py:_flow_key`. Both halves of a transfer share a log index: 12,819
such pairs in the side database, 26,535 flows at multi-flow chain positions. With `sub_index` defaulting to 0
there is no order at a chain position, and today's accidental order puts the receiver first 56.8% of the time.
**Fix inside A:** put the movement's side in the key.

### P1-4. B's action definition fails the cases B exists for

Verified by union-find on the real leg sets: buy-and-sell through one pool is one component; gift plus
unrelated payment is one component. The plan's claim that B removes the coincidence inference "as a side
effect" does not hold under its own definition, and batched multi-user transactions and 4337 bundles break the
obvious repair. Replace connectivity with evidence matching.

### P1-5. C's origin fallback is wrong on C's own transaction

`0xd786059a…`: `tx.from` is an EOA in no transfer; the actor is `tx.to`, which is also the V4 Swap `sender`;
the profit lands on a third, 7702-delegated address. Contradicts CLAUDE.md's standing rule. Use the venue
event's actor field; where there is none, record the movement unattributed against the venue's custody.

### P1-6. C without an emitter gate is a regression, not a fix

`engine.py:240`. Reproduced: an `LT` event from `0x…dede`, for which `accepts_log_for_indexing("LT", …)`
returns False, produces `kind=buy, quote=−10 MON, basis_state=observed`. Today that only mis-prices a transfer
that really happened. Under C, "a venue event naming a registered token" becomes *sufficient evidence to admit
a transaction*, so a forged event alone would create a movement. **Ship the gate in the same change as C**, and
make it generation-aware — the plan already notes `accepts_log_for_indexing` admits only the live
`CRYSTAL_ADDR`, which blocks crystal-era replay anyway.

### P1-7. D as specified cannot express what the fold computes

`fold.py:122`, `fold.py:160`, `types.py:122`. See question 5. Store the vector, not two labels.

### P1-8. Step E is 28× the current fold cost, and no checkpoint is currently possible

Measured above. `refold` calls `fold_fn(None, flows)` unconditionally (`store.py:222`).

### P1-9. The plan drops four confirmed P1s while stating that their audit does not exist

`REPAIR_PLAN.md`: "The verification tooling is unaudited. `feedback4.md` was commissioned and never
delivered." **`feedback4.md` is in the repo root**, 43 KB, mtime 16:39 against the plan's 17:33, and it is
precisely that audit — "8 of those rows are hardcoded to PASS, one invariant is structurally incapable of
failing, and of 4,373 position rows carrying a cost basis exactly 2 are checked against anything." It also
confirms, with numbers, the findings the plan neither maps to a seam nor lists as out of scope:

- **Finding 7 (scoped replay coverage).** `feedback4.md`: 10,322 of the 10,501 leftover flows (98.3%) sit in a
  transaction that also carries a fixture-token flow; those tokens hold 1,276 position rows carrying 846,024
  MON of cost basis. This is a **prerequisite**, not a side issue: the plan's whole validation strategy is
  re-replay, and the only corpus where the defects are visible is the corpus finding 7 says is incoherent. I
  independently confirmed the fixtures hold 0 negatives and 0 duplicates, so they cannot show A or C working.
- **Finding 8 (quote conservation).** I reproduced the drop independently: 10 WMON + 20 USDC for 100 tokens at
  1 USD/MON records **20 MON, `observed`** (`netflow.py:222`). `feedback4.md` adds the router-fee half: a
  wallet paying 109.90 MON is recorded as paying 100.00 while the fee is inside `FEE_TOLERANCE = 0.10`
  (`netflow.py:340`). Nothing in A–E touches `_own_quote`, and B does not help — it nets per action but still
  collapses each action to one quote asset. The seam: **a quote is a bag of assets, not one asset.**
- **Finding 10 (LP/vault basis pooled).** `feedback4.md` reproduces it exactly: park 100 @ 100 into vault A and
  100 @ 1,000 into vault B, withdraw from A, get back 550 instead of 100. D changes confidence axes; it does
  not add the entitlement dimension.
- **Finding 12's valuation half.** `feedback4.md` lists five independent differences between the replay and
  live rate paths (60 s vs 300 s buckets, timestamp vs block selection, a 100× minimum-sample difference,
  first-seen vs own-bucket caching, and different fallbacks). This bites the plan directly: **value-level
  fixtures that must produce specific numbers are not reproducible while replay and live value the same flow
  differently.**

### P2-1. Excluding the `LEDGER_ENABLED` safety change is the wrong cut for 9/13

The plan excludes "anything touching the live indexer before 9/13". `feedback6.md` P1-3 is not a feature: I
confirmed `LEDGER.process_block(…, cur)` runs on the sequencer's own cursor (`core/sequencer.py:1036`) and
`init_ledger_schema` has no non-test caller, so setting the flag against a normally initialised prod database
raises inside the block transaction. Making the flag impossible to turn on — a preflight that refuses when
`to_regclass('wallet_flows')` is NULL, plus the lazy import — is the single change that costs nothing and
removes the only way this branch can break the launch. Otherwise the cut is right, and `feedback6.md`'s
"non-negotiable" first item is now moot: at `f9a6013` the branch is 0 behind main and carries `0x2388208C…`.

### P3-1. `ledger_verify.py`'s invariants cannot fail on the defects they name

`scripts/ledger_verify.py:93` groups on `(txhash, log_index, sub_index, wallet, token)` — the brief's known
example, and `feedback4.md` shows it is a strict tautology against the primary key. Under A's proposed key it
*becomes* the primary key and stays a tautology. Two more that matter here: the "observed buy/sell with no
quote" and "unresolved with a quote" checks compare `quote_delta = 0` / `<> 0`, and an unresolved disposal has
`quote_delta` **NULL**, so finding 3's 325 rows pass invisibly. Any invariant meant to gate this repair must be
NULL-safe, and at least one must be a cross-run diff rather than a within-run identity.

---

## The sequence I would run instead

Three seams, one clean rebuild, instead of five steps and three rebuilds.

**0. Coverage (finding 7), first, because everything else is measured through it.** Per-token contiguous
coverage ranges committed with the flows; a token whose coverage is incomplete is excluded from positions
rather than served partially. Without this the leftovers cannot be used as evidence and the fixtures cannot
show A working.

**1. The movement seam — A + B + C as one change.** Replace `_Leg` (one per wallet/token per transaction) with
a `Movement` record: `(txhash, evidence_log_index, side, wallet, token, quantity, evidence_kind, counterparty,
attribution_evidence)` where `evidence_kind ∈ {erc20_transfer, venue_swap, curve_trade, core_fill, custody}`.
Identity is that tuple plus an interpretation version — no ordinal, no anchor, and `side` gives E its ordering
for free. Actions are a *matching* over movements, not a connected component. Ship the emitter gate here,
widened to the generation list. `ON CONFLICT` becomes replace-if-newer-version. One schema change, one clean
rebuild — and B's and C's acceptance become separable, because a movement records which evidence produced it.

**2. The disposal-shape seam — D, finding 12's quantity half, and E's checkpoint together.** Persist the vector
the fold computes on each disposal, and persist the full `PositionState` inventory (three-state open, five
parked fields, and a resumable substitute for the three tx-hash sets). Fold-only, no rebuild. One change
unblocks D's correctness *and* E's checkpoint, which is why they should not be two steps.

**3. Ordered fold with a watermark (E).** Fold per token in movement order to a persisted checkpoint; apply new
flows with `fold(prev, …)`; full refold only when a flow lands below the watermark. Fold-only.

**4. Then the two P1s the plan drops:** quote conservation (finding 8) and per-entitlement parked basis
(finding 10).

**Is repair still the right call?** Yes, but the plan understates what step 1 is. Of `netflow.py`'s 754 lines,
roughly 300 — `_collect`'s netting, `_resolve_wallet`, `_resolve_across_wallets`, `_swap_pairs`, leg
construction — are replaced, and `_Leg` itself changes shape. What survives is real and worth keeping:
`_hints`, `_counterparty_and_venue`, `_classify_unpriced`, the valuation helpers, all of `kinds.py`,
`store.py`, `fold.py`'s proportional-release arithmetic, and the replay harness. So `feedback3.md`'s reasoning
holds — the evidence is collected and discarded, not absent — but calling it "four sequenced repairs to
`netflow.py`" budgets for the wrong thing. Doing A, B and C as one rewrite of the movement spine is *cheaper*
than three sequenced repairs, because each of the plan's rebuilds is a serialized gate (constraint: no two
replays at once) over a multi-million-hot-block corpus.

---

## Acceptance fixtures, concretely

Quantity checks cannot see any of this; the plan is right about that. These are the value-level cases with the
number each must produce. Cases 1–4 and 6–9 I built and ran, so the "today" column is measured, not inferred.

| # | shape | inputs | must produce | today |
|---|---|---|---|---|
| 1 | **claim-settled V4 arbitrage cycle** | tx `0xd786059a…`, moncock, block 45,905,959 | two moncock movements, `+320.019831901713613752` and `−320.019831901713613752`, attributed to `0x9d54c129fb…`; buy quote `1.000000 USDC`; sell quote `27,667.169952331123251085 WMON`; **no flow on `0x6807af96…`**; net balance 0 | no rows at all |
| 2 | **unequal round trip** | buy 100 @ 100 WMON (log 5), sell 60 @ 72 WMON (log 9), one tx, one pool | buy 100 @ 100 **and** sell 60 @ 72; buy unit cost 1.00 | one flow, `+40 @ −28 WMON`, unit cost 0.70 |
| 3 | **sell-then-buy, log order reversed** | sell 60 at log 2, buy 100 at log 7 | two flows with distinct keys; the row anchored at 2 is the **sell** | one flow, anchor 2, `kind=buy` |
| 4 | **gift plus unrelated payment** | A receives 100 from B (log 3); A pays 10 WMON to C (log 4) | A: `transfer_in`, `unresolved`, no quote | A: `buy`, `−10 WMON`, **`observed`** |
| 5 | **cross-wallet basis, both address orders** | S buys 100 @ 100 observed, transfers all to R. Run once with `S < R` and once with `S > R` | **identical** both ways: R holds observed basis 100, S holds 0; JAMES unresolved share falls from 44.6% toward ~2% | R unresolved, and the two orders fold differently in 56.8% of same-position pairs |
| 6 | **unpriced disposal** | buy 100 @ 100 observed, sell 100 with no observable proceeds | realized 0; an unresolved disposal of 100 tokens with 100 MON of basis held **out of** the running average | `realized_estimated = −100 MON` |
| 7 | **mixed-inventory sale** | 100 observed @100 + 100 estimated @300 + 100 unresolved; sell 150 @ 600 | the row reproduces released_observed 50, released_estimated 150, realized 150 / estimated 50 / unresolved_proceeds 200 | `basis_delta −200, realized_delta 200, basis_state observed` |
| 8 | **forged emitter** | wallet-to-wallet transfer plus an `LT` from an address `accepts_log_for_indexing` rejects | `transfer_in`, `unresolved` | `buy`, `−10 MON`, `observed` |
| 9 | **mixed quote** | 10 WMON + 20 USDC for 100 tokens at 1 USD/MON | cost 30 MON | 20 MON, `observed` |
| 10 | **router fee retained** | wallet pays 109.90 MON, venue receives 100.00 | cost 109.90 MON, or 100.00 with 9.90 recorded as fee | 100.00 MON, fee erased (`FEE_TOLERANCE`) |
| 11 | **identity under registry change** | replay range R for token T; register a token sorting earlier; replay R again | flow count unchanged; no `(txhash, wallet, token)` group above 1 | must be built deliberately — the three fixtures already show 0 |
| 12 | **replay vs live valuation** | fold one transaction through both rate paths with the same source | identical `mon_value` / `usd_value` | not guaranteed; five differences (`feedback4.md` 12b) |

Case 1 also forces a product decision the plan does not surface. Under average-cost accounting the ledger books
that contract **+27,666 MON realized** on moncock for a cycle whose real profit was 645.88 USDC. Decide at the
fixture whether that is the intended answer, before B and C are built rather than after.

---

## What I did not check

- **I did not run the test suite.** Three prior reviews report 706 passed / 4 skipped; nothing I claim depends
  on it.
- **I did not replay anything.** Every measurement is a `net_transaction`/`fold` call in process, a read-only
  query, or an RPC read.
- **I did not size the C class beyond moncock and JAMES.** The 16/62 split is exact for those two tokens; I did
  not scan the 64M-row log cache for claim-settled swaps across the registry, so I cannot bound C system-wide.
  `feedback5.md`'s "38 of 83 V4 pools" is the only bound I have and I did not re-verify it.
- **I did not verify the 2,228 moncock / 1,034 JAMES distorted cases** from `feedback3.md`. My round-trip
  reproductions are synthetic; only the 26 and 130 exact-zero cases were checked against prod.
- **The fold benchmark is CPU-only.** No `load_flows`, no delta writes, and the 500-block chunk size is my
  assumption, not read from the replay script's actual flush cadence. The 33 h figure is a floor.
- **I did not test A's key against reorgs.** `(txhash, …)` is not reorg-safe on its own; the plan removes
  `block_number` from the key and I did not evaluate `POSITION_LEDGER_PLAN.md` §10's reorg policy against it.
- **I did not evaluate findings 11, 13 or 14** beyond confirming the `LEDGER_ENABLED` mechanism and the two
  NULL-blind invariants. Classification stability across replays (`feedback5.md`'s open item) I did not
  attempt, so I cannot say whether A's stable key would freeze a *wrong* classification in practice or only in
  principle.
- **I did not check whether ERC-6909 claim `Transfer` events exist for other V4 settlement shapes.** The one
  transaction I decoded settles through `settle`/`take` with no 6909 log, so an ERC-6909 topic would not have
  helped there; whether other pools mint claim tokens and would, I do not know.
- **I read `feedback4.md`'s verdict and its sections on findings 7, 8, 10 and 12 only**, to establish that the
  plan's statement about it is wrong and what it concluded on the four dropped P1s. I did not audit the rest of
  it or re-derive its numbers.
