import inspect
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from api import ws


def test_channel_is_registered_everywhere():
    assert "tracked_trades" in ws.KNOWN_CHANNELS
    assert "tracked_trades" in ws.IMPLEMENTED_CHANNELS


def test_push_and_snapshot_are_wired():
    src = inspect.getsource(ws)
    assert '"tracked_trades": self._push_tracked_trades' in src
    assert 'if channel == "tracked_trades":' in src


def test_row_shape_carries_token_identity_for_alert_cards():
    from api import ws_data

    src = inspect.getsource(ws_data.tracked_wallet_trades)
    for field in ("symbol", "name", "metadataCid", "caller", "isBuy", "blockNumber"):
        assert f'"{field}"' in src


def test_empty_address_list_returns_nothing():
    from api.ws_data import tracked_wallet_trades

    assert tracked_wallet_trades([]) == []
