import json
import os
import urllib.error
import uuid
from contextlib import contextmanager

import pytest

from core import chain as h
from core.ledger import kinds as kinds_mod
from core.ledger.kinds import (
    SOURCE_GETCODE,
    SOURCE_HEURISTIC,
    SOURCE_KNOWN_LIST,
    SOURCE_USEROP,
    USEROP_TAG,
    AddressKinds,
    JsonRpc,
    classify_code,
    parse_userop_event,
    userop_senders_from_logs,
)
from core.ledger.types import (
    ENTRYPOINT_V06,
    ENTRYPOINT_V07,
    KIND_CONTRACT_UNKNOWN,
    KIND_EOA,
    KIND_EOA_7702,
    KIND_TOKEN,
    KIND_VENUE_CURVE,
    KIND_VENUE_CUSTODY,
    KIND_VENUE_POOL,
    KIND_VENUE_ROUTER,
    KIND_WALLET_4337,
    KIND_ZERO,
    USDC,
    USEROP_EVENT_TOPIC,
    WMON,
    ZERO,
    TraceResult,
    TransferLeg,
    TxBundle,
    TxMeta,
    VenueEvent,
)


def addr(n: int) -> str:
    return "0x" + f"{n:040x}"


def padded(address: str) -> str:
    return "0x" + address[2:].rjust(64, "0")


TOKEN = addr(0xA1)
TOKEN2 = addr(0xA2)
POOL = addr(0xB1)
BOT = addr(0xB2)
ROUTER = addr(0xB3)
WALLET = addr(0xC1)
WALLET2 = addr(0xC2)
DELEGATED = addr(0xC3)
ACCOUNT = addr(0xC4)
BUNDLER = addr(0xC5)
VAULT = addr(0xD1)
MARKET = addr(0xD2)
UNI4_POOL_ID = "0x" + "77" * 32

CONTRACT_CODE = "0x6080604052"
DELEGATION_CODE = "0xef0100" + "ab" * 20


class FakeCursor:
    def __init__(self, tables=None):
        self.tables = {name: list(rows) for name, rows in (tables or {}).items()}
        self.address_kinds: dict[str, dict] = {}
        self.venues: dict[str, dict] = {}
        self.executed: list[tuple[str, object]] = []
        self._result: list = []

    def execute(self, sql, params=None):
        self.executed.append((sql, params))
        text = " ".join(sql.split())
        if text.startswith("INSERT INTO address_kinds"):
            self._insert_kinds(text, params)
        elif text.startswith("INSERT INTO venues"):
            self._insert_venues(params)
        elif "FROM address_kinds WHERE address = ANY" in text:
            wanted = set(params[0])
            self._result = [(a, r["kind"]) for a, r in self.address_kinds.items() if a in wanted]
        elif "FROM venues" in text:
            self._result = [(a, r["kind"]) for a, r in self.venues.items()]
        else:
            table = text.split(" FROM ")[1].split()[0]
            self._result = list(self.tables.get(table, []))

    def _insert_kinds(self, text, params):
        do_nothing = "DO NOTHING" in text
        guard_getcode = "address_kinds.source = 'getcode'" in text
        for i in range(0, len(params), 5):
            address, kind, source, block, evidence = params[i : i + 5]
            existing = self.address_kinds.get(address)
            if existing is not None:
                if do_nothing:
                    continue
                if guard_getcode and existing["source"] != SOURCE_GETCODE:
                    continue
                if existing["first_seen_block"] is not None:
                    block = existing["first_seen_block"]
            self.address_kinds[address] = {
                "kind": kind,
                "source": source,
                "first_seen_block": block,
                "evidence": json.loads(evidence) if evidence else None,
            }

    def _insert_venues(self, params):
        for i in range(0, len(params), 6):
            address, kind, token0, token1, discovered, evidence = params[i : i + 6]
            decoded = json.loads(evidence) if evidence else None
            existing = self.venues.get(address)
            if existing is None:
                self.venues[address] = {
                    "kind": kind,
                    "token0": token0,
                    "token1": token1,
                    "discovered": discovered,
                    "evidence": decoded,
                }
                continue
            if existing["discovered"] or not discovered:
                existing["kind"] = kind
            existing["token0"] = token0 if token0 is not None else existing["token0"]
            existing["token1"] = token1 if token1 is not None else existing["token1"]
            existing["discovered"] = existing["discovered"] and discovered
            existing["evidence"] = decoded if decoded is not None else existing["evidence"]

    def fetchall(self):
        return list(self._result)

    def fetchone(self):
        return self._result[0] if self._result else None

    @contextmanager
    def factory(self):
        yield self


class FakeRpc:
    def __init__(self, codes=None):
        self.codes = dict(codes or {})
        self.calls: list[list] = []

    def __call__(self, calls):
        self.calls.append(calls)
        for method, _params in calls:
            assert method == "eth_getCode"
        return [self.codes.get(params[0], "0x") for _method, params in calls]

    @property
    def addresses_queried(self) -> list[str]:
        return [params[0] for batch in self.calls for _method, params in batch]


def leg(i: int, token: str, frm: str, to: str, amount: int = 10**18) -> TransferLeg:
    return TransferLeg(log_index=i, token=token, from_addr=frm, to_addr=to, amount=amount)


def meta(txhash: str, frm: str, to: str, value: int = 0) -> TxMeta:
    return TxMeta(txhash=txhash, block_number=1, tx_index=0, from_addr=frm, to_addr=to, value=value, selector=None)


def bundle(txhash, block=100, transfers=(), events=(), tx_meta=None, trace=None, userop=None) -> TxBundle:
    return TxBundle(
        txhash=txhash,
        block_number=block,
        tx_index=0,
        timestamp=0,
        transfers=list(transfers),
        venue_events=list(events),
        meta=tx_meta,
        trace=trace,
        userop_sender=userop,
    )


def known_tables() -> dict:
    return {
        "launchpad_pools": [(POOL, TOKEN, WMON, True)],
        "univ4_pools": [(UNI4_POOL_ID, TOKEN2, WMON, False)],
        "crystal_markets": [(MARKET, TOKEN2, USDC, 500)],
        "crystal_pools": [(MARKET,)],
        "crystal_vaults": [(VAULT, TOKEN2, USDC, 600)],
        "launchpad_tokens": [(TOKEN, 400)],
        "nadfun_v2_tokens": [(addr(0xA3),)],
    }


def test_classify_code_applies_the_7702_designator_rule():
    assert classify_code("0x") == KIND_EOA
    assert classify_code("") == KIND_EOA
    assert classify_code(None) == KIND_EOA
    assert classify_code(DELEGATION_CODE) == KIND_EOA_7702
    assert classify_code(DELEGATION_CODE.upper()) == KIND_EOA_7702
    assert classify_code("0xef0100" + "ab" * 21) == KIND_CONTRACT_UNKNOWN
    assert classify_code("0xef0100" + "ab" * 19) == KIND_CONTRACT_UNKNOWN
    assert classify_code(CONTRACT_CODE) == KIND_CONTRACT_UNKNOWN


def test_is_wallet_matches_the_write_seam_rule():
    kinds = AddressKinds(None, rpc=FakeRpc())
    assert all(kinds.is_wallet(k) for k in (KIND_EOA, KIND_EOA_7702, KIND_WALLET_4337, KIND_CONTRACT_UNKNOWN))
    assert not any(
        kinds.is_wallet(k)
        for k in (KIND_VENUE_POOL, KIND_VENUE_ROUTER, KIND_VENUE_CURVE, KIND_VENUE_CUSTODY, KIND_TOKEN, KIND_ZERO)
    )


def test_constants_are_classified_before_load_known():
    rpc = FakeRpc()
    kinds = AddressKinds(None, rpc=rpc)
    cur = FakeCursor()
    assert kinds.kind(h.CRYSTAL_ADDR, cur) == KIND_VENUE_CUSTODY
    assert kinds.kind(h.NADFUN_ADDRS[0], cur) == KIND_VENUE_CURVE
    assert kinds.kind(h.PASSTHROUGH_ADDRS[0], cur) == KIND_VENUE_ROUTER
    assert kinds.kind(h.UNIV4_POOL_MANAGER_ADDR, cur) == KIND_VENUE_POOL
    assert kinds.kind(h.VAULT_FACTORY_ADDRS[0], cur) == KIND_VENUE_ROUTER
    assert kinds.kind(ENTRYPOINT_V06, cur) == KIND_VENUE_ROUTER
    assert kinds.kind(WMON, cur) == KIND_TOKEN
    assert kinds.kind(ZERO, cur) == KIND_ZERO
    assert kinds.kind("", cur) == KIND_ZERO
    assert kinds.kind(h.CRYSTAL_ADDR.upper(), cur) == KIND_VENUE_CUSTODY
    assert rpc.calls == []


def test_load_known_seeds_kinds_and_venues_from_tables():
    rpc = FakeRpc()
    cur = FakeCursor(known_tables())
    kinds = AddressKinds(cur.factory, rpc=rpc)
    kinds.load_known(cur)

    assert kinds.kind(POOL, cur) == KIND_VENUE_POOL
    assert kinds.kind(MARKET, cur) == KIND_VENUE_POOL
    assert kinds.kind(VAULT, cur) == KIND_VENUE_CUSTODY
    assert kinds.kind(TOKEN, cur) == KIND_TOKEN
    assert kinds.kind(TOKEN2, cur) == KIND_TOKEN
    assert kinds.kind(addr(0xA3), cur) == KIND_TOKEN
    assert kinds.kind(USDC, cur) == KIND_TOKEN
    assert rpc.calls == []

    assert cur.address_kinds[POOL]["source"] == SOURCE_KNOWN_LIST
    assert cur.address_kinds[TOKEN]["first_seen_block"] == 400
    assert cur.address_kinds[MARKET]["first_seen_block"] == 500
    assert cur.address_kinds[VAULT]["first_seen_block"] == 600
    assert cur.address_kinds[h.CRYSTAL_ADDR]["kind"] == KIND_VENUE_CUSTODY
    assert cur.address_kinds[WMON]["kind"] == KIND_TOKEN

    assert cur.venues[POOL] == {
        "kind": KIND_VENUE_POOL,
        "token0": TOKEN,
        "token1": WMON,
        "discovered": False,
        "evidence": {"list": "launchpad_pools"},
    }
    assert cur.venues[UNI4_POOL_ID]["token0"] == WMON
    assert cur.venues[UNI4_POOL_ID]["token1"] == TOKEN2
    assert cur.venues[MARKET]["token0"] == TOKEN2
    assert cur.venues[MARKET]["token1"] == USDC
    assert cur.venues[VAULT]["kind"] == KIND_VENUE_CUSTODY
    assert cur.venues[h.CRYSTAL_ADDR]["kind"] == KIND_VENUE_CUSTODY
    assert cur.venues[h.UNIV4_POOL_MANAGER_ADDR]["kind"] == KIND_VENUE_POOL
    assert h.CRYSTAL_ADDR not in {a for a, r in cur.venues.items() if r["discovered"]}
    assert WMON not in cur.venues
    assert UNI4_POOL_ID not in cur.address_kinds


def test_load_known_reloads_discovered_venues_into_the_cache():
    cur = FakeCursor(known_tables())
    cur.venues[addr(0xE1)] = {
        "kind": KIND_VENUE_POOL,
        "token0": None,
        "token1": None,
        "discovered": True,
        "evidence": None,
    }
    rpc = FakeRpc()
    kinds = AddressKinds(cur.factory, rpc=rpc)
    kinds.load_known(cur)
    assert kinds.kind(addr(0xE1), cur) == KIND_VENUE_POOL
    assert rpc.calls == []


def test_getcode_classifies_batches_and_caches_in_process_and_in_db():
    rpc = FakeRpc({DELEGATED: DELEGATION_CODE, POOL: CONTRACT_CODE})
    cur = FakeCursor()
    kinds = AddressKinds(cur.factory, rpc=rpc)

    resolved = kinds.kinds_for([WALLET, DELEGATED, POOL, WALLET.upper()], cur, block=123)
    assert resolved == {WALLET: KIND_EOA, DELEGATED: KIND_EOA_7702, POOL: KIND_CONTRACT_UNKNOWN}
    assert len(rpc.calls) == 1
    assert sorted(rpc.addresses_queried) == sorted([WALLET, DELEGATED, POOL])

    assert kinds.kind(WALLET, cur) == KIND_EOA
    assert kinds.kind(POOL, cur) == KIND_CONTRACT_UNKNOWN
    assert len(rpc.calls) == 1

    assert cur.address_kinds[DELEGATED] == {
        "kind": KIND_EOA_7702,
        "source": SOURCE_GETCODE,
        "first_seen_block": 123,
        "evidence": {"code_len": 23},
    }
    assert cur.address_kinds[WALLET]["evidence"] == {"code_len": 0}

    fresh = AddressKinds(cur.factory, rpc=rpc)
    assert fresh.kind(DELEGATED, cur) == KIND_EOA_7702
    assert fresh.kind(WALLET, cur) == KIND_EOA
    assert len(rpc.calls) == 1


def test_getcode_never_overrides_a_stronger_row():
    cur = FakeCursor()
    cur.address_kinds[POOL] = {
        "kind": KIND_VENUE_POOL,
        "source": SOURCE_HEURISTIC,
        "first_seen_block": 5,
        "evidence": None,
    }
    rpc = FakeRpc({POOL: CONTRACT_CODE})
    kinds = AddressKinds(cur.factory, rpc=rpc)
    assert kinds.kind(POOL, cur) == KIND_VENUE_POOL
    assert rpc.calls == []

    fresh_cur = FakeCursor()
    fresh_cur.address_kinds[WALLET] = {
        "kind": KIND_VENUE_POOL,
        "source": SOURCE_HEURISTIC,
        "first_seen_block": 5,
        "evidence": None,
    }
    other = AddressKinds(fresh_cur.factory, rpc=rpc)
    other._kinds.pop(WALLET, None)
    other._put_kinds(fresh_cur, [(WALLET, KIND_EOA, SOURCE_GETCODE, 9, {"code_len": 0})])
    assert fresh_cur.address_kinds[WALLET]["kind"] == KIND_VENUE_POOL


def test_getcode_batches_are_capped():
    many = [addr(0x1000 + i) for i in range(kinds_mod.GETCODE_BATCH + 7)]
    rpc = FakeRpc()
    cur = FakeCursor()
    kinds = AddressKinds(cur.factory, rpc=rpc)
    resolved = kinds.kinds_for(many, cur)
    assert set(resolved.values()) == {KIND_EOA}
    assert [len(batch) for batch in rpc.calls] == [kinds_mod.GETCODE_BATCH, 7]


def swap_sell_tx(txhash: str, block: int, pool: str, wallet: str, target: str) -> TxBundle:
    return bundle(
        txhash,
        block,
        transfers=[leg(1, TOKEN, wallet, pool), leg(2, WMON, pool, wallet)],
        tx_meta=meta(txhash, wallet, target),
    )


def swap_buy_tx(txhash: str, block: int, pool: str, wallet: str, target: str) -> TxBundle:
    return bundle(
        txhash,
        block,
        transfers=[leg(1, WMON, wallet, pool), leg(2, TOKEN, pool, wallet)],
        tx_meta=meta(txhash, wallet, target),
    )


def test_discovery_promotes_a_pool_shaped_contract_after_two_transactions():
    rpc = FakeRpc({POOL: CONTRACT_CODE, ROUTER: CONTRACT_CODE})
    cur = FakeCursor()
    kinds = AddressKinds(cur.factory, rpc=rpc)
    registry = {TOKEN: object()}

    assert kinds.observe_tx(swap_sell_tx("0x01", 10, POOL, WALLET, ROUTER), registry) == []
    assert kinds.kind(POOL, cur) == KIND_CONTRACT_UNKNOWN
    assert POOL not in cur.venues

    assert kinds.observe_tx(swap_sell_tx("0x01", 10, POOL, WALLET, ROUTER), registry) == []

    assert kinds.observe_tx(swap_buy_tx("0x02", 11, POOL, WALLET2, ROUTER), registry) == [POOL]
    assert kinds.kind(POOL, cur) == KIND_VENUE_POOL
    assert not kinds.is_wallet(kinds.kind(POOL, cur))

    row = cur.address_kinds[POOL]
    assert row["kind"] == KIND_VENUE_POOL
    assert row["source"] == SOURCE_HEURISTIC
    assert row["first_seen_block"] == 10
    assert row["evidence"]["txs"] == ["0x01", "0x02"]
    assert row["evidence"]["tokens"] == [TOKEN]
    assert row["evidence"]["quotes"] == [WMON]

    venue = cur.venues[POOL]
    assert venue["kind"] == KIND_VENUE_POOL
    assert venue["discovered"] is True
    assert venue["token0"] == TOKEN
    assert venue["token1"] == WMON
    assert venue["evidence"]["rule"] == "pool_shape_across_txs"

    assert kinds.observe_tx(swap_buy_tx("0x03", 12, POOL, WALLET2, ROUTER), registry) == []


def test_discovery_counts_native_value_and_trace_as_quote_legs():
    rpc = FakeRpc({POOL: CONTRACT_CODE})
    cur = FakeCursor()
    kinds = AddressKinds(cur.factory, rpc=rpc)
    registry = {TOKEN}

    curve_buy = bundle(
        "0x11",
        20,
        transfers=[leg(1, TOKEN, POOL, WALLET)],
        tx_meta=meta("0x11", WALLET, ROUTER, value=10**18),
        trace=TraceResult(available=True, transfers=[(ROUTER, POOL, 10**18)]),
    )
    assert kinds.observe_tx(curve_buy, registry) == []
    curve_sell = bundle(
        "0x12",
        21,
        transfers=[leg(1, TOKEN, WALLET, POOL)],
        tx_meta=meta("0x12", WALLET, ROUTER),
        trace=TraceResult(available=True, transfers=[(POOL, ROUTER, 5 * 10**17), (ROUTER, WALLET, 5 * 10**17)]),
    )
    assert kinds.observe_tx(curve_sell, registry) == [POOL]
    assert cur.venues[POOL]["token1"] == "native"


def test_discovery_ignores_transaction_targets_and_origins():
    rpc = FakeRpc({BOT: CONTRACT_CODE, POOL: CONTRACT_CODE})
    cur = FakeCursor()
    kinds = AddressKinds(cur.factory, rpc=rpc)
    registry = {TOKEN}

    bot_buy = bundle(
        "0x21",
        30,
        transfers=[leg(1, WMON, BOT, POOL), leg(2, TOKEN, POOL, BOT)],
        tx_meta=meta("0x21", WALLET, BOT),
    )
    bot_sell = bundle(
        "0x22",
        31,
        transfers=[leg(1, TOKEN, BOT, POOL), leg(2, WMON, POOL, BOT)],
        tx_meta=meta("0x22", WALLET, BOT),
    )
    assert kinds.observe_tx(bot_buy, registry) == []
    assert kinds.observe_tx(bot_sell, registry) == [POOL]
    assert kinds.kind(BOT, cur) == KIND_CONTRACT_UNKNOWN
    assert kinds.is_wallet(kinds.kind(BOT, cur))
    assert BOT not in cur.venues

    seen_as_origin = addr(0xB9)
    rpc.codes[seen_as_origin] = CONTRACT_CODE
    kinds.observe_tx(bundle("0x23", 32, transfers=[], tx_meta=meta("0x23", seen_as_origin, ROUTER)), registry)
    assert kinds.observe_tx(swap_sell_tx("0x24", 33, seen_as_origin, WALLET, ROUTER), registry) == []
    assert kinds.observe_tx(swap_buy_tx("0x25", 34, seen_as_origin, WALLET, ROUTER), registry) == []
    assert kinds.kind(seen_as_origin, cur) == KIND_CONTRACT_UNKNOWN


def test_discovery_needs_metadata_and_skips_wallets_and_known_kinds():
    rpc = FakeRpc({POOL: CONTRACT_CODE})
    cur = FakeCursor()
    kinds = AddressKinds(cur.factory, rpc=rpc)
    registry = {TOKEN}

    without_meta = bundle("0x31", 40, transfers=[leg(1, TOKEN, WALLET, POOL), leg(2, WMON, POOL, WALLET)])
    assert kinds.observe_tx(without_meta, registry) == []
    assert kinds.observe_tx(without_meta, registry) == []
    assert rpc.calls == []
    assert kinds._sightings == {}

    otc = bundle(
        "0x32",
        41,
        transfers=[leg(1, TOKEN, WALLET, WALLET2), leg(2, WMON, WALLET2, WALLET)],
        tx_meta=meta("0x32", WALLET, ROUTER),
    )
    assert kinds.observe_tx(otc, registry) == []
    otc2 = bundle(
        "0x33",
        42,
        transfers=[leg(1, TOKEN, addr(0xC9), WALLET2), leg(2, WMON, WALLET2, addr(0xC9))],
        tx_meta=meta("0x33", addr(0xC9), ROUTER),
    )
    assert kinds.observe_tx(otc2, registry) == []
    assert kinds.kind(WALLET2, cur) == KIND_EOA
    assert WALLET2 not in cur.venues

    unregistered = bundle(
        "0x34",
        43,
        transfers=[leg(1, addr(0xAF), WALLET, POOL), leg(2, WMON, POOL, WALLET)],
        tx_meta=meta("0x34", WALLET, ROUTER),
    )
    assert kinds.observe_tx(unregistered, registry) == []
    assert kinds.observe_tx(unregistered, registry) == []
    assert POOL not in kinds._sightings

    assert kinds.observe_tx(swap_sell_tx("0x35", 44, h.CRYSTAL_ADDR, WALLET, ROUTER), registry) == []
    assert kinds.observe_tx(swap_buy_tx("0x36", 45, h.CRYSTAL_ADDR, WALLET, ROUTER), registry) == []
    assert kinds.kind(h.CRYSTAL_ADDR, cur) == KIND_VENUE_CUSTODY


def userop_log(entrypoint: str, sender: str, paymaster: str = ZERO, success: bool = True) -> dict:
    nonce = f"{7:064x}"
    ok = f"{1 if success else 0:064x}"
    cost = f"{123456:064x}"
    used = f"{54321:064x}"
    return {
        "address": entrypoint,
        "topics": [USEROP_EVENT_TOPIC, "0x" + "11" * 32, padded(sender), padded(paymaster)],
        "data": "0x" + nonce + ok + cost + used,
    }


def test_parse_userop_event_reads_sender_from_topic_two():
    log = userop_log(ENTRYPOINT_V07, ACCOUNT, paymaster=addr(0xF1))
    parsed = parse_userop_event(log["address"], log["topics"], log["data"][2:])
    assert parsed == {
        "user_op_hash": "0x" + "11" * 32,
        "sender": ACCOUNT,
        "paymaster": addr(0xF1),
        "nonce": 7,
        "success": True,
        "actual_gas_cost": 123456,
        "actual_gas_used": 54321,
    }
    assert parse_userop_event(ROUTER, log["topics"], log["data"][2:]) is None
    assert parse_userop_event(ENTRYPOINT_V06, log["topics"][:3], log["data"][2:]) is None
    assert parse_userop_event(ENTRYPOINT_V06, [h.TOPICS[0], *log["topics"][1:]], log["data"][2:]) is None
    assert parse_userop_event(ENTRYPOINT_V06.upper(), log["topics"], "")["nonce"] == 0


def test_userop_senders_from_logs_dedupes_and_ignores_other_emitters():
    logs = [
        userop_log(ENTRYPOINT_V06, ACCOUNT),
        userop_log(ROUTER, addr(0xF2)),
        userop_log(ENTRYPOINT_V07, addr(0xF3)),
        userop_log(ENTRYPOINT_V07, ACCOUNT),
        {"address": ENTRYPOINT_V07, "topics": [], "data": "0x"},
    ]
    assert userop_senders_from_logs(logs) == [ACCOUNT, addr(0xF3)]


def test_userop_sender_from_bundle_marks_the_account_a_4337_wallet():
    rpc = FakeRpc({ACCOUNT: CONTRACT_CODE})
    cur = FakeCursor()
    kinds = AddressKinds(cur.factory, rpc=rpc)
    log = userop_log(ENTRYPOINT_V07, ACCOUNT)
    parsed = parse_userop_event(log["address"], log["topics"], log["data"][2:])
    event = VenueEvent(tag=USEROP_TAG, log_index=3, parsed=parsed, address=ENTRYPOINT_V07)
    tx = bundle(
        "0x41",
        50,
        transfers=[leg(1, TOKEN, POOL, ACCOUNT)],
        events=[event],
        tx_meta=meta("0x41", BUNDLER, ENTRYPOINT_V07),
    )

    assert kinds.userop_sender(tx) == ACCOUNT
    assert kinds.kind(ACCOUNT, cur) == KIND_WALLET_4337
    assert kinds.is_wallet(kinds.kind(ACCOUNT, cur))
    assert rpc.calls == []
    assert cur.address_kinds[ACCOUNT] == {
        "kind": KIND_WALLET_4337,
        "source": SOURCE_USEROP,
        "first_seen_block": 50,
        "evidence": {"event": "UserOperationEvent", "entrypoint": ENTRYPOINT_V07},
    }

    plain = bundle("0x42", 51, transfers=[leg(1, TOKEN, POOL, WALLET)], tx_meta=meta("0x42", WALLET, ROUTER))
    assert kinds.userop_sender(plain) is None

    foreign = VenueEvent(tag=USEROP_TAG, log_index=1, parsed={"sender": addr(0xF4)}, address=ROUTER)
    assert kinds.userop_sender(bundle("0x43", 52, events=[foreign])) is None

    prefilled = bundle("0x44", 53, userop=addr(0xF5).upper())
    assert kinds.userop_sender(prefilled) == addr(0xF5)
    assert kinds.kind(addr(0xF5), cur) == KIND_WALLET_4337


def test_userop_sender_upgrades_getcode_rows_but_not_known_venues():
    rpc = FakeRpc({ACCOUNT: CONTRACT_CODE})
    cur = FakeCursor()
    kinds = AddressKinds(cur.factory, rpc=rpc)
    assert kinds.kind(ACCOUNT, cur) == KIND_CONTRACT_UNKNOWN
    event = VenueEvent(tag=USEROP_TAG, log_index=1, parsed={"sender": ACCOUNT}, address=ENTRYPOINT_V06)
    assert kinds.userop_sender(bundle("0x51", 60, events=[event])) == ACCOUNT
    assert kinds.kind(ACCOUNT, cur) == KIND_WALLET_4337
    assert cur.address_kinds[ACCOUNT]["source"] == SOURCE_USEROP

    venue_event = VenueEvent(tag=USEROP_TAG, log_index=1, parsed={"sender": h.CRYSTAL_ADDR}, address=ENTRYPOINT_V06)
    assert kinds.userop_sender(bundle("0x52", 61, events=[venue_event])) == h.CRYSTAL_ADDR
    assert kinds.kind(h.CRYSTAL_ADDR, cur) == KIND_VENUE_CUSTODY


def test_multiple_userops_in_one_bundle_keep_log_order():
    kinds = AddressKinds(FakeCursor().factory, rpc=FakeRpc())
    first = VenueEvent(tag=USEROP_TAG, log_index=1, parsed={"sender": addr(0xF6)}, address=ENTRYPOINT_V07)
    second = VenueEvent(tag=USEROP_TAG, log_index=2, parsed={"sender": addr(0xF7)}, address=ENTRYPOINT_V07)
    tx = bundle("0x61", 70, events=[first, second])
    assert kinds.userop_senders(tx) == [addr(0xF6), addr(0xF7)]
    assert kinds.userop_sender(tx) == addr(0xF6)


def test_userop_senders_are_never_discovered_as_venues():
    rpc = FakeRpc({ACCOUNT: CONTRACT_CODE})
    cur = FakeCursor()
    kinds = AddressKinds(cur.factory, rpc=rpc)
    registry = {TOKEN}
    event = VenueEvent(tag=USEROP_TAG, log_index=9, parsed={"sender": ACCOUNT}, address=ENTRYPOINT_V07)
    for i, txhash in enumerate(("0x71", "0x72", "0x73")):
        tx = bundle(
            txhash,
            80 + i,
            transfers=[leg(1, TOKEN, POOL, ACCOUNT), leg(2, WMON, ACCOUNT, POOL)],
            events=[event],
            tx_meta=meta(txhash, BUNDLER, ENTRYPOINT_V07),
        )
        assert kinds.observe_tx(tx, registry) == []
    assert kinds.kind(ACCOUNT, cur) == KIND_WALLET_4337
    assert ACCOUNT not in cur.venues


class FakeResponse:
    def __init__(self, body: bytes):
        self.body = body

    def read(self) -> bytes:
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def test_json_rpc_batches_retries_and_orders_results(monkeypatch):
    attempts: list[list[dict]] = []
    sleeps: list[float] = []

    def fake_urlopen(request, timeout):
        payload = json.loads(request.data)
        attempts.append(payload)
        if len(attempts) == 1:
            raise urllib.error.URLError("boom")
        if len(attempts) == 2:
            return FakeResponse(json.dumps([{"id": 0, "result": "0x"}, {"id": 1, "error": {"code": -32000}}]).encode())
        replies = [{"jsonrpc": "2.0", "id": item["id"], "result": item["params"][0][-2:]} for item in payload]
        return FakeResponse(json.dumps(list(reversed(replies))).encode())

    monkeypatch.setattr(kinds_mod, "_urlopen", fake_urlopen)
    monkeypatch.setattr(kinds_mod.time, "sleep", lambda s: sleeps.append(s))
    client = JsonRpc("http://rpc.test", max_rps=1000, attempts=4)
    results = client.batch([("eth_getCode", [WALLET, "latest"]), ("eth_getCode", [POOL, "latest"])])
    assert results == [WALLET[-2:], POOL[-2:]]
    assert len(attempts) == 3
    assert attempts[0][0]["method"] == "eth_getCode"
    assert attempts[0][1]["id"] == 1
    assert [s for s in sleeps if s >= 0.5] == [0.5, 1.0]
    assert client.batch([]) == []


def test_json_rpc_gives_up_after_the_attempt_budget(monkeypatch):
    def always_fail(request, timeout):
        raise urllib.error.URLError("down")

    monkeypatch.setattr(kinds_mod, "_urlopen", always_fail)
    monkeypatch.setattr(kinds_mod.time, "sleep", lambda s: None)
    client = JsonRpc("http://rpc.test", max_rps=1000, attempts=2)
    with pytest.raises(RuntimeError, match="after 2 attempts"):
        client.batch([("eth_getCode", [WALLET, "latest"])])


def test_json_rpc_honours_the_rate_limit(monkeypatch):
    clock = {"now": 0.0}
    sleeps: list[float] = []

    monkeypatch.setattr(kinds_mod.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(kinds_mod.time, "sleep", lambda s: sleeps.append(s))
    monkeypatch.setattr(kinds_mod, "_urlopen", lambda request, timeout: FakeResponse(b'[{"id": 0, "result": "0x"}]'))
    client = JsonRpc("http://rpc.test", max_rps=4)
    client.batch([("eth_getCode", [WALLET, "latest"])])
    client.batch([("eth_getCode", [WALLET, "latest"])])
    assert sleeps == [0.25]


SIDE_DSN = os.getenv("LEDGER_TEST_DSN")


@pytest.mark.skipif(not SIDE_DSN, reason="LEDGER_TEST_DSN not set")
def test_side_database_round_trip():
    import psycopg2

    from core.ledger.schema import init_ledger_schema

    schema = "ledger_kinds_" + uuid.uuid4().hex[:8]
    conn = psycopg2.connect(SIDE_DSN)
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute(f"CREATE SCHEMA {schema}")
    cur.execute(f"SET search_path TO {schema}")
    try:
        init_ledger_schema(cur)
        cur.execute(
            "CREATE TABLE launchpad_pools (pool TEXT PRIMARY KEY, token_addr TEXT, native_addr TEXT, token_is_0 BOOLEAN)"
        )
        cur.execute(
            "CREATE TABLE univ4_pools (pool_id TEXT PRIMARY KEY, token_addr TEXT, native_addr TEXT, token_is_0 BOOLEAN)"
        )
        cur.execute(
            "CREATE TABLE crystal_markets (market TEXT PRIMARY KEY, base_address TEXT, quote_address TEXT, created_block BIGINT)"
        )
        cur.execute("CREATE TABLE crystal_pools (market TEXT PRIMARY KEY)")
        cur.execute(
            "CREATE TABLE crystal_vaults (vault TEXT PRIMARY KEY, base TEXT, quote TEXT, deployed_block BIGINT)"
        )
        cur.execute("CREATE TABLE launchpad_tokens (token TEXT PRIMARY KEY, created_block BIGINT)")
        cur.execute("CREATE TABLE nadfun_v2_tokens (token TEXT PRIMARY KEY)")
        for table, rows in known_tables().items():
            for row in rows:
                placeholders = ", ".join(["%s"] * len(row))
                cur.execute(f"INSERT INTO {table} VALUES ({placeholders})", row)

        @contextmanager
        def factory():
            yield cur

        rpc = FakeRpc({POOL: CONTRACT_CODE, DELEGATED: DELEGATION_CODE, addr(0xB5): CONTRACT_CODE})
        kinds = AddressKinds(factory, rpc=rpc)
        kinds.load_known(cur)
        kinds.load_known(cur)
        assert kinds.kind(POOL, cur) == KIND_VENUE_POOL
        assert kinds.kind(MARKET, cur) == KIND_VENUE_POOL
        assert kinds.kind(VAULT, cur) == KIND_VENUE_CUSTODY
        assert kinds.kind(TOKEN, cur) == KIND_TOKEN
        assert kinds.kind(DELEGATED, cur, block=77) == KIND_EOA_7702
        assert kinds.kind(WALLET, cur) == KIND_EOA

        unknown = addr(0xB5)
        registry = {TOKEN}
        assert kinds.observe_tx(swap_sell_tx("0x91", 90, unknown, WALLET, ROUTER), registry) == []
        assert kinds.observe_tx(swap_buy_tx("0x92", 91, unknown, WALLET2, ROUTER), registry) == [unknown]

        event = VenueEvent(tag=USEROP_TAG, log_index=1, parsed={"sender": ACCOUNT}, address=ENTRYPOINT_V07)
        assert kinds.userop_sender(bundle("0x93", 92, events=[event])) == ACCOUNT

        cur.execute("SELECT address, kind, source, first_seen_block, evidence FROM address_kinds ORDER BY address")
        rows = {r[0]: r for r in cur.fetchall()}
        assert rows[POOL][1:3] == (KIND_VENUE_POOL, SOURCE_KNOWN_LIST)
        assert rows[TOKEN][3] == 400
        assert rows[DELEGATED][1:4] == (KIND_EOA_7702, SOURCE_GETCODE, 77)
        assert rows[DELEGATED][4] == {"code_len": 23}
        assert rows[unknown][1:4] == (KIND_VENUE_POOL, SOURCE_HEURISTIC, 90)
        assert rows[unknown][4]["txs"] == ["0x91", "0x92"]
        assert rows[ACCOUNT][1:3] == (KIND_WALLET_4337, SOURCE_USEROP)
        assert rows[h.CRYSTAL_ADDR][1] == KIND_VENUE_CUSTODY

        cur.execute("SELECT address, kind, token0, token1, discovered FROM venues ORDER BY address")
        venues = {r[0]: r[1:] for r in cur.fetchall()}
        assert venues[POOL] == (KIND_VENUE_POOL, TOKEN, WMON, False)
        assert venues[UNI4_POOL_ID] == (KIND_VENUE_POOL, WMON, TOKEN2, False)
        assert venues[unknown] == (KIND_VENUE_POOL, TOKEN, WMON, True)
        assert venues[h.UNIV4_POOL_MANAGER_ADDR][0] == KIND_VENUE_POOL

        again = AddressKinds(factory, rpc=rpc)
        calls_before = len(rpc.calls)
        assert again.kind(DELEGATED, cur) == KIND_EOA_7702
        assert again.kind(unknown, cur) == KIND_VENUE_POOL
        assert again.kind(ACCOUNT, cur) == KIND_WALLET_4337
        assert len(rpc.calls) == calls_before
    finally:
        cur.execute(f"DROP SCHEMA {schema} CASCADE")
        conn.close()


def test_observe_tx_writes_through_the_callers_cursor_and_never_opens_a_second_connection():
    from core.ledger.kinds import AddressKinds
    from core.ledger.types import TokenReg, TransferLeg, TxBundle, TxMeta

    executed = []

    class Cur:
        def execute(self, sql, params=None):
            executed.append(sql)

        def fetchall(self):
            return []

    def boom():
        raise AssertionError("a second connection was opened inside the caller's transaction")

    def rpc(calls):
        return ["0x6001" for _ in calls]

    token = "0x" + "aa" * 20
    pool = "0x" + "b0" * 20
    wallet = "0x" + "11" * 20
    router = "0x" + "70" * 20
    wmon = "0x3bd359c1119da7da1d913d1c4d2b7c461115433a"
    registry = {token: TokenReg(token=token, source="crystal", registered_block=1)}
    kinds = AddressKinds(boom, rpc=rpc, min_txs=2)
    cur = Cur()
    for i in (1, 2):
        txh = f"0x{i:064x}"
        bundle = TxBundle(
            txhash=txh,
            block_number=100 + i,
            tx_index=1,
            timestamp=1_700_000_000,
            transfers=[
                TransferLeg(log_index=1, token=wmon, from_addr=router, to_addr=pool, amount=10**18),
                TransferLeg(log_index=2, token=token, from_addr=pool, to_addr=router, amount=10**18),
                TransferLeg(log_index=3, token=token, from_addr=router, to_addr=wallet, amount=10**18),
            ],
            venue_events=[],
            meta=TxMeta(
                txhash=txh, block_number=100 + i, tx_index=1, from_addr=wallet, to_addr=router, value=0, selector="0x"
            ),
            trace=None,
            userop_sender=None,
        )
        newly = kinds.observe_tx(bundle, registry, cur)
    assert newly == [pool]
    assert any("INSERT INTO address_kinds" in sql for sql in executed)
    assert any("INSERT INTO venues" in sql for sql in executed)
