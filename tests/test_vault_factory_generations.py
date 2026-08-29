import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import chain as h

VAULT_TAGS = ("VD", "VDP", "VWD", "VLOCK", "VUNLOCK", "VCLOSE", "VMAX", "VLOCKUP", "VDECR")


def test_every_vault_generation_is_accepted():
    assert len(h.VAULT_FACTORY_ADDRS) >= 2, "a retired factory still holds user funds and must stay indexed"
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
