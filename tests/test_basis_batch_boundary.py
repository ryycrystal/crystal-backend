import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import state as state_mod  # noqa: E402

E = 10**18
TOKEN = "0xtoken"
USER = "0xuser000000000000000000000000000000000001"


class DbBackedBasis(state_mod.State):
    """Only the basis overlay machinery, over a stand-in positions table."""

    def __init__(self, committed):
        self._basis_overlay = {}
        self._basis_block = -1
        self.committed = committed

    def _basis_for(self, user, token, cur=None):
        key = ((user or "").lower(), (token or "").lower())
        entry = self._basis_overlay.get(key)
        if entry is None:
            open_tokens, cost_basis = self.committed.get(key, (0, 0))
            entry = [int(open_tokens), int(cost_basis)]
            self._basis_overlay[key] = entry
        return entry


def test_sell_keeps_basis_from_a_buy_earlier_in_the_same_batch():
    # the table has nothing yet: this chunk's writes are still in the accumulator
    st = DbBackedBasis(committed={})

    st._basis_reset_if_new_block(100, batched=True)
    st._basis_apply_buy(USER, TOKEN, token_amt=1000 * E, native_amt=8598 * E)

    # a later block in the SAME chunk, so the overlay must not re-seed from the table
    st._basis_reset_if_new_block(127, batched=True)
    released = st._basis_apply_sell(USER, TOKEN, token_amt=1000 * E)

    assert released == 8598 * E, "the sell must release the basis the buy created"
    realized = 8581 * E - released
    assert realized < 0, f"a sell below cost must not book a profit, got {realized / E}"


def test_overlay_reseeds_from_the_table_once_the_batch_flushed():
    st = DbBackedBasis(committed={(USER, TOKEN): (1000 * E, 8598 * E)})
    st._basis_reset_if_new_block(100, batched=True)
    st._basis_apply_buy(USER, TOKEN, token_amt=1000 * E, native_amt=8598 * E)

    st.basis_clear_overlay()

    # after the flush boundary the committed row is authoritative again
    entry = st._basis_for(USER, TOKEN)
    assert entry == [1000 * E, 8598 * E]


def test_unbatched_path_still_resets_per_block():
    st = DbBackedBasis(committed={})
    st._basis_reset_if_new_block(100)
    st._basis_apply_buy(USER, TOKEN, token_amt=1000 * E, native_amt=50 * E)
    st._basis_reset_if_new_block(101)
    assert st._basis_overlay == {}, "outside a batch each block re-reads the table"
