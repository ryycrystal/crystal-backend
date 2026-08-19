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
Three fixed buckets, 30 rows each, fully enriched (see §5 row shape):
`recent_created`, `recent_approaching`, `recent_graduated`, plus `as_of_block`.

Optional params:
- `since_block=<n>` — delta mode: buckets contain **only tokens touched after n** (traded/created/migrated), plus `ids` = full current membership per bucket (drop anything you hold that isn't in `ids`). ~97% smaller when quiet.
- `exclude_dev`, `exclude_ca`, `exclude_website`, `exclude_twitter` — comma-separated blacklists (see §4).
- `filters=<url-encoded JSON>` — **per-bucket filters**, one call:
  `{"new":{"marketcap_min":20},"approaching":{...},"graduated":{"marketcap_min":200}}`
  Each bucket accepts any §3 filter plus `query`/`sort`. Response adds `<bucket>_total` and per-bucket `applied_filters`.

### GET|POST `/search/query`
One filter set over the full token universe. GET params or POST JSON body (identical names; use POST when blacklists exceed ~300 entries — the ingress rejects URLs past ~17KB).
Returns `{results:[rows], count, total, limit, offset, query, sort, applied_filters}`.
`total` is the honest full-universe match count.

## 2. Token detail (one call per page)

### GET `/token/{addr}/{res}?series=&tracked=`
The board-page aggregate. `res` ∈ 1,5,15,60,300,900,3600,14400,86400 (seconds).
Contains: header fields, `series.klines` + `mini.klines` (24 hourly bars), `trades` (50), holders, `topTraders`, `devTokens`, `snipers`, **`stats`** (identical to `/stats/{addr}` — don't call it separately), **`fees`** (§6), `sourceRaw`, `graduationPercentageBps`.
`?series=false` omits the big kline array. `?tracked=a,b` adds `trackedtrades` for those wallets.
Candles: `time` = bucket start, unix **seconds**, strings. Each candle's `open` is stitched to the previous close server-side; high/low envelope it.

### GET `/token/{addr}/meta`
Everything stored for one token, unshaped: `raw` = every DB column verbatim, plus `source` (wire: 1 = any nad.fun), `sourceRaw` (0 native / 1 nadfun-v1 / 2 nadfun-v2), `nadfunVersion`, `phase`, `progressBps`, `fees`, `as_of_block`. 404 if unknown. Built for the terminal: one read, no chain calls before a swap.

### Others
- `/stats/{addr}` — windowed stats: `change_pct_5m/1h/6h/24h`, `price_ref_*`, per-window buy/sell counts + USD volumes. Windowed values live **only** here (and inside the overview's `stats`).
- `/holders/{addr}` — holder list with PnL fields, USD values.
- `/token/{addr}/trades?limit=&before=` — range-queryable trades. Trade id = `txhash-logIndex` (decimal logIndex).
- `/chart/{addr}/{res}` — klines only, same stitched builder as the overview.
- `/user/{addr}`, `/portfolio/{addr}[...]` — per-wallet positions/summary. Spot portfolio is its own one-call endpoint.

## 3. Filters (used by `/search/query` and per-bucket in `/tokens?filters=`)

Ranges (`_min`/`_max` each): `price` (native, compares against `price_quote`), `marketcap` (USD), `volume_24h` (USD — **note: currently bound to lifetime volume**, name predates the window), `fees` (USD, lifetime), `holders`, `top10`, `dev_holding`, `sniper_holding`, `insider_holding` (all percent of 1e27 supply), `pro_traders` (count: wallets with realized PnL > 0 and ≥10 trades), `buy_tx`, `sell_tx`, `age` (**minutes**).
Booleans: `has_website`, `has_twitter`, `has_telegram`, `has_discord`.
Strings: `query` (name/symbol/description/CA substring), `phase` (`new|graduating|graduated`), `source` (`0` = crystal native, `1` = nad.fun both generations), `sort` (`mc|volume_24h|volume_1h|holders|recent`).

## 4. Blacklists

`exclude_dev` (creator addr), `exclude_ca` (token addr), `exclude_website` (host), `exclude_twitter` (handle). Both list endpoints. Normalisation is applied server-side to your values too: `@handle`, bare handle, or full x.com/twitter.com URL are equivalent; websites reduce to bare host (scheme/www/path stripped). Buckets are backfilled after exclusion — never a short column.

## 5. List row shape — which window is which

Every row in `/tokens` and `/search/query` carries:
- **Lifetime**: `volume_usd`, `native_volume`, `token_volume`, `fees_usd`, `tx` {buy,sell,total}.
- **Windowed**: `change_pct_24h` (null until the token is 24h old), `change_pct_since_launch` (null only if never traded → render both nulls as a dash, never 0).
- **Current-state**: `price_quote`, `marketcap_*`, `holders`, `top10_holding`, `developer_holding`, `snipers{count,addresses,holdingShare}`, `insider_holding` (raw; % = /1e25), `pro_traders`, `progressBps`, `phase`, `migrated`, `market`, `source`, `sourceRaw`, `nadfunVersion`, metadata/socials, `fees` (§6).
Percent basis for all holding fields: raw balance / 1e25 = percent of the 1e9-token supply.

## 6. Fees

`fees` object (identical on list rows, overview, and `/token/{addr}/meta`):
- `curveFeeRate` — fraction charged per bonding-curve trade (`0.01` v1, `0.02` v2, `null` for native).
- `pair` — for nad.fun tokens with a DEX pair: `{pair, ok, feeCollector, baseToken, quoteToken, creatorFeeRate, curveProtocolFeeRate, dexProtocolFeeRate, fetchedAt}`. `ok:false` = the pair has no fee collector (plain pair). Swap fee = `creatorFeeRate + dexProtocolFeeRate`; **never** use `curveProtocolFeeRate` for DEX swaps.
- `crystalMarket` — for native tokens with a market: `{market, takerFee}` (fee = (100000-takerFee)/100000).

### GET `/pair/{pair}/fees`
Same object for an arbitrary pair address, cached server-side (1h TTL, fetch-on-miss). Use for pairs you don't have a token row for.

## 7. WebSocket `/ws`

One socket per app. Client protocol:
```jsonc
{"op":"subscribe","token":"0x…","channels":["token","stats","trades","holders","positions","top_traders","dev_tokens"],"addresses":["0xwallet"]}
{"op":"subscribe","token":"tokens","channels":["tokens"]}   // the explorer list — literal pseudo-token "tokens"
{"op":"unsubscribe","token":"…","channels":[…]}             // omit channels = all for that token
{"op":"ping"} -> {"op":"pong"}                              // send every ≤25s; silent sockets are dropped at 300s
{"op":"query","id":123,"filters":{…§3…,"limit":50}} -> {"op":"query_result","id":123, …same shape as /search/query}
```
Every data frame: `channel`, `token`, `kind` (`snapshot|delta`), `seq`, `as_of_block`.
On subscribe you always get a full `snapshot`; deltas follow only when data changed (max 1 frame / 400ms block tick).

Channels:
- `tokens` (the explorer): snapshot = full `/tokens` response. Delta = `{new:[full rows], u:{addr:{changed fields only}}, gone:[addrs], ids:{bucket membership}}`. Apply patches over held rows; drop rows absent from `ids`. New tokens arrive as complete, quick-buy-ready rows ~0.5–1s after on-chain create.
- `token` — per-trade half of the detail page (24h `volumeNative`/`buyTxs`/`sellTxs` matching REST; lifetime under `*Lifetime` keys). `stats` — the `/stats` body. `trades` — `{added:[…]}` append-only; ignore any notion of removal. `holders`/`top_traders` — `{upserts, removed}` keyed by address (top_traders ranked by PnL). `positions` — wallet-scoped; `addresses` array on subscribe **replaces** the set. `dev_tokens` — creator's launches.

**Filters never touch the socket.** The `tokens` channel is unfiltered; apply your active filter predicates to incoming rows client-side (every filterable field is on the row), and run one `query` op (or REST call) on filter commit for full-universe discovery. Adding/changing a filter = **no disconnect, no resubscribe, nothing** — the subscription is filter-agnostic by design.

Reconnect contract (client already implements): ping ≤25s; on close reconnect with jittered backoff and resubscribe everything; every resubscribe yields a fresh snapshot so missed state heals wholesale; `seq` restarting at 1 on a new connection is normal (per-connection numbering); a `seq` gap mid-connection = refetch once. Drops are expected on backend deploys — recovery is the mechanism, not drop-avoidance.

## 8. Ops
`/health`, `/openapi.json` (full route list), `/sync`, `/debug/mon_price`. Pools/markets/vaults: `/pools/list`, `/pools/{addr}`, `/markets/list`, `/vaults/*`. Pool `apy24h`/`dailyYield24h` = invariant growth per share (wash-resistant), not fee×volume.
