import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import chain as h

VAULT_TAGS = ("VD", "VDP", "VWD", "VLOCK", "VUNLOCK", "VCLOSE", "VMAX", "VLOCKUP", "VDECR")


RETIRED_FACTORIES = (
    "0x2388208c8f39e1e5a7ffbf8a2b30c73c7009cc00",
    "0xe35937f2c0e01589a9e8e9a2c9223b7b816247e7",
    "0x3dbf7da6bec21f82e75e693cff1d0bfa0cd07db1",
)


CRYSTAL_CORE = "0x23df569a15b8c0c2bbddff0a9b312c58f4893f97"
VAULT_FACTORY = "0xae1cc58d968dbafb80afdd90fe08b23af5e2c70b"

ADDRESS_ENV_VARS = (
    "CRYSTAL_ADDRESS",
    "ROUTER_ADDRESS",
    "VAULT_FACTORY_ADDRESS",
    "VAULTS_ADDRESS",
    "VAULT_FACTORY_ADDRESSES",
)


@pytest.fixture
def committed_defaults(monkeypatch):
    """chain.py as production resolves it, since prod sets no address env and a local .env would mask the default."""
    import importlib

    for name in ADDRESS_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    module = importlib.reload(h)
    yield module
    for name in ADDRESS_ENV_VARS:
        monkeypatch.undo()
    importlib.reload(h)


def test_only_the_current_factory_is_indexed(committed_defaults):
    assert committed_defaults.VAULT_FACTORY_ADDRS == [VAULT_FACTORY]


def test_retired_factories_are_no_longer_accepted(committed_defaults):
    h = committed_defaults
    for factory in RETIRED_FACTORIES:
        for tag in VAULT_TAGS:
            assert h.accepts_log_for_indexing(tag, factory) is False, (
                f"{tag} still accepted from retired factory {factory}"
            )


def test_every_configured_factory_is_accepted():
    for factory in h.VAULT_FACTORY_ADDRS:
        for tag in VAULT_TAGS:
            assert h.accepts_log_for_indexing(tag, factory) is True, f"{tag} rejected from {factory}"


def test_vault_events_are_rejected_from_unknown_addresses():
    for tag in VAULT_TAGS:
        assert h.accepts_log_for_indexing(tag, "0x" + "9" * 40) is False


def test_every_vault_factory_is_watched_and_cached():
    for factory in h.VAULT_FACTORY_ADDRS:
        assert factory in h.ADDRS, f"{factory} must be in ADDRS or its logs are never fetched"


def test_the_current_factory_is_still_the_configured_one():
    assert h.CONTRACTS["VAULTS"].lower() in h.VAULT_FACTORY_ADDRS


def test_factory_addresses_are_lowercase_and_unique():
    assert h.VAULT_FACTORY_ADDRS == [a.lower() for a in h.VAULT_FACTORY_ADDRS]
    assert len(set(h.VAULT_FACTORY_ADDRS)) == len(h.VAULT_FACTORY_ADDRS)


def test_state_applies_vault_events_from_every_generation():
    import inspect

    import state as state_module

    src = inspect.getsource(state_module)
    assert 'h.CONTRACTS["VAULTS"].lower()' not in src, (
        "a vault apply guard still pins to one factory, so retired generation events are dropped"
    )
    assert src.count("not in h.VAULT_FACTORY_ADDRS") >= 9


def test_the_crystal_core_is_the_new_deployment(committed_defaults):
    c = committed_defaults
    assert c.CRYSTAL_ADDR == CRYSTAL_CORE
    assert c.CONTRACTS["ROUTER"].lower() == CRYSTAL_CORE
    assert c.accepts_log_for_indexing("MC", CRYSTAL_CORE) is True
    assert c.accepts_log_for_indexing("MC", "0x8e42afa92a8b0ed3ee23db6b108419aae47ad61f") is False


def test_nadfun_indexing_is_untouched_by_the_crystal_migration():
    assert len(h.NADFUN_ADDRS) == 2, "nad.fun history must keep indexing across the crystal redeploy"
    for addr in h.NADFUN_ADDRS:
        assert addr in h.ADDRS
