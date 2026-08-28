REFERRAL_TOPIC = "0x9d05414fb79fac216c15606de5cc06664e91a254e4d5f57664d5f1beaf7fb7ef"


def parse_referral(addr: str, tops: list[str], data_no0x: str) -> dict:
    referrer = ("0x" + tops[1][-40:]) if len(tops) > 1 else "0x" + "0" * 40
    referee = ("0x" + data_no0x[24:64]) if len(data_no0x) >= 64 else "0x" + "0" * 40
    return {"referrer": referrer.lower(), "referee": referee.lower()}


CLAIM_TOPIC = "0xc53cb8bc1a7200a84d0b66a538905a245c4915aace7f1ce5dc4a0ba107ebc15c"


def parse_rewards_claimed(addr: str, tops: list[str], data_no0x: str) -> dict:
    user = ("0x" + tops[1][-40:]) if len(tops) > 1 else "0x" + "0" * 40

    def word(i: int) -> str:
        return data_no0x[i * 64 : (i + 1) * 64]

    tokens: list[str] = []
    amounts: list[int] = []
    try:
        off_tokens = int(word(0), 16) // 32
        off_amounts = int(word(1), 16) // 32
        n = int(word(off_tokens), 16)
        m = int(word(off_amounts), 16)
        tokens = ["0x" + word(off_tokens + 1 + i)[-40:] for i in range(n)]
        amounts = [int(word(off_amounts + 1 + i), 16) for i in range(min(m, n))]
    except (ValueError, IndexError):
        tokens, amounts = [], []
    return {
        "user": user.lower(),
        "tokens": [t.lower() for t in tokens[: len(amounts)]],
        "amounts": amounts,
    }
