# Accounting ledger: second independent review

**Verdict: changes required. Keep this as a shadow prototype; do not treat the REVIEW marker or the three fixture checks as acceptance of the comprehensive positions replacement.** The existing tests pass, and the sampled JAMES balances are correct. Independent reproductions and the side database nevertheless demonstrate accounting and replay defects beyond the acknowledged missing transfer inheritance.

Reviewed on 2026-09-07: branch `accounting-fix`, commit `c516a34b3bcd0d36ef8d59949b40dd6e36cde70b`, against `34a414750fdd1948c99b02c27abf53631fea42a3`. The worktree was clean when captured and remained at that revision before this report. Validation used an immutable git-archive snapshot. This review covers the new ledger, replay/check scripts, tests, sequencer integration, completion claims, and relevant existing position consumers. It does not claim a new line-by-line audit of every unchanged backend file.

The user's policy is settled: unknown-cost receipts stay **unresolved**, estimates remain separate, and only confirmed inputs may feed ranked or paid features. The open question in plan §14 and the earlier feedback asking for this decision again are stale. Observed proceeds alone do not establish a gain.

## Evidence actually checked

- Full existing suite against separately named local scratch databases: **706 passed, 4 skipped**, 103.89 seconds. No tests used the direct ledger-database override that truncates the shared side database.
- Ruff check of the ledger modules, ledger scripts, and ledger tests: passed.
- **12 independent synthetic reproductions** against the frozen implementation. Their assertions establish current defects; they are not additional passing acceptance tests. Source and exact outputs are linked below.
- Shared side database inspected in a PostgreSQL **read-only, repeatable-read transaction**. No production database writes or replay/wipe commands were run.
- Independently called JAMES `balanceOf` at **block 102,523,914** for 100 ledger wallets: the 25 largest balances plus a deterministic sample across the remaining ordering. **100/100 exact matches**, no unanswered calls. Also checked the five non-venue production-holder addresses missing from the ledger: all five held zero at that block. Six intentionally excluded venue/token addresses were excluded from the wallet comparison.
- This was a 100-wallet chain sample, **not** an independent rerun of the author's full 4,936-wallet comparison or a fresh historical replay.

| Check | Independently observed result | What this establishes |
|---|---|---|
| CHIPOTLE target wallet | 20 trades; 20,724.080700 MON spent; 34,574.253543 received; 13,850.172843 confirmed realized | Matches the current fixture tolerances |
| moncock target wallet | Combined realized -194,863.895521 MON versus expected -193,957 | Passes the script's ±0.5% tolerance; the 906.895521 MON difference is not exact reconciliation |
| JAMES wallet balances | 100 sampled rows exact at the pinned block | Quantity coverage for that sample; no proof of basis correctness |
| Estimated / unresolved shares, current script definition | moncock 7.249% / 6.823%; JAMES 7.372% / 44.617% | Material uncertainty remains; these are flow-based metrics, not remaining-inventory basis shares |
| Unresolved disposals in side data | 352 rows; aggregate `realized_delta` **-1,029.6048877092433965 MON** | The missing-proceeds defect below occurs in real replay output; 351 rows and the entire loss are moncock |
| Side-data replay integrity | 357 duplicate `(txhash,wallet,token)` groups; 389 negative balance/custody rows | All are outside the three fixture tokens. Incidental token histories are not safe to serve as complete positions |

The chain-skipping checker alone reports five missing JAMES holders. The pinned chain calls explain those absences; I do **not** count that as a JAMES balance failure. The side database may contain output from earlier replay iterations; there is no run/version provenance sufficient to attribute every stored anomaly to this commit. The key-instability reproduction below independently confirms that the current code can create duplicate identities.

## Blocking accounting and replay findings

### 1. [P1] Transaction-wide netting deletes economically real actions

Locations: [core/ledger/netflow.py:457](<C:/Users/ryanl/OneDrive/Desktop/projects/crystal-backend-accounting-fix/core/ledger/netflow.py:457>), [core/ledger/netflow.py:723](<C:/Users/ryanl/OneDrive/Desktop/projects/crystal-backend-accounting-fix/core/ledger/netflow.py:723>).

`_collect` reduces the entire transaction to one signed quantity per wallet/token, and `net_transaction` discards zero deltas. The reproduction buys 100 tokens for 100 WMON and sells all 100 for 120 WMON in the same transaction: **the ledger emits zero flows**, losing both trades and the 20 MON realized gain. A buy followed by a gift similarly merges acquired and transferred quantities and loses the receiver's basis lineage. Matching the final balance cannot detect this.

**Required change:** preserve ordered economic actions and individual movement edges, then derive net balances as a projection. Keep aggregator hops collapsed within an action without collapsing independent buys, sells, and transfers across the entire transaction. This requires correcting plan §6.1/§6.3, not just adjusting a fold formula. Add round-trip, buy-then-transfer, and multi-recipient batch fixtures.

### 2. [P1] Opposite-sign wallet movements are treated as proof of a purchase

Locations: [core/ledger/netflow.py:503](<C:/Users/ryanl/OneDrive/Desktop/projects/crystal-backend-accounting-fix/core/ledger/netflow.py:503>), [core/ledger/netflow.py:645](<C:/Users/ryanl/OneDrive/Desktop/projects/crystal-backend-accounting-fix/core/ledger/netflow.py:645>).

A batch containing a 100-token gift from wallet B and an unrelated 10 WMON payment to wallet C becomes an **observed buy with 10 MON cost** for wallet A. No venue event, trace path, or attribution connects the two movements. Likewise, two opposite-sign non-quote token movements are automatically considered a swap and reference-priced. Amount-only matching of event hints also does not establish unique wallet attribution in equal-sized multi-wallet batches, and already-observed guesses do not trigger `_needs_trace`.

There is a stronger emitter-authentication failure at [core/ledger/engine.py:239](<C:/Users/ryanl/OneDrive/Desktop/projects/crystal-backend-accounting-fix/core/ledger/engine.py:239>) and [core/ledger/netflow.py:137](<C:/Users/ryanl/OneDrive/Desktop/projects/crystal-backend-accounting-fix/core/ledger/netflow.py:137>): `build_bundles` accepts known trade signatures from arbitrary emitters. The independent raw-log reproduction pairs a genuine token transfer with an `LT` event emitted by an unrelated address; the existing `accepts_log_for_indexing` gate rejects that emitter, but the ledger parses it and invents **10 MON observed purchase cost without any quote movement**. This directly defeats the assumption that an observed tag necessarily represents money actually paid.

**Required change:** authenticate venue emitters using historical known deployments or verified discovery, validate their token/currency mapping, and make confirmed attribution an explicit, auditable link between the action's token and quote legs. Unknown pools require evidence rather than merely a matching ABI topic. Ambiguous batches must remain estimated or unresolved according to available evidence. Preserve separate gifts/payments and do not manufacture trade shape from coincidental net directions. Add forged-emitter, equal-quantity multi-wallet, and unrelated-transfer cases. The net-direction inference is also a design-spec defect.

### 3. [P1] An unresolved sale invents zero proceeds and realizes a loss

Locations: [core/ledger/fold.py:87](<C:/Users/ryanl/OneDrive/Desktop/projects/crystal-backend-accounting-fix/core/ledger/fold.py:87>), [core/ledger/fold.py:160](<C:/Users/ryanl/OneDrive/Desktop/projects/crystal-backend-accounting-fix/core/ledger/fold.py:160>).

When sale proceeds are missing, `_quote_wei` returns zero. `_apply_sell` releases existing basis, calculates `0 - cost`, and adds that amount to estimated realized PnL. Buying for 100 MON and then making an unpriced sale produces **-100 MON estimated realized**, with no remaining unresolved quantity or explicit missing-proceeds amount. The code has no reference-price evidence for that loss. This occurs in the side data: 351 unresolved moncock sales collectively produce the 1,029.6048877092433965 MON loss above.

**Required change:** represent proceeds confidence independently from acquisition-cost confidence. A disposal with unknown proceeds must retain its released basis and an unresolved disposal record; it cannot produce a numeric gain or loss until proceeds are observed or explicitly estimated. Add observed/estimated/unresolved cost × observed/estimated/unresolved proceeds tests, including partial sales.

### 4. [P1] Disposal flow fields lose the confirmed/estimated split

Locations: [core/ledger/fold.py:160](<C:/Users/ryanl/OneDrive/Desktop/projects/crystal-backend-accounting-fix/core/ledger/fold.py:160>), [core/ledger/types.py:105](<C:/Users/ryanl/OneDrive/Desktop/projects/crystal-backend-accounting-fix/core/ledger/types.py:105>), [core/ledger/schema.py:16](<C:/Users/ryanl/OneDrive/Desktop/projects/crystal-backend-accounting-fix/core/ledger/schema.py:16>).

An estimated-cost purchase for 100 MON followed by an observed sale for 150 MON correctly leaves position confirmed realized at zero and estimated realized at +50. However, its sale flow says **`basis_state=observed`, `realized_delta=+50 MON`**. A consumer filtering observed flows will misclassify that gain as confirmed. Separately, an estimated 150 MON sale of unresolved-cost inventory increments `unresolved_proceeds_native` by 150, although the user's meaning of this bucket is confirmed proceeds on unknown-cost tokens. Its position row cannot distinguish estimated from observed proceeds.

**Required change:** store separate released-basis and realized/proceeds components with their confidence on each disposal. An observed quote flag is not a realized-PnL confidence flag. The confirmed-only restriction must be enforceable through the data contract, including flow consumers and rewards/referral/ranking queries.

### 5. [P1] Known basis disappears when tokens change wallets

Locations: [core/ledger/fold.py:180](<C:/Users/ryanl/OneDrive/Desktop/projects/crystal-backend-accounting-fix/core/ledger/fold.py:180>), [core/ledger/fold.py:185](<C:/Users/ryanl/OneDrive/Desktop/projects/crystal-backend-accounting-fix/core/ledger/fold.py:185>), [core/ledger/store.py:215](<C:/Users/ryanl/OneDrive/Desktop/projects/crystal-backend-accounting-fix/core/ledger/store.py:215>).

The sender releases basis, but `refold` rebuilds every wallet/token independently and the receiver has no sender state to inherit. Incoming transfers are unresolved, even when the sender's observed purchase is in the ledger. The completion document acknowledges this; it is still an unimplemented requirement of plan §6.7. The large JAMES unresolved share is not evidence that all of those receipts truly have unknowable cost.

**Required change:** fold causally linked transfers in chain order across wallets, carrying observed/estimated/unresolved quantities and their basis components. Preserve an explicit dependency graph or equivalent replay ordering so historical corrections propagate downstream. Merely sorting the existing per-wallet rows is insufficient: finding 1 already loses multiple transfer edges within a transaction. Unknown-source receipts must continue to remain unresolved; no new product decision is needed.

### 6. [P1] Flow identity changes when the registry or classified wallet set changes

Locations: [core/ledger/netflow.py:753](<C:/Users/ryanl/OneDrive/Desktop/projects/crystal-backend-accounting-fix/core/ledger/netflow.py:753>), [core/ledger/schema.py:40](<C:/Users/ryanl/OneDrive/Desktop/projects/crystal-backend-accounting-fix/core/ledger/schema.py:40>), [core/ledger/store.py:67](<C:/Users/ryanl/OneDrive/Desktop/projects/crystal-backend-accounting-fix/core/ledger/store.py:67>).

`sub_index` is the enumeration of all currently eligible wallet/token legs in the transaction. It is part of the primary key. Registering another token that sorts earlier changes an existing token's key from **(100,3,1,0) to (100,3,1,1)** in the reproduction. `ON CONFLICT DO NOTHING` therefore inserts the same economic movement twice rather than deduplicating it. Classification changes can shift the same indices. Conversely, an existing key prevents replacement when better trace/evidence changes the flow.

The side database contains 357 duplicate wallet/token/transaction groups. One concrete example is tx `0x53db149a3b98ccc609e6320a31e707388e7a4c93ec7a6be3d662ff6934817798`, wallet `0xcd6b980029e6e6e0733ac8ec3e02be9410d09799`, token `0x0158abff6d8344b35b5afffe7dbefc431b731a70`: the same log 130 and +227.5-token amount are stored with sub-indices 2 and 3.

**Required change:** derive identity from immutable transaction/action/movement provenance, independent of registry membership and classification. Version derived interpretations and support atomic replacement/refolding when evidence changes. Include canonical block identity and an explicit correction/reorg policy. Test replay with expanded registration, reclassification, improved traces, and repeated overlapping ranges.

### 7. [P1] Scoped replays write incomplete histories for other tokens; resume mistakes last activity for coverage

Locations: [scripts/ledger_replay.py:390](<C:/Users/ryanl/OneDrive/Desktop/projects/crystal-backend-accounting-fix/scripts/ledger_replay.py:390>), [scripts/ledger_replay.py:526](<C:/Users/ryanl/OneDrive/Desktop/projects/crystal-backend-accounting-fix/scripts/ledger_replay.py:526>), [scripts/ledger_replay.py:556](<C:/Users/ryanl/OneDrive/Desktop/projects/crystal-backend-accounting-fix/scripts/ledger_replay.py:556>), [core/ledger/engine.py:103](<C:/Users/ryanl/OneDrive/Desktop/projects/crystal-backend-accounting-fix/core/ledger/engine.py:103>).

Replay selects transactions involving requested tokens/venues, then nets **all active registered tokens** in those transactions. Other tokens acquire partial positions even though their independent transactions were never scanned. The side data has 389 negative rows, all outside the three fixtures, consistent with this incomplete-history path. `--resume` uses a single `MAX(block_number)` over the requested tokens, so a token with a later incidental flow can cause another token's missing earlier history to be skipped. A final flow block is not a completed scan checkpoint. Merging `--chain-logs-file` also unions blocks without enforcing the requested range.

**Required change:** distinguish observed incidental movements from complete position coverage. Use per-token, contiguous coverage checkpoints tied to input scope/version and commit them atomically. Either fold only the requested complete universe or mark partial histories explicitly and exclude them from authoritative positions. Resume each scope from its own checkpoint; respect range bounds when supplementing logs.

### 8. [P1] Mixed quote payments are dropped, and router fees are erased to match venue amounts

Locations: [core/ledger/netflow.py:222](<C:/Users/ryanl/OneDrive/Desktop/projects/crystal-backend-accounting-fix/core/ledger/netflow.py:222>), [core/ledger/netflow.py:340](<C:/Users/ryanl/OneDrive/Desktop/projects/crystal-backend-accounting-fix/core/ledger/netflow.py:340>).

`_own_quote` chooses either the MON or USD family and discards the other. A purchase funded by **10 WMON + 20 USDC at 1 USD/MON records only 20 MON**, losing a third of the cost. `_prefer_venue_quote` replaces an observed wallet spend with the venue amount whenever the difference is within 10%. A wallet paying 101 MON, with 100 reaching the venue and 1 retained by a router, is recorded as spending **100 MON**, with no fee ledger entry. This understates cost and overstates subsequent net gain; matching an old fixture total is not evidence that the wallet did not pay that fee.

**Required change:** retain every quote leg and its units, attribute refunds/fees to the relevant action, and conserve wallet outflow. If PnL excludes a fee, expose it separately and retain gross/net reconciliation. Do not use a percentage tolerance to silently overwrite observed monetary movement. The existing plan says protocol fees are included in observed quote legs.

### 9. [P1] Trades entirely inside custody are never processed

Locations: [core/ledger/engine.py:249](<C:/Users/ryanl/OneDrive/Desktop/projects/crystal-backend-accounting-fix/core/ledger/engine.py:249>), [core/ledger/engine.py:302](<C:/Users/ryanl/OneDrive/Desktop/projects/crystal-backend-accounting-fix/core/ledger/engine.py:302>), [core/ledger/fold.py:234](<C:/Users/ryanl/OneDrive/Desktop/projects/crystal-backend-accounting-fix/core/ledger/fold.py:234>).

Only a registered token `Transfer` sets a transaction's `moved` flag. `process_block` filters out all other bundles. An order-book maker fill or an internal-balance trade can change a user's inventory and quote ownership entirely inside custody without a wallet ERC-20 transfer, so it produces no ledger accounting. The fold's custody handling only moves deposit/withdrawal balances; it does not account for internal fills. A `TR` hint cannot repair a missing action or distinguish wallet from custody inventory.

**Required change:** ingest authoritative internal balance and fill events as economic movements, representing wallet and custody locations separately while sharing the wallet/token position. Reconcile wallet ERC-20 balances and custody entitlements independently. Add maker/taker fills, partial orders, internal reinvestment, deposit-trade-withdraw, and same-transaction internal round trips. This is required by plan §13.7, even if API cutover is deferred.

### 10. [P1] LP/vault basis is pooled across unrelated destinations

Locations: [core/ledger/fold.py:198](<C:/Users/ryanl/OneDrive/Desktop/projects/crystal-backend-accounting-fix/core/ledger/fold.py:198>), [core/ledger/fold.py:208](<C:/Users/ryanl/OneDrive/Desktop/projects/crystal-backend-accounting-fix/core/ledger/fold.py:208>), [core/ledger/types.py:132](<C:/Users/ryanl/OneDrive/Desktop/projects/crystal-backend-accounting-fix/core/ledger/types.py:132>).

Parked basis is one aggregate per wallet/token, with no vault, pool, or share identifier. Deposit 100 tokens with 100 MON basis into vault A, then another 100 tokens with 1,000 MON basis into vault B: withdrawing the first 100 from A restores **550 MON** instead of 100 and leaves 550 parked instead of 1,000. The venue on each flow is ignored. Share transfers and changed redemption ratios have no entitlement model, and parked state is omitted from `PositionRow`.

**Required change:** keep basis attached to the actual pool/vault/custody entitlement and its share quantity. Define partial redemption, yield/loss, share transfer, multiple-underlying, and cross-venue behavior. A single parked counter is only adequate for an explicitly constrained one-destination, one-for-one model.

## Further requirements before live shadow/cutover

### 11. [P1 before enabling] Startup and retry behavior are not ready for the live flag

Locations: [core/sequencer.py:1036](<C:/Users/ryanl/OneDrive/Desktop/projects/crystal-backend-accounting-fix/core/sequencer.py:1036>), [core/ledger/engine.py:182](<C:/Users/ryanl/OneDrive/Desktop/projects/crystal-backend-accounting-fix/core/ledger/engine.py:182>), [core/ledger/engine.py:466](<C:/Users/ryanl/OneDrive/Desktop/projects/crystal-backend-accounting-fix/core/ledger/engine.py:466>), [core/ledger/kinds.py:297](<C:/Users/ryanl/OneDrive/Desktop/projects/crystal-backend-accounting-fix/core/ledger/kinds.py:297>), [scripts/ledger_replay.py:463](<C:/Users/ryanl/OneDrive/Desktop/projects/crystal-backend-accounting-fix/scripts/ledger_replay.py:463>).

The only non-test caller of `init_ledger_schema` is the side replay script. Enabling `LEDGER_ENABLED` on a normally initialized backend database therefore reaches missing ledger tables; because the hook shares the existing sequencer cursor, failure also aborts the existing block transaction. RPC metadata/classification/traces and full-history refolds run within that transaction, so live throughput and failure isolation remain unproven.

In-memory registry/classification state is also mutated before commit, while chunk retries reuse the engine. An independent reproduction makes the classification write fail, then retries: the second call returns the cached classification and persists **zero rows**. Discovery and registry caches have analogous rollback concerns. `flush` clears affected keys before refolding succeeds.

**Required change:** provide an idempotent startup migration or a verified preflight that fails before ingestion, move external fetching outside the write transaction, and make caches/affected keys commit-aware or rebuild them after rollback. Test a failed chunk followed by retry and process restart against a clean replay. Keep the live flag off until the shared-sequencer behavior and busy-day throughput gate are exercised.

### 12. [P1 before serving] Persist the three-state inventory and historical valuation contract

Locations: [core/ledger/fold.py:70](<C:/Users/ryanl/OneDrive/Desktop/projects/crystal-backend-accounting-fix/core/ledger/fold.py:70>), [core/ledger/types.py:132](<C:/Users/ryanl/OneDrive/Desktop/projects/crystal-backend-accounting-fix/core/ledger/types.py:132>), [core/ledger/schema.py:51](<C:/Users/ryanl/OneDrive/Desktop/projects/crystal-backend-accounting-fix/core/ledger/schema.py:51>), [scripts/ledger_replay.py:203](<C:/Users/ryanl/OneDrive/Desktop/projects/crystal-backend-accounting-fix/scripts/ledger_replay.py:203>), [scripts/ledger_replay.py:255](<C:/Users/ryanl/OneDrive/Desktop/projects/crystal-backend-accounting-fix/scripts/ledger_replay.py:255>), [core/ledger/engine.py:424](<C:/Users/ryanl/OneDrive/Desktop/projects/crystal-backend-accounting-fix/core/ledger/engine.py:424>).

Observed and estimated **token quantities** exist only in transient `PositionState`; the row stores their monetary basis but only unresolved quantity. That is insufficient to calculate separately observed-basis and estimated-basis unrealized PnL from a served row. USD PnL/basis, valuation confidence, missing-rate reasons, and persisted parked entitlements are also absent. A quote being observed in USDC does not make its historical MON conversion observed.

Replay and live valuation differ: replay seeds the **last trade in each minute** and selects by minute bucket without checking the sample's block, so an earlier transaction can use a later rate. Live caches the first rate it encounters for five minutes. Both use the current LVMON meta rate for historical flows, and live falls back to current MON/USD meta when history is absent. Identical inputs are therefore not guaranteed identical valuations across replay and live operation.

**Required change:** persist inventory components and valuation provenance needed by consumers; use historical rates at or before each action with explicit missing/stale states and shared live/replay logic. Distinguish observed asset units from estimated conversion. Port API/WS/portfolio/ranking/referral/reward consumers to explicit confirmed fields when cutover is undertaken. Current consumers still read old position tables; that is expected for shadow phase, but means the promised comprehensive replacement and confirmed-only consumer contract are not yet delivered.

### 13. [P2] Completion checks report uncertainty as PASS and miss important invariants

Locations: [scripts/ledger_check.py:163](<C:/Users/ryanl/OneDrive/Desktop/projects/crystal-backend-accounting-fix/scripts/ledger_check.py:163>), [scripts/ledger_check.py:316](<C:/Users/ryanl/OneDrive/Desktop/projects/crystal-backend-accounting-fix/scripts/ledger_check.py:316>), [scripts/ledger_check.py:381](<C:/Users/ryanl/OneDrive/Desktop/projects/crystal-backend-accounting-fix/scripts/ledger_check.py:381>), [POSITION_LEDGER_PLAN.md:500](<C:/Users/ryanl/OneDrive/Desktop/projects/crystal-backend-accounting-fix/POSITION_LEDGER_PLAN.md:500>).

Estimated and unresolved share rows always use `ok=True`; the plan's **<1% estimated basis share per token** is neither enforced nor replaced with a quantified revised gate. The metric counts flow quote confidence for buys/sells, rather than carried acquisition-basis confidence, and excludes `swap_leg`. It can report an observed sale of estimated-cost inventory as observed volume. The checker does not reject the incidental negative positions or duplicate groups found above. Also, comparing `balance_token + custody_balance` to wallet `balanceOf` is incorrect when custody is nonzero: the ERC-20 wallet balance excludes the custody entitlement. This mistaken invariant is inherited from the build spec.

**Required change:** separate informational metrics from acceptance gates, define denominators for remaining basis and unresolved disposal coverage, enforce agreed thresholds, and add conservation, nonnegative complete-history inventory, stable replay, and separate wallet/custody reconciliation. Keep unresolved *causes* and coverage holes visible; a passing numeric fixture cannot establish completeness.

### 14. [P2] Reject null bytecode evidence and allow classification corrections

Locations: [core/ledger/kinds.py:75](<C:/Users/ryanl/OneDrive/Desktop/projects/crystal-backend-accounting-fix/core/ledger/kinds.py:75>), [core/ledger/kinds.py:199](<C:/Users/ryanl/OneDrive/Desktop/projects/crystal-backend-accounting-fix/core/ledger/kinds.py:199>), [core/ledger/kinds.py:297](<C:/Users/ryanl/OneDrive/Desktop/projects/crystal-backend-accounting-fix/core/ledger/kinds.py:297>), [core/ledger/kinds.py:448](<C:/Users/ryanl/OneDrive/Desktop/projects/crystal-backend-accounting-fix/core/ledger/kinds.py:448>).

`classify_code(None)` becomes EOA and `kinds_for` persists/caches it. The reproduction returns a null bytecode result and confirms subsequent calls never retry. **Ordinary transport errors are already retried and raised by `JsonRpc.batch`; this finding concerns null/malformed successful results, not that normal error path.** Negative pair probes are also permanent and use `latest`, while later event/UserOperation promotion guards do not accept existing `source='pair_probe'`, making classification corrections inconsistent between memory and the database.

**Required change:** validate bytecode results, retain unknown status for absent evidence, and make classification transitions explicit and consistent across restart. Use historical classification or a documented temporal policy for upgrades/delegation changes. An unrecognized custom pool can still be a holder candidate; a venue-leak count based only on known classification cannot prove there are no unknown venues among holders.

## Recommended next implementation sequence

1. Correct the action/movement model and immutable identities first (findings 1, 2, 6). Patching transfer inheritance onto lossy transaction nets will require another rewrite.
2. Implement the cost/proceeds confidence matrix and cross-wallet inheritance (3–5), then multi-quote/fee conservation (8).
3. Add custody and per-entitlement LP/vault accounting (9–10), with persisted inventory and historical valuation state (12).
4. Make scoped replay, evidence correction, startup, and retries deterministic (7, 11, 14). Rebuild the side ledger from clean inputs before using the existing incidental rows as evidence.
5. Expand acceptance beyond the three examples (13): retain CHIPOTLE/moncock/JAMES, add the reproductions below and real aggregator/batched/custody/LP/vault cases, then compare replay/live output and measure throughput. Do not remove the old system or enable paid/ranked consumers until the confirmed-only contract is tested end to end.

## Reproduction artifacts and limits

- [Independent probes](<C:/Users/ryanl/OneDrive/Email attachments/Documents/crystal/feedback2_probes.py>) and [exact results](<C:/Users/ryanl/OneDrive/Email attachments/Documents/crystal/feedback2-probes.json>).
- [Existing suite output](<C:/Users/ryanl/OneDrive/Email attachments/Documents/crystal/feedback2-tests.txt>) and [isolated test runner](<C:/Users/ryanl/OneDrive/Email attachments/Documents/crystal/run_feedback2_tests.py>).
- [Read-only data-check script](<C:/Users/ryanl/OneDrive/Email attachments/Documents/crystal/feedback2_data_check.py>) and [data/chain results](<C:/Users/ryanl/OneDrive/Email attachments/Documents/crystal/feedback2-data.json>).

These artifacts use the local immutable `accounting-review-c516a34` snapshot. The probe script uses existing test constructors only to build inputs; the counterexamples and assertions are independent. The data scripts obtain the local side DSN programmatically and do not print credentials. No implementation source, completion marker, production data, branch history, or existing `feedback.md` was changed by this review. This report is the only new file written into the implementation worktree.

Not independently validated: a fresh full-history rebuild, all 4,936 JAMES wallets, every unknown venue's semantics, production throughput, the historical 841/8,584 cohorts, or a live cutover. None of those is implied by the green existing suite or the sample checks above.
