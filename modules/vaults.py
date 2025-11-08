from __future__ import annotations

def to_addr(w) -> str:
    return "0x" + (w.hex() if isinstance(w, bytes) else w)[-40:]

def chunks(s: str, n: int):
    return (s[i:i+n] for i in range(0, len(s), n))

def parse_vault_created(addr: str, tops: list[str], data_no0x: str) -> dict:
    vault = to_addr(tops[1]).lower() if len(tops) > 1 else addr.lower()
    words = list(chunks(data_no0x, 64))
    
    def uaddr(i: int) -> str:
        return ("0x" + words[i][-40:]).lower() if i < len(words) else "0x0000000000000000000000000000000000000000"
    
    quote = uaddr(0)
    base = uaddr(1)
    
    return {
        "vault": vault, 
        "quote": quote, 
        "base": base
    }