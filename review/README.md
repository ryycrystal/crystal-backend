# Review round 3: shared brief

Read this first, then your own brief (`reviewer3.md` … `reviewer6.md`). One agent per brief.

## What you are reviewing

Branch `accounting-fix` in `C:/Users/ryanl/OneDrive/Desktop/projects/crystal-backend-accounting-fix`,
pushed to `origin/accounting-fix`, HEAD `f913de9`, 43 commits ahead of `origin/main` and 19 behind.

It is a new position ledger that replaces the current engine's approach to holdings, cost basis and PnL.
Instead of interpreting individual trade events, it nets each transaction into per-wallet flows, classifies
every address as a wallet or a venue, and folds flows into positions with cost basis in three confidence
states: `observed` (the quote leg was seen on chain), `estimated` (trade shape known, quote priced at a
reference), `unresolved` (tokens moved with no cost evidence; no number is invented).

Start with `POSITION_LEDGER_PLAN.md` (the design), then `COMPLETE.md` (what the author claims was verified),
then the code: `core/ledger/{types,netflow,fold,kinds,engine,store,schema,txmeta,receipts}.py`,
`core/sequencer.py` (the wiring, guarded by `LEDGER_ENABLED`), and `scripts/ledger_{replay,check,verify,chain_logs}.py`.

## What already happened, so you do not repeat it

Two reviews exist in the repo root. **Read both before starting.**

- `feedback.md` (09-07 11:38). Verified the suite, the flag-off behaviour, and that the ledger beats the
  currently-deployed engine on CHIPOTLE and moncock against hand-derived numbers. Lists blockers before cutover.
- `feedback2.md` (09-07 13:01). Fourteen findings, twelve P1, with independent reproductions. Verdict:
  keep as a shadow prototype; do not treat the `REVIEW` marker or the three fixture checks as acceptance.
  Headline claims: transaction-wide netting deletes intra-transaction actions; opposite-sign movements are
  taken as proof of a purchase; an unresolved sale books zero proceeds and realizes a false loss; basis is
  lost when tokens change wallets; flow identity (`sub_index`) is not stable across registry changes, with
  357 duplicate `(txhash, wallet, token)` groups in the side database; scoped replays write partial
  positions for tokens that were not the subject of the replay.

The two reviews differ in tone. Part of this round's job is to settle that, not to add a third opinion.

## Ground truth and how to check things

The chain is the arbiter. Production's database is **not** ground truth: its holder lists include pool
contracts, it reports negative balances for some wallets, and on 2026-09-06 a purge deleted 545,686 rows
covering every crystal-generation token, trade and position.

- **Side database** (the ledger's own data, safe to read, safe to write only if your brief says so):
  DSN in `C:/Users/ryanl/AppData/Local/Temp/claude/C--Users-ryanl-OneDrive-Desktop-projects/79e4cc35-2274-4196-b460-6db123110bd1/scratchpad/side_dsn.txt`.
  It holds three fully replayed tokens (CHIPOTLE `0x8e74f6e9…`, moncock `0x405b6330…`, JAMES `0x43cf5407…`)
  and 159 partially replayed leftovers from earlier experiments. Open it read-only unless you must write.
- **Production**, read-only, through a tunnel on `127.0.0.1:15433` (credentials in the repo's git-ignored
  `.env`; `PGHOST`, `PGUSER`, `PGPASSWORD`, `PGDATABASE`). If the port refuses connections the tunnel is
  down; say so rather than working around it.
- **Chain**: `https://rpc.monad.xyz`. `eth_getLogs` is capped at 100 blocks and rejects topic filters.
  Batched `eth_call` works; the rate limit is 50 per second. Archive state is only available for roughly the
  last few hundred thousand blocks, so `balanceOf` at an old block fails with "Block requested not found".

Run the test suite with a scratch database name unique to you, or you will fight other agents:

```bash
TEST_DATABASE_URL="postgresql://postgres:<pw>@localhost:5432/postgres?sslmode=disable" \
  SCRATCH_DB_NAME="crystal_<yourname>_itest" REWARDS_WORKER=0 python -m pytest -q
```

## Hard constraints

1. **Never write to production.** Read-only transactions only. No repair scripts, no merges.
2. **Never point `DATABASE_URL` or `LEDGER_TEST_DATABASE_URL` at the side database while running tests.**
   The engine tests truncate ledger tables; this would destroy the replayed fixtures.
3. **Do not run two replays at once.** They deadlock on the shared classification tables.
4. This tree is shared with other agents. Never `git add -A`, never `git stash`, never `git checkout` a file
   you did not write, never switch branches. Check a file's mtime before assuming it is yours.
5. Commit style if you commit anything: lowercase, one sentence, no co-author trailers.
6. If you need to write scratch files, put them in your own session scratchpad, not the repo.

## Evidence standard

A finding is only useful if someone can act on it. For each one give:

- **a concrete failing case**: inputs and the wrong output, ideally a runnable reproduction or a real
  transaction hash, not a description of a code path that looks wrong;
- **the location**: `file.py:line`;
- **why it matters**: what a user or a downstream consumer sees, and roughly how often, measured against the
  side database or the chain where you can;
- **severity**: P1 blocks cutover, P2 should be fixed before it matters, P3 is a note;
- **what you did not check**, explicitly. An unverified suspicion labelled as such is useful. One presented
  as a finding is not.

Prefer measuring blast radius over listing possibilities. "This is wrong for every routed buy, which is 7.2%
of moncock's flows" is worth more than "this could be wrong".

Do not trust `COMPLETE.md`, this brief, or the author's commit messages. Where a claim matters, verify it.
Two claims worth testing directly, because the author wrote both the code and the checks that pass it:
the fixture checks in `scripts/ledger_check.py`, and the invariants in `scripts/ledger_verify.py`. As one
known example, the duplicate-flow invariant keys on the table's primary key, so it structurally cannot see
the coarser duplicates `feedback2.md` reports.

## Output

Write one markdown file in the repo root: the filename is in your brief (`feedback3.md` … `feedback6.md`).
Do not edit `feedback.md` or `feedback2.md`, and do not edit each other's files.

Structure it as: verdict in one or two sentences, then what you verified independently with results, then
findings ordered by severity, then what you did not check. Lead with what is wrong and what it costs.
