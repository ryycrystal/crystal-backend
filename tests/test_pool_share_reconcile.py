import os
import sys
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from state import State


def _state_with_pool(total_shares: int, market_type: int = 2) -> tuple[State, str]:
    st = State()
    market = "0xd6043115d75d622acf4f233e9ab0178c05a47ff7"
    st.addressToMarket[market] = SimpleNamespace(market=market, marketType=market_type, totalShares=total_shares)
    return st, market


def test_lp_market_addresses_only_lists_amm_markets():
    st, market = _state_with_pool(100)
    clob = "0x1111111111111111111111111111111111111111"
    st.addressToMarket[clob] = SimpleNamespace(market=clob, marketType=1, totalShares=0)
    assert st.lp_market_addresses() == [market]


def test_reconcile_pool_shares_heals_understated_supply():
    st, market = _state_with_pool(68513768385269402489302)
    chain = 962940959385185281052971
    with patch("state.storage.update_crystal_pool_total_shares") as write:
        st.reconcile_pool_shares(market, chain)
    assert st.addressToMarket[market].totalShares == chain
    write.assert_called_once_with(market, chain)


def test_reconcile_pool_shares_ignores_zero_and_matching_supply():
    st, market = _state_with_pool(500)
    with patch("state.storage.update_crystal_pool_total_shares") as write:
        st.reconcile_pool_shares(market, 0)
        st.reconcile_pool_shares(market, 500)
    assert st.addressToMarket[market].totalShares == 500
    write.assert_not_called()


def test_reconcile_pool_shares_ignores_unknown_market():
    st, _ = _state_with_pool(500)
    with patch("state.storage.update_crystal_pool_total_shares") as write:
        st.reconcile_pool_shares("0x2222222222222222222222222222222222222222", 999)
    write.assert_not_called()
