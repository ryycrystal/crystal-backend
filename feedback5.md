# Reviewer 5: breaking it with real transactions

Reviewed at `f913de9`. Everything below comes from real on-chain transactions or the two databases, not
from reading code paths. Where I could not make something fail, I say so.

## Verdict

One new P1 against the ledger: a whole class of real trading is invisible to it, and **fixing finding 1's
netting will not fix this class**, because the actions leave no ERC-20 transfer to preserve. Separately I
found a live defect in the currently deployed engine that is worse than anything in the ledger, and one
result that argues *for* the ledger's design. My adversarial attempts on the classifier did not land.

## 1. [P1] Uniswap V4 actions settled in claim balances are invisible to the ledger

V4 lets a swap settle against the PoolManager's internal ERC-6909 claim balances instead of moving ERC-20s.
`LedgerEngine.process_block` only considers a transaction when a registered token `Transfer` appears, so
these transactions are dropped before netting ever runs.

Verified transaction, block 45,905,959:

```
0xd786059afa5e0b48b50105988d9eb113044c8f624627a0d36a7555b8f4550e9b
  3 V4 swaps on the PoolManager, moncock on both sides of a 3-hop cycle
  ERC-20 transfers of moncock in the whole transaction: 0
  production:  bought 320.02, sold 320.02, realized +27,667.17 MON
  ledger:      no rows at all
```

All 26 moncock wallet-transactions that net to zero have **zero** rows in `wallet_flows`; this is one of
them. The nuance that matters for the plan: finding 1 in `feedback2.md` is about netting discarding legs
that exist, and the fix is to net per action. Here there are no legs at all. Preserving intra-transaction
actions cannot recover a trade whose only evidence is a swap event and a claim-balance delta. The ledger
must ingest V4 swap events as economic movements in their own right, with the wallet resolved from the
transfer graph or the transaction origin, exactly as it must ingest order-book fills for `feedback2.md`
finding 9. Both are the same root cause: **economic movement without an ERC-20 transfer.**

Scope I can state: 26 moncock and 130 JAMES wallet-transactions vanish entirely; 38 of the 83 registered V4
pools are quoted in an asset that is not WMON, and V4 claim settlement is available on every one of them.

## 2. [P1, but against the deployed engine, not this branch] USDC quote legs are read as 18-decimal MON

While deriving the truth for the transaction above I found the production number is fabricated, and the
cause is exact and reproducible. In that transaction the wallet bought 320.02 moncock from a moncock/USDC
V4 pool. The raw swap amounts:

```
amount0 (moncock, 18 decimals) =  320019831901713613752   ->    320.0198 tokens
amount1 (USDC,     6 decimals) =              -1000000    ->      1.0000 USDC
production recorded the cost as                                  0.000000000001 MON
```

USDC is 6 decimals, confirmed on chain (`decimals()` = 6, `symbol()` = USDC). The engine treats the V4
quote leg as 18-decimal native, understating the cost by a factor of 10¹². The position therefore carries
essentially zero basis, and the next sale books the entire proceeds as realized profit.

`UNIV4_QUOTE_TOKENS` in `core/sequencer.py:16` whitelists four quote assets, of which USDC is one, and the
pool table shows 38 of 83 pools registered against it, spanning 29 tokens.

Signature population, which I measured but did not attribute row by row: those 29 tokens carry 633,807 buys
with effectively zero recorded cost (269,678,925 tokens acquired), and their positions hold +324,625,529 MON
of positive realized PnL against a production-wide total of +648,590,206 MON. I am **not** claiming all of
that is fabricated — these are also among the most heavily traded tokens, and I did not verify each row's
cause. I am claiming the mechanism is proven on one transaction and that the exposed population is large
enough to warrant its own investigation, independently of this branch.

This is worth raising with whoever owns the deployed engine today, since it is live and the ledger is not.

## 3. [P2] Production credits the Uniswap V4 PoolManager with a position; the ledger correctly does not

Of moncock's top 40 holders in production, exactly one is a contract, and it is the PoolManager itself:

```
0x188d586ddcf5…  balance 9,029,306 moncock   realized +356,096 MON   (production)
```

The ledger classifies that address `venue_pool` from its known list and books it nothing. This is the
clearest quantified argument for the classifier and the venue write gate: one address, nine million tokens
and a third of a million MON of phantom realized PnL, excluded by construction rather than by a cleanup
script. Worth keeping in the plan as evidence, since the classifier is one of the pieces I would keep.

## 4. Adversarial attempts that did not land

Reported because a failed attack is a result.

- **Make a wallet look like a venue.** The classifier promotes an unknown contract that answers
  `token0()`/`token1()`. I probed the contract holders among moncock's top 40 positions: only one is a
  contract at all (the PoolManager), and it does not answer the pair interface. I found **no** address
  whose position is accidentally erased this way. The attack remains theoretically cheap — deploy a
  contract with two getters — but I could not show it happening by accident in the replayed data.
- **Make a venue look like a wallet.** 819 addresses are `contract_unknown` and 400 of them hold positions
  totalling 270,611,323 tokens, every one classified by `pair_probe` returning `pair: False`. I inspected
  the largest, `0xb0baace23ac7…`: 103 tokens in the ledger, 10,129 `transfer_in` flows, 5,990 sells, 3
  `transfer_out`, and not a known pool in production. That profile is a fee or treasury sink rather than a
  pool, so the classification is defensible. I did not find a pool masquerading as a wallet, but I also
  cannot rule one out of 819 with the sampling I did.

That same address is the largest single amplifier of `feedback2.md` findings 3 and 5: over ten thousand
inbound transfers with no inherited basis, followed by nearly six thousand sales that will each release
nothing and price their proceeds against unresolved inventory.

## Ranking

| finding | severity | how often |
|---|---|---|
| 1. claim-settled V4 actions invisible | P1 for the ledger | 26 moncock and 130 JAMES wallet-transactions vanish; every V4 pool can settle this way |
| 2. USDC leg read as 18-decimal MON | P1 for the deployed engine | proven on one transaction; 38 of 83 pools and 29 tokens exposed |
| 3. PoolManager holds a production position | P2, evidence for the ledger | one address, 9,029,306 tokens, +356,096 MON |
| 4. classifier attacks | not reproduced | no accidental case found in the samples above |

## What I did not check

- ERC-4337 bundles, LP add and remove, vault deposit and withdraw against real transactions. I ran none of
  these; `feedback2.md` finding 10 covers the vault basis model from the code side and I did not add to it.
- Rebasing or fee-on-transfer tokens. I did not establish whether any exist in the registry.
- A token whose pool is in no registry, met cold by the classifier.
- Whether classification can flip between replays for the same address. I saw evidence consistent with it
  (37 of 200 duplicate flow groups disagree on `kind`, reported in `feedback3.md`) but did not isolate an
  address whose kind changed.
- Attribution of the 633,807 near-zero-cost buys in finding 2 to the decimals path, row by row.
- Any independent rerun of the author's 9,375-wallet `balanceOf` comparison; I relied on the quantity
  results being correct, which is also what makes the value-layer defects invisible to them.
