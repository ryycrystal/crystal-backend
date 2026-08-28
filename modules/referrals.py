REFERRAL_TOPIC = "0x9d05414fb79fac216c15606de5cc06664e91a254e4d5f57664d5f1beaf7fb7ef"


def parse_referral(addr: str, tops: list[str], data_no0x: str) -> dict:
    referrer = ("0x" + tops[1][-40:]) if len(tops) > 1 else "0x" + "0" * 40
    referee = ("0x" + data_no0x[24:64]) if len(data_no0x) >= 64 else "0x" + "0" * 40
    return {"referrer": referrer.lower(), "referee": referee.lower()}
