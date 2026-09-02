# Crystal Backend — Agent Handbook

Everything an agent picking up this repo cold needs to know. Written for future
sessions, not for users.

> **This repo is PUBLIC on GitHub.** Never put credentials, connection strings,
> API keys, or private addresses in this file or anywhere else in the tree. A
> hardcoded twitterapi.io key leaked this way on 2026-08-29. Secrets live in
> `.env` (gitignored) and in Azure container app env vars.

---

## 1. Read this before you touch anything

These are the things that have actually caused incidents. They are not
hypothetical.

### The working tree is shared by many agents at once

Multiple Claude sessions edit this checkout **simultaneously**. There is one
working tree and one index.

- **Never `git add -A`, `git add .`, or `git commit -a`.** You will sweep another
  agent's half-finished work into your commit.
- Always stage explicit paths: `git add path/one.py path/two.py`.
- Before committing, run `git status --short` and confirm every file you are
  staging is actually yours.
- **`git stash` is dangerous here.** A stash/pop collided with another session's
  concurrent writes on 2026-09-01 and the pop aborted. Untracked files survive a
  stash (they are not included), which is the only reason nothing was lost.

If a file you need to commit *also* contains another agent's edits, stage only
your own hunks. Interactive `git add -p` is not available in this environment.
Use a zero-context patch instead:

```bash
git diff -U0 -- src/file.py > /tmp/full.patch
# filter the patch down to only the hunks containing your marker string
git apply --cached --unidiff-zero /tmp/mine.patch
```

A `-U3` patch will fail to apply (`patch does not apply`) because the context
lines have shifted under the other agent's changes. `-U0` plus
`--unidiff-zero` is what works.

### The live database is `crystal-prod-db-r3`

`crystal-prod-db` still exists and still answers queries. It is **stale and
misleading**. The old server was deleted 2026-08-30; `-r3` is the only real one.
If numbers look inexplicably wrong, check which host you are pointed at first.

### `--clean` means a FULL derived wipe

On the indexer, `--clean` does not mean "tidy up". It wipes all derived tables.
This caused an incident on 2026-08-28 that required a PITR recovery to `db-r3`.
Do not pass it casually.

### Never run two position/basis rewrites at once

Cost-basis and position rewrite scripts are not safe to run concurrently. They
will corrupt each other's chunk boundaries. Deploys are fine during a rewrite;
a second rewrite is not.

### Deploys to prod require manual human approval

`.github/workflows/deploy.yml` runs only on push to `main` (and
`workflow_dispatch`). It is gated on the GitHub `production` environment, which
has `required_reviewers` naming a single person.

**A push to main will sit in `waiting` indefinitely until that person clicks
approve.** A deploy sat unshipped for 5 hours this way on 2026-09-02. If a
deploy seems hung, check for a pending approval before assuming the build broke:

```bash
gh run list --limit 5
gh run view <run-id>
```

The `concurrency: deploy-prod` group means a stuck run blocks all later deploys.

### `az containerapp exec` times out on long commands

Long-running work over `az containerapp exec` dies around exit 255. Do not use it
to run backfills or repairs. Run those locally against the prod DB, or as a
detached process.

---

## 2. What this system is

Crystal is an on-chain exchange on **Monad mainnet (chain 143, ~400ms blocks)**.
The backend is the indexer plus the API that serves the frontend.

Four product surfaces share one indexer:

| Surface | What it is |
| --- | --- |
| Spot / CLOB | On-chain orderbook, `crystal_markets` + `crystal_market_trades` |
| AMM | Uniswap-V2-style pools attached to markets |
| Launchpad | Bonding-curve token launches (`crystal.fun`), graduates to V3 |
| Vaults | LP vaults and the MM vault, share-based accounting |

**Stack:** Python 3.12, asyncio, FastAPI, psycopg2. Lint is ruff and it is
enforced in CI — run `python -m ruff check .` before committing. Tests are
pytest: `python -m pytest tests/ -q` (should be ~314 passed, ~158 skipped).

**Hosting:** Azure Container Apps, resource group `crystal-prod-rg`, Japan East.
Two apps off one image: `crystal-api` and `crystal-indexer`. Registry is
`crystalprodacr`. Images are tagged with the full git SHA, so you can always map
a running revision back to a commit:

```bash
az containerapp show -g crystal-prod-rg -n crystal-api \
  --query "properties.template.containers[0].image" -o tsv
```

**Code style:** no comments anywhere, by standing instruction. Commit messages
are lowercase, one descriptive sentence, no Claude co-author trailers.

---

## 3. How indexing actually works

### Log fetching is topic-filtered, not address-filtered

This is the single most misunderstood thing in the codebase. The indexer
subscribes by **event topic**, not by contract address. That means logs from
*any* contract that emits a matching topic reach the sequencer.

`PASSTHROUGH_EVENT_TAGS = {"TF", "V3SWAP"}` — Transfer and V3 swap events from
arbitrary tokens are deliberately allowed through, because the transfer graph is
needed to attribute trades.

### Event tags

Defined in `core/chain.py` as `EVENT_SIGS`:

| Tag | Meaning |
| --- | --- |
| `TR` | Spot trade on the CLOB |
| `LT` | Launchpad trade |
| `NFB` / `NFS` | nad.fun buy / sell |
| `NFC` / `TC` | Token created |
| `V2SWAP` / `V3SWAP` | AMM swaps |
| `TF` | ERC-20 Transfer (passthrough) |
| `PSYNC` | Pool sync (reserves) |
| `MC` / `MPC` | Market created / market params changed |
| `PMINT` / `PBURN` | LP mint / burn |

### Trade attribution — `_resolve_trade_user`

`core/sequencer.py`. The problem: a trade event names **whoever called the
core**, which for a routed trade is a router or aggregator, not the trader. So
the sequencer walks the tx's ERC-20 transfer graph backwards to find the real
wallet.

- On a **buy**, it takes the address with the largest positive net token
  retention, preferring leaf nodes, then deepest-from-pool.
- On a **sell**, it takes the first sender that is not the pool, the zero
  address, or a known passthrough.

**Limitation that cannot be fixed by this approach:** arbitrage bots trade
through their *own contracts*. The EOA that signed the transaction never touches
the token, so it is nowhere in the transfer graph. Verified on-chain
2026-09-01: tokens flowed curve → settler → bot contract → back to curve, with
the signer absent entirely. The only thing naming a human is `tx.from`, which
costs an extra RPC per trade and is wrong for ERC-4337 (it returns the bundler).
Do not "fix" this by inventing a trader.

### Passthrough / router addresses

`core/chain.py` defines `PASSTHROUGH_ADDRS` — stateless execution contracts that
forward someone else's trade and never hold a position of their own.

```python
PASSTHROUGH_ADDRS = _addrs_from_env([...], "PASSTHROUGH_ADDRESSES")
```

Two enforcement points:

1. `_resolve_trade_user` skips them when walking the graph, so resolution lands
   past the hop instead of on it.
2. `BatchAccumulator.add_position_delta` **early-returns** for them. Every
   position write in the codebase funnels through that one method — launchpad
   trades, graduated trades, and the token-transfer path — so the guard covers
   all of them and any future path for free.

To add a new router: append to the list in `core/chain.py`, **or** set the
`PASSTHROUGH_ADDRESSES` env var (comma-separated) with no code change or
redeploy. Then clean up history with:

```bash
python scripts/repair_router_positions.py --apply --force
```

With no `--traders` argument it defaults to the same list. `--force` also drops
rows where the router has untraceable trades of its own; it still refuses to
delete any row holding a nonzero balance.

### EIP-7702 delegated wallets break naive contract checks

A delegated EOA **has code**, so `eth_getCode(addr) != "0x"` is not a valid
"is this a contract" test. A 7702 designator is exactly `0xef0100` + 20-byte
address = 48 hex chars. Exclude that shape explicitly.

This also affects gas: a plain transfer to a delegated EOA needs ~21,212 gas,
not 21,000. Estimate per destination rather than assuming.

---

## 4. Known-good addresses and infrastructure

Public, on-chain, safe to record here.

- gen-3 core / router: `0x6eb2aF5FC575689053Ac9b413220CaBfd01A2F9A`
- nad.fun v1 emitter: `0xA7283d07812a02AFB7C09B60f8896bCEA3F90aCE`
- nad.fun v2 emitter: `0x9f3832732923252A21044F21eE6bd87F09514ae4`
- 0x Settler: `0xC2D3689cF6ce2859a3ffBc8fE09ab4C8623766b8`
- 0x AllowanceHolder: `0x0000000000001fF3684f28c67538d4D072C22734`
- Referral manager: `0x1AB7ea187CEe63Cf01bBD8fa8837C748a769F8DF`

**nad.fun generations:** v1 and v2 have separate emitters. As of Aug 2026 create
activity shifted *back* to the v1 emitter. The `source` column is written per
generation and is correct — do not "fix" it.

**Cloudflare:** `api.crystal.exchange` needs a CF origin/SNI override pointing at
the Azure Container Apps FQDN. There is no ACA custom domain. Getting this wrong
produced a 522 outage on 2026-08-21.

**China:** mainland China cannot reach the RPC, so chain-only reads render as $0.
Backend-served endpoints are the fix; LP reads `/pools/positions` first for this
reason. Other chain-only paths are still exposed.

---

## 5. Gotchas that have burned people

### There are two duplicate token-overview handlers

`crystal.fun` is served by `fun.py`. There is a near-identical handler in
`launchpad.py`. **Editing `launchpad.py` alone does nothing** for crystal.fun.
Check which one the route actually hits.

### Vault ledger writes are two separate seams

Writing a vault flow row and updating the depositor counters are **two distinct
operations**. A backfill that calls only `insert_*` leaves the counters stale,
and the drift is invisible until someone reads the summary. If you write vault
flows, write both.

### `fees_usd` is the curve fee, not Crystal's cut

`fees_usd` records the bonding-curve fee. Crystal's 1% frontend cut is
**unindexed**. This is why `fees_usd` never equals 1% of volume. It is not a bug.

### Graduated pools are Uniswap V3, so V2SYNC never fires

Graduated launchpad tokens move to V3 pools, which do not emit the V2 sync event
the reserve tracker listens for. A reconciler was deployed 2026-09-01 to handle
this. Stale graduated prices are **not** a live problem any more.

### Transfers can carry cost basis

There is a basis-carrying transfer pipeline (live + history). A swap-leg bug
drained cost basis on 1CT sells and was fixed 2026-08-31 with a same-tx fix plus
a 174,733-row repair. Rows where `sold > bought` are now legitimate.

---

## 6. Operational runbook

### Is the indexer healthy?

Compare the newest indexed block against the chain head. Anything within a few
hundred blocks is live (400ms blocks, so 50 blocks ≈ 20s).

```sql
SELECT max(block_number) FROM launchpad_trades;
```

Do **not** use `crystal_market_trades` for this — spot volume is genuinely low,
so it can sit tens of thousands of blocks behind while the indexer is perfectly
current.

### Is anything rewriting right now?

```sql
SELECT pid, state, now() - query_start AS runtime, left(query, 120)
FROM pg_stat_activity
WHERE datname = current_database() AND pid <> pg_backend_pid() AND state <> 'idle'
ORDER BY query_start;
```

Reads are normal. Long-running writes mean a rewrite is in flight — do not start
another.

### Health endpoints

`GET /health` returns `{"ok":true}`. There is no `/status`.

---

## 7. Repair scripts

Live in `scripts/`. All default to a **dry run**; pass `--apply` to commit.

| Script | Purpose |
| --- | --- |
| `repair_router_positions.py` | Drop phantom positions held by passthrough routers. `--force` also drops rows with untraceable trades. Balance guard always applies. |
| `repair_trade_traders.py` | Re-attribute mis-credited trades. `--table {launchpad_trades,crystal_market_trades}`, `--token`, `--traders`, `--scan-top`. |
| `rebuild_basis_with_transfers.py` | Cost-basis rebuild including transfer legs. **Never run concurrently with another rewrite.** |
| `vault_reconcile.py` | Cross-checks flow ledger, indexer tables and chain state; asserts share and value conservation. |

`crystal_market_trades` has **no `token` column** — it keys off `market`. Join
`crystal_markets` to get the base token.

---

## 8. Testing

```bash
python -m pytest tests/ -q      # ~314 passed, ~158 skipped
python -m ruff check .          # must be clean, CI enforces it
```

Skipped tests are usually ones needing a live DB or RPC. A large skip count is
normal, not a problem.

When you fix an indexer bug, add a test that pins the *behaviour*, not the
implementation. `tests/test_passthrough_positions.py` is a good model: it
asserts both that a passthrough accumulates nothing and that adding an address
to the list drops it, so the extension mechanism itself is covered.

Write the test to assert what the code *should* do, then let it tell you if you
were wrong. On 2026-09-01 a test written to assert "arb bot resolves to nothing"
failed and revealed the resolver correctly lands on the bot's own contract —
which was better behaviour than the assertion. The test was wrong, not the code.

---

## 9. Deployment

1. Push to `main`.
2. `.github/workflows/deploy.yml` fires, builds in ACR (~30s), deploys indexer,
   waits for health, deploys api, smoke checks. Full run ~2-3 minutes.
3. **It will block on the `production` environment approval gate.** Someone must
   approve it in the GitHub UI.

`dev` should track `main`. If `dev` is strictly behind with nothing ahead, it
needs a fast-forward, not a PR:

```bash
git push origin main:dev
```

`origin/block-scoped-clear` is a stale branch (last touched 2026-08-05, heavily
diverged, empty merge-base). Leave it alone.

---

## 10. Frontend contract notes

The frontend is a separate repo (`Crystal-Interface`, deployed at
**app.crystal.exchange**, not crystal.exchange). Pushes to its main auto-deploy.

Things the backend must keep in mind:

- The WS channels the frontend depends on include `tracked_trades`. A
  subscriber's first positions frame is labelled a snapshot so the client
  replaces its set rather than merging into stale rows from an earlier
  connection.
- Backend-served data should power everything possible, because chain-only reads
  break in mainland China.
- Chart series are median-bucketed with **both ends anchored** — the first and
  last points must be the true first and last samples, or PnL baselines shift.

---

## 11. History worth knowing

Condensed incident log. Details matter because these recur.

- **2026-08-21** — 522 outage from a missing Cloudflare origin override.
- **2026-08-28** — gen-3 contract migration; `--clean` wiped derived tables,
  recovered by PITR to `db-r3`.
- **2026-08-29** — public-repo key leak (twitterapi.io). The X/Twitter tracker
  has been returning 402 since; socials are frozen.
- **2026-08-30** — settler transfer-leg drained cost basis on 1CT sells;
  same-tx fix plus 174,733-row repair.
- **2026-08-31** — points/rewards engine shipped backend-only. Weekly close is
  Wednesday 12am PT with a `^0.8` curve.
- **2026-09-01** — graduated-pool reserve reconciler deployed (149 rows merged);
  cost-basis chunk-boundary bug fixed; vault summary endpoint taken from
  6.7-12.1s to ~0.9s by reducing balance samples in SQL.
- **2026-09-02** — passthrough router guard added; 10 phantom settler positions
  and ~1,000 phantom router positions removed.

---

## 12. Open items / known gaps

- No API authentication, open CORS, no rate limits. The repo is public.
- The X/Twitter tracker is dead (402) until the API key is replaced.
- Referrals: contract fee mechanics exist, frontend partially wired, **no live
  data services**. Effectively a rebuild.
- Arb-bot trades routed through bot-owned contracts cannot be attributed to a
  human. Accepted limitation, documented above.
