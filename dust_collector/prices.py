"""Token prices: one table, live-updated from CoinGecko where possible.

Previously prices were scattered as magic numbers (ETH was "3800" in one
file and "2093" in another). Now there is a single PRICES dict; majors are
refreshed at runtime so thresholds stay roughly current.

Prices only gate "is this dust worth bridging?" decisions — they never move
funds, so approximate fallbacks are fine if CoinGecko is unreachable.
"""

import requests

# Approximate USD fallbacks. Overwritten at runtime for the majors below.
PRICES = {
    "ETH": 3000.0, "POL": 0.40, "BNB": 600.0, "AVAX": 25.0,
    "FTM": 0.70, "MNT": 0.70, "CELO": 0.50, "ZETA": 0.30,
    "CORE": 0.80, "G": 0.04, "S": 0.50, "SEI": 0.30,
    "BTC": 75000.0, "CAKE": 1.90,
    # Stables / pegged
    "xDAI": 1.0, "USDC": 1.0, "USDC.e": 1.0, "USDT": 1.0,
    "EURA": 1.08, "MIM": 1.0, "GHO": 1.0, "DAI": 1.0,
}

# Native symbol -> CoinGecko id, for the runtime refresh.
COINGECKO_IDS = {
    "ETH": "ethereum", "POL": "matic-network", "BNB": "binancecoin",
    "AVAX": "avalanche-2", "FTM": "fantom", "MNT": "mantle",
    "CELO": "celo", "ZETA": "zetachain", "CORE": "coredaoorg",
    "S": "sonic-3", "SEI": "sei-network",
    "BTC": "bitcoin", "CAKE": "pancakeswap-token",
}


def update_prices():
    """Refresh PRICES for the majors from CoinGecko. Best-effort."""
    ids = ",".join(sorted(set(COINGECKO_IDS.values())))
    try:
        r = requests.get(
            "https://api.coingecko.com/api/v3/simple/price",
            params={"ids": ids, "vs_currencies": "usd"},
            timeout=10,
        )
        data = r.json()
        for symbol, cg_id in COINGECKO_IDS.items():
            if cg_id in data and "usd" in data[cg_id]:
                PRICES[symbol] = float(data[cg_id]["usd"])
        print("[*] Prices updated from CoinGecko")
    except Exception:
        print("[!] CoinGecko unavailable, using fallback prices")


def price_of(symbol):
    return PRICES.get(symbol, 0.0)


def to_usd(symbol, amount_human):
    return amount_human * price_of(symbol)


def token_price(token):
    """USD per whole token of a TOKENS entry, 0 if unknown.

    The live price of its "price_of" symbol (or its own) comes first; a pool
    share with no market price can carry an approximate "usd" instead, used
    only to judge whether moving it is worth the fee.
    """
    return price_of(token.get("price_of", token["symbol"])) or token.get("usd", 0.0)


def token_usd(token, amount_human):
    """USD value of a TOKENS entry's amount."""
    return amount_human * token_price(token)


def token_priced(token):
    """Whether a TOKENS entry has any price to judge it by."""
    return token_price(token) > 0
