import json

from core.ledger.receipts import RECEIPT_LOG_TABLE, ReceiptLogs, txs_missing_venue_logs
from core.ledger.types import TRANSFER_TOPIC

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
