# Reviewer 3: adjudication of findings 1 to 6

Reviewed at `f913de9`. `core/ledger/*` is unchanged since `c516a34`, so every reproduction below runs
against the code as pushed; the two newer commits add `COMPLETE.md` text and `scripts/ledger_verify.py`,
not fixes.

## Verdict

All six hold. None is refuted. Two are narrower than `feedback2.md` implies in how much replayed data they
have already corrupted, and one is **wider** than stated. The right answer to the closing question is
**repair, in a specific order**, not rebuild: the transaction model is salvageable because the evidence the
fixes need is already collected and then discarded. Identity must be fixed first, before any re-replay is
used to validate anything else.

## Adjudication

| # | claim | verdict | blast radius I measured |
|---|---|---|---|
| 1 | per-transaction netting deletes real actions | **confirmed, and wider than stated** | 26 moncock and 130 JAMES wallet-transactions vanish entirely; 2,228 and 1,034 more are silently distorted |
| 2 | opposite-sign movements taken as proof of a purchase | **confirmed** | reproduced; frequency not measurable from stored flows |
| 3 | unresolved sale invents zero proceeds | **confirmed** | 352 rows, −1,029.6049 MON, 351 of them moncock |
| 4 | disposal loses the confirmed/estimated split | **confirmed** | affects every sale that releases estimated basis |
| 5 | basis vanishes when tokens change wallets | **confirmed; supporting number 89.1%, not 94.9%** | 1,761 JAMES positions hold 874,160,375 unresolved tokens |
| 6 | flow identity is unstable | **confirmed, narrower in current data** | 357 duplicate groups, all outside the three fixtures |

### 1. Confirmed, and worse than the review states

The stated case (a buy and a sell of equal size producing no flow) reproduces:

```
one transaction: buy 100 tokens for 100 WMON, then sell all 100 for 120 WMON
  flows emitted: 0        (control: the buy alone emits 1)
```

`feedback2.md` frames this as the exact-zero case. The larger population is the **unequal** round trip,
which does not vanish but is silently rewritten: buy 100 and sell 60 in one transaction becomes a net buy
of 40, and the sale, its proceeds and its realized PnL disappear while the balance stays correct.

Measured against production, per token, over all history:

| token | net exactly zero, flows vanish | net non-zero, quantities rewritten |
|---|---|---|
| moncock | 26 wallet-transactions | 2,228 |
| JAMES | 130 wallet-transactions | 1,034 |

This is not theoretical for the fixtures. moncock is fully replayed, and **all 26 of its vanishing
wallet-transactions have zero rows in `wallet_flows`**. Production books +27,719.90 MON of realized PnL
across them, none of it visible to the ledger. Example, verified in both databases:

```
0xd786059afa5e0b48b50105988d9eb113044c8f624627a0d36a7555b8f4550e9b
  wallet 0x9d54c129fb…  prod: bought 320.02, sold 320.02, realized +27,667.17 MON, 2 legs
  ledger: no rows at all
```

(That production number is itself fabricated for a different reason. See `feedback5.md` finding 1. The
point here is that the ledger's answer is "this transaction never happened", which is also wrong.)

### 2. Confirmed

```
one transaction: wallet A is gifted 100 tokens by B, and separately pays 10 WMON to C
  wallet A: kind=buy  token +100.00  quote -10.00  basis_state=observed
```

Two unrelated movements become an `observed` purchase with an invented price. `observed` is the strongest
confidence the system has, and it is being granted on coincidence. I could not measure how often this fires
in the replayed data, because the stored flow does not record which evidence produced it: a genuine
venue-backed buy and a coincidence-backed buy are the same row. That is itself worth fixing, since it makes
the defect unauditable after the fact.

### 3. Confirmed

```
buy 100 tokens for 100 MON, then sell all 100 with proceeds not observed
  realized_estimated  = -100.00 MON
  unresolved_proceeds =    0.00 MON
```

A disposal with unknown proceeds books a definite loss. In the side database this is 352 rows totalling
**−1,029.6049 MON**, 351 of them moncock, matching `feedback2.md` exactly. The failure is in `_quote_wei`
returning `0` for "no evidence", so absence is indistinguishable from a genuinely free disposal.

### 4. Confirmed

```
estimated-cost buy 100 MON, then observed sale 150 MON
  position: confirmed realized +0.00, estimated realized +50.00     (correct)
  sale flow row: basis_state=observed, realized_delta=+50.00        (wrong)
```

The position splits confidence correctly; the flow row does not. Any consumer that filters flows on
`basis_state='observed'` reads a +50 MON confirmed gain that the position itself calls estimated. Given the
policy that only confirmed inputs may feed ranked or paid features, this is the row a rewards query would
read.

### 5. Confirmed, with a corrected number

The mechanism is as described and the author acknowledges it. On the supporting statistic I get a different
split. Totals agree exactly (1,287,202,079 unresolved inflow tokens; 2.1% from senders absent from the
ledger), but by my classification — sender has an `observed` acquisition of that token in the ledger — the
share is **89.1%, not 94.9%**, with 8.8% from senders present but without observed buys. The difference is
definitional, not a contradiction, and it does not change the conclusion: the large majority of unresolved
inventory has a knowable cost sitting in the same database. 1,761 JAMES positions currently carry
874,160,375 unresolved tokens.

### 6. Confirmed, narrower in the data than in the mechanism

The mechanism reproduces exactly. `sub_index` is the leg's position in the transaction's list sorted by
`(wallet, token)` (`netflow.py:753`), and it is part of the primary key:

```
before an earlier-sorting token is registered   TOKEN row key = (100, 3, 2, 0)
after it is registered                          TOKEN row key = (100, 3, 2, 1)
```

Same movement, same log index, different key, so `ON CONFLICT DO NOTHING` inserts a second row rather than
recognising it. The 357 duplicate groups are real and I confirmed the named example: sub-indices 2 and 3,
identical log index 130, identical delta, identical counterparty. Across a 200-group sample, log index and
delta are identical in 200 of 200 — these are the same movement stored twice, not two movements.

Narrower than stated in one respect that matters for triage: **all 357 are outside the three fixture
tokens**, in the 159 partially replayed leftovers. The fixtures are clean. Wider in another: in 37 of those
200 groups the `kind` differs between the duplicate rows, so classification, not only numbering, changed
between replays and both versions are now stored.

## Why the quantity results and these findings are both true

The author's strongest evidence — supply conservation, 9,375 wallets reconciling to the wei against
`balanceOf`, zero unaccounted — is real, and it is untouched by all six findings, because **every one of
them preserves net token quantity per wallet while corrupting value, confidence, ordering or identity**:

- netting a round trip to zero preserves the balance exactly; that is what makes it invisible;
- inferring a purchase from a coincidental payment attaches a wrong cost to a correct quantity;
- a zero-proceeds sale moves the right number of tokens and invents the money;
- the confidence label is metadata the balance never sees;
- unresolved inbound transfers hold the right quantity with no basis;
- duplicate rows double a movement in the flow table while the position fold, keyed per wallet and token,
  still lands on the right balance.

So the two pictures are not in tension: the quantity layer is genuinely solid and the value layer is not
yet checked by anything. A `balanceOf` comparison cannot detect a single one of these. That is the honest
summary for whoever reads both reports: the fixtures prove the ledger knows *what* moved, and prove nothing
about *what it cost*.

## Repair or rebuild

**Repair, in this order.** I do not think a rebuild is justified, and the reasoning is that the information
the fixes need is already gathered and then thrown away, which is a different situation from a model that
cannot express the truth.

1. **Identity first** (finding 6). Derive the key from immutable provenance that is already on the leg —
   `(txhash, log_index, wallet, token)` plus a discriminator for genuinely repeated movements — instead of
   the leg's ordinal position. Nothing else can be trusted until re-replays are idempotent, because every
   other fix will be validated by re-replaying, and today that silently accumulates rows. Small code change,
   requires one clean re-replay of the side database.
2. **Action grouping** (findings 1 and 2). Stop collapsing the transaction to one signed quantity per
   wallet and token. `_collect` already walks ordered legs with log indices; group them into actions and net
   only within an action, keeping aggregator hops collapsed. Then a purchase requires an actual link between
   a token leg and a quote leg in the same action, which also removes the coincidence inference, because two
   unrelated movements are no longer in one action. These two findings share one fix.
3. **Confidence model** (findings 3 and 4). Separate proceeds confidence from cost confidence, and carry
   both on the disposal row. Unknown proceeds must produce an unresolved disposal, never a number.
4. **Transfer basis** (finding 5). Fold per token across wallets in chain order, which is possible once
   step 2 preserves individual transfer edges. Doing this before step 2 would inherit basis along edges that
   have already been merged away.

What can be kept: the flow and position schema, the three-state confidence idea, the classifier and its
venue gate, the fold's proportional release, the store, and the replay and check harnesses. What must
change is confined to `netflow.py` grouping, the identity key, and roughly forty lines of `fold.py`.

The order is the load-bearing part of this recommendation. Attempting 2 before 1, or 4 before 2, produces
results that cannot be validated by re-replay.

## What I did not check

- Findings 7 to 14 of `feedback2.md`; reviewers 4 and 6 have those.
- Whether the 2,228 moncock and 1,034 JAMES distorted cases are all genuine round trips rather than
  artefacts of production's own double-booking. I verified the mechanism and the 26 vanishing cases in the
  ledger's own data, and did not audit each distorted group.
- The `kind` disagreement in 37 of 200 duplicate groups: I established that it happens, not which
  classification is correct or which replay produced which row.
- `scripts/ledger_verify.py`, beyond noting the brief's point that its duplicate check keys on the primary
  key and therefore cannot see these duplicates by construction.
- Any measurement of finding 2's frequency in real data, for the reason given above.
