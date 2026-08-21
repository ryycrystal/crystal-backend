MULTICALL3_ADDR = "0xca11bde05977b3631167028862be2a173976ca11"
MULTICALL3_AGGREGATE3_SELECTOR = bytes.fromhex("82ad56cb")
MULTICALL3_GET_ETH_BALANCE_SELECTOR = bytes.fromhex("4d2301cc")
ERC20_BALANCE_OF_SELECTOR = bytes.fromhex("70a08231")


# read one uint256 at a byte offset
def u256_at(buf: bytes, off: int) -> int:
    if off < 0 or (off + 32) > len(buf):
        return 0
    return int.from_bytes(buf[off : off + 32], "big")


# abi encode a uint256
def abi_u256(n: int) -> bytes:
    return int(n).to_bytes(32, "big")


# abi encode an address
def abi_addr(addr: str) -> bytes:
    h = str(addr or "").lower().replace("0x", "")[-40:].rjust(40, "0")
    return (b"\x00" * 12) + bytes.fromhex(h)


# abi encode a bool
def abi_bool(v: bool) -> bytes:
    return abi_u256(1 if v else 0)


# abi encode a dynamic bytes argument
def abi_bytes(data: bytes) -> bytes:
    d = data or b""
    pad = (-len(d)) % 32
    return abi_u256(len(d)) + d + (b"\x00" * pad)


# build multicall3 aggregate3 calldata for a batch of calls
def encode_multicall3_aggregate3(calls: list[tuple[str, bytes]]) -> str:
    elems = []
    for target, calldata in calls:
        body = abi_addr(target) + abi_bool(False) + abi_u256(96) + abi_bytes(calldata)
        elems.append(body)
    n = len(elems)
    arr_heads_size = 32 * n
    cur = arr_heads_size
    arr_offsets = []
    for e in elems:
        arr_offsets.append(cur)
        cur += len(e)
    arr = abi_u256(n) + b"".join(abi_u256(o) for o in arr_offsets) + b"".join(elems)
    payload = MULTICALL3_AGGREGATE3_SELECTOR + abi_u256(32) + arr
    return "0x" + payload.hex()


# decode multicall3 aggregate3 output into success and return bytes
def decode_multicall3_aggregate3_result(data_hex: str) -> list[tuple[bool, bytes]]:
    if not isinstance(data_hex, str) or not data_hex.startswith("0x"):
        return []
    try:
        buf = bytes.fromhex(data_hex[2:])
    except Exception:
        return []
    top_off = u256_at(buf, 0)
    base = top_off
    if (base + 32) > len(buf):
        return []
    n = u256_at(buf, base)
    tuple_base = base + 32
    out = []
    for i in range(n):
        off = u256_at(buf, tuple_base + (32 * i))
        elem = tuple_base + off
        success = bool(u256_at(buf, elem))
        bytes_off = u256_at(buf, elem + 32)
        bpos = elem + bytes_off
        blen = u256_at(buf, bpos)
        bstart = bpos + 32
        bend = bstart + blen
        if bstart > len(buf):
            out.append((success, b""))
            continue
        out.append((success, buf[bstart : min(bend, len(buf))]))
    return out
