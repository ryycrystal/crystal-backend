import asyncio
import decimal
import json
import os
import time

import modules.launchpad as lp
import modules.markets as m
import modules.nadfun as n
import modules.orderbook as ob
import modules.pools as p
import modules.protocol as proto
import modules.referrals as ref
import modules.vaults as v
from env_loader import load_env

load_env()

decimal.getcontext().prec = 100


def _parse_transfer(addr: str, tops: list[str], data_no0x: str) -> dict:
    from_addr = lp.to_addr(tops[1]) if len(tops) > 1 else "0x" + "0" * 40
    to_addr = lp.to_addr(tops[2]) if len(tops) > 2 else "0x" + "0" * 40

    amount = 0
    if data_no0x:
        try:
            amount = int(data_no0x[:64], 16)
        except ValueError:
            amount = 0

    return {
        "token": addr.lower(),
        "from": from_addr.lower(),
        "to": to_addr.lower(),
        "amount": amount,
    }


def _addr_from_env(default: str, *names: str) -> str:
    for name in names:
        val = os.getenv(name)
        if val:
            return val.lower()
    return default.lower()


def _addrs_from_env(defaults: list[str], *names: str) -> list[str]:
    values: list[str] = []
    for name in names:
        val = os.getenv(name)
        if val:
            values.extend(v.strip() for v in val.split(",") if v.strip())

    if not values:
        values = defaults

    return list(dict.fromkeys(v.lower() for v in values))


CRYSTAL_ADDR = _addr_from_env(
    "0x6eb2aF5FC575689053Ac9b413220CaBfd01A2F9A",
    "CRYSTAL_ADDRESS",
    "ROUTER_ADDRESS",
)
VAULT_FACTORY_ADDR = _addr_from_env(
    "0xe35937f2c0E01589a9E8E9A2C9223b7B816247E7",
    "VAULT_FACTORY_ADDRESS",
    "VAULTS_ADDRESS",
)
VAULT_FACTORY_LEGACY_ADDR = _addr_from_env(
    "0x3dbf7Da6BeC21F82E75e693cfF1d0BFA0Cd07Db1",
    "VAULT_FACTORY_LEGACY_ADDRESS",
)
NADFUN_ADDR = _addr_from_env(
    "0xA7283d07812a02AFB7C09B60f8896bCEA3F90aCE",
    "NADFUN_ADDRESS",
)
NADFUN_V2_ADDR = _addr_from_env(
    "0x9f3832732923252A21044F21eE6bd87F09514ae4",
    "NADFUN_V2_ADDRESS",
)
REFERRAL_MANAGER_ADDR = _addr_from_env(
    "0x1AB7ea187CEe63Cf01bBD8fa8837C748a769F8DF",
    "REFERRAL_MANAGER_ADDRESS",
)
NADFUN_ADDRS = _addrs_from_env(
    [
        NADFUN_ADDR,
        NADFUN_V2_ADDR,
    ],
    "NADFUN_ADDRESSES",
)
VAULT_FACTORY_ADDRS = _addrs_from_env(
    [
        VAULT_FACTORY_ADDR,
        VAULT_FACTORY_LEGACY_ADDR,
    ],
    "VAULT_FACTORY_ADDRESSES",
)

CONTRACTS = {
    "ROUTER": CRYSTAL_ADDR,
    "CRYSTAL": CRYSTAL_ADDR,
    "VAULTS": VAULT_FACTORY_ADDR,
    "VAULT_FACTORY": VAULT_FACTORY_ADDR,
    "NADFUN": NADFUN_ADDR,
    "REFERRALS": REFERRAL_MANAGER_ADDR,
}
ADDRS = list(dict.fromkeys([*(a.lower() for a in CONTRACTS.values()), *NADFUN_ADDRS, *VAULT_FACTORY_ADDRS]))

EVENT_SIGS = {
    "0xaf714121669901a97bedd215ae52bf255f4b5ecb9b5baa168800e5bdcc32c21a": "MC",
    "0x77c39d27acc1bbb9c6519311b11749cea1e4ac28704c34f4dd35aff06e870442": "MPC",
    "0x9adcf0ad0cda63c4d50f26a48925cf6405df27d422a39c456b5f03f661c82982": "TR",
    "0x2f00e3cdd69a77be7ed215ec7b2a36784dd158f921fca79ac29deffa353fe6ee": "PMINT",
    "0xd3986fdc78865c06fb072387efddb45772a87fe2105e598db99f085be3d05b84": "PBURN",
    "0xc95e30a514d4115dee44b3ba17b2fc114501726562d4c5f2663c06f42df8f1e7": "PSYNC",
    "0x24ad3570873d98f204dae563a92a783a01f6935a8965547ce8bf2cadd2c6ce3b": "TC",
    "0xc367a2f5396f96d105baaaa90fe29b1bb18ef54c712964410d02451e67c19d3e": "LT",
    "0xa2e7361c23d7820040603b83c0cd3f494d377bac69736377d75bb56c651a5098": "MG",
    "0xc06e2355c9da33769608ef0b4a541792c64990d67d8fe190ccc295daffa0a61c": "VD",
    "0x4e2ca0515ed1aef1395f66b5303bb5d6f1bf9d61a353fa53f73f8ac9973fa9f6": "VDP",
    "0xebff2602b3f468259e1e99f613fed6691f3a6526effe6ef3e768ba7ae7a36c4f": "VWD",
    "0x44427e3003a08f22cf803894075ac0297524e09e521fc1c15bc91741ce3dc159": "VLOCK",
    "0x7e6adfec7e3f286831a0200a754127c171a2da564078722cb97704741bbdb0ea": "VUNLOCK",
    "0x13607bf9d2dd20e1f3a7daf47ab12856f8aad65e6ae7e2c75ace3d0c424a40e8": "VCLOSE",
    "0x1e05b6315930dd64f61371495352862f00b5567797d2a62c78f1b527c24a919a": "VMAX",
    "0x00ec7b1c26d1057308e56c6900fb540231a33b23494817d35dbfd7f056078451": "VLOCKUP",
    "0x3f0bf479ded477a0724977a228e5e9afa2efdb537c527aba3cc7169403ef422a": "VDECR",
    "0xd37e3f4f651fe74251701614dbeac478f5a0d29068e87bbe44e5026d166abca9": "NFC",
    "0xac11ceed5187dce8e72afc92116e2aebbbd4fb263cb4021374a5df1b90c89936": "NFC",
    "0x00a7ba871905cb955432583640b5c9fc6bdd27d36884ab2b5420839224638862": "NFB",
    "0x89f5adc174562e07c9c9b1cae7109bbecb21cf9d1b2847e550042b8653c54a0e": "NFB",
    "0x0eb25df0e2137de8ce042eeaf39080d25f0c8d451372c99db69a4c0a298d0fa1": "NFS",
    "0xa082022e93cfcd9f1da5f9236718053910f7e840da080c789c7845698dc032ff": "NFS",
    "0xfd4bb47bd45abdbdb2ecd61052c9571773f9cde876e2a7745f488c20b30ab10a": "NFSYNC",
    "0x27efe2fec96cb0ff68b9206e3dca402a919bb7172e2f2d12d49b633df3eabc4f": "NFSYNC",
    "0xa1cae252e597e19f398a442722a17a17e62d17f9d4f3656786e18aabcd428908": "NFT",
    "0xc0682ac2d5a530c92664a8717db0ab335b5c5cbdfdc740185679e170244633e0": "NFT",
    "0x381d54fa425631e6266af114239150fae1d5db67bb65b4fa9ecc65013107e07e": "NFT",
    "0x9cf337bf5592ea341168705a5dd168d5d26aaedb4d4725f6f13dd30aeae3322d": "NFPEN",
    "0xd78ad95fa46c994b6551d0da85fc275fe613ce37657fb8d5e3d130840159d822": "V2SWAP",
    "0x1c411e9a96e071241c2f21f7726b17ae89e3cab4c78be50e062b03a9fffbbad1": "V2SYNC",
    "0xc42079f94a6350d7e6235f29174924f928cc2ac818eb64fed8004e115fbcca67": "V3SWAP",
    "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef": "TF",
    ob.ORDERS_UPDATED_TOPIC: "OBU",
    ob.FILL_TOPIC: "OBF",
    ob.USER_REGISTERED_TOPIC: "UR",
    ob.USER_REGISTERED_V2_TOPIC: "UR",
    m.MARKET_CREATED_V2_TOPIC: "MC",
    m.MARKET_PARAMS_CHANGED_V2_TOPIC: "MPC",
    ref.REFERRAL_TOPIC: "REF",
    ref.CLAIM_TOPIC: "REFCLAIM",
    proto.BALANCE_DEPOSIT_TOPIC: "IBD",
    proto.BALANCE_WITHDRAW_TOPIC: "IBW",
    proto.LAUNCHPAD_PARAMS_TOPIC: "LPC",
    proto.GOV_CHANGED_TOPIC: "GOV",
}
TOPICS = list(EVENT_SIGS.keys())

PARSERS = {
    "MC": m.parse_market_created,
    "MPC": m.parse_market_params_changed,
    "TR": m.parse_trade,
    "PMINT": p.parse_mint,
    "PBURN": p.parse_burn,
    "PSYNC": p.parse_sync,
    "TC": lp.parse_token_created,
    "LT": lp.parse_launchpad_trade,
    "MG": lp.parse_migrated,
    "VD": v.parse_vault_created,
    "VDP": v.parse_vault_deposit,
    "VWD": v.parse_vault_withdraw,
    "VLOCK": v.parse_vault_lock,
    "VUNLOCK": v.parse_vault_unlock,
    "VCLOSE": v.parse_vault_close,
    "VMAX": v.parse_vault_max_shares_changed,
    "VLOCKUP": v.parse_vault_lockup_changed,
    "VDECR": v.parse_vault_decrease_on_withdraw_changed,
    "NFC": n.parse_nadfun_token_created,
    "NFB": n.parse_nadfun_buy,
    "NFS": n.parse_nadfun_sell,
    "NFSYNC": n.parse_nadfun_sync,
    "NFT": n.parse_nadfun_graduated,
    "NFPEN": n.parse_nadfun_sniping_penalty,
    "V2SWAP": n.parse_v2_pair_swap,
    "V2SYNC": n.parse_v2_pair_sync,
    "V3SWAP": n.parse_v3_trade,
    "TF": _parse_transfer,
    "OBU": ob.parse_orders_updated,
    "OBF": ob.parse_fill,
    "UR": ob.parse_user_registered,
    "REF": ref.parse_referral,
    "REFCLAIM": ref.parse_rewards_claimed,
    "IBD": proto.parse_balance_deposit,
    "IBW": proto.parse_balance_withdraw,
    "LPC": proto.parse_launchpad_params_changed,
    "GOV": proto.parse_gov_changed,
}

ROUTER_EVENT_TAGS = {
    "MC",
    "MPC",
    "TR",
    "PMINT",
    "PBURN",
    "PSYNC",
    "TC",
    "LT",
    "MG",
    "OBU",
    "OBF",
    "UR",
    "REFCLAIM",
    "IBD",
    "IBW",
    "LPC",
    "GOV",
}
REFERRAL_EVENT_TAGS = {"REF"}
VAULT_FACTORY_EVENT_TAGS = {"VD", "VDP", "VWD", "VLOCK", "VUNLOCK", "VCLOSE", "VMAX", "VLOCKUP", "VDECR"}
NADFUN_EVENT_TAGS = {"NFC", "NFB", "NFS", "NFSYNC", "NFT"}
NADFUN_AUX_EVENT_TAGS = {"NFPEN"}
V2_PAIR_EVENT_TAGS = {"V2SWAP", "V2SYNC"}
PASSTHROUGH_EVENT_TAGS = {"TF", "V3SWAP"}
NADFUN_V2_DIRECT_TRADE_TOPICS = {n.V2_BUY_TOPIC, n.V2_SELL_TOPIC}


def is_nadfun_address(addr: str) -> bool:
    return (addr or "").lower() in NADFUN_ADDRS


def _topic_addr(topic: str) -> str:
    if not isinstance(topic, str):
        return ""
    hex_topic = topic[2:] if topic.startswith("0x") else topic
    if len(hex_topic) < 40:
        return ""
    return ("0x" + hex_topic[-40:]).lower()


def register_dynamic_addresses_from_log(raw_log: dict) -> None:
    topics = raw_log.get("topics") or []
    if not topics:
        return

    tag = EVENT_SIGS.get(str(topics[0]).lower())
    addr = (raw_log.get("address") or "").lower()
    pool = ""

    if tag == "NFC" and is_nadfun_address(addr) and len(topics) > 3:
        pool = _topic_addr(topics[3])
    elif tag == "NFT" and is_nadfun_address(addr) and len(topics) > 2:
        pool = _topic_addr(topics[2])
    elif tag == "MG" and addr == CONTRACTS.get("ROUTER", "").lower() and len(topics) > 2:
        pool = _topic_addr(topics[2])

    if pool and pool not in ADDRS:
        ADDRS.append(pool)


def accepts_log_for_indexing(tag: str, addr: str) -> bool:
    addr = (addr or "").lower()
    if tag in ROUTER_EVENT_TAGS:
        return addr == CONTRACTS["ROUTER"].lower()
    if tag in VAULT_FACTORY_EVENT_TAGS:
        return addr in VAULT_FACTORY_ADDRS
    if tag in REFERRAL_EVENT_TAGS:
        return addr == CONTRACTS["REFERRALS"].lower()
    if tag in NADFUN_EVENT_TAGS:
        return is_nadfun_address(addr)
    if tag in NADFUN_AUX_EVENT_TAGS:
        return is_nadfun_address(addr)
    if tag in V2_PAIR_EVENT_TAGS:
        return addr in ADDRS
    if tag in PASSTHROUGH_EVENT_TAGS:
        return True
    return False


WS_URL = "wss://rpc-mainnet.monadinfra.com"
_RPC_MAX_RPS = int(os.getenv("RPC_MAX_RPS", "20"))
_last_rpc_ts = 0.0
_rpc_lock = asyncio.Lock()


async def ack(ws, rid, stash: list[dict] | None = None):
    deferred: list[dict] = []
    while True:
        if stash is not None and stash:
            resp = stash.pop(0)
        else:
            resp = json.loads(await ws.recv())
        if resp.get("id") == rid:
            if stash is not None and deferred:
                stash[:0] = deferred
            if "error" in resp:
                raise RuntimeError(resp)
            return resp
        if stash is not None:
            deferred.append(resp)


async def rate_gate() -> None:
    global _last_rpc_ts
    async with _rpc_lock:
        now = time.monotonic()
        wait = 1 / _RPC_MAX_RPS - (now - _last_rpc_ts)
        if wait > 0:
            await asyncio.sleep(wait)
        _last_rpc_ts = time.monotonic()
