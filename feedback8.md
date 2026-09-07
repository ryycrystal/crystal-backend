# Reviewer 4: replay, operations, and whether the verification proves anything

**Verdict: do not turn `LEDGER_ENABLED` on, and do not read `COMPLETE.md`'s green table as acceptance of
anything except token quantities.** Against a normally-initialised backend database the flag is not shadow
mode but an indefinite indexing outage, reproduced below. And the verification machinery is much weaker than
28 PASS rows suggest: 8 of those rows are hardcoded `True`, one invariant is structurally incapable of
failing, one is vacuous on this data, the coverage and supply checks are scoped by a prod-seeded table that
has already silently dropped one of the three fixtures, and — the finding that matters most — **the side
database contains 10,222 trade flows worth 2,772,498 MON that price a rounding residue at hundreds of MON
per wei, 99.9 % of them stamped `observed`, and every single check in the suite passes on them.**

Reviewed at `f9a6013` (branch `accounting-fix`, `origin/main` merged). The ledger code is byte-identical to
the `f913de9` the briefs describe; the only source change since is the old engine's `_market_base_token`
attribution fix that came in from main. Prod was read through the tunnel in a read-only transaction; the side
database was opened read-only throughout; all writes went to my own scratch databases `crystal_rev4b_probe`,
`crystal_rev4b_supply`, `crystal_rev4b_retry`, `crystal_rev4b_hold2` and test scratch `crystal_rev4b_itest`.
No replay was run. Nothing was written to prod or to the side database.

> A previous reviewer-4 report occupied this path (43 KB, 2026-09-07 16:39). It survives in the repo as
> `feedback4-original.md`, byte-identical to the copy I took before starting
> (`<session scratchpad>/feedback4.prior.md`). This report was written independently of it and does not
> supersede it; where the two agree, that is two passes reaching the same place.

---

## What I verified independently

| check | result |
|---|---|
| full suite, `SCRATCH_DB_NAME=crystal_rev4b_itest`, `REWARDS_WORKER=0` | **719 passed, 4 skipped**, 100.5 s |
| `scripts/ledger_verify.py`, rerun by me against the unmodified side DB | 12/12 invariants, coverage 159 tokens / 389 negatives — but **"supply conservation (2 fully replayed token(s))"**, not 3 |
| `LEDGER_ENABLED=1` on a DB built by `core.storage.schema.init_db()` | **`UndefinedTable: relation "token_registry" does not exist`**; the shared cursor is poisoned; the chunk rolls back; 0 blocks recorded |
| rolled-back chunk, then retry on the same engine | `token_registry` empty on disk, token still in the engine's *and* `store._REGISTRY`'s in-memory registry, retry does **not** re-insert |
| `flush()` after a failed refold | the affected-key set is **already cleared**; those positions are never refolded |
| duplicate-flow invariant vs. the coarser duplicate | invariant sees **0**; `(txhash, wallet, token)` has **357 groups / 360 extra rows**, every one with a single distinct `log_index` *and* `token_delta` |
| effect of those duplicates | **163 positions across 46 tokens hold double their true balance**; one verified against chain at exactly **2.0000×** |
| supply conservation, driven against a stubbed chain | **passes** when a whole Transfer log is dropped, and **passes** when a real wallet is misclassified as a venue |
| the 14 addresses the prod differential drops as venues, probed on chain | 12 answer `token0()`, 1 is the token contract, 1 is the 24 KB curve; **0 are EOAs** — the exclusion was sound *this time* |
| moncock's 9,375 compared wallets | **6,426 (68.5 %) hold zero at head**, so two thirds of the "0 mismatched" result is `0 == 0` |
| the plan's §11 closed-position invariant (implemented nowhere), run by me | **877 of 9,417 violations**; on strictly-closed buy/sell-only rows the gap equals unreleased basis **447 times out of 447** |
| netted intra-transaction round trips, verified on chain | tx `0xf2c23ff2fc00…`: wallet moved 244,660.597126 moncock **in and out**; ledger booked an `observed` **buy of 1 wei for 16,185.329 MON** |
| consumers of `positions_v2` / `wallet_flows` outside `core/ledger/` | **none**; `LEDGER_ENABLED` appears in no config file — the shadow-isolation claim holds |
| prod order-book / custody footprint today | `crystal_market_trades` **1** row, `crystal_balance_events` **8**, `crystal_orderbook_events` **14**, `launchpad_tokens` source-0 rows **0** |

---

# Part one: adjudication of `feedback2.md` findings 7–14

Settling 11 first, because it gates the shadow run.

## 11. Startup and retry are not ready for the live flag — **CONFIRMED, both halves. [P1]**

### 11a. The tables do not exist, and the failure is an indexing outage, not a degraded mode

`init_ledger_schema` (`core/ledger/schema.py:141`) has exactly two non-test callers, both in
`scripts/ledger_replay.py` (`:49`, `:85`). `core/storage/schema.py` creates none of the eight tables in
`LEDGER_TABLES`. Reproduced on a fresh database:

```
[setup] core.storage.schema.init_db() completed
[setup] init_db created 59 tables
[result] ledger tables present after init_db: []
[result] ledger tables MISSING after init_db: ['wallet_flows', 'positions_v2', 'address_kinds',
         'venues', 'token_registry', 'tx_meta', 'tx_traces', 'ledger_meta']
[repro] process_block raised UndefinedTable: relation "token_registry" does not exist
[repro] the SAME cursor is now poisoned: InFailedSqlTransaction
[repro] after rollback, launchpad_blocks rows recorded for the chunk: 0
```

The severity comes from where the call sits. `core/sequencer.py:1037-1038` invokes
`LEDGER.process_block(..., cur)` on the chunk's own cursor, inside `_process_block_inner`, with no `try`.
The exception propagates to the `while True:` loop in `backfill.py:284`, which catches it, resumes from
`storage.get_last_processed_block()` — unchanged, because the transaction rolled back — and retries the same
chunk. **The indexer loops on the same block forever**, printing `[Backfill] Fatal Error` and making no
progress. Nothing else in the process notices; `/health` still returns `{"ok":true}`.

### 11b. In-memory state survives the rollback and defeats the retry

`store._REGISTRY` is a module-level global and `LedgerEngine._registry` an instance dict; both are written
before commit. Reproduced:

```
[in tx ] token_registry rows = 1;  TOKEN in engine registry: True
[rollback] chunk transaction rolled back
[after ] token_registry rows on disk = 0
[after ] TOKEN still in the engine's in-memory registry: True
[after ] TOKEN still in store._REGISTRY (module-level cache): True
[retry ] the SAME engine retries the chunk -> token_registry rows = 0
         (the guard `token in self._registry` skips the re-insert)
```

The guard is `core/ledger/engine.py:150`. After any rolled-back chunk the engine nets a token as registered
that has no `token_registry` row, until something calls `refresh_registry` — which happens only on a cold
start or on an `MC` (market-created) event.

Same shape in `flush` (`core/ledger/engine.py:471-473`): `self._affected.clear()` runs *before*
`store.refold`. Reproduced — after a refold that raises, the affected set is empty, so a later successful
flush never refolds those `(wallet, token)` keys and `positions_v2` stays silently stale against
`wallet_flows`. Only `ledger_verify.py` can see that afterwards, and nothing runs it in production.

### 11c. RPC inside the write transaction

`process_block` issues `eth_getTransactionByHash` batches (`_tx_meta_for().get_many`), `eth_getCode` batches
(`kinds.kinds_for`), `eth_call` pair probes (`kinds.probe_pairs`) and, within 500 k blocks of head,
`debug_traceTransaction` per unresolved transaction — all synchronously, all on the cursor holding the
chunk's locks, all behind a client limiter of `RPC_MAX_RPS` (default 20/s). CLAUDE.md's two lock rules say
never hold a transaction open across slow work; this does exactly that on every block. `COMPLETE.md` concedes
the point ("the reason Phase 1 stays shadow-only") and proposes to run it anyway.

I measured the hold time on a synthetic cold block — one block, N transactions, each a registered-token
transfer between two addresses the classifier has never seen, against the public RPC:

```
RPC_MAX_RPS = 20 (client-side limiter default)
   5 transactions in one block -> process_block held the open transaction    25.73s (5.15s per transaction)
  25 transactions in one block -> process_block held the open transaction    95.57s (3.82s per transaction)
  50 transactions in one block -> process_block held the open transaction   117.67s (2.35s per transaction)
```

Monad blocks are ~400 ms. This is the cold path and an upper bound — every address is unseen, every
transaction hash misses the `tx_meta` cache, and my sandbox's RPC latency is in the number — so I do not
present it as the steady-state cost. But it is the cost the flag would meet on its first blocks, and holding
a write transaction for two minutes is precisely the shape that took the API down twice (CLAUDE.md, "Writing
to prod Postgres: the two lock rules"). The busy-day throughput number the plan asks for (§13.5) has never
been produced.

## 7. Scoped replays write partial histories; `--resume` mistakes activity for coverage — **CONFIRMED. [P1]**

The brief asked whether the 159 leftover tokens are incidental netting, deliberate experiments, or both.
**They are entirely incidental.** Classifying all 162 tokens in the side database by whether their flows
appear in transactions that also carry a fixture token's flow:

| group | tokens | flows | positions | negative balances |
|---|---:|---:|---:|---:|
| every flow shares a transaction with a fixture token | 129 | 167,157 | 14,735 | 125 |
| some flows outside such a transaction (same hot *blocks*) | 33 | 7,945 | 849 | 264 |
| **no flow related to a fixture replay at all** | **0** | 0 | 0 | 0 |

There are no leftover experiments. Every one of the 15,584 position rows and all 389 negative balances is a
by-product of the three fixture replays. The 33 "partial" tokens show the scope is wider than the brief's
description: `relevant_logs` (`scripts/ledger_replay.py:390`) selects a *transaction* if any log's address is
watched and then returns **every log in that transaction**, and hot blocks are chosen per block, so tokens
that merely shared a block with a fixture token also acquired rows.

The `--resume` half is worse than "not the same as coverage". `scripts/ledger_replay.py:526-531` takes a
single `MAX(block_number) FROM wallet_flows WHERE token = ANY(%s)` over **all** requested tokens and uses it
as the floor for **all** of them. Measured against the 159 partial tokens — the distance between a token's
creation block and its highest incidental flow, which is exactly what `--resume` would adopt as its start:

```
partial tokens: 159
median 13,369,159 blocks   max 64,751,351 blocks   109 of 159 exceed 1,000,000 blocks
```

At ~400 ms blocks the median is about **62 days of history that a `--resume` would skip in silence**. For the
full-registry run this is the operative risk: any token that picked up an incidental flow during the fixture
replays can never be replayed with `--resume` again without `--wipe-token` first, and nothing warns.

`--chain-logs-file` compounds it: `scripts/ledger_replay.py:543` runs
`UPDATE launchpad_tokens SET created_block = LEAST(created_block, %s)` for every requested token — mutating
the exact column `ledger_verify.py`'s coverage report, its `FULLY_REPLAYED` scope and its negative-balance
invariant all key on. A replay flag moves the verdict of the verification.

## 8. Mixed quotes dropped, router fees erased — **CONFIRMED, both halves, with measured curves. [P1]**

**8a — a purchase funded in two quote families records one of them.** `_own_quote`
(`core/ledger/netflow.py:222-239`) sums a MON family and a USD family and returns whichever is larger.
Reproduced against the shipped code:

```
wallet really paid : 10 WMON + 20 USDC = 30 MON-equivalent (rate 1)
ledger recorded    : quote_asset=USDC quote_delta=-20000000 mon_value=20 basis_state=observed kind=buy
cost understated by: 10 MON (33.3% of the true cost), and it is labelled observed
```

The label is the aggravating part: a cost that is knowably a third too low is presented as confirmed.

**8b — `_prefer_venue_quote` (`core/ledger/netflow.py:340-351`) overwrites the wallet's own outflow.**
Sweeping the router's fee across the 10 % `FEE_TOLERANCE` band, everything else held constant:

| router fee | wallet paid | ledger cost | fee erased | flow `source` |
|---:|---:|---:|---:|---|
| 0 % | 100.00 | 100.00 | 0.00 | transfer_net |
| 1 % | 101.00 | 100.00 | **1.00** | venue_event |
| 5 % | 105.00 | 100.00 | **5.00** | venue_event |
| 9 % | 109.00 | 100.00 | **9.00** | venue_event |
| 11 % | 111.00 | 111.00 | 0.00 | transfer_net |
| 20 % | 120.00 | 120.00 | 0.00 | transfer_net |

Every row is `basis_state = observed`. Two problems beyond the erasure. The recorded cost is **discontinuous**
in the true cost — a 9 % fee books 100 MON and an 11 % fee books 111, so two percentage points of fee move
recorded cost by 11 %. And the rewrite is **not auditable after the fact**:
`source = 'venue_event'` is the same value used when the wallet's own outflow was never seen at all, so the
stored rows do not distinguish "the venue amount was all we had" from "the venue amount overwrote a known
wallet outflow". I could not measure this defect's footprint in the side database for that reason, and I say
so rather than guessing.

## 9. Trades entirely inside custody are never processed — **CONFIRMED as written; blast radius today is near zero and structurally unbounded. [P2 now, P1 before any cutover]**

`core/ledger/engine.py:262-263` sets a transaction's `moved` flag only on a registered-token `Transfer`, and
`:302` drops every bundle without it. The confirmation is sharper than the brief: **the ledger parses only
the `TR` tag** (`engine.py:35`, `netflow.py:51`). It never reads `OBF` (order-book fill, whose `topics[2]` is
the *maker*), nor `IBD` / `IBW` (internal balance deposit and withdraw) — all three of which the old engine
does handle (`core/sequencer.py:809`, `:818`). So the maker side of every CLOB fill and every
internal-balance movement is invisible to the ledger by construction, not merely dropped by the `moved`
filter. What the ledger calls `custody_deposit` / `custody_withdraw` is inferred from an ERC-20 transfer to a
`venue_custody` address (`netflow.py:429-432`), which is a different event.

Today that costs almost nothing: prod holds **1** `crystal_market_trades` row, **8** `crystal_balance_events`
and **14** `crystal_orderbook_events`, because the 2026-09-06 purge deleted 496,255 order-book events the day
before the fixtures ran. The side database contains exactly **two** custody flows — one deposit and one
withdrawal, same wallet, same token, same amount, netting to zero. **The custody path is therefore untested
by construction**, and the plan's §13.7 requirement ("spot positions and PnL exist for order-book trading")
is unmet. Reviewer 5's V4 claim-balance class is the same defect reached by another route.

## 10. LP and vault basis is pooled across destinations — **CONFIRMED exactly as written; no footprint in current data. [P2]**

`_apply_park` / `_apply_restore` (`core/ledger/fold.py:198-224`) never look at `flow.venue`. Reproduced with
the shipped fold:

```
deposited 100 tok @ 100 MON into pool A  and 100 tok @ 1000 MON into pool B
withdrew the pool-A position (true basis 100 MON)
restored cost_basis_native : 550.00 MON   (expected 100.00)
still parked               : 550.00 MON   (expected 1000.00)
```

feedback2's 550 figure is exact. Two additions. First, **`PositionRow` carries no parked field at all**
(`core/ledger/fold.py:70-71` copies only `PositionRow.__dataclass_fields__`), so a wallet whose inventory is
entirely in an LP shows `cost_basis_native = 0` on the served row with no indication that basis exists
elsewhere; the parked state survives only because `refold` re-derives it from the whole flow list every time.
Second, the blast radius today is nil: the side database holds 31 `lp_add` and 17 `lp_remove` flows across 20
and 14 `(wallet, token)` pairs, and **zero** of those pairs deposit into more than one venue.

## 12. Three-state inventory is not persisted; replay and live valuation differ — **CONFIRMED. [P1 before serving]**

Persistence: `PositionState` carries `observed_tokens`, `estimated_tokens`, `parked_observed_tokens`,
`parked_estimated_tokens`, `parked_unresolved_tokens`, `parked_observed_basis`, `parked_estimated_basis`
(`core/ledger/fold.py:59-65`). `PositionRow` (`core/ledger/types.py:121-143`) and the `positions_v2` DDL
(`core/ledger/schema.py:52-77`) carry **none** of them; only `unresolved_tokens` survives. A consumer holding
a row cannot split unrealized PnL by confidence, which is the ledger's entire premise.

Valuation: replay uses `SideRates` (`scripts/ledger_replay.py:255-279`) — 60-second buckets seeded with
`DISTINCT ON (timestamp/60) … ORDER BY timestamp DESC`, i.e. the **last** trade in each minute, selected by
`bucket_ts <= ts` with no block check, so a transaction at the start of a minute is priced with a rate set up
to 59 seconds later. Live uses `LedgerEngine._rates_at` (`core/ledger/engine.py:424-437`) — 300-second
buckets, computed from the **first** block seen in each bucket via `block_number <= blk`, falling back to the
`mon_price_usd` meta (today's rate) where history is absent. Different bucket size, different direction,
different source, different fallback.

This is not cosmetic, because `_quote_wei` (`core/ledger/fold.py:88-93`) takes a non-MON quote's basis from
`mon_value`, which is `usd / usdc_per_mon`. **The MON cost basis of any USD-funded trade depends on which
rate path produced it.** Measured exposure in the side database: 10,460 USD-quoted trade flows (10,021 USDC,
439 AUSD) carrying 36,532,303 MON — 6.07 % of the 601,741,108 MON of trade value. A shadow comparison of live
output against replay output is therefore not a clean diff for those rows.

## 13. Completion checks report uncertainty as PASS — **CONFIRMED and materially worse than P2. [P1]**

Eight of the 28 rows in `COMPLETE.md`'s three fixture tables are constructed with `ok=True` regardless of
their value: `scripts/ledger_check.py:234-240` (moncock realized split), `:334-336` (prod holder rows that
are venues), and `:393-395` (estimated and unresolved share, twice per fixture run). They render as `PASS`.

The plan's gate is gone, replaced by a different quantity. Plan §11 defines *estimated share = estimated
basis ÷ total basis* and *unresolved share = unresolved tokens ÷ tokens held or sold*; §13.4 gates on
**"estimated basis share < 1 % per token and overall"**. `ledger_check.py` contains `estimated_share(rows)`
at `:135` and `unresolved_share(rows)` at `:144` implementing exactly those definitions — and **neither
function is ever called.** `invariant_checks` uses `flow_shares` (`:161-176`) instead, which measures
trade-flow `mon_value`. The two disagree materially:

| token | plan's estimated share | reported | plan's unresolved share | reported |
|---|---:|---:|---:|---:|
| CHIPOTLE | 0.000 % | 0.000 % | 0.000 % | 0.000 % |
| moncock | **12.520 %** | 7.249 % | **0.906 %** | 6.823 % |
| JAMES | **2.625 %** | 7.372 % | **35.217 %** | 44.617 % |

Under the plan's own definition moncock is 12.5× the abandoned gate. Worse, the reported metric's denominator
is only value the ledger *could* price — unresolved flows carry `quote_delta` NULL and `mon_value` 0, so
moncock's 1,200 unresolved buy/sell flows contribute nothing to numerator *or* denominator, and `swap_leg` is
excluded entirely.

Finding 13's remark about `balance_token + custody_balance` vs `balanceOf` is also correct and untested. A
custody deposit reduces the wallet's ERC-20 balance while the ledger moves the amount from `balance_token`
into `custody_balance`, so the sum equals `balanceOf` only while custody is zero. **Every position in the
side database has `custody_balance = 0`**, so the JAMES 4,936-wallet comparison and the supply check's
`ledger_held` term have never exercised the branch where the formula is wrong.

## 14. `classify_code(None)` becomes EOA and is cached forever — **CONFIRMED as a latent defect; no measured footprint. [P3]**

`classify_code` (`core/ledger/kinds.py:199-205`) coerces `None` to `"0x"` and returns `KIND_EOA`; `kinds_for`
(`:315-324`) stores it with `SOURCE_GETCODE`, whose entry in `_KIND_GUARDS` (`:76`) is `None`, which
`_put_kinds` (`:572-576`) renders as `ON CONFLICT (address) DO NOTHING` — permanent. feedback2 is right that
this is the *null successful result* path, not the transport-error path: `JsonRpc.batch` (`:174-175`) does
raise for a non-`eth_call` that never answered.

Blast radius: I sampled 300 of the 5,000 addresses classified `eoa` by getcode that also hold positions and
read `eth_getCode` for each. **One** has code today — 23 bytes, a 7702 delegation designator, which
`classify_code` would classify `eoa_7702`, still a wallet kind. The defect has no measurable footprint in
this data and I would not block on it.

The sharper edge in the same file is the irreversibility of a negative pair probe.
`_KIND_GUARDS[SOURCE_VENUE_EVENT]` is `"WHERE address_kinds.source IN ('getcode', 'heuristic')"`, which does
not include `'pair_probe'`. So when `observe_tx` (`:523`) later promotes a contract that emits a pool event,
the `address_kinds` UPDATE is filtered out for any address already recorded by a probe. 712 addresses sit at
`source='pair_probe', kind='contract_unknown'` today, 204 of them holding ledger positions (the busiest moved
469,186,043 tokens across 16,122 flows). It self-heals in practice, because `_put_venues` has no such guard
and `load_known` re-seeds kinds from `venues` on restart — I measured **0** addresses in `venues` whose
`address_kinds` row is not a venue kind — so this is a P3 note, not a live defect. Probes also use `"latest"`
rather than the historical block, which is wrong for a replay of old history and is unmeasured here.

---

# Part two: does the verification machinery prove what `COMPLETE.md` says?

## The short answer

The machinery proves one thing well — **that the ledger's token quantities reconcile to chain for wallets it
knows about and did not classify away** — and it proves almost nothing about the numbers the ledger exists to
produce. Of 15,584 position rows, **4,373 carry a cost basis** and **9,496 carry a realized PnL**. The
fixture tables assert a basis or a PnL figure for exactly **two** of them (`CHIPOTLE_WALLET`,
`MONCOCK_WALLET`): 0.046 % of the basis-bearing rows. Nothing else in the suite, in `ledger_verify.py`, in
the chain comparison or in the prod differential looks at cost, proceeds, realized PnL, buy/sell counts or
`native_spent` at all.

## 1–3. The three fixture tables

**8 of 28 rows are hardcoded to PASS** (see finding 13). Of the remainder, CHIPOTLE's nine substantive rows
grade **one wallet with 29 flows**; moncock's four grade **one wallet**; JAMES's four are quantity-only. The
`check_le(balance_token, dust_limit)` rows are one-sided. The JAMES comparison's wallet set is
`sorted(w for w in ledger if kinds.get(w) not in VENUE_KINDS)` (`ledger_check.py:329`) — **the ledger's own
classification decides which of its rows are graded**, the same circularity the brief flags for the
differential.

## 4. Supply conservation — the docstring's central claim is false in two of three failure modes

`run_supply` (`scripts/ledger_verify.py:194-238`) computes
`residual = totalSupply − (Σ ledger balances + Σ chain balances of venues/token/zero/dead not already in
positions_v2)`. The brief asked whether the exclusion of balances the ledger already counts is right and
whether it can mask a missing holder. The exclusion is right — it prevents double counting — but the
docstring claims the check "is the only check that can catch a holder the replay never recorded at all", and
that is true only in the narrowest case. I drove the real `run_supply` against a stubbed chain:

```
A  honest ledger                         chain W1=600 W2=400; ledger W1=600 W2=400        PASS
B  a whole Transfer log was dropped      chain W1=600 W2=400; ledger W1=1000, no W2 row   PASS   <-- wrong
C  a real wallet misclassified as venue  chain W1=600 W2=400; ledger W1=600, W2 probed    PASS   <-- wrong
D  holder never recorded, still holding  chain W1=600 W2=400; ledger W1=600 only          FAIL 40%
```

**B** passes because a dropped transfer is conserved: the sender keeps what it never sent, so the sum is
unchanged. Reviewer 5's V4 claim-balance class and finding 9's custody class are both shape B. **C** passes
because the check adds the chain balance of precisely the addresses the ledger decided not to track — a
wallet wrongly promoted to a venue is *automatically forgiven*. Commit `305bdbd` records that an earlier
classifier did promote 259 trading bots to venues; supply conservation would have reported PASS.

Only shape D fails, and even D is invisible for any wallet that exited before the head block. Measured:
**68.5 % of moncock's positions and 48.5 % of JAMES's hold zero at their head block.** For all of those, the
entire history could be missing and the residual would not move.

Scope is the other problem. `FULLY_REPLAYED` and `COVERAGE` (`ledger_verify.py:123-133`) INNER JOIN
`launchpad_tokens`, a table `seed_from_prod` **truncates and re-copies from prod** on every seeded replay
(`ledger_replay.py:54-62`, `:248-250`). Prod now holds **zero** source-0 rows after the purge. Consequences I
measured today, on the unmodified side database:

- One token (`0x01bff41798a0…`) has flows and **no `launchpad_tokens` row**, so it is invisible to the
  coverage count, to the negative-balance invariant *and* to the supply check — and it holds 1 of the 389
  negative balances.
- **CHIPOTLE is one of the 159 "partial" tokens.** Its earliest flow is block 100,890,493 against a
  `created_block` of 100,890,244. Running the committed script today prints
  `supply conservation (2 fully replayed token(s))` and silently omits CHIPOTLE; passing `--token` for
  CHIPOTLE alone prints `supply conservation (1 fully replayed token(s))`. The header labels whatever list it
  is handed as "fully replayed", so `COMPLETE.md`'s recorded `3` is only possible if the run named all three
  explicitly. The generator (`<scratchpad>/append_verification.py:57-60`) then appends the hardcoded sentence
  *"None of the three fixture tokens is affected"*, which is **false**.

A full-registry replay that seeds reference tables will drop every crystal token from `launchpad_tokens` and
therefore from all three of these checks, with no message.

## 5. The twelve SQL invariants — one cannot fail, one is vacuous, most restate the fold

| # | invariant | what it actually is |
|---|---|---|
| 1 | balance = Σ flow deltas | an identity of `_apply` (`fold.py:237`); catches a lost write, nothing semantic |
| 2 | custody = Σ custody legs | identity of `fold.py:238-243`; **2 flows exist in the whole database** |
| 3 | no negative balance among tokens replayed from creation | scoped by an INNER JOIN on `launchpad_tokens` that **excludes every token showing the symptom** — all 389 negatives are outside the scope by definition |
| 4 | no negative custody balance | 0 rows have custody at all |
| 5 | no negative `token_sold` | **structurally impossible**: `token_sold` starts at 0 and is only ever incremented by `abs(delta)` (`fold.py:162`) |
| 6, 7 | flows ↔ positions | identities of `store.refold` |
| 8 | no duplicate flow keys | **tautological** — see below |
| 9 | no position on a venue address | restates the netting rule: `net_transaction` only creates a leg when `kinds.is_wallet(addr)` (`netflow.py:722`). It can fail only if a discovery lands after a flush; it verifies the purge, not the classification |
| 10 | no quote without an asset | `quote_asset` and `quote_delta` are always assigned together in `_flow`; tautological |
| 11 | observed basis never on a zero quote | `basis_state='observed'` is only ever set alongside a non-zero quote; 0 rows in the data have `quote_delta = 0` |
| 12 | unresolved flows carry no invented cost | **vacuous**: all 29,091 unresolved rows have `quote_delta` NULL, and `NULL <> 0` is NULL, so the check inspects zero rows |

**Invariant 8 is provably incapable of failing.** It groups by
`(txhash, log_index, sub_index, wallet, token)`, and the table's primary key is
`(block_number, tx_index, log_index, sub_index)`. A txhash determines `(block_number, tx_index)`, so the
grouping key is a superkey of the primary key and `HAVING count(*) > 1` can never be satisfied. Confirmed
empirically: the invariant's own query returns **0** while `(txhash, wallet, token)` returns **357 groups /
360 extra rows**, and every one of the 357 has a single distinct `log_index` *and* a single distinct
`token_delta` — the identical movement stored twice.

The invariants pass on that data because `positions_v2` is folded from the same duplicated flows. The
consequence, which no check in the repository can see:

> **163 `(wallet, token)` positions across 46 tokens hold exactly double their true balance**, 88,309,894
> tokens of duplicated mass. Verified against chain: wallet `0x5cf20573ac41…` on token `0x0cc9b2e2acd7…`
> reads **8,649,980.7979** in the ledger and **4,324,990.3990** on chain at that token's head block — a ratio
> of exactly **2.0000**.

None of the 357 groups is inside a fixture token, which is why `COMPLETE.md` is green.

## 6. The determinism replay — a warm-cache rerun of the smallest fixture

`<scratchpad>/test_determinism.py` re-invokes `run_chipotle.sh` with `--wipe-token --skip-seed`.
`wipe_tokens` (`scripts/ledger_replay.py:105-112`) deletes **only** `wallet_flows` and `positions_v2` for the
named tokens. `address_kinds`, `venues`, `tx_meta`, `tx_traces` and `token_registry` all survive. Run 2 reads
every classification, every venue discovery, every cached transaction and every cached trace that run 1
wrote. Every plausible source of non-determinism — `probe_pairs` against `"latest"`, `observe_tx` discovery
order, `TxMetaStore` fetches, `_reference_price` reading `wallet_flows` (its own prior output), `_rate_cache`
bucket seeding — is excluded by construction.

And the subject is CHIPOTLE: **29 flows, 2 positions, 100 % `observed`, 0 % estimated, 0 % unresolved**, so
zero reference-price lookups and zero trace requests. It does not generalise to moncock (114,623 flows, 7.2 %
estimated) or JAMES (49,949 flows, 44.6 % unresolved), and it says nothing about the chunked live path, where
a retry reuses a mutated engine (finding 11b).

## 7. The 9,375-wallet moncock chain comparison — real, and two thirds of it is `0 == 0`

This is the strongest result in the document and I do not dispute it: every ledger wallet's
`balance_token + custody_balance` equals `balanceOf` at the head block. Three qualifications, all measured:

- **6,426 of the 9,375 (68.5 %) hold zero at head**, so 68.5 % of the comparisons are `0 == 0`. The
  informative population is 2,949 wallets.
- The wallet set is `k.kind <> ALL(VENUE_KINDS)` over the ledger's own `address_kinds` — again, the ledger
  chooses who grades it.
- It compares balances only. `cost_basis_native`, `realized_pnl_native`, `native_spent`, `native_received`,
  `token_bought`, `token_sold`, `trade_count`, `buy_count` and `sell_count` are compared to nothing,
  anywhere.

## 8. The differential against prod — circular in mechanism, sound in fact on this run

`<scratchpad>/test_prod_differential.py` removes ledger positions the ledger calls venues (`:60-63`) **and
removes prod holders using the same `address_kinds` table** (`:74-79`). An address the ledger wrongly
classified as a venue therefore disappears from *both* sides and can never surface as "only prod has" — the
one signal that would reveal the error. The 259-bots-as-venues regression in commit `305bdbd` is exactly this
shape, and this harness could not have detected it.

I checked whether the exclusion was in fact sound here. All 14 excluded addresses, probed on chain: 12 answer
`token0()` with 22–35 KB of code, one is the moncock token contract itself, one is a 24 KB contract holding
20.8 M moncock at head (the curve). **Zero are EOAs.** So the verdict "chain agrees with the ledger: 213,
with prod: 0" stands as reported. Two further qualifications: the adjudication is capped at
`material[:400] ∪ only_ledger[:100] ∪ only_prod[:100]` and would silently sample a larger disagreement set;
and, again, it adjudicates **balances only** — it is not evidence that the ledger's PnL beats prod's.

---

## Three defects the machinery let through

### N1. Intra-transaction round trips are booked as `observed` purchases of dust at fabricated prices — [P1]

This is reviewer 3's finding 1 (transaction-wide netting) observed in the shipped fixture output, and it is
the most damaging thing in the side database.

Wallet `0x16c5633189dbc37fee5bd8a4eac474086feb2823` on moncock — a fixture token — has a position row reading
`token_bought = 230 wei`, `token_sold = 225 wei`, `balance_token = 5 wei`, alongside
`native_spent = 189,976.70 MON`, `native_received = 76,838.30 MON` and
`realized_pnl_native = −112,986.84 MON`, across 312 flows every one of which is a **1-wei** buy or sell.
Verified on chain:

```
tx 0xf2c23ff2fc004f87b0b5400b2583e25bba0aa190148b7601e17d101f9887e136  block 39,292,386
  on chain: the wallet RECEIVED 244,660.597126 moncock and SENT 244,660.597126 moncock in this transaction
  ledger  : observed BUY of 1 wei of moncock for 16,185.329 MON
```

`_collect` (`core/ledger/netflow.py:457-500`) reduces the transaction to one signed quantity per
`(wallet, token)`; the arbitrage legs cancel to a 1-wei residue while the quote legs do not, so the entire
quote is attached to the residue. The implied price on these rows runs from ~10²⁰ to ~10²² MON per token
against a real moncock price of about 10⁻² MON per token — roughly 22 to 24 orders of magnitude out — and the
row is stamped `observed`.

Blast radius in the side database:

| | flows moving < 1e-6 token but valued > 1 MON | share of that token's trade flows | value | share of value |
|---|---:|---:|---:|---:|
| moncock | 7,285 | 7.14 % | 2,023,219 MON | 0.42 % |
| JAMES | 2,800 | 10.33 % | 622,674 MON | 0.71 % |
| whole ledger | **10,222** | — | **2,772,498 MON** | — |

**10,208 of the 10,222 (99.9 %) are `basis_state = 'observed'`.** They contaminate **71** distinct positions,
**50 of them inside the two large fixture tokens**. 52 positions have `native_spent > 100 MON` against
`token_bought < 0.001 token`, aggregating **1,812,857 MON** of spend and **−139,105 MON** of realized PnL;
29 of those are on moncock. Every check passes on them, because `balance_token = 5 wei` is *correct*.

This is the cleanest statement of what the machinery is worth. That wallet's row claims 189,976.70 MON was
spent to acquire **230 wei** of moncock, and reports a realized PnL of −112,986.84 MON derived from that
inventory. The quantities are provably wrong by about eighteen orders of magnitude, so no PnL computed from
them means anything — and the row satisfies the chain comparison, supply conservation, all twelve invariants
and the prod differential simultaneously, because `balance_token = 5 wei` is correct.

### N2. Closed positions retain unreleased cost basis — [P2]

Plan §11 specifies a nightly invariant: *"**Closed positions**: `realized = Σ proceeds − Σ cost` for every
position with `balance ≤ dust`. This is exact and cheap to assert nightly."* It is implemented **nowhere**.
I ran it. On the three-state form (`realized + realized_estimated + unresolved_proceeds == received − spent`)
it fails on **877 of 9,417** closed positions; restricted to rows whose only flows are buys and sells, **447**
violations, and in **447 of 447** the gap equals the position's unreleased
`cost_basis_native + basis_estimated_native` exactly.

Across the whole ledger, **622 positions hold zero tokens and still carry 1,703,710 MON of cost basis; 577 of
them (1,463,630 MON) are inside the three fixture tokens.** On moncock, 398 of 6,272 strictly-closed
buy/sell-only positions (6.35 %) overstate PnL by 1,155 MON in aggregate; on JAMES, 27 of 1,240 (2.18 %) by
4,569 MON. Some of these rows are contaminated by N1 and by the duplicates above, so I do not claim a single
root cause — but the plan's own invariant would have surfaced them, and it was dropped.

### N3. `COMPLETE.md`'s own numbers are not reproducible from the committed script — [P2]

Running `scripts/ledger_verify.py` today against the same, unmodified side database prints
`supply conservation (2 fully replayed token(s))` where `COMPLETE.md` records 3, and the appended sentence
"None of the three fixture tokens is affected" is contradicted by the script's own coverage rule, which puts
CHIPOTLE among the 159. The generator that wrote that section derives its summary table by string matching on
log files (`"OK" in verify and "FAIL" not in verify`) and hardcodes the prose. A completion document should
not be assembled by regex over its own logs.

---

## What an unqualified `REVIEW` marker is entitled to claim

Today, honestly, this:

> For three tokens, replayed from a pinned block range into a side database, the ledger's **token quantities**
> reconcile to chain: every wallet it recorded and did not classify as a venue matches `balanceOf` at that
> token's head block, and total supply is fully accounted for on two of them. The existing 719-test suite is
> green and the flag is off, so deployed behaviour is unchanged.

It is **not** entitled to claim that positions, cost basis, realized PnL, trade counts or `native_spent` are
correct. Two rows out of 4,373 basis-bearing rows are graded against a number; 77 % of "observed" cost comes
from a venue event matched by amount rather than from money the wallet was seen to move (91,706 of 119,512
observed trade flows take their quote from `venue_event`; only 27,782 come from the wallet's own transfer or
native leg, and 24 from a trace); and 10,222 flows worth 2.77 M MON price a rounding residue.

It is **not** entitled to claim replay determinism beyond a 29-flow, fully-observed, warm-cache rerun.

It is **not** entitled to claim the ledger beats production generally; it beat production **on moncock
balances, on 213 adjudicated wallets, with the ledger choosing the comparison set**.

It is **not** entitled to claim readiness for shadow mode.

I would replace the bare `REVIEW` with
`REVIEW — quantities only; cost basis, PnL and trade counts unverified; not ready for LEDGER_ENABLED`, and
move the four "additional verification" rows under a heading that states what each one cannot see.

---

## Before `LEDGER_ENABLED` is turned on in the live indexer

In this order. Each is checkable.

1. **Create the ledger tables from `init_db()`.** Call `init_ledger_schema` from
   `core/storage/schema.py:init_db` (statement-by-statement, autocommit, under the existing `lock_timeout`
   retry — the DDL is all `CREATE … IF NOT EXISTS`, so it is idempotent).
   *Check:* on a database built only by `init_db()`, all eight `LEDGER_TABLES` exist and
   `LedgerEngine.process_block` completes.
2. **Add a preflight that fails before ingestion, not during it.** At indexer start, if `LEDGER.enabled`,
   verify the eight tables and exit non-zero with a named error if any is missing.
   *Check:* drop `wallet_flows` and confirm the process refuses to start rather than looping on a chunk.
3. **Isolate the ledger from the block transaction.** Wrap `LEDGER.process_block` and `LEDGER.flush` in
   `try/except`, log, disable the engine for the rest of the run, and let the old engine continue — or give
   the ledger its own connection. A shadow feature must not be able to stop indexing.
   *Check:* inject an exception in `process_block` and confirm `launchpad_blocks` still advances.
4. **Make the caches commit-aware.** `store._REGISTRY`, `LedgerEngine._registry`, `AddressKinds._kinds`,
   `._not_pairs`, `.tx_venues` and `LedgerEngine._affected` must roll back with the transaction (stage
   mutations, apply on commit) or be dropped on any exception. `flush` must clear `_affected` only after
   `refold` returns.
   *Check:* the 11b reproduction — after a rolled-back chunk, a retry on the same engine must write the
   `token_registry` row; after a failed refold, the affected keys must still be pending.
5. **Move the RPC off the write transaction.** Prefetch per chunk the way `ledger_replay.py` already does, so
   the only work inside the transaction is CPU and writes.
   *Check:* measure the write-transaction hold time over a chunk of real blocks and require it below the
   block interval; `SELECT count(*) FROM pg_locks WHERE NOT granted` stays 0 with the API under load.
6. **Meet the plan's throughput criterion (§13.5) with a number:** ≥ 2× block rate over a replay of a busy
   day, with the trace budget inside the limiter. Nothing in `COMPLETE.md` reports this.
7. **Prove the flag can be turned off cleanly.** Run with it on, kill the process mid-chunk, restart with it
   off, and confirm the old engine's tables are untouched and block processing resumes.

## Before any API reads these tables

Everything above, plus:

1. **Fix the netting model first (findings 1, 2, 6 and N1).** Until an intra-transaction round trip stops
   becoming a 1-wei `observed` purchase at a fabricated price, no cost, PnL or volume field in
   `positions_v2` is safe to serve.
   *Check:* re-fold moncock and require zero `buy`/`sell` flows with `abs(token_delta) < 1e12` and
   `mon_value > 1`; today there are 7,285.
2. **Give flows an identity independent of the registry and the classifier**, and make re-processing replace
   rather than duplicate.
   *Check:* `(txhash, wallet, token)` has zero duplicate groups; today 357, causing 163 positions to hold
   double their chain balance.
3. **Rebuild the side database from clean inputs.** The current one is not evidence: all 15,584 rows come
   from three scoped replays, 159 tokens are partial, 389 balances are negative.
   *Check:* every token in `wallet_flows` has `MIN(block_number) <= created_block` and zero negative
   balances, with the scope taken from `token_registry` (the ledger's own complete record) rather than
   `launchpad_tokens` (prod-seeded and provably lossy since the purge).
4. **Persist the three-state inventory and the parked entitlements** (`observed_tokens`, `estimated_tokens`,
   parked buckets per venue), so a served row can split PnL by confidence without a refold.
   *Check:* recompute observed-only unrealized PnL from a single row, without reading `wallet_flows`.
5. **Conserve the wallet's own money.** Retain every quote leg and its units; never overwrite an observed
   outflow with a venue amount; record fees separately.
   *Check:* the 8a and 8b reproductions — a 10 WMON + 20 USDC purchase records 30 MON, and a 9 % router fee
   stays in cost.
6. **Ingest `OBF`, `IBD` and `IBW`** and reconcile wallet and custody entitlements separately, or explicitly
   scope the ledger to exclude order-book activity.
   *Check:* a deposit-trade-withdraw round trip on a spot market produces the right position, and `balanceOf`
   reconciles against `balance_token` alone while custody is compared to the internal-balance ledger.
7. **Replace the acceptance gates.** Enforce the plan's §13.4 threshold — or a revised one *stated with its
   number* — against the plan's §11 definitions; call the two dead functions in `ledger_check.py`; remove the
   `ok=True` literals; split informational metrics from gates.
   *Check:* the script exits non-zero when a share exceeds its threshold. Today it cannot exit non-zero on
   any share at all.
8. **Add the four §11 invariants that were specified and not built:** per-transaction
   `Σ token_delta = mint − burn`; the closed-position identity (N2 — currently failing on 877 rows);
   unclassified-contract count; and surface all of them in `integrity_last`. Fix the duplicate-flow invariant
   to key on `(txhash, wallet, token)`; fix the negative-balance invariant to cover *all* tokens rather than
   the subset that joins a prod-seeded table.
9. **Compare the things the API will actually serve.** Extend the chain and prod differentials beyond
   balances to `cost_basis_native`, `realized_pnl_native`, `native_spent`, `native_received` and trade counts
   on a sampled cohort, with hand-derived truth for a handful. Today those columns are compared to nothing.
10. **Keep the old tables and the view shim for at least a week after cutover, with the flag as the kill
    switch** — as the plan already says.

### Acceptance criteria a repair should be held to

Offered because part two bears on `REPAIR_PLAN.md`, which I did not review:

- Fixing the netting must be demonstrated by **re-deriving the fixtures from scratch**, not by patching the
  fold. The specific regression to pin: tx `0xf2c23ff2fc00…` must produce a buy of 244,660.597126 and a sell
  of 244,660.597126, not a 1-wei buy.
- No repair should be accepted on the strength of the current side database. It must be rebuilt, and the
  rebuild is clean only when the checks under "before any API reads" §3 pass.
- Any check the repair adds must be shown to **fail** on the defect it is meant to catch before it passes on
  the fix. The duplicate-flow invariant is the cautionary example: it has passed on 360 wrong rows since the
  day it was written.
- Every metric that gates a decision needs its denominator written down and a threshold attached. A row that
  is always `PASS` should be printed as a measurement, not as a check.
- The verification's scope must not be derived from a table a replay can rewrite. Join `token_registry`, not
  `launchpad_tokens`, and print the tokens that were skipped rather than silently narrowing.

---

## What I did not check

- **I did not run a replay** of any kind, so I have not independently reproduced the fixture numbers from raw
  logs — only re-read the rows they produced.
- **I did not re-run the 9,375-wallet moncock comparison or the 4,936-wallet JAMES comparison** against
  chain. I accept both as reported and analysed what they cover. The only balances I read from chain myself
  were 6 duplicate-affected positions (2 answered, the rest archive-pruned), the 14 differential exclusions,
  2 round-trip transactions, and 300 getcode classifications.
- **I did not measure the live indexer's throughput on real blocks.** My hold-time measurement is synthetic
  (appendix) and the structural argument is in 11c; neither is the busy-day number the plan asks for.
- **I did not audit findings 1–6**; reviewers 3 and 5 hold those. N1 is an observed consequence of finding 1
  in the shipped data, not an independent adjudication of it.
- **I did not review `REPAIR_PLAN.md`**, per the brief.
- **I could not measure `_prefer_venue_quote`'s real footprint**, because the stored `source` value does not
  distinguish a rewrite from a venue-only quote. The 8b table is the mechanism and the tolerance band, not a
  count of affected rows.
- **I did not verify the 447 stranded-basis positions transaction by transaction**, so N2 is stated as "the
  plan's invariant fails and the gap equals the unreleased basis", not as a single mechanism.
- **I did not exercise the custody path**, because no data exists that would: two custody flows in the entire
  side database, netting to zero.
- **I did not test reorg handling, concurrent replays, or a `--wipe` / `--reset-discovered` cycle**, and I did
  not review the API, WebSocket or rewards consumers, which still read the old tables.

## Appendix: reproductions

All in the session scratchpad, all read-only against the side database and prod, writing only to my own
scratch databases:

| script | what it shows |
|---|---|
| `rev4b_flagon.py` | 11a — `init_db()` creates no ledger table; `process_block` raises `UndefinedTable`; the shared cursor is poisoned; the chunk records 0 blocks |
| `rev4b_retry.py` | 11b — the registry cache survives a rollback and defeats the retry; `flush` loses its affected keys on a failed refold |
| `rev4b_repros.py` | 8a and 10 — a two-family purchase records 20 of 30 MON as `observed`; pooled LP basis restores 550 instead of 100 |
| `rev4b_8b.py` | 8b — the router-fee erasure curve across the 10 % tolerance band |
| `rev4b_supply.py` | part two §4 — `run_supply` PASSes on a dropped transfer and on a wallet misclassified as a venue |
| `rev4b_hold.py` | 11c — synthetic write-transaction hold time with the flag on |
