# Crystal — Target Data Architecture

Written after a full audit of all three layers (frontend network touchpoints, backend
indexer/API/WS, contract event surface). This is the design to build toward: fastest
possible UI, minimum reads, minimum bandwidth, no polling, and every number accurate
to a stated block.

---

## 1. The one principle

**Every datum crosses each boundary exactly once, driven by the block clock.**

The chain is the only writer. A fact (a fill, a transfer, a price move) should be:
read from the chain **once** (by the indexer), written to Postgres **once**, pushed to
each interested client **once** (as a delta), and rendered from client memory until
the next block changes it. Anything else — polling, re-reading, recomputing on a
wall-clock timer, fetching the same fact from two sources — is waste, and it is also
where inaccuracy lives (two sources = two answers).

Monad produces a block every ~400ms and has single-slot finality (no reorgs). So the
system's natural tick is the block. Nothing should update faster (there is nothing new
to say) and nothing should update slower (staleness is a choice, not a constraint).

## 2. Where we are today (measured, not guessed)

The frontend currently runs, per active user:

| What | Period | Should be |
|---|---|---|
| Main RPC multicall (quotes/allowance/balances/orderbook/prices) | **300ms** on trade page | event-driven push + on-intent reads |
| Terminal RPC multicall | 800ms | same |
| `/tokens` explorer poll | 3s | `tokens` WS channel (exists) |
| `/stats` poll in TokenDetail | 15s | `stats` WS channel (exists — poll is redundant already) |
| Vaults, referrals, leaderboard, arb-USDC polls | 3–5s each | WS / on-demand |
| Portfolio graph | 25–120 archive RPC reads **per page load** | server bucket cache (one archive read per bucket ever) |
| Chain-WS log stream decoded in the browser | continuous | backend user-fills channel |
| goldsky subgraph (markets, klines, trades, vaults) | per navigation | retire — indexer already sees every event |
| react-query `gcTime: 0` | — | actual caching + ETag/304 |

Three sources for price (stork eth_call, goldsky, multicall `getPrices`), three for
trades (backend WS, chain WS, goldsky), three for balances. Every duplicate source is
a consistency bug waiting to render.

## 3. Target architecture

```
                     ┌────────────────────────────────────────────┐
 chain (400ms) ──►   │ INDEXER: one ingestion plane                │
                     │  every topic, incl. orderbook raw log2      │
                     │  topics (fills 0xc3bc…, batch 0x1c87…)      │
                     │  → Postgres (single writer, idempotent)     │
                     │  → NOTIFY on commit                         │
                     └────────────────┬───────────────────────────┘
                                      │ LISTEN (no polling)
                     ┌────────────────▼───────────────────────────┐
                     │ API: cold load = ONE query per page,        │
                     │  stamped as_of_block, ETag = watermark      │
                     │ WS: ONE socket, all channels multiplexed,   │
                     │  one coalesced frame per block per page,    │
                     │  deltas only, per-socket seq                │
                     └────────────────┬───────────────────────────┘
                                      │
                     ┌────────────────▼───────────────────────────┐
                     │ CLIENT: passive cache                       │
                     │  render = f(snapshot ⊕ deltas)              │
                     │  RPC only for wallet-local intent:          │
                     │   quote on input change (debounced),        │
                     │   allowance on demand, gas at submit        │
                     └────────────────────────────────────────────┘
```

### The ingestion plane (indexer)

- **Decode both orderbook topics.** The contracts emit fills via raw `log2`
  (`0xc3bcf95b…`) and batch order updates via a second raw topic (`0x1c87843c…`),
  distinct from the single-order `emit` topic. `OrdersFilled.filled[]` carries
  per-order **remaining size**, `amounts` carries in/out (effective price), so the
  entire orderbook state — book levels, last price, per-user fills, open orders with
  remaining size — is derivable from events alone, no `getPriceLevels`/`getPrice`
  reads. This is also the root-cause fix for the stale `remainingSize` bug: any order
  touched via the batch path is currently invisible to us.
- **Market OHLCV from fills**, same table/machinery as launchpad candles. This plus
  the above retires goldsky entirely: markets list, klines, trades, leaderboard all
  come from our own plane.
- **Derive, don't read.** Claimable fees are never emitted but are exactly
  `fee_rate × fill volume` per user — accumulate them in the indexer instead of
  `eth_call accumulatedFee*`. Same discipline as PnL.
- **Balances**: for indexed tokens (launchpad + market bases/quotes) we already see
  every `Transfer` — serve balances from the DB. Native MON balance has no event: the
  API multicalls it **per block, only for wallets with an active portfolio
  subscription**, shared across all viewers of that wallet. Zero client RPC.

### The serving plane (API/WS)

- **Cold load: one query per page** (done for /board and /spectra; extend to
  portfolio, vaults, referrals, leaderboard). Response carries `as_of_block`; ETag is
  the watermark, so an unchanged page re-validates as a 304 with an empty body.
- **Hub: LISTEN/NOTIFY replaces the 400ms poll.** The indexer NOTIFYs channel keys it
  touched in the commit; the hub recomputes exactly those. Latency chain becomes
  block → commit → push ≈ block time + ~10ms, and an idle chain costs zero queries.
- **One frame per block per page.** All channels a socket subscribes to are coalesced
  into a single multiplexed frame per block (one JSON object, one permessage-deflate
  window). No per-channel frame spray.
- **Private channels** on the same socket: `user_fills`, `user_orders` (from the
  decoded batch topic), `balances`, `positions` — replaces the client's own chain-WS
  subscription and its decoding logic.
- **Deltas everywhere** with per-socket `seq` (done); seq gap → client resnapshots via
  the cold-load endpoint with `since_block` (done for /tokens).

### The client

- Renders from its cache; every displayed page is internally consistent at one
  `as_of_block` (never merge fields from different blocks).
- **RPC reads only at intent time**: `getAmountsOut` when the input amount changes
  (debounced ~250ms), allowance once per token (invalidated by own approval tx), gas
  estimate at click. The 300ms loop dies; the swap click path keeps its current
  "everything already known" latency because the same data now arrives by push.
- react-query gets real cache config; polls deleted page by page as channels land.

## 4. Accuracy rules (the non-negotiables)

1. **One serializer per shape** — REST and WS can never disagree (learned via
   `volumeNative` 24h-vs-lifetime; enforced already, keep it).
2. **One stamp** — every payload carries `as_of_block`; anything derived carries its
   inputs' stamp. No wall-clock windows over chain data.
3. **Single writer** — only the indexer writes derived state; API is read-only.
4. **Idempotent replay** — any block range can be re-applied without double counting
   (guards exist; keep `trade_exists`-class checks on every new accumulator).
5. **Explicit staleness** — if a source is down, serve the cache **and say so**
   (`stale: true`), never 500, never silently zero. A zero that means "unknown" is a
   lie (learned via snipers `0.0` and hardcoded stats).
6. **Echo what was applied** — filters/params echo back (`applied_filters`), so a
   client can detect a misspelled request instead of rendering an unfiltered answer.

## 5. Contract wishlist (next deploy, not blocking)

The current contracts are ~90% event-complete. For the last 10%:
- `FeeAccrued(user, quote, base)` on fill-time fee credit (or keep deriving — works).
- Events on `registerUser`, `setReferral` (old router variants are silent).
- `TriggerExecuted` should carry the resulting fill (currently stubbed anyway).
- Keep every governance setter emitting (contract.sol's `ParamsChanged` is right;
  the older factory/router silent setters are why V0-style TTL caches exist).

## 6. Build order

1. **Orderbook event decode** (both raw topics) + market OHLCV + open-orders state —
   unlocks: wez's remainingSize fix, goldsky retirement, `user_orders`/`user_fills`
   channels, `activeOrders` stat. Biggest single unlock, pure indexer work.
2. **LISTEN/NOTIFY hub** — removes the last polling loop server-side, cuts push
   latency to ~block time. Contained change (indexer commit hook + hub listener).
3. **Frame coalescing + private channels + ETag/304** on the existing socket.
4. **Client migration, page by page**: delete each poll as its channel lands; fix
   react-query caching; move quote/allowance/gas to intent-time.
5. **Spot portfolio** per the agreed design (balances endpoint → history buckets →
   stats), riding on 1–3.
6. Contract wishlist rides the next natural redeploy.

## 7. What this buys, concretely

Per active trade-page user today: ~3.3 RPC multicalls/s + 4 REST polls + 3 WS streams
+ goldsky ≈ **~250 requests/min**, most returning unchanged data.
Target: 1 WS frame per block actually consumed (~150/min, ~90% smaller each than a
multicall response), **zero** steady-state REST or RPC, cold loads 304 when nothing
moved. Roughly a 95% cut in requests and more in bytes, while *improving* freshness
(300ms poll → push at block cadence) and making every number carry the block it is
true at.
