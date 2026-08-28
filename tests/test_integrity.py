from core.integrity import evaluate


def test_evaluate_clean():
    r = evaluate(1000, 1.0, 1010, 1, 744, 744, 0)
    assert r["ok"] is True
    assert r["findings"] == []
    assert r["processed_gaps"] == 0
    assert r["cache_holes"] == 0
    assert r["lag"] == 10


def test_evaluate_gaps():
    r = evaluate(1000, 1.0, 1010, 1, 744, 700, 0)
    assert r["ok"] is False
    assert r["processed_gaps"] == 44
    assert any("never processed" in f for f in r["findings"])


def test_evaluate_holes():
    r = evaluate(1000, 1.0, 1010, 1, 744, 744, 12)
    assert r["ok"] is False
    assert r["cache_holes"] == 12
    assert any("missing cached logs" in f for f in r["findings"])


def test_evaluate_stall_and_lag():
    r = evaluate(1000, 300.0, 5000, 1, 744, 744, 0)
    assert r["ok"] is False
    assert r["lag"] == 4000
    assert any("no block processed" in f for f in r["findings"])
    assert any("behind the chain head" in f for f in r["findings"])


def test_evaluate_no_head():
    r = evaluate(1000, 1.0, None, 1, 744, 744, 0)
    assert r["ok"] is True
    assert r["lag"] is None
