from core.sequencer import Sequencer

TXH = "0x09131fec043a601c37c359e8d91b8d84954e480c0c22a2138f6fdbc55fbe74c3"
TOKEN = "0x405b6330e213ded490240cbcdd64790806827777"
USER = "0xb9e37df144f7e6a86da69642a1f01bec7d2035d2"
PERMIT2 = "0xb92fe925dc43a0ecde6c8b1a2709c170ec4fff4f"

DELIVERED = 7038992716474790000000000
CANONICAL_LEG_ONLY = 3939541450000000000000000
CANONICAL_NATIVE = 88936900000000000000000


class _FakeState:
    def __init__(self, attributed):
        self._attributed = attributed
        self.reconciled = []

    def take_attributed_token_deltas(self):
        taken = self._attributed
        self._attributed = {}
        return taken

    def apply_reconciliation_trade(self, **kw):
        self.reconciled.append(kw)
        return True


class _Stub:
    def __init__(self, attributed):
        self._state = _FakeState(attributed)
        self.attribution_mismatches = 0
        self._block_timestamps = {101979140: 1788400000}


def _maps():
    return {
        (TXH, TOKEN): {
            "ordered": [
                {"log_idx": 128, "from": PERMIT2, "to": USER, "amount": DELIVERED},
            ]
        }
    }


def _sell_maps():
    return {(TXH, TOKEN): {"ordered": [{"log_idx": 5, "from": USER, "to": PERMIT2, "amount": DELIVERED}]}}


def test_a_gap_is_reconciled_rather_than_only_logged():
    stub = _Stub({(TXH, TOKEN, USER): (CANONICAL_LEG_ONLY, CANONICAL_NATIVE)})
    Sequencer._verify_attribution(stub, 101979140, _maps(), cur=None, batch=object())
    assert stub.attribution_mismatches == 0
    assert len(stub._state.reconciled) == 1
    r = stub._state.reconciled[0]
    assert r["token_delta"] == DELIVERED - CANONICAL_LEG_ONLY
    assert r["log_idx"] == 128
    assert r["txh"] == TXH


def test_the_reconciled_leg_is_priced_at_the_observed_legs_vwap():
    stub = _Stub({(TXH, TOKEN, USER): (CANONICAL_LEG_ONLY, CANONICAL_NATIVE)})
    Sequencer._verify_attribution(stub, 101979140, _maps(), cur=None, batch=object())
    r = stub._state.reconciled[0]
    missing = DELIVERED - CANONICAL_LEG_ONLY
    assert r["native_amount"] == missing * CANONICAL_NATIVE // CANONICAL_LEG_ONLY
    observed_price = CANONICAL_NATIVE / CANONICAL_LEG_ONLY
    imputed_price = r["native_amount"] / r["token_delta"]
    assert abs(imputed_price - observed_price) / observed_price < 1e-6


def test_silent_once_every_leg_is_attributed():
    stub = _Stub({(TXH, TOKEN, USER): (DELIVERED, CANONICAL_NATIVE)})
    Sequencer._verify_attribution(stub, 101979140, _maps(), cur=None, batch=object())
    assert stub.attribution_mismatches == 0
    assert stub._state.reconciled == []


def test_dust_rounding_below_tolerance_does_not_reconcile():
    stub = _Stub({(TXH, TOKEN, USER): (DELIVERED - 1, CANONICAL_NATIVE)})
    Sequencer._verify_attribution(stub, 101979140, _maps(), cur=None, batch=object())
    assert stub._state.reconciled == []
    assert stub.attribution_mismatches == 0


def test_a_sell_reconciles_with_a_negative_delta():
    stub = _Stub({(TXH, TOKEN, USER): (-CANONICAL_LEG_ONLY, CANONICAL_NATIVE)})
    Sequencer._verify_attribution(stub, 101979140, _sell_maps(), cur=None, batch=object())
    assert len(stub._state.reconciled) == 1
    assert stub._state.reconciled[0]["token_delta"] == -(DELIVERED - CANONICAL_LEG_ONLY)


def test_without_a_batch_it_falls_back_to_logging():
    stub = _Stub({(TXH, TOKEN, USER): (CANONICAL_LEG_ONLY, CANONICAL_NATIVE)})
    Sequencer._verify_attribution(stub, 101979140, _maps(), cur=None, batch=None)
    assert stub.attribution_mismatches == 1
    assert stub._state.reconciled == []


def test_nothing_attributed_means_nothing_to_check():
    stub = _Stub({})
    Sequencer._verify_attribution(stub, 101979140, _maps(), cur=None, batch=object())
    assert stub.attribution_mismatches == 0


def test_transfer_maps_ignore_a_log_delivered_twice():
    log = {
        "address": TOKEN,
        "topics": [
            "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef",
            "0x" + "0" * 24 + PERMIT2[2:],
            "0x" + "0" * 24 + USER[2:],
        ],
        "data": "0x" + f"{DELIVERED:064x}",
        "transactionHash": TXH,
        "logIndex": hex(128),
    }
    maps = Sequencer._build_transfer_maps(_Stub({}), [log, dict(log)])
    ordered = maps[(TXH, TOKEN)]["ordered"]
    assert len(ordered) == 1
    assert ordered[0]["amount"] == DELIVERED
