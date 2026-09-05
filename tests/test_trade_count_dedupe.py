import state as state_mod

USER = "0xb9e37df144f7e6a86da69642a1f01bec7d2035d2"
TOKEN = "0x405b6330e213ded490240cbcdd64790806827777"
TXH = "0x09131fec043a601c37c359e8d91b8d84954e480c0c22a2138f6fdbc55fbe74c3"


class CountingState(state_mod.State):
    """Only the per-transaction trade-count bookkeeping."""

    def __init__(self):
        self._basis_overlay = {}
        self._basis_block = -1
        self._counted_trade_keys = set()

    def count_leg(self, txh, token, user):
        key = ((txh or "").lower(), token, user)
        if key in self._counted_trade_keys:
            return 0
        self._counted_trade_keys.add(key)
        return 1


def test_a_routed_buy_counts_once_no_matter_how_many_legs_filled_it():
    st = CountingState()
    legs = [st.count_leg(TXH, TOKEN, USER) for _ in range(3)]
    assert legs == [1, 0, 0]
    assert sum(legs) == 1


def test_separate_transactions_each_count():
    st = CountingState()
    assert st.count_leg(TXH, TOKEN, USER) == 1
    assert st.count_leg("0x" + "ab" * 32, TOKEN, USER) == 1


def test_different_users_or_tokens_in_one_transaction_count_separately():
    st = CountingState()
    assert st.count_leg(TXH, TOKEN, USER) == 1
    assert st.count_leg(TXH, TOKEN, "0x" + "11" * 20) == 1
    assert st.count_leg(TXH, "0x" + "22" * 20, USER) == 1


def test_the_flush_hook_clears_the_keys_so_a_later_block_counts_again():
    st = CountingState()
    assert st.count_leg(TXH, TOKEN, USER) == 1
    st.basis_clear_overlay()
    assert st.count_leg(TXH, TOKEN, USER) == 1


def test_a_real_state_starts_with_no_counted_keys():
    assert state_mod.State.__init__ is not CountingState.__init__
    st = CountingState()
    assert st._counted_trade_keys == set()
