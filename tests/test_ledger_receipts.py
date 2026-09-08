import json

from core.ledger.receipts import RECEIPT_LOG_TABLE, ReceiptLogs, txs_missing_venue_logs
from core.ledger.types import TRANSFER_TOPIC
from modules import univ4

MANAGER = "0x" + "b4" * 20
POOL = "0x" + "b0" * 20
WALLET = "0x" + "11" * 20
TOKEN = "0x" + "aa" * 20
TX_V4 = "0x" + "01" * 32
TX_V3 = "0x" + "02" * 32
TX_SEEN = "0x" + "03" * 32
SWAP_TOPIC = "0x" + "5c" * 32


def _topic(addr: str) -> str:
    return "0x" + addr.removeprefix("0x").rjust(64, "0")


def transfer(txhash: str, log_index: int, src: str, dst: str) -> dict:
    return {
        "address": TOKEN,
        "topics": [TRANSFER_TOPIC, _topic(src), _topic(dst)],
        "data": "0x" + "1".rjust(64, "0"),
        "transactionHash": txhash,
        "logIndex": hex(log_index),
        "blockTimestamp": "0x1",
    }


def manager_log(txhash: str, log_index: int) -> dict:
    return {
        "address": MANAGER.upper(),
        "topics": [SWAP_TOPIC],
        "data": "0x",
        "transactionHash": txhash,
        "logIndex": hex(log_index),
    }


class FakeRpc:
    def __init__(self, receipts: dict):
        self.receipts = receipts
        self.calls: list[list] = []

    def batch(self, calls):
        self.calls.append(calls)
        out = []
        for method, params in calls:
            assert method == "eth_getTransactionReceipt"
            receipt = self.receipts.get(params[0])
            out.append({"result": receipt} if receipt is not None else {"error": {"code": -32000}})
        return out


class FakeCursor:
    def __init__(self):
        self.rows: dict[str, list] = {}
        self._result: list = []

    def execute(self, sql, params=None):
        assert RECEIPT_LOG_TABLE in sql
        if sql.startswith("SELECT"):
            self._result = [(txhash, self.rows[txhash]) for txhash in params[0] if txhash in self.rows]
            return
        assert sql.count("(%s, %s::jsonb)") == len(params) // 2
        for i in range(0, len(params), 2):
            self.rows.setdefault(params[i], json.loads(params[i + 1]))

    def fetchall(self):
        return list(self._result)


def test_txs_missing_venue_logs_wants_transfers_touching_the_venue_without_its_logs():
    logs = [
        transfer(TX_V4, 1, MANAGER, WALLET),
        transfer(TX_V3, 2, POOL, WALLET),
        transfer(TX_SEEN, 3, WALLET, MANAGER),
        manager_log(TX_SEEN, 4),
    ]
    assert txs_missing_venue_logs(logs, MANAGER) == [TX_V4]
    assert txs_missing_venue_logs(logs, MANAGER, {TOKEN}) == [TX_V4]
    assert txs_missing_venue_logs(logs, MANAGER, {POOL}) == []
    assert ReceiptLogs(FakeRpc({}), MANAGER, {WALLET}).complete({1: logs}, FakeCursor()) == 0


def test_complete_fetches_manager_logs_once_and_merges_them_into_the_block():
    rpc = FakeRpc({TX_V4: {"logs": [manager_log(TX_V4, 7), transfer(TX_V4, 8, WALLET, POOL)]}})
    cur = FakeCursor()
    receipts = ReceiptLogs(rpc, MANAGER)
    block_logs = {100: [transfer(TX_V4, 1, MANAGER, WALLET), transfer(TX_V3, 2, POOL, WALLET)]}

    assert receipts.complete(block_logs, cur) == 1
    assert [lg["logIndex"] for lg in block_logs[100]] == ["0x1", "0x2", "0x7"]
    assert block_logs[100][2]["address"] == MANAGER.upper()
    assert rpc.calls == [[("eth_getTransactionReceipt", [TX_V4])]]
    assert cur.rows[TX_V4] == [manager_log(TX_V4, 7)]

    again = {101: [transfer(TX_V4, 1, MANAGER, WALLET)]}
    assert receipts.complete(again, cur) == 1
    assert len(rpc.calls) == 1
    assert receipts.complete(again, cur) == 0
    assert (receipts.fetched, receipts.added) == (1, 2)


def test_complete_leaves_unanswered_receipts_for_a_later_attempt():
    rpc = FakeRpc({})
    cur = FakeCursor()
    receipts = ReceiptLogs(rpc, MANAGER)
    block_logs = {100: [transfer(TX_V4, 1, MANAGER, WALLET)]}
    assert receipts.complete(block_logs, cur) == 0
    assert cur.rows == {}
    assert receipts.complete(block_logs, cur) == 0
    assert len(rpc.calls) == 2


class FakeBlockRpc(FakeRpc):
    def __init__(self, blocks: dict[int, list[dict]], receipts: dict):
        super().__init__(receipts)
        self.blocks = blocks

    def batch(self, calls):
        self.calls.append(calls)
        out = []
        for method, params in calls:
            if method == "eth_getBlockReceipts":
                out.append({"result": self.blocks[int(params[0], 16)]})
                continue
            assert method == "eth_getTransactionReceipt"
            receipt = self.receipts.get(params[0])
            out.append({"result": receipt} if receipt is not None else {"error": {"code": -32000}})
        return out


def test_complete_fetches_block_receipts_when_several_transactions_in_a_block_need_them():
    tx_other = "0x" + "04" * 32
    rpc = FakeBlockRpc(
        {
            7: [
                {"transactionHash": TX_V4, "logs": [manager_log(TX_V4, 9)]},
                {"transactionHash": tx_other, "logs": [manager_log(tx_other, 10)]},
                {"transactionHash": TX_V3, "logs": [manager_log(TX_V3, 11), transfer(TX_V3, 12, WALLET, POOL)]},
            ]
        },
        {TX_SEEN: {"logs": [manager_log(TX_SEEN, 13)]}},
    )
    cur = FakeCursor()
    block_logs = {
        7: [transfer(TX_V4, 1, MANAGER, WALLET), transfer(TX_V3, 2, WALLET, MANAGER)],
        8: [transfer(TX_SEEN, 3, MANAGER, WALLET)],
    }

    assert ReceiptLogs(rpc, MANAGER).complete(block_logs, cur) == 3
    assert [(m, p[0]) for calls in rpc.calls for m, p in calls] == [
        ("eth_getBlockReceipts", "0x7"),
        ("eth_getTransactionReceipt", TX_SEEN),
    ]
    assert [lg["logIndex"] for lg in block_logs[7]] == ["0x1", "0x2", "0x9", "0xb"]
    assert sorted(cur.rows) == sorted([TX_V4, TX_V3, TX_SEEN])
    assert cur.rows[TX_V3] == [manager_log(TX_V3, 11)]


def v4_swap_log(txhash: str, log_index: int, amount0: int, amount1: int) -> dict:
    def word(v: int) -> str:
        return (v % (1 << 256)).to_bytes(32, "big").hex()

    data = "0x" + word(amount0) + word(amount1) + word(1) + word(1) + word(0) + word(3000)
    return {
        "address": MANAGER,
        "topics": [univ4.V4_SWAP_TOPIC, "0x" + "9a" * 32, _topic(WALLET)],
        "data": data,
        "transactionHash": txhash,
        "logIndex": hex(log_index),
    }


def test_a_liquidity_change_hidden_behind_a_cached_swap_still_wants_the_receipt():
    swapped = [transfer(TX_V4, 1, MANAGER, WALLET), v4_swap_log(TX_V4, 2, -1, 1)]
    assert txs_missing_venue_logs(swapped, MANAGER, {TOKEN}) == []
    deposited = [transfer(TX_V3, 3, WALLET, MANAGER), v4_swap_log(TX_V3, 4, -5, 5)]
    assert txs_missing_venue_logs(deposited, MANAGER, {TOKEN}) == [TX_V3]
    modified = deposited + [
        {
            "address": MANAGER,
            "topics": [univ4.V4_MODIFY_LIQUIDITY_TOPIC],
            "data": "0x",
            "transactionHash": TX_V3,
            "logIndex": "0x5",
        }
    ]
    assert txs_missing_venue_logs(modified, MANAGER, {TOKEN}) == []
