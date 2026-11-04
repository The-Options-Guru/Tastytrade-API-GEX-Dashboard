"""Listed contracts for a single option root and expiration."""
from datetime import datetime
from urllib.parse import quote

import requests

from utils.auth import get_access_token
from utils.exposure_profile import _nonnegative_number
from utils.gex_calculator import parse_option_symbol


def fetch_expiry_chain(symbol, option_prefix, expiration):
    expiry_date = datetime.strptime(expiration, "%y%m%d").date().isoformat()
    response = requests.get(
        f"https://api.tastytrade.com/option-chains/{quote(symbol, safe='')}/nested",
        headers={
            "Authorization": f"Bearer {get_access_token()}",
            "User-Agent": "local-gex-dashboard/1.0",
        },
        timeout=20,
    )
    response.raise_for_status()
    payload = response.json()
    if payload.get("pagination", {}).get("total-pages", 1) > 1:
        raise ValueError("Option-chain response is paginated; full-chain coverage is not established.")
    contracts = {}
    for chain in payload.get("data", {}).get("items", []):
        if chain.get("root-symbol") != option_prefix:
            continue
        multiplier = _nonnegative_number(chain.get("shares-per-contract"))
        if not multiplier:
            raise ValueError("The listed chain has no valid contract multiplier.")
        for expiry in chain.get("expirations", []):
            if expiry.get("expiration-date") != expiry_date:
                continue
            for strike in expiry.get("strikes", []):
                strike_price = _nonnegative_number(strike.get("strike-price"))
                if not strike_price:
                    raise ValueError("The listed chain contains an invalid strike.")
                for side, option_type in (("call", "C"), ("put", "P")):
                    streamer_symbol = strike.get(f"{side}-streamer-symbol")
                    parsed = parse_option_symbol(streamer_symbol) if isinstance(streamer_symbol, str) else None
                    if not parsed or (
                        parsed["strike"] != strike_price
                        or parsed["type"] != option_type
                        or parsed["prefix"] != option_prefix
                        or parsed["expiration"] != expiration
                    ):
                        raise ValueError("The listed chain contains an inconsistent option symbol.")
                    contract = {"strike": strike_price, "side": side, "multiplier": multiplier}
                    if streamer_symbol in contracts and contracts[streamer_symbol] != contract:
                        raise ValueError("The listed chain contains conflicting contract metadata.")
                    contracts[streamer_symbol] = contract
    if not contracts:
        raise ValueError(f"No listed {option_prefix} contracts for {expiry_date}.")
    return contracts
