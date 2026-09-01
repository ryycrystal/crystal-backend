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


def test_evaluate_flags_vault_ledger_divergence():
    # the shape of the real 9/1 gap: two withdrawals dropped at index time, so the
    # ledger still credits shares the chain says are burned
    divs = [
        ("0xvault", "0xdf90", 345181111881864, 0),
        ("0xvault", "0x5664", 16563903913321, 0),
    ]
    r = evaluate(1000, 1.0, 1010, 1, 744, 744, 0, divs)
    assert r["ok"] is False
    assert any("ledger disagrees with their chain shares" in f for f in r["findings"])
    assert r["vault_ledger_divergences"][0]["ledgerNet"] == "345181111881864"
    assert r["vault_ledger_divergences"][0]["chainShares"] == "0"


def test_evaluate_stays_clean_when_ledger_matches_chain():
    r = evaluate(1000, 1.0, 1010, 1, 744, 744, 0, [])
    assert r["ok"] is True
    assert r["vault_ledger_divergences"] == []
    r2 = evaluate(1000, 1.0, 1010, 1, 744, 744, 0)
    assert r2["ok"] is True


def test_evaluate_flags_vault_counter_drift():
    # the shape of the real 9/1 residue: the backfill wrote withdrawal rows straight
    # through insert_crystal_vault_withdrawal, so shares reconciled but the counters
    # the vault page renders never moved
    drift = [
        ("0xvault", "0x5664", "withdraws", 0, 1),
        ("0xvault", "0x5664", "last_withdraw", 0, 1787903175),
    ]
    r = evaluate(1000, 1.0, 1010, 1, 744, 744, 0, [], drift)
    assert r["ok"] is False
    assert any("counters disagree with the ledger" in f for f in r["findings"])
    assert r["vault_user_counter_drift"][0]["field"] == "withdraws"
    assert r["vault_user_counter_drift"][0]["stored"] == "0"
    assert r["vault_user_counter_drift"][0]["actual"] == "1"


def test_counter_drift_is_independent_of_share_divergence():
    # shares matching chain is exactly the state that hid this for a day
    r = evaluate(1000, 1.0, 1010, 1, 744, 744, 0, [], [("0xv", "0xu", "withdraws", 0, 1)])
    assert r["ok"] is False
    assert r["vault_ledger_divergences"] == []
    r2 = evaluate(1000, 1.0, 1010, 1, 744, 744, 0, [], [])
    assert r2["ok"] is True
    assert r2["vault_user_counter_drift"] == []
