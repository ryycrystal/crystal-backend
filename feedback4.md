# Reviewer 4: replay, operations, and whether the verification proves anything

**Verdict: do not turn `LEDGER_ENABLED` on. Against a normally-initialised backend database it is not
shadow mode, it is an indefinite indexing outage — reproduced below — and the verification machinery is
much weaker than its 28 green rows suggest: 8 of those rows are hardcoded to PASS, one invariant is
structurally incapable of failing, and of 4,373 position rows carrying a cost basis exactly 2 are checked
against anything.** The quantity results are real, I reproduced them, and they are the only thing the
machinery actually proves.

Reviewed 2026-09-07 at `f913de9` on branch `accounting-fix`. Prod was read through the tunnel read-only;
the side database was opened read-only; my scratch database was `crystal_rev4_probe` and my test scratch
was `crystal_rev4_itest`. No replay was run and nothing was written to prod or to the side database.

---

## What I verified independently

| check | result |
|---|---|
| full suite, own scratch DB, `REWARDS_WORKER=0` | **706 passed, 4 skipped**, 104.9 s — matches both prior reviews |
| `scripts/ledger_verify.py --skip-supply`, rerun by me | 12/12 invariants; coverage 159 tokens / 389 negatives — reproduces exactly |
| moncock chain comparison, my own 150-wallet sample (40 largest + 110 random) at block 102,355,176 | **150/150 exact, 0 unanswered** — the author's 9,375-wallet result holds |
| the 14 addresses the prod differential drops as venues, checked on chain | 12 answer `token0()`, 1 is the token contract, 1 is the 24 KB curve from `launchpad_pools`; **0 are EOAs** |
| `LEDGER_ENABLED=1` on a DB built by `storage.init_db()` | **`UndefinedTable: relation "token_registry" does not exist`**; block transaction poisoned; chunk rolled back; 0 blocks recorded |
| write-transaction hold time with the flag on | **299.9 s for one 20-block chunk (15.0 s/block), 86 % of it RPC round trips**, against a 400 ms block time |
| duplicate-flow invariant vs. the coarser duplicate | invariant sees **0**; `(txhash, wallet, token, log_index, token_delta)` has **357 duplicate groups / 360 extra rows** |
| incidental positions from scoped replays, sampled against chain | **29 answered, 0 matched, 29 mismatched** |
| prod order-book footprint (finding 9 blast radius) | `crystal_market_trades` holds **1** row across 4 spot markets |
| consumers of `positions_v2` / `wallet_flows` outside `core/ledger/` | **none** — the shadow-phase claim holds |

---

# Part one: adjudication of `feedback2.md` findings 7–14

## 11. Startup and retry are not ready for the live flag — **CONFIRMED. Settled first because it gates the shadow run, and it is the single thing blocking it.** [P1]

### 11a. The tables do not exist, and the failure takes the indexer down

`init_ledger_schema` (`core/ledger/schema.py:141`) has exactly two non-test callers, both in
`scripts/ledger_replay.py` (`:49`, `:85`). `core/storage/schema.py` creates none of the eight ledger
tables, and `indexer_main.py:221` calls only `storage.init_db()`. Reproduced end to end against a fresh
database:

```
[setup] storage.init_db() done  (exactly what indexer_main.py:221 runs)
[check] backend init_db created 60 tables; of the 8 ledger tables present: NONE

[state]  LEDGER.enabled = True
[result] SEQUENCER.process_chunk RAISED: UndefinedTable: relation "token_registry" does not exist
           core/ledger/engine.py:106   registry = dict(store.registry(cur, refresh=True))
           core/ledger/store.py:347    cur.execute(_SELECT_REGISTRY_SQL)
[txn]      cursor POISONED: InFailedSqlTransaction
[rollback] old-engine write in the same txn survived? None
[rollback] launchpad_blocks rows recorded: 0
```

`LEDGER.process_block` runs at the end of `_process_block_inner` (`core/sequencer.py:1036-1037`) on the
**sequencer's own cursor**, so the exception aborts the whole chunk including every old-engine write.
`backfill.py:389-401` catches it, rebuilds state, sleeps 5 s and resumes from
`storage.get_last_processed_block()` — which never advanced. **The result is an indefinite crash loop
with zero indexing progress and zero ledger rows, triggered by the first block carrying any log.** That
is a production outage, not a shadow run. It is also invisible in the deploy gate: the flag is env-only,
so nothing in CI or the test suite exercises the enabled path against a real backend schema.

### 11b. In-memory registry and classification are not rolled back with the transaction

Reproduced against a database that *does* have the ledger tables, using one engine object across a
rollback and a retry, as the sequencer does:

```
attempt 1 (inside the txn): 2 flows, token_registry=1, address_kinds=23
  ... the chunk fails for an unrelated reason and the sequencer retries it
after rollback:             token_registry=0, address_kinds=0
  engine's in-memory registry still holds 1 token; kinds cache still holds 23 addresses
attempt 2 (the retry):      2 flows, token_registry=0, address_kinds=0, wallet_flows=2
```

After the retry commits, the ledger holds flows for a token with **no registry row** and wallets with
**no classification row**, and neither is ever rewritten: `_register_from_events` skips a token already
in `self._registry` (`engine.py:150`), and `kinds_for` returns from `self._kinds` before touching the
database (`kinds.py:302-308`). On the next process restart the registry is rebuilt from the database, the
token is gone, and every later flow for it is silently never netted. This is exactly the CLAUDE.md
"watched contract address fails silently and fixing the code does not repair history" failure class,
reproduced inside the ledger.

`LedgerEngine.flush` (`engine.py:466-475`) has the same shape: it clears `self._affected` **before**
`store.refold` runs, so a refold failure permanently loses those keys and leaves positions stale relative
to their flows, with no error and no invariant that can see it.

### 11c. RPC inside the write transaction, measured

Both prior reviews flag this as a risk. Here is the number. Driving `SEQUENCER.process_chunk` with
`LEDGER_ENABLED=1` against a database that has the ledger tables, with `urlopen` instrumented on both
call paths (`urllib.request.urlopen` and the module-level alias `core/ledger/kinds.py:62`):

```
20 blocks / 60 transfers / 120 previously-unseen addresses, one chunk:
  postgres WRITE TRANSACTION held open : 299.9 s   (14,996 ms per block)
  http round trips to the rpc          : 130, 256.7 s = 86% of the held time
     eth_getCode      120
     eth_blockNumber   10
  monad block time ~400 ms; plan section 13.5 requires <= 200 ms/block.
  measured: 0.03x the block rate.

three earlier 10-block chunks (60 fresh addresses each): 141.6 s / 140.9 s / 149.1 s held,
  ~100% of it rpc; a lighter mostly-cached run: 25.3 s held, mean round trip 2.28 s
```

130 round trips for 120 previously-unseen addresses is close to one serial round trip per address:
`kind_of` resolves a single address at a time (`kinds.py:291-295` -> `kinds_for([addr])`) whenever
`observe_tx`'s batch has not already covered it, and every one of those waits happens with the block's
write transaction open.

Absolute latency is environment-dependent — Azure Japan East will beat this sandbox — but the shape is
not: the RPC work is serial, synchronous, unbatched at the point that matters and inside an open write
transaction, so held time is a direct multiple of round trips with no prefetch. The *best* of my five
measurements is **2.5 s per block against a 400 ms block time**; the best-instrumented is 15.0 s.
`POSITION_LEDGER_PLAN.md:509` (§13.5) requires **>= 2x block rate**; measured is 0.03x to 0.16x.
Separately, CLAUDE.md's lock rules exist because holding a transaction across slow work took the
production API down twice; this holds one every chunk, forever.

---

## 7. Scoped replays write partial histories for other tokens — **CONFIRMED, and worse than reported** [P1 before any full-registry run]

`scripts/ledger_replay.py:389` (`relevant_logs`) keeps every log of any transaction touching a watched
address, and `net_transaction` then nets **every registered token** in those transactions.

**The brief asks whether the 159 leftover tokens are incidental netting, deliberate experiments, or
both. They are incidental netting.** Measured on the side database:

- 10,501 flows belong to the 159 non-fixture tokens. **10,322 of them (98.3 %) sit in a transaction that
  also carries a fixture-token flow, and all 159 of the tokens appear in fixture transactions.**
- Those tokens hold **1,276 position rows** — 389 negative, 876 positive, 225 with a cost basis, 27 with
  a realized PnL — carrying **846,024 MON of cost basis, −21,581 MON realized and −854,589 MON estimated
  realized**.

The positive rows are the dangerous ones because they look plausible. I sampled the 60 largest and read
`balanceOf` at each token's own last folded block:

```
wallet                                     token                                          ledger          chain
0x2666303e75f9079a14b942b944d78cca661c854a 0x2cb31c268819ee133de378a3a2e087f0b0ec7777  23,111,347.84        0.00
0x5ab2d6502a12e63baabc555ea4e57c58e359a8c4 0x3842751a46d23b41a47e702473dff316e6237777  20,443,921.85        0.00
0xb0baace23ac7cbd25211134a0b47ad70a9d42fa4 0xc09c8242eb21b24298303799bb5af402a2957777  18,663,968.73       60.39
0x5cf20573ac41926347384461b9f9577a4ace9afa 0x0cc9b2e2acd7bacff79eb7db48f5662b622e7777   8,649,980.80  4,324,990.40
sampled 60 -> answered 29, MATCHED 0, MISMATCHED 29   (31 unanswered: block older than the archive window)
```

Every incidental position I could check is wrong, and nothing marks it: `positions_v2` has no coverage or
scope column at all.

**The `--resume` half is what gates a full-registry run.** `ledger_replay.py:526-531` takes a single
`MAX(block_number) FROM wallet_flows WHERE token = ANY(%s)` as the coverage mark. Because incidental
netting already gave those tokens flows at recent blocks:

| | |
|---|---|
| incidental tokens with a `launchpad_tokens` row | 158 |
| of those, whose first flow is after their creation block | **158 (all)** |
| mean blocks of real history a `--resume` would skip | **24,542,541** |
| worst case `0x0a917fcc…7777` (created 37,714,035, last incidental flow 102,465,386) | **64,751,351 blocks** |

A full-registry run with `--resume` today would produce positions for 158 tokens from a fraction of their
history and write them into `positions_v2` indistinguishably from correct ones.

Two aggravations neither prior review names:

- `--chain-logs-file` runs `UPDATE launchpad_tokens SET created_block = LEAST(created_block, %s)`
  (`ledger_replay.py:539-543`). That is the **same column** `ledger_verify.py`'s coverage check and its
  "no negative balance among tokens replayed from creation" invariant use as ground truth. The replay
  script can lower the bar the verifier grades it against.
- `store.purge_wallets` (`core/ledger/store.py:236-242`) deletes a newly-discovered venue's flows and
  positions **across every token, with no token scope**. 320 venues were discovered at replay time. In a
  full-registry run, discovering a venue while replaying token A silently deletes that address's
  already-correct history for tokens B, C… and makes the output order-dependent.

---

## 8. Mixed quote payments dropped, router fees erased — **CONFIRMED** [P1]

Runnable reproductions against the shipped code:

```
_own_quote drops one quote family entirely  (core/ledger/netflow.py:222)
  10 WMON + 20 USDC (1 USD/MON)  -> recorded    20.00 MON   truth    30.00 MON   LOST 10.00 (33.3%)
  30 WMON +  5 USDC              -> recorded    30.00 MON   truth    35.00 MON   LOST  5.00 (14.3%)
  1 native MON + 1000 USDC       -> recorded 1,000.00 MON   truth 1,001.00 MON   LOST  1.00 ( 0.1%)

_prefer_venue_quote overwrites the wallet's real outflow  (netflow.py:340, FEE_TOLERANCE = 0.10)
  router fee  1.0%: wallet paid 101.00 MON -> ledger records 100.00 MON  (fee erased)
  router fee  5.0%: wallet paid 105.00 MON -> ledger records 100.00 MON  (fee erased)
  router fee  9.9%: wallet paid 109.90 MON -> ledger records 100.00 MON  (fee erased)
  router fee 10.5%: wallet paid 110.50 MON -> ledger records 110.50 MON  (kept)
```

`_own_quote` returns one `(asset, amount)` pair; when both families are present it returns whichever is
larger in MON terms and discards the other. There is no path that sums them.

Blast radius, and the reason this is unauditable rather than merely wrong: **91,706 of 175,102 flows
(52.4 %) carry `source = 'venue_event'` with `kind IN (buy, sell)` and `basis_state = 'observed'`** — the
recorded quote is the venue's number. Whether the wallet's own outflow differed by up to 10 % is not
recoverable from the stored rows, because the observed delta is overwritten in place and never persisted.
COMPLETE.md's "Deviations" calls this deliberate. It is — but it destroys the evidence needed to reverse
it, and the plan says protocol fees belong in the observed quote leg.

---

## 12. Three-state inventory not persisted; replay and live valuation differ — **CONFIRMED** [P1 before serving]

**12a.** Confirmed by inspection of `PositionRow` (`core/ledger/types.py:121-143`): the row persists
`unresolved_tokens` only. `observed_tokens`, `estimated_tokens` and all five `parked_*` fields exist only
in the transient `PositionState` (`fold.py:59-65`). A consumer reading a row cannot split unrealized PnL
by confidence — which is the entire product premise of the three-state design.

**12b.** The two rate paths differ in five independent ways:

| | replay (`SideRates`, `ledger_replay.py:253-279`) | live (`_rates_at` / `_mon_usd_at`, `engine.py:424-453`) |
|---|---|---|
| bucket | `ts // 60` | `ts // 300` |
| selection | last trade **in** the 60 s bucket, by timestamp | last trade **at or before `blk`**, by block |
| minimum sample | `native_amount >= 1e16` | `native_amount >= 1e18` (100×) |
| cache policy | the bucket's own value | the **first** rate seen in the 300 s bucket |
| fallback | 0 | current `launchpad_meta.mon_price_usd` |

The replay selects purely by timestamp bucket and never checks the block, so a transaction early in a
minute is priced from a trade later in that same minute. Both use the **current** `lvmon_mon_rate` meta
for all of history.

Blast radius, which neither prior review quantifies: MON-quoted legs are unaffected (`mon_value` is just
`quote_delta / 1e18`), but for a USD-quoted leg `fold._quote_wei` (`fold.py:88-93`) falls through to
`mon_value`, so the **cost basis itself** comes from the rate. That is **10,460 flows (6.0 %) carrying
36,532,302.94 MON of value**, plus 1,287 LVMON flows priced at today's meta rate across all of history.
Replaying history and then letting the live engine continue would value the two halves of one table by
two different rules.

---

## 10. LP and vault basis pooled across destinations — **CONFIRMED exactly; tiny footprint today** [P2 now, P1 before any API reads the table]

`core/ledger/fold.py:198` (`_apply_park`) and `:208` (`_apply_restore`) keep one `parked_*` aggregate per
`(wallet, token)` and never read `flow.venue`. Reproduced with feedback2's own scenario:

```
park 100 tokens @   100 MON basis into vault A
park 100 tokens @ 1,000 MON basis into vault B
withdraw 100 tokens from vault A
  -> restored basis       550.00 MON   (correct:   100.00)
  -> basis still parked   550.00 MON   (correct: 1,000.00)
```

Footprint in the side data is 31 `lp_add` and 17 `lp_remove` flows, so nothing is visibly wrong today.
The compounding problem is that none of the parked state is persisted (see 12a): a wallet with LP'd
tokens gets a `positions_v2` row whose basis silently excludes the parked basis and whose balance
silently excludes the parked tokens, with nothing saying so. Vaults launch inside this window.

---

## 9. Trades entirely inside custody are never processed — **CONFIRMED by construction, negligible footprint today** [P2 for the shadow run; P1 for plan §13.7]

`core/ledger/engine.py:262-263` sets `moved` only when a **registered-token `Transfer`** appears; `:302`
drops every bundle not in `moved`. Reproduced with a real `TR` core-fill log shape:

```
custody-only fill: bundles built = 1, venue_events = 1
  transactions marked 'moved'  (engine.py:262)        = 0
  bundles surviving the filter (engine.py:302)        = 0     -> no flows, no position change
same fill + one registered-token Transfer:  moved = 1, surviving bundles = 1
```

Blast radius today is essentially nil: prod's `crystal_market_trades` holds **1 row** across 4 spot
markets, and there is no internal-balance table. The 09-06 purge removed crystal-generation rows, so this
understates history. I am downgrading this to P2 for the shadow run on measured footprint, while agreeing
it is a P1 for the plan's §13.7 spot-position goal.

---

## 13. Completion checks report uncertainty as PASS — **CONFIRMED** [P1 for anyone reading the tables as acceptance]

`scripts/ledger_check.py:394-395` constructs both share rows with `ok` hardcoded `True`. JAMES's
**44.617 % unresolved** and moncock's **7.249 % estimated** therefore print as `PASS`.
`POSITION_LEDGER_PLAN.md:505` sets the bar at "**estimated basis share < 1 % per token and overall**" and
adds that "the threshold is revised **with the number**, not waived". COMPLETE.md changes the *metric*
(flow value-weighted rather than remaining basis) but publishes no revised threshold and enforces
nothing. Even under the new metric the fixtures sit at 7.2× and 7.4× the plan's bar and read green.

Two more of the same shape that feedback2 does not name:

- `check_le` (`ledger_check.py:110`) tests `actual <= limit`, so the CHIPOTLE and moncock `balance_token`
  checks pass for **any negative balance**, including −3.17e27. The side database contains 389 negative
  balances, so this is not hypothetical.
- `CHIPOTLE | unresolved_tokens | 0` is trivially satisfied: CHIPOTLE's 29 flows are 100 % `observed`, so
  the strongest fixture never exercises the estimated or unresolved paths at all.

---

## 14. `classify_code(None)` becomes EOA and is cached forever — **CONFIRMED, and permanent in a way feedback2 understates** [P2]

```
classify_code(None) = eoa      classify_code('0x') = eoa      classify_code(0) = eoa
getCode returned null -> in-memory kind 'eoa'; persisted row ('eoa','getcode',{'code_len': 0})
after a process restart, re-read from address_kinds -> 'eoa'; extra rpc batches: 0
```

feedback2 is right that transport failures are already retried and raised by `JsonRpc.batch`
(`kinds.py:174-176`); this concerns only a *successful* reply carrying `"result": null`, where
`"result" in reply` is true and `results[i]` becomes `None`.

The escalation: the misclassification is not merely sticky, it is **unreachable by every correction
path**. `observe_tx` builds its discovery candidate list as
`unknown = [a for a in touched if kinds.get(a) == KIND_CONTRACT_UNKNOWN]` (`kinds.py:514`). An address
stuck at `eoa` is never a candidate, so even if it later emits swap or sync events it stays a wallet and
keeps accruing positions forever. The `_KIND_GUARDS` promotion rules (`kinds.py:74-82`) can only fire for
rows that reach `_put_kinds` at all.

I could not measure how often the public RPC actually returns a null `eth_getCode` result, so the
frequency is unknown; the consequence when it happens is proven.

---

# Part two: does the verification prove what COMPLETE.md says it proves?

Short answer: the **quantity** claims hold and are well made. Every claim about cost basis, realized PnL,
trade counts, classification correctness or replay reproducibility is either unproven or proven far more
narrowly than the document reads.

## Summary table

| result in COMPLETE.md | does the check prove it? |
|---|---|
| three fixture tables, "12/12, 8/8, 8/8" | **No, as stated.** 28 displayed rows contain **8 hardcoded PASSes** and the same global venue check three times. **18 unique falsifiable checks**, of which only 2 wallets have any money number checked. |
| supply conservation, "0 unaccounted on all three" | **Proves ERC-20 transfer coverage only.** It is an arithmetic identity over `Σ token_delta`; invariant to every accounting defect in this report. |
| 12 SQL invariants | **Partly.** 1 is a tautology, 2 restate the fold's own arithmetic, 3 restate `netflow`'s own labelling rules, 1 is circular against the write gate, and 1 is scoped by a column the replay script can lower. 4 are genuine and cheap. |
| determinism replay | **No.** It replays one token's 29 flows with the classification and trace caches already populated, so it cannot see the three real nondeterminism sources. |
| 9,375-wallet moncock chain comparison | **Yes, for quantities.** I reproduced 150/150. Strongest result in the document. |
| differential vs prod, "chain sided with the ledger 213 to 0" | **Yes, for quantities.** Structurally circular in one direction; I checked all 14 exclusions on chain and none is a false exclusion. |

## 1. The three fixture tables

Counting the `Check(..., ok=True)` call sites in `scripts/ledger_check.py` with an AST pass:

```
ledger_check.py:234  'realized split confirmed / estimated'
ledger_check.py:335  'prod holder rows that are venues'
ledger_check.py:348  'chain balanceOf'                     (only in the --skip-chain path)
ledger_check.py:394  '<token> estimated share'
ledger_check.py:395  '<token> unresolved share'
```

Of the 28 rows printed across the three tables, **8 are labels that cannot fail** and the
"positions on venue addresses" row is one global check printed three times. That leaves **18 unique
falsifiable checks**: 9 CHIPOTLE, 4 moncock, 4 JAMES, 1 global.

CHIPOTLE is the strongest of the three and deserves its billing — 20 trades, exact to ±0.01 MON on
`native_spent`, `native_received` and `realized_pnl_native`, and reviewer 1 confirmed the expected values
against chain independently. But it is **one wallet, 29 flows, 2 position rows, 100 % observed**, so it
never exercises the estimated, unresolved, trace, LP, custody or transfer-inheritance paths.

moncock's realized check passes at **0.468 % against a 0.500 % tolerance** (906.90 MON residual on
193,957). feedback.md already called this thin; I confirm the arithmetic. There is no stated expected
residual, so a future regression is indistinguishable from drift.

**The gap the brief asked me to establish:** the side database holds **4,373 position rows with a cost
basis and 9,496 with a realized PnL**. Exactly **two wallets** — CHIPOTLE's and moncock's — have those
numbers compared to anything. The JAMES (4,936 wallets) and moncock (9,375 wallets) chain comparisons
read `balanceOf` and compare it to `balance_token + custody_balance`. Quantities can reconcile perfectly
while cost basis, realized PnL, `native_spent`, `native_received`, `token_bought`, `token_sold`,
`trade_count`, `buy_count` and `sell_count` are all wrong. The unit tests exercise the fold's arithmetic
on synthetic inputs, but **no check in this branch compares any of those columns against external ground
truth beyond those two wallets.**

## 2. Supply conservation — proves transfer coverage, nothing else

`run_supply` (`ledger_verify.py:194-238`) computes
`residual = totalSupply − (Σ positions_v2 balance+custody) − (Σ chain balanceOf of venue/token/zero/dead
probes not already in positions_v2)`.

The exclusion the brief asks about is right in the narrow sense — an address counted in `ledger_held`
must not also be counted from chain, or it would double — and the check does catch its stated target: a
holder the replay never recorded is absent from `ledger_held`, is not in `probes`, and produces a nonzero
residual. That claim holds.

What it cannot catch follows from the identity. Invariant 1 guarantees `balance_token = Σ token_delta`
per wallet, and I confirmed the aggregate directly:

```
CHIPOTLE  Σ positions_v2 (balance+custody) = 0.007385
CHIPOTLE  Σ wallet_flows token_delta       = 0.007385
```

So supply conservation reduces to *"the per-wallet net of the ERC-20 Transfers we saw, plus the chain
balances of a handful of excluded addresses, equals totalSupply"*. Because transaction-wide netting is
sum-preserving, this is **invariant under every accounting defect in this report**: netting away a
round trip (finding 1), dropping a quote family (8), booking zero proceeds (3), losing basis on transfer
(5), pooling LP basis (10), and — importantly — **any classification error**, because a wallet-vs-venue
mistake just moves the same quantity between `ledger_held` and `held`.

It is a good coverage test. It is not evidence about accounting, and COMPLETE.md's "catches a holder the
replay never recorded" is the correct and complete description of what it does.

## 3. The twelve SQL invariants

| # | invariant | what it can actually detect |
|---|---|---|
| 8 | "no duplicate flow keys" | **Nothing. Tautology.** It groups by `txhash, log_index, sub_index, wallet, token`; `(block, tx_index, log_index, sub_index)` is the table's primary key and `txhash → (block, tx_index)` is 1:1, so the count can never exceed 1. Measured: PK duplicates **0**; `(txhash, wallet, token, log_index, token_delta)` duplicates **357 groups / 360 extra rows** — the same log index, the same amount, the same wallet, stored twice. |
| 1, 2 | balance / custody equal the sum of deltas | Only a stale write-back from `store.refold`. `positions_v2` is written *by* the fold that sums those deltas, so this compares SQL's arithmetic to Python's over identical rows. It cannot see a missing flow, a wrong `kind`, a wrong quote or a wrong basis. |
| 9 | "no position sits on an address classified as a venue" | Circular against the write gate: the engine refuses to write venue positions (`WALLET_KINDS`) and `purge_wallets` deletes any that slip through, so this asks whether the code that prevents X did X. It says nothing about a venue the classifier does not know about — which is the only interesting case. |
| 10, 11, 12 | quote/asset and basis-state consistency | Restate `netflow`'s own labelling rules. Note `quote_delta <> 0` is not NULL-safe, so `NULL` quote rows are excluded from 10 and 12 by SQL semantics. |
| 6, 7 | flows ↔ positions row correspondence | Genuine but weak write-back checks. |
| 3 | "no negative balance among tokens replayed from creation" | Genuine, but scoped by `JOIN launchpad_tokens` on `created_block` — **a column `ledger_replay.py:539-543` lowers**. |
| 4, 5 | non-negative custody / sold | Genuine, cheap, currently green. |

**The scoping blind spot is the one that gets worse, not better.** Invariant 3, the coverage report and
the `FULLY_REPLAYED` list that drives supply conservation all `JOIN launchpad_tokens`. Today that hides
almost nothing (1 token, 1 negative row of 389). But the side database's `token_registry` holds **32,686
tokens while `launchpad_tokens` holds 32,119, and 567 registry tokens have no `launchpad_tokens` row at
all**. Prod's `launchpad_tokens` now has **zero** `source = 0` rows after the 09-06 purge. So on the
crystal generation — precisely the history COMPLETE.md says only this ledger can restore — every one of
those tokens would be **structurally invisible** to the negative-balance invariant, the coverage report
and supply conservation, and `ledger_verify.py` would print `OK`. The verifier should join
`token_registry`, the ledger's own registry, not prod's.

## 4. The determinism replay

`test_determinism.py` replays CHIPOTLE with `--blocks-file` (a cached block list), `--wipe-token` and
`--skip-seed`, and compares SHA-256 of 29 flows and 2 positions. `wipe_tokens`
(`ledger_replay.py:105-112`) deletes **only** `wallet_flows` and `positions_v2` for the token. It leaves
`address_kinds`, `venues`, `token_registry`, `tx_meta` and `tx_traces` (1,096 rows, all
`available = true`) fully populated from run 1.

So run 2 resolves every address and every trace from run 1's cache. The test therefore cannot see the
three things that actually make a replay nondeterministic:

1. **Classification order-dependence.** 320 venues were discovered at replay time, each triggering an
   unscoped `purge_wallets`. Whether a contract is discovered before or after a token is folded changes
   the output — and `--skip-seed` guarantees no rediscovery happens.
2. **`sub_index` instability.** feedback2's finding 6 needs the registry to change between runs;
   `--skip-seed` guarantees it does not.
3. **The trace window is wall-clock dependent, and it expires this week.**
   `_within_trace_window` (`engine.py:401-403`) compares the block to the **live chain head**
   (`TRACE_WINDOW_BLOCKS = 500_000`). 24 flows in the side database carry `source = 'trace'`, holding
   **133,415.71 MON** of quote value, all `basis_state = 'observed'` **only because a trace answered**.
   At head 102,703,077 (measured today) the oldest of them (block 102,084,486) is **already 118,591
   blocks outside the window**, and the newest (102,497,544) leaves it in **294,467 blocks ≈ 33 hours**,
   i.e. around **2026-09-09** — four days before launch. A fresh replay from an empty database after
   that date resolves those flows as estimated or unresolved instead of observed, changing basis states,
   MON values, realized PnL and the estimated/unresolved shares the fixtures report. The determinism
   test cannot see it because `tx_traces` is cached before it starts.

The determinism result is true and worth having; the honest statement of it is *"given a fixed block
list, a pre-populated classification and trace cache, and an unchanged registry, one token's 29 fully
observed flows reproduce byte for byte."* That does not generalise.

## 5. The 9,375-wallet chain comparison — holds

`test_moncock_chain.py` is well built: exact wei equality, pinned block, unanswered reads counted as
failure. I reproduced it on a 150-wallet sample (40 largest + 110 random) at block 102,355,176:
**150/150 exact, 0 unanswered.**

Its one structural limit is that it asks only about wallets the ledger already has
(`SELECT p.wallet FROM positions_v2`). A holder the ledger never recorded is invisible — which is exactly
what supply conservation covers, and moncock passed that at 0 unaccounted. Taken together, the two make a
genuinely tight *quantity* proof for moncock. They make no claim about basis or PnL.

## 6. The differential against prod — holds, and the circularity is real but did not bite

The brief's concern is correct in form. `test_prod_differential.py:74-79` computes prod's excluded
holders by querying the **side database's** `address_kinds` — the ledger's own classification — and
removes them from prod's side; lines 57-64 already remove them from the ledger's side. So a wallet the
ledger *wrongly* calls a venue is dropped from both sides and can never appear in `only_prod`. That is
the ledger deciding which comparisons it is graded on, in exactly one direction (the opposite direction —
a pool wrongly kept as a wallet — is compared and adjudicated normally).

I tested it. All 14 excluded addresses, on chain:

```
0x188d586ddcf52439676ca21a244753fa19f9ea8e  venue_pool  known_list  24,009 bytes  no token0()   (the curve)
0x023eb9279bf54969a36f3ce00b71d4463259b2b4  venue_pool  pair_probe  22,586 bytes  token0() ok   (pair)
... 10 more pairs, all answering token0() ...
0x405b6330e213ded490240cbcdd64790806827777  token       known_list      45 bytes                (moncock itself)
addresses excluded as venues that have NO CODE on chain (real EOAs): 0
```

No false exclusion. The 213–0 headline stands for the wallets it compared, and it is the best evidence in
the document. It is a **balance** differential: prod's `launchpad_positions` also carries
`cost_basis_native`, `realized_pnl_native`, `token_bought`, `token_sold`, `native_spent` and
`native_received`, and none of them is compared.

One process note: three of the five "additional verification" results are produced by harnesses that are
**not committed** (`test_moncock_chain.py`, `test_determinism.py`, `test_prod_differential.py` live in
the author's session scratchpad). They are readable and I audited them, but they cannot be re-run by
anyone else, cannot be reviewed in a diff, and will not survive the session. Two of them are the
strongest evidence on the branch; they belong in `scripts/`.

---

## What an unqualified `REVIEW` marker in `COMPLETE.md` is entitled to claim today

**Entitled to claim:**

> Against the three replayed fixture tokens, the net-flow ledger reproduces per-wallet token quantities
> exactly: 9,375 moncock wallets and 4,936 JAMES wallets match `balanceOf` at each token's last folded
> block with zero mismatches and zero unanswered reads, total supply is fully accounted for on all three,
> and where the ledger and production's engine disagree on a moncock balance the chain sides with the
> ledger 213 times and with production zero times. On the one wallet where cost basis and realized PnL
> are checked against hand-derived values (CHIPOTLE), the ledger is exact to ±0.01 MON where the deployed
> engine is not. The existing suite is green (706 passed, 4 skipped) and the flag-off path is unchanged.

**Not entitled to claim, and currently implied:**

- that the ledger is verified for **cost basis, realized PnL or trade counts** at any scale — 2 wallets
  of 4,373 with a basis;
- that "12/12 invariants hold" means twelve independent properties — one is a tautology and several
  restate the fold's own arithmetic;
- that "determinism: PASS" means replays are reproducible — it means one token reproduces with warm
  caches and a frozen registry, while the wall-clock trace window makes 24 trace-resolved flows
  (133,415.71 MON, mostly JAMES) unreproducible from a fresh database from around 2026-09-09;
- that the fixture tables are an acceptance gate — 8 of 28 rows cannot fail, and the plan's only
  numeric quality bar (§13.4, estimated basis < 1 % per token) is neither met (7.2 %, 7.4 %) nor formally
  revised;
- that the tables are safe to read — **1,276 of 15,584 position rows are incidental partial histories
  carrying 846,024 MON of basis, and every one I could check disagrees with chain**;
- that the ledger is ready to be switched on — it cannot start.

**What the marker should say instead:** `REVIEW — shadow prototype. Quantities verified on three
fixture tokens; cost basis and PnL verified on two wallets. Not startable against a backend database
(no schema migration). 1,276 position rows are incidental partial histories and must be excluded or
rebuilt. Not an acceptance gate.`

---

## What I did **not** check

Stated plainly, because an unverified suspicion labelled as such is useful and one presented as a finding
is not.

- **feedback2 findings 1–6.** Out of my brief (reviewer 3 adjudicates them). I did not independently
  reproduce transaction-wide netting deleting round trips, the opposite-sign purchase inference, the
  forged-emitter path, the zero-proceeds unresolved sale, the disposal confidence split, or the
  `sub_index` instability — although the 357 coarser duplicates I measured are consistent with finding 6,
  and I confirmed the invariant that would have caught them cannot.
- **How often the public RPC returns a null `eth_getCode` result.** Finding 14's consequence is proven;
  its frequency is not.
- **The remaining 4,836 JAMES wallets and 9,225 moncock wallets.** I sampled 150 of moncock; reviewer 2
  sampled 100 of JAMES. Neither of us re-ran the author's full comparisons.
- **A fresh full-history replay, or any replay at all.** I ran none, so I did not verify throughput at
  scale, the checkpoint gap COMPLETE.md acknowledges, or whether a full-registry run terminates.
- **Whether `_own_quote`'s mixed-family case occurs in real prod transactions, and how often.** I proved
  the code path with synthetic inputs; I did not find a real transaction hash exhibiting it. Given USDC
  and AUSD are 6 % of flows, I would expect it to be rare but not zero, and I did not measure it.
- **The exact production latency of the RPC-in-transaction path.** My numbers are from this sandbox.
  Azure will be faster; the serial-and-in-transaction structure will not change.
- **The merge path (`scripts/merge_side.py`) back into prod.** Not exercised, not read closely.
- **API/WS/rewards/referral consumers.** I confirmed none of them read `positions_v2` or `wallet_flows`
  today; I did not review what porting them would require.
- **`feedback3.md`, `feedback5.md` and `feedback6.md`,** which other reviewers were writing concurrently.
  I did not read them, so any agreement or disagreement with them is independent.

---

# Deliverable: the two checklists

## A. What must be true before `LEDGER_ENABLED` is turned on in the live indexer

In this order. Items 1–3 are the crash itself; 4–8 are why a crash-free run would still not be safe.

1. **Create the ledger schema from the indexer's own startup path.** Call `init_ledger_schema` from
   `core/storage/schema.py:init_db()` (statement-by-statement in autocommit with the existing 5 s
   `lock_timeout` and retry, per the 2026-09-01 deadlock fix), or add an explicit preflight that fails
   the process *before* ingestion starts. **Verify by running the indexer against a fresh database with
   the flag on and confirming `[SQ]` lines advance** — not by reading the diff.
2. **Make the flag fail closed rather than fail fatal.** Wrap the two `LEDGER.*` call sites
   (`core/sequencer.py:1036`, `:1098`) so a ledger exception logs, disables the engine for the process
   and lets the chunk commit. The old engine must never be able to lose a block because the shadow
   ledger threw. Lazy-import `LedgerEngine` inside the guard while you are there
   (`core/sequencer.py:11`, `:1130`), so an import-time error in the ledger package cannot take the
   indexer down with the flag off.
3. **Prove the guard has teeth the way CLAUDE.md requires:** force the ledger to raise on a real chunk
   and watch the indexer keep indexing.
4. **Move the RPC work out of the write transaction.** Prefetch transaction metadata, `getCode` and
   traces per chunk before the transaction opens, the way `scripts/ledger_replay.py` already does. Then
   measure: **held time per chunk must be under the plan's §13.5 bar of 2× block rate (<= 200 ms/block)**,
   measured on a busy day, not a synthetic one. Today it is 2.5-15.0 s/block, i.e. 0.03x to 0.16x.
5. **Make the caches commit-aware.** Rebuild `self._registry` and `AddressKinds._kinds` after a rollback,
   or write registry and classification rows through a separate short transaction that commits
   independently. Regression test: run a chunk, roll it back, retry, assert `token_registry` and
   `address_kinds` are complete. (The reproduction above is a ready-made test.)
6. **Fix `flush` to clear `self._affected` only after `store.refold` succeeds** (`engine.py:471-473`).
7. **Freeze `TRACE_WINDOW_BLOCKS` behaviour or record it.** Either pin the window to the replay head
   rather than the live head, or stamp every trace-sourced flow with the head it was resolved at, so a
   later rerun can tell "the evidence expired" from "the answer changed".
8. **Decide and write down what shadow mode is for.** With no consumer, the only value is the comparison;
   so land a committed differential script (see B4) before turning the flag on, not after.

## B. What must be true before any API reads these tables

1. **Purge or quarantine the incidental positions.** 1,276 rows across 159 tokens, 389 of them negative,
   carrying 846,024 MON of basis, and 29 of 29 sampled disagree with chain. Either delete every row for a
   token not fully replayed from its creation block, or add a `coverage` column to `positions_v2` and
   make every read filter on it. Do not ship a table where a partial row is indistinguishable from a
   complete one.
2. **Give the replay real coverage checkpoints.** Per token, per scope, contiguous, committed atomically;
   `--resume` must read that checkpoint, not `MAX(block_number)`. As it stands `--resume` on any of the
   158 incidental tokens would skip a mean of 24.5 M blocks of history. Scope `store.purge_wallets` to the
   tokens being replayed.
3. **Persist the three-state inventory and the parked entitlements.** Add `observed_tokens`,
   `estimated_tokens` and per-venue parked basis and quantity to `PositionRow` and the schema, or no
   consumer can compute confidence-split unrealized PnL — which is the entire premise.
4. **Add automated checks for the numbers the API will actually serve.** Nothing today checks cost basis,
   realized PnL, `native_spent`, `native_received` or the trade counts beyond two wallets. At minimum:
   the prod differential, committed as a script and extended to compare `cost_basis_native`,
   `realized_pnl_native`, `token_bought`, `token_sold`, `native_spent` and `native_received` on every
   wallet the two engines share, with the disagreements classified rather than counted.
5. **Fix or delete the checks that cannot fail.** Rewrite invariant 8 to key on
   `(txhash, wallet, token, log_index, token_delta)` — it will immediately report the 357 real duplicates.
   Make `check_le` two-sided. Replace the hardcoded `ok=True` share rows with an enforced threshold, and
   either meet the plan's §13.4 <1 % bar or publish the revised number the plan demands. Join
   `token_registry` rather than `launchpad_tokens` in `ledger_verify.py`, or the crystal generation will
   be invisible to the coverage checks.
6. **Conserve the wallet's own outflow.** Keep every quote leg and its units (`_own_quote` must sum
   families, not choose one) and record router fees separately instead of overwriting the observed spend
   inside a 10 % band. Persist the gross and the net so the choice is reversible.
7. **Unify replay and live valuation** on one code path with one bucket size, one selection rule, one
   minimum sample and historical LVMON rates — or the two halves of the same table are valued by
   different rules for the 6.0 % of flows (36.5 M MON) whose basis comes from a rate.
8. **Settle transfer basis inheritance** (feedback.md blocker 1, COMPLETE.md known gap 1) before any PnL
   surface reads this. Cutting over with 44.6 % of JAMES unresolved is a visible regression against
   shipped behaviour, and 94.9 % of it comes from senders whose cost the ledger already knows.
9. **Add per-venue LP/vault entitlement accounting** before vault positions are served. The current
   single parked pool returns 550 MON where 100 is correct.
10. **Do not let rewards, referrals or any ranked or paid surface read a non-`observed` number** until
    items 3, 4 and 6 are done. A `basis_state = 'observed'` flow can still carry an estimated realized
    component (feedback2 finding 4), so `observed` on a flow is not a confidence flag for PnL.
