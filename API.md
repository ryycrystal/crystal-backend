# Crystal Backend — API & WebSocket Reference

Base URL: `https://crystal-api.yellowfield-3f176fc9.japaneast.azurecontainerapps.io`
WebSocket: same host, path `/ws`.

Rules that apply everywhere:
- Addresses are lowercase hex on the wire. Send them in any case; matching is case-insensitive but **exact** (no prefixes).
- Big numbers (wei balances, raw volumes) are **strings**. Prices/percentages are trimmed decimal strings. Never parse them as JS floats if precision matters.
- `as_of_block` = the indexer watermark the data reflects. `seq` (WS only) = per-connection, per-channel frame counter, contiguous — a skipped number means you missed a frame.
- Unknown/misspelled query params are **silently ignored**. Every filtered response echoes `applied_filters`; assert that everything you sent is in it, otherwise you got an unfiltered result that looks filtered.

---

## 1. Token lists

### GET `/tokens`
Three fixed buckets, 30 rows each, fully enriched (see §6 row shape):
`recent_created`, `recent_approaching`, `recent_graduated`, plus `as_of_block`.

Optional params:
- `since_block=<n>` — delta mode: buckets contain **only tokens touched after n** (traded/created/migrated), plus `ids` = full current membership per bucket (drop anything you hold that isn't in `ids`). ~97% smaller when quiet.
- `source=0|1` — optional launchpad source filter. `0` returns only native Crystal launches; `1` returns nad.fun launches across supported generations.
- `exclude_dev`, `exclude_ca`, `exclude_website`, `exclude_twitter` — comma-separated blacklists (see §5).
- `filters=<url-encoded JSON>` — **per-bucket filters**, one call:
  `{"new":{"marketcap_min":20},"approaching":{...},"graduated":{"marketcap_min":200}}`

### GET `/tokens/feeds`

Independent launchpad discovery rankings. Params: `source=0|1`, `limit=1..100`.

- `trending`: highest rolling 24-hour native-token volume.
- `new`: newest non-graduated launches by creation time.
- `near_graduation`: non-graduated launches closest to graduation.
- `graduated`: most recently graduated launches.

Rows include `volume_native_24h` and its frontend-compatible alias `volumeNative`, both in raw wei.
  Each bucket accepts any §4 filter plus `query`/`sort`. Response adds `<bucket>_total` and per-bucket `applied_filters`.

### GET|POST `/search/query`
One filter set over the full token universe. GET params or POST JSON body (identical names; use POST when blacklists exceed ~300 entries — the ingress rejects URLs past ~17KB).
Returns `{results:[rows], count, total, limit, offset, query, sort, applied_filters}`.
`total` is the honest full-universe match count.

## 2. Token detail (one call per page)

### GET `/token/{addr}/{res}?series=&tracked=`
The board-page aggregate. `res` ∈ 1,5,15,60,300,900,3600,14400,86400 (seconds).
Contains: header fields, `series.klines` + `mini.klines` (24 hourly bars), `trades` (50), holders, `topTraders`, `devTokens`, `snipers`, **`stats`** (identical to `/stats/{addr}` — don't call it separately), **`fees`** (§8), `sourceRaw`, `graduationPercentageBps`.
`?series=false` omits the big kline array. `?tracked=a,b` adds `trackedtrades` for those wallets.
Candles: `time` = bucket start, unix **seconds**, strings. Each candle's `open` is stitched to the previous close server-side; high/low envelope it.

### GET `/token/{addr}/meta`
Everything stored for one token, unshaped: `raw` = every DB column verbatim, plus `source` (wire: 1 = any nad.fun), `sourceRaw` (0 native / 1 nadfun-v1 / 2 nadfun-v2), `nadfunVersion`, `phase`, `progressBps`, `fees`, `as_of_block`. 404 if unknown. Built for the terminal: one read, no chain calls before a swap.

**Reserves.** `reserveQuote`/`reserveBase` (wei strings) are the reserves of the venue that actually holds the liquidity right now, plus `reservesFrom` naming it and `reservesSyncedAt`:
- `curve` — still bonding, these are the curve reserves (`raw.curve_*`).
- `pair` — graduated to a nad.fun AMM pair, streamed from its `Sync` log.
- `crystal_pool` — a native graduate trading on a Crystal AMM market.

Curve reserves **freeze at graduation**, so never read `raw.curve_native_reserve` directly for a migrated token — use these fields. The same four keys appear on `/token/{addr}/{res}` and on every list row (§5).

### Others
- `/stats/{addr}` — windowed stats: `change_pct_5m/1h/6h/24h`, `price_ref_*`, per-window buy/sell counts + USD volumes. Windowed values live **only** here (and inside the overview's `stats`).
- `/holders/{addr}` — holder list with PnL fields, USD values.
- `/token/{addr}/trades?limit=&before=` — range-queryable trades. Trade id = `txhash-logIndex` (decimal logIndex).
- `/chart/{addr}/{res}` — klines only, same stitched builder as the overview. Before the first trade, both chart responses contain one zero-volume flat point at the token's creation price so clients can initialize immediately.
- **Portfolio scope**: the backend only carries wallets that have interacted with
  Crystal (any indexed position, orderbook order, LP share, or vault deposit).
  `/spot/{wallet}` answers `supported: false` with empty rows/graph for anyone
  else — no RPC is spent on them, and clients should render "no activity", not
  zeros. The cheap DB endpoints simply return empty for unknown wallets.
- `/user/{addr}` — per-wallet positions + summary. Rows carry `realized/unrealized/total_pnl_native` and `last_price_native` — render these, never re-derive. `?include_native=1` adds `native_balance` (wei string, null on RPC failure, `native_stale` flag) so spectator views need no client RPC.
- `/user?addresses=a,b&merged=1` — one combined position list summed per token across up to 100 wallets in a single query (`wallet_count` per row); unmerged form unchanged (max 25).
- `/portfolio/{addr}[...]` — summary, paginated `/positions`, `/history` (real per-trade history — use for the History tab), and `/daily?days=N`: per-UTC-day `realized_pnl_native` (same average-cost basis as the position columns), `volume_native/usd`, `buy/sell_volume_native`, trade/buy/sell counts. Feeds the PnL calendar and realized-PnL chart.
- `/volume/{addr}` — now also `volume_usd`, summed at each trade's own price.
- `/pools/positions/{addr}` — LP share positions from indexed transfers; no more per-pool chain reads. `{ok, user, count, positions[]}`.
- `/pools/{pool}/liquidity?user=&limit=100` — LP deposit/withdrawal history for a pool, optionally scoped to one wallet: `{ok, market, count, events[]}`.
- `/pools/{pool}/preview?quote=&base=&shares=` — add/remove previews computed from indexed reserves, so quoting a deposit costs no RPC. Returns `{detail}`.

## 3. Orderbook (spot CLOB) — served from decoded order state

These replaced the goldsky subgraph for OrderCenter. Goldsky remains a client-side
fallback for spot only.

**Order identity is `(market, price, order_id)`, not `(market, order_id)`.** Native
order ids are per-price-level counters, so they repeat across levels. Client ids are
`(cloid << 41) | userId`.

**Multi-wallet.** Every route below takes `?addresses=a,b,c` (max **16**) and answers
across the whole set; the path wallet is the fallback when it is absent. Responses
echo both `wallet` (the first) and `wallets` (the full set).

**Staleness gate.** If the indexer is more than 300s behind head these routes return
**503** `{"detail":"indexer is catching up, serve from fallback"}` rather than an
empty 200 — an empty 200 reads as "no orders" and would stop a client falling back.
Handle 503 by using the fallback, not by retrying.

### GET `/orderbook/open/{wallet}?market=&addresses=`
Open resting orders. `{wallet, wallets, orders[], count, as_of_block}`.
Row: `market, order_id, cloid, is_buy, price, size, original_size, filled_size,
status, txhash, created_block, created_ts, updated_block, updated_ts`.
`original_size` shrinks on decrease; `filled_size` accumulates on fills — a cancelled
order shows what it actually filled, never 100%.

### GET `/orderbook/orders/{wallet}?market=&limit=200&before_ts=&addresses=`
Every order ever owned with current status (open, cancelled, filled). Same row shape.
Pages backwards via `next_before_ts`.

### GET `/orderbook/trades/{wallet}?market=&limit=100&before_ts=&addresses=`
Taker trades and maker fills merged, newest first. `{…, trades[], next_before_ts}`.
Row: `kind, txhash, log_index, block_number, timestamp, market, is_buy, amount_in,
amount_out, start_price, end_price, order_id, legs`.
`legs` = how many fills were merged into one batch-tx row.

### GET `/orderbook/history/{wallet}?market=&limit=100&before_ts=&addresses=`
Order lifecycle events. `{…, events[], next_before_ts}`.
Row: `action` (place/cancel/decrease/fill), `txhash, log_index, entry_index,
block_number, timestamp, market, is_buy, price, order_id, size`.

### GET `/activity/{wallet}?limit=50&before_ts=&addresses=`
One feed of everything a wallet did — launchpad buys/sells plus vault deposits and
withdrawals, newest first. `{wallet, wallets, items[], count, next_before_ts}`.
Row: `type` (`buy|sell|vault_deposit|vault_withdraw`), `timestamp, blockNumber,
txhash, subject, symbol, name, amountNative, amountToken, priceNative, usdAmount`.
Fee claims are **not** here — no claim event is indexed yet, only the running
claimable balance on `/referral/{address}`.

### GET|PUT `/wallet-prefs/{key}`
Cross-device sync for derived-wallet selection. The key is a **64-char lowercase hex**
hash the client derives; address-shaped keys are rejected (400). The server stores a
count and which indices are selected — **never an address, never a key** — and has no
way to tell whose record it is.
GET → `{count, selected[], updatedAt}` (zeros when unset).
PUT body `{count, selected[]}`, both bounded by 16 → echoes the stored record.

### GET `/mon-usd/series?from_ts=&to_ts=&resolution=60`
MON priced in USD over time, so a chart can be denominated in USD using the rate from
**each candle's own moment** rather than today's rate.
`{resolution, from, to, before, points:[{t, rate}]}`.
`before` = last known rate at or before the range, so the first candles still convert
when no MON trade landed in their bucket. The feed only carries a point where MON
itself traded, so it is sparse — carry the last value forward. A zero-width range is
widened to one bucket rather than rejected; ranges over 5000 buckets are a 400.

**Converting a chart to USD: convert first, then stitch.** Each bar converts at its
own rate, so matching a bar's open to the previous close *before* converting leaves it
somewhere else in USD as soon as the rate moves — which is most bars. Doing it in the
wrong order broke 42% of candles on a 429-bar window.

---

## 4. Filters (used by `/search/query` and per-bucket in `/tokens?filters=`)

Ranges (`_min`/`_max` each): `price` (native, compares against `price_quote`), `marketcap` (USD), `volume_24h` (USD — **note: currently bound to lifetime volume**, name predates the window), `fees` (USD, lifetime), `holders`, `top10`, `dev_holding`, `sniper_holding`, `insider_holding` (all percent of 1e27 supply), `pro_traders` (count: wallets with realized PnL > 0 and ≥10 trades), `buy_tx`, `sell_tx`, `age` (**minutes**).
Booleans: `has_website`, `has_twitter`, `has_telegram`, `has_discord`.
Strings: `query` (name/symbol/description/CA substring), `phase` (`new|graduating|graduated`), `source` (`0` = crystal native, `1` = nad.fun both generations), `sort` (`mc|volume_24h|volume_1h|holders|recent`).

## 5. Blacklists

`exclude_dev` (creator addr), `exclude_ca` (token addr), `exclude_website` (host), `exclude_twitter` (handle). Both list endpoints. Normalisation is applied server-side to your values too: `@handle`, bare handle, or full x.com/twitter.com URL are equivalent; websites reduce to bare host (scheme/www/path stripped). Buckets are backfilled after exclusion — never a short column.

## 6. List row shape — which window is which

Every row in `/tokens` and `/search/query` carries:
- **Lifetime**: `volume_usd`, `native_volume`, `token_volume`, `fees_usd`, `tx` {buy,sell,total}.
- **Windowed**: `change_pct_24h` (null until the token is 24h old), `change_pct_since_launch` (null only if never traded → render both nulls as a dash, never 0).
- **Current-state**: `price_quote`, `marketcap_*`, `holders`, `top10_holding`, `developer_holding`, `snipers{count,addresses,holdingShare}`, `insider_holding` (raw; % = /1e25), `pro_traders`, `progressBps`, `phase`, `migrated`, `market`, `source`, `sourceRaw`, `nadfunVersion`, metadata/socials, `fees` (§8).
Percent basis for all holding fields: raw balance / 1e25 = percent of the 1e9-token supply.

## 7. Referrals, tiers, leaderboard

### GET `/referral/{address}`
The whole referrals page in one call: `{ok, address, referrer, referredAt,
referredCount, referees[], rewards, totalClaimableUsd, totalEarnedUsd}`.

### GET `/tiers`
The configured ladder alone, for pages with no wallet connected:
`{ok, windowDays, tiers[]}`. The `volume_tiers` table is SQL-editable — tiers can be
renamed or rethresholded without a deploy.

### GET `/tiers/{address}`
One wallet's trailing volume and the tier it earns:
`{ok, address, windowDays, volumeUsd, tradeCount, tier, nextTier, remainingUsd,
progressBps, tiers[]}`. Volume is launchpad USD volume over `windowDays`.

### GET `/leaderboard?search=&limit=100`
`{users[]}` ranked by pnl / volume / winrate.

### GET `/trades/{addresses}`
Recent trades for one or more comma-separated wallets: `{addresses, count, trades[]}`.

---

## 8. Fees

`fees` object (identical on list rows, overview, and `/token/{addr}/meta`):
- `curveFeeRate` — fraction charged per bonding-curve trade (`0.01` v1, `0.02` v2, `null` for native).
- `pair` — for nad.fun tokens with a DEX pair: `{pair, ok, feeCollector, baseToken, quoteToken, creatorFeeRate, curveProtocolFeeRate, dexProtocolFeeRate, fetchedAt}`. `ok:false` = the pair has no fee collector (plain pair). Swap fee = `creatorFeeRate + dexProtocolFeeRate`; **never** use `curveProtocolFeeRate` for DEX swaps.
- `crystalMarket` — for native tokens with a market: `{market, takerFee}` (fee = (100000-takerFee)/100000).

### GET `/pair/{pair}/fees`
Same object for an arbitrary pair address, cached server-side (1h TTL, fetch-on-miss). Use for pairs you don't have a token row for.

## 9. WebSocket `/ws`

One socket per app. Client protocol:
```jsonc
{"op":"subscribe","token":"0x…","channels":["token","stats","trades","holders","positions","top_traders","dev_tokens"],"addresses":["0xwallet"]}
{"op":"subscribe","token":"tokens","channels":["tokens"]}   // the explorer list — literal pseudo-token "tokens"
{"op":"subscribe","token":"portfolio","channels":["user_positions"],"addresses":["0xmain","0xsub1"]}  // wallet-scoped, all tokens — literal pseudo-token "portfolio"
{"op":"unsubscribe","token":"…","channels":[…]}             // omit channels = all for that token
{"op":"ping"} -> {"op":"pong"}                              // send every ≤25s; silent sockets are dropped at 300s
{"op":"query","id":123,"filters":{…§4…,"limit":50}} -> {"op":"query_result","id":123, …same shape as /search/query}
```
Every data frame: `channel`, `token`, `kind` (`snapshot|delta`), `seq`, `as_of_block`.
On subscribe you always get a full `snapshot`; deltas follow only when data changed (max 1 frame / 400ms block tick).

Channels:
- `tokens` (the explorer): snapshot = full `/tokens` response. Delta = `{new:[full rows], u:{addr:{changed fields only}}, gone:[addrs], ids:{bucket membership}}`. Apply patches over held rows; drop rows absent from `ids`. New tokens arrive as complete, quick-buy-ready rows ~0.5–1s after on-chain create.
- `token` — per-trade half of the detail page (24h `volumeNative`/`buyTxs`/`sellTxs` matching REST; lifetime under `*Lifetime` keys). Also carries `curveNativeReserve`/`curveTokenReserve` and `poolNativeReserve`/`poolTokenReserve`. **The pool pair resolves across both venues**: a nad.fun token graduates into an AMM pair (`launchpad_pools`), a Crystal-launchpad token graduates into a Crystal market (`crystal_pools`); the frame coalesces them so a client never has to know which applies. Both are 0 pre-graduation.
- `stats` — the `/stats` body.
- `trades` — `{added:[…]}` append-only; ignore any notion of removal.
- `holders`/`top_traders` — `{upserts, removed}` keyed by address (top_traders ranked by PnL).
- `positions` — wallet-scoped; the `addresses` array on subscribe **replaces** the set.
- `user_positions` — like `positions` but across **every** token for the wallet set (pseudo-token `portfolio`); rows carry the full `/user` row shape keyed `address:token`, so the portfolio page can drop its positions poll.
- `dev_tokens` — creator's launches.
- `balances` — wallet token balances.
- `vaults` — vault state for the subscribed wallet set.
- `user_orders` / `user_trades` / `user_history` — the live half of the orderbook REST
  routes above (pseudo-token `portfolio`, wallet-scoped via `addresses`). All three
  share one push shape: a per-wallet body resent only when it **materially** changed,
  so a quiet book produces no frames. Bodies match `/orderbook/open`, `/orderbook/trades`
  and `/orderbook/history` respectively. Subscribe to these instead of polling; the REST
  routes are for first paint and pagination.

**Filters never touch the socket.** The `tokens` channel is unfiltered; apply your active filter predicates to incoming rows client-side (every filterable field is on the row), and run one `query` op (or REST call) on filter commit for full-universe discovery. Adding/changing a filter = **no disconnect, no resubscribe, nothing** — the subscription is filter-agnostic by design.

A reserve-only change (liquidity added or removed with no swap) updates the stored
value but does not itself trigger a `token` push — the corrected numbers ride the next
frame rather than arriving instantly.

Reconnect contract (client already implements): ping ≤25s; on close reconnect with jittered backoff and resubscribe everything; every resubscribe yields a fresh snapshot so missed state heals wholesale; `seq` restarting at 1 on a new connection is normal (per-connection numbering); a `seq` gap mid-connection = refetch once. Drops are expected on backend deploys — recovery is the mechanism, not drop-avoidance.

## 10. Ops & misc
`/health` → `{ok}`. `/sync` → `{last_block}`. `/debug/mon_price` — the cached MON/USD
reference price used across the backend. `/openapi.json` — machine-readable route list,
authoritative over this document if the two ever disagree.
`/integrity` reports the indexer's self-check: `ok`, `last_block`,
`seconds_since_last_block`, and the last sweep (processed gaps, cache holes,
head lag, stall) — alert on `ok: false`. Pools/markets/vaults: `/pools/list`, `/pools/{addr}`, `/markets/list`, `/vaults/*`.
Pool `apy24h`/`dailyYield24h` = invariant growth per share (wash-resistant), not fee×volume.

`GET /x?url=…` proxies X (twitter) user/tweet/community URL resolution through the
upstream API, server-cached. `POST /x?clear=1` clears that cache. Needs `X_BEARER_TOKEN`
in the environment or it answers 500.

### Full route inventory (45)
Token lists `/tokens`, `/tokens/feeds`, `/search/query` (GET+POST) · Token detail `/token/{a}/{res}`,
`/token/{a}/meta`, `/token/{a}/trades`, `/chart/{a}/{res}`, `/stats/{a}`, `/holders/{a}`,
`/pair/{p}/fees` · Portfolio `/user`, `/user/{a}`, `/spot/{w}`, `/portfolio/{a}`,
`/portfolio/{a}/positions`, `/portfolio/{a}/history`, `/portfolio/{a}/daily`,
`/volume/{a}`, `/trades/{addrs}`, `/leaderboard` · Orderbook `/orderbook/open|orders|trades|history/{w}`,
`/activity/{w}`, `/wallet-prefs/{k}` (GET+PUT), `/mon-usd/series` · Referrals `/referral/{a}`,
`/tiers`, `/tiers/{a}` · Pools `/pools/list`, `/pools/{a}`, `/pools/positions/{a}`,
`/pools/{p}/liquidity`, `/pools/{p}/preview` · Vaults `/vaults/list`, `/vaults/{a}/{u}`,
`/vaults/{a}/history/{tf}`, `/vaults/{a}/refresh-balance` (POST) · Markets `/markets/list` ·
Ops `/health`, `/sync`, `/integrity`, `/debug/mon_price` · Misc `/x` (GET+POST) ·
WebSocket `/ws`.
