from __future__ import annotations

def to_addr(w) -> str:
    return "0x" + (w.hex() if isinstance(w, bytes) else w)[-40:]

def chunks(s: str, n: int):
    return (s[i:i+n] for i in range(0, len(s), n))

def read_str(buf: bytes, base: int, rel: int) -> str:
    start = base + rel
    if start + 32 > len(buf):
        return ""
    ln = int.from_bytes(buf[start:start+32], "big")
    s = buf[start+32:start+32+ln]
    try:
        return s.decode("utf-8", errors="ignore").replace("\x00", "")
    except Exception:
        return ""
    
def u256_at(buf: bytes, off: int) -> int:
    return int.from_bytes(buf[off:off+32], "big") if off + 32 <= len(buf) else 0

def parse_vault_created(addr: str, tops: list[str], data_no0x: str) -> dict:
    h = data_no0x or ""
    h = h if len(h) % 2 == 0 else "0" + h
    w = list(chunks(h, 64))
    vault = to_addr(tops[1][-40:]) if len(tops) > 1 else ""
    quote_asset = to_addr(w[0][-40:]) if len(w) > 0 else ""
    base_asset = to_addr(w[1][-40:]) if len(w) > 1 else ""
    owner = to_addr(w[2][-40:]) if len(w) > 2 else ""
    max_shares = int(w[3], 16) if len(w) > 3 else 0
    lockup = int(w[4], 16) if len(w) > 4 else 0
    decrease_on_withdraw = int(w[5], 16) != 0 if len(w) > 5 else False
    
    meta = {"name": "", "description": "", "social1": "", "social2": "", "social3": ""}
    
    if len(w) > 6:
        try:
            byte_off = int(w[6], 16)
            b = bytes.fromhex(h)
            tup = byte_off
            o0 = u256_at(b, tup + 0)
            o1 = u256_at(b, tup + 32)
            o2 = u256_at(b, tup + 64)
            o3 = u256_at(b, tup + 96)
            o4 = u256_at(b, tup + 128)
                
            meta["name"] = read_str(b, tup, o0)
            meta["description"] = read_str(b, tup, o1)
            meta["social1"] = read_str(b, tup, o2)
            meta["social2"] = read_str(b, tup, o3)
            meta["social3"] = read_str(b, tup, o4)
        except Exception:
            pass
        
    return {
        "vault": vault,
        "quoteAsset": quote_asset,
        "baseAsset": base_asset,
        "owner": owner,
        "maxShares": max_shares,
        "lockup": lockup,
        "decreaseOnWithdraw": decrease_on_withdraw,
        "metadata": meta,
        "locked": False,
        "closed": False,
    }

def parse_vault_deposit(addr: str, tops: list[str], data_no0x: str) -> dict:
    h = data_no0x or ""
    h = h if len(h) % 2 == 0 else "0" + h
    w = list(chunks(h, 64))
    vault = to_addr(tops[1][-40:]) if len(tops) > 1 else ""
    sender = to_addr(tops[2][-40:]) if len(tops) > 2 else ""
    shares = int(w[0], 16) if len(w) > 0 else 0
    quote_amt = int(w[1], 16) if len(w) > 1 else 0
    base_amt = int(w[2], 16) if len(w) > 2 else 0
    
    return {
        "vault": vault,
        "sender": sender,
        "shares": shares,
        "quoteAmount": quote_amt,
        "baseAmount": base_amt,
    }

def parse_vault_withdraw(addr: str, tops: list[str], data_no0x: str) -> dict:
    h = data_no0x or ""
    h = h if len(h) % 2 == 0 else "0" + h
    w = list(chunks(h, 64))
    vault = to_addr(tops[1][-40:]) if len(tops) > 1 else ""
    sender = to_addr(tops[2][-40:]) if len(tops) > 2 else ""
    shares = int(w[0], 16) if len(w) > 0 else 0
    quote_amt = int(w[1], 16) if len(w) > 1 else 0
    base_amt = int(w[2], 16) if len(w) > 2 else 0
    
    return {
        "vault": vault,
        "sender": sender,
        "shares": shares,
        "quoteAmount": quote_amt,
        "baseAmount": base_amt,
    }

def parse_vault_lock(addr: str, tops: list[str], data_no0x: str) -> dict:
    vault = to_addr(tops[1][-40:]) if len(tops) > 1 else ""
    return {"vault": vault}

def parse_vault_unlock(addr: str, tops: list[str], data_no0x: str) -> dict:
    vault = to_addr(tops[1][-40:]) if len(tops) > 1 else ""
    return {"vault": vault}

def parse_vault_close(addr: str, tops: list[str], data_no0x: str) -> dict:
    vault = to_addr(tops[1][-40:]) if len(tops) > 1 else ""
    return {"vault": vault}

def parse_vault_max_shares_changed(addr: str, tops: list[str], data_no0x: str) -> dict:
    h = data_no0x or ""
    h = h if len(h) % 2 == 0 else "0" + h
    w = list(chunks(h, 64))
    vault = to_addr(tops[1][-40:]) if len(tops) > 1 else ""
    max_shares = int(w[0], 16) if len(w) > 0 else 0
    return {"vault": vault, "maxShares": max_shares}

def parse_vault_lockup_changed(addr: str, tops: list[str], data_no0x: str) -> dict:
    h = data_no0x or ""
    h = h if len(h) % 2 == 0 else "0" + h
    w = list(chunks(h, 64))
    vault = to_addr(tops[1][-40:]) if len(tops) > 1 else ""
    lockup = int(w[0], 16) if len(w) > 0 else 0
    return {"vault": vault, "lockup": lockup}

def parse_vault_decrease_on_withdraw_changed(addr: str, tops: list[str], data_no0x: str) -> dict:
    h = data_no0x or ""
    h = h if len(h) % 2 == 0 else "0" + h
    w = list(chunks(h, 64))
    vault = to_addr(tops[1][-40:]) if len(tops) > 1 else ""
    value = int(w[0], 16) != 0 if len(w) > 0 else False
    return {"vault": vault, "decreaseOnWithdraw": value}
