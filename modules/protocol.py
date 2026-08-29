from __future__ import annotations

BALANCE_DEPOSIT_TOPIC = "0xd2f8022f659fd9c8c558f30c00fd5ee7038f7cb56da45095c3e0e7d48b3e0c4b"
BALANCE_WITHDRAW_TOPIC = "0x457f950b75085c30ff780acd57bde642ff1316cc4aad9f286af2c1ffc4163a78"
LAUNCHPAD_PARAMS_TOPIC = "0x5d2f0c0dd6d77e3386b737e8b626250fe38b8f1afdad9554151797d97496a80a"
GOV_CHANGED_TOPIC = "0x3d1e4c3a68fed9f4f8315582b7297cf8fa264bc8e6704287603ba8c72bf05ac2"

LAUNCHPAD_PARAM_FIELDS = (
    "initial_native_supply",
    "launchpad_fee",
    "creator_fee_split",
    "graduated_min_size",
    "graduated_taker_fee",
    "graduated_maker_rebate",
    "graduated_creator_fee_split",
)


def _word(data: str, index: int) -> int:
    chunk = data[index * 64 : (index + 1) * 64]
    if len(chunk) < 64:
        return 0
    try:
        return int(chunk, 16)
    except ValueError:
        return 0


def _addr(topic: str) -> str:
    return ("0x" + str(topic)[-40:]).lower()


def _parse_balance_move(kind: str, topics: list[str], data: str) -> dict | None:
    if len(topics) < 4:
        return None
    return {
        "kind": kind,
        "user": _addr(topics[1]),
        "user_id": int(topics[2], 16) if topics[2] else 0,
        "token": _addr(topics[3]),
        "amount": _word(data, 0),
    }


def parse_balance_deposit(addr: str, topics: list[str], data: str) -> dict | None:
    return _parse_balance_move("deposit", topics, data)


def parse_balance_withdraw(addr: str, topics: list[str], data: str) -> dict | None:
    return _parse_balance_move("withdraw", topics, data)


def parse_launchpad_params_changed(addr: str, topics: list[str], data: str) -> dict | None:
    return {
        "kind": "launchpad_params_changed",
        "params": {name: _word(data, i) for i, name in enumerate(LAUNCHPAD_PARAM_FIELDS)},
    }


def parse_gov_changed(addr: str, topics: list[str], data: str) -> dict | None:
    return {
        "kind": "gov_changed",
        "params": {
            "previous": ("0x" + data[24:64]).lower() if len(data) >= 64 else "0x" + "0" * 40,
            "gov": ("0x" + data[88:128]).lower() if len(data) >= 128 else "0x" + "0" * 40,
        },
    }
