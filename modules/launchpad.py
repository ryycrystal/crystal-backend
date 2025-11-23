# 32-byte word or hex string into a 0x-prefixed address
def to_addr(w) -> str:
    return "0x" + (w.hex() if isinstance(w, bytes) else w)[-40:]

# yield s in fixed-size n-character chunks (used for 32-byte words)
def chunks(s: str, n: int):
    return (s[i : i + n] for i in range(0, len(s), n))

# LaunchpadTrade(
#   address indexed token, 
#   address indexed user, 
#   bool isBuy, 
#   uint256 amountIn, 
#   uint256 amountOut, 
#   uint256 virtualNativeReserve, 
#   uint256 virtualTokenReserve
# );
# into a flat dict for state.apply_launchpad_trade
def parse_launchpad_trade(_addr, tops, data):
    token = to_addr(tops[1]).lower()
    user = to_addr(tops[2]).lower()

    words = list(chunks(data, 64))

    is_buy = int(words[0], 16) != 0 if len(words) > 0 else False
    amount_in = int(words[1], 16) if len(words) > 1 else 0
    amount_out = int(words[2], 16) if len(words) > 2 else 0
    native_reserve = int(words[3], 16) if len(words) > 3 else 0
    token_reserve = int(words[4], 16) if len(words) > 4 else 0

    return {
        "token": token,
        "user": user,
        "is_buy": is_buy,
        "amount_in": amount_in,
        "amount_out": amount_out,
        "native_reserve": native_reserve,
        "token_reserve": token_reserve,
    }

# TokenCreated(
#   address indexed token, 
#   address indexed creator, 
#   string name, 
#   string symbol, 
#   string metadataCID, 
#   string description, 
#   string social1, 
#   string social2, 
#   string social3, 
#   string social4
# );
# into a flat dict for state.apply_token_created
def parse_token_created(_addr, tops, data):
    token = to_addr(tops[1]).lower()
    creator = to_addr(tops[2]).lower()

    words = list(chunks(data, 64))

    def read_string(head_index: int) -> str:
        if head_index >= len(words):
            return ""
        try:
            offset = int(words[head_index], 16)
        except Exception:
            return ""

        start_word = offset // 32
        if start_word < 0 or start_word >= len(words):
            return ""

        try:
            length = int(words[start_word], 16)
        except Exception:
            return ""

        n_words = (length + 31) // 32
        data_words = words[start_word + 1 : start_word + 1 + n_words]
        hex_str = "".join(data_words)[: length * 2]

        try:
            return bytes.fromhex(hex_str).decode("utf-8", "ignore")
        except Exception:
            return ""

    name = read_string(0)
    symbol = read_string(1)
    metadata_cid = read_string(2)
    description = read_string(3)
    social1 = read_string(4)
    social2 = read_string(5)
    social3 = read_string(6)
    social4 = read_string(7)

    return {
        "token": token,
        "creator": creator,
        "name": name,
        "symbol": symbol,
        "metadata_cid": metadata_cid,
        "description": description,
        "social1": social1,
        "social2": social2,
        "social3": social3,
        "social4": social4,
        "source": 0,
    }

# Migrated(address indexed token);
# for state.apply_migrated
def parse_migrated(_addr, tops, _data):
    token = to_addr(tops[1]).lower()
    return {
        "token": token
    }