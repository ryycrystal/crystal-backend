from core.sequencer import Sequencer

TXH = "0x09131fec043a601c37c359e8d91b8d84954e480c0c22a2138f6fdbc55fbe74c3"
TOKEN = "0x405b6330e213ded490240cbcdd64790806827777"
USER = "0xb9e37df144f7e6a86da69642a1f01bec7d2035d2"
PERMIT2 = "0xb92fe925dc43a0ecde6c8b1a2709c170ec4fff4f"

DELIVERED = 7038992716474790000000000
CANONICAL_LEG_ONLY = 3939541450000000000000000


class _FakeState:
    def __init__(self, attributed):
        self._attributed = attributed

    def take_attributed_token_deltas(self):
        taken = self._attributed
        self._attributed = {}
        return taken


class _Stub:
    def __init__(self, attributed):
        self._state = _FakeState(attributed)
        self.attribution_mismatches = 0


def _maps():
    return {
        (TXH, TOKEN): {
            "ordered": [
                {"log_idx": 128, "from": PERMIT2, "to": USER, "amount": DELIVERED},
            ]
        }
    }


def test_fires_when_only_one_leg_of_a_routed_buy_was_attributed(capsys):
    stub = _Stub({(TXH, TOKEN, USER): CANONICAL_LEG_ONLY})
    Sequencer._verify_attribution(stub, 101979140, _maps())
    assert stub.attribution_mismatches == 1
    out = capsys.readouterr().out
    assert "attribution mismatch" in out
    assert str(DELIVERED - CANONICAL_LEG_ONLY) in out


def test_silent_once_every_leg_is_attributed():
    stub = _Stub({(TXH, TOKEN, USER): DELIVERED})
    Sequencer._verify_attribution(stub, 101979140, _maps())
    assert stub.attribution_mismatches == 0


def test_dust_rounding_below_tolerance_does_not_fire():
    stub = _Stub({(TXH, TOKEN, USER): DELIVERED - 1})
    Sequencer._verify_attribution(stub, 101979140, _maps())
    assert stub.attribution_mismatches == 0


def test_a_sell_is_compared_with_the_right_sign():
    maps = {(TXH, TOKEN): {"ordered": [{"log_idx": 5, "from": USER, "to": PERMIT2, "amount": DELIVERED}]}}
    stub = _Stub({(TXH, TOKEN, USER): -DELIVERED})
    Sequencer._verify_attribution(stub, 1, maps)
    assert stub.attribution_mismatches == 0

    stub = _Stub({(TXH, TOKEN, USER): -CANONICAL_LEG_ONLY})
    Sequencer._verify_attribution(stub, 1, maps)
    assert stub.attribution_mismatches == 1


def test_nothing_attributed_means_nothing_to_check():
    stub = _Stub({})
    Sequencer._verify_attribution(stub, 1, _maps())
    assert stub.attribution_mismatches == 0
