import os
import subprocess
import sys

from core.ledger_gate import LedgerGate

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class FakeCursor:
    def __init__(self, regclass):
        self._regclass = regclass
        self.executed: list[str] = []

    def execute(self, sql, params=None):
        self.executed.append(sql)

    def fetchone(self):
        return (self._regclass, self._regclass)


class FakeEngine:
    def __init__(self, cur_factory, enabled=None):
        self.calls: list[tuple] = []

    def process_block(self, blk, ts, logs, cur):
        self.calls.append(("process_block", blk))
        return 3

    def cover(self, cur, tokens, from_block, to_block):
        self.calls.append(("cover", tokens, from_block, to_block))

    def flush(self, cur):
        self.calls.append(("flush",))
        return 1


def test_importing_the_sequencer_with_the_flag_off_loads_nothing_from_the_ledger():
    env = {k: v for k, v in os.environ.items() if k != "LEDGER_ENABLED"}
    code = "import sys, core.sequencer; print('core.ledger.engine' in sys.modules, 'core.ledger' in sys.modules)"
    out = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=env, capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr[-2000:]
    assert out.stdout.strip() == "False False"


def test_gate_stays_off_when_the_flag_is_off(monkeypatch):
    monkeypatch.delenv("LEDGER_ENABLED", raising=False)
    gate = LedgerGate(None)
    cur = FakeCursor("wallet_flows")
    assert gate.enabled is False
    assert gate.process_block(1, 1, [], cur) == 0
    assert gate.flush(cur) == 0
    assert cur.executed == []


def test_gate_turns_itself_off_when_the_ledger_tables_are_missing(monkeypatch, capsys):
    monkeypatch.setenv("LEDGER_ENABLED", "1")
    monkeypatch.setattr("core.ledger.engine.LedgerEngine", FakeEngine)
    gate = LedgerGate(None)
    cur = FakeCursor(None)
    assert gate.enabled is True
    assert gate.process_block(1, 1, [{}], cur) == 0
    assert gate.enabled is False
    assert gate.flush(cur) == 0
    assert "the ledger tables do not exist here" in capsys.readouterr().out
    assert len(cur.executed) == 1


def test_gate_runs_the_engine_and_covers_each_block_when_the_tables_exist(monkeypatch):
    monkeypatch.setenv("LEDGER_ENABLED", "true")
    monkeypatch.setattr("core.ledger.engine.LedgerEngine", FakeEngine)
    gate = LedgerGate(None)
    cur = FakeCursor("wallet_flows")
    assert gate.process_block(7, 1, [{}], cur) == 3
    assert gate.process_block(8, 1, [], cur) == 3
    assert gate.flush(cur) == 1
    assert gate._engine.calls == [
        ("process_block", 7),
        ("cover", None, 7, 7),
        ("process_block", 8),
        ("cover", None, 8, 8),
        ("flush",),
    ]
    assert len(cur.executed) == 1, "the table check runs once, not per block"


def test_the_batch_flush_leaves_the_served_positions_to_the_ledger_once_it_is_on(monkeypatch):
    import core.sequencer as sequencer

    calls = []
    for name in (
        "insert_trades_batch",
        "update_tokens_batch",
        "update_users_batch",
        "upsert_ohlcv_batch",
        "add_snipers_batch",
    ):
        monkeypatch.setattr(sequencer.storage, name, lambda *a, _n=name, **k: calls.append(_n))
    monkeypatch.setattr(
        sequencer.storage, "upsert_positions_batch", lambda *a, **k: calls.append("upsert_positions_batch")
    )
    batch = sequencer.BatchAccumulator()
    batch.position_updates[("0xab", "0xcd")] = {"token_bought_delta": 1}

    monkeypatch.setattr(sequencer.LEDGER, "enabled", True)
    batch.flush(cur=None)
    assert "upsert_positions_batch" not in calls
    assert not batch.position_updates

    calls.clear()
    batch.position_updates[("0xab", "0xcd")] = {"token_bought_delta": 1}
    monkeypatch.setattr(sequencer.LEDGER, "enabled", False)
    batch.flush(cur=None)
    assert "upsert_positions_batch" in calls


def test_the_attribution_reconciler_stands_down_once_the_ledger_is_on(monkeypatch):
    """The reconciler invents a trade for whatever the venue's amount and the wallet's transfers disagree
    by, which patched the legacy accumulator. With the fold owning positions those rows only pollute the
    feed, dated 1970 when the block's timestamp is unknown. The buffer it drains must still be drained."""
    import core.sequencer as sequencer

    class State:
        def __init__(self):
            self.drained = 0
            self.reconciled = []

        def take_attributed_token_deltas(self):
            self.drained += 1
            return {("0xtx", "0xtoken", "0xuser"): (10**18, 5 * 10**18)}

        def apply_reconciliation_trade(self, **kw):
            self.reconciled.append(kw)
            return True

    seq = object.__new__(sequencer.Sequencer)
    seq._state = State()
    seq._block_timestamps = {}
    seq.attribution_mismatches = 0
    maps = {
        ("0xtx", "0xtoken"): {
            "ordered": [{"to": "0xuser", "from": "0xpool", "amount": 3 * 10**18, "log_idx": 7, "tx_index": 1}]
        }
    }

    monkeypatch.setattr(sequencer.LEDGER, "enabled", True)
    seq._verify_attribution(100, maps)
    assert seq._state.drained == 1
    assert seq._state.reconciled == []
    assert seq.attribution_mismatches == 0

    monkeypatch.setattr(sequencer.LEDGER, "enabled", False)
    seq._verify_attribution(100, maps)
    assert seq._state.drained == 2
    assert len(seq._state.reconciled) == 1
