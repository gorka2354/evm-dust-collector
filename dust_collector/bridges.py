"""Bridge adapters behind one interface.

Each adapter turns a "move this balance to the target chain" request into a
ready-to-sign transaction. The executor does not care which bridge it is.

    quote(cfg, to_chain_id, amount_wei, sender, token=None) -> dict

Success returns a normalised quote:
    {
        "to":           checksum address to send the tx to,
        "data":         hex calldata,
        "value":        wei to attach,
        "gas_limit":    int or None  (None -> estimate at send time),
        "gas_price":    int or None  (None -> use chain EIP-1559 default),
        "approve_to":   address to ERC20-approve first, or None,
        "receive":      estimated output (human units) or None,
        "receive_sym":  symbol of the output,
        "dest_chain_id":where funds actually land,
        "note":         extra guidance (e.g. multi-hop) or None,
    }
Failure returns {"error": "..."}.

Only the sender's PUBLIC address is shared with these bridge APIs (it is
already public on-chain). Private keys are never part of any request.
"""

import requests
from web3 import Web3

NATIVE = "0x0000000000000000000000000000000000000000"


# ─── Relay Protocol (default) ─────────────────────────────────────

def _relay(cfg, to_chain_id, amount_wei, sender, token=None):
    payload = {
        "user": sender,
        "originChainId": cfg["id"],
        "destinationChainId": to_chain_id,
        "originCurrency": token or NATIVE,
        "destinationCurrency": NATIVE,
        "amount": str(amount_wei),
        "recipient": sender,
        "tradeType": "EXACT_INPUT",
    }
    try:
        r = requests.post("https://api.relay.link/quote", json=payload, timeout=15)
    except Exception as e:
        return {"error": str(e)}
    if r.status_code != 200:
        return {"error": f"HTTP {r.status_code}: {r.text[:100]}"}

    quote = r.json()
    steps = quote.get("steps", [])
    if not steps or not steps[0].get("items"):
        return {"error": "no route"}

    tx = steps[0]["items"][0].get("data", {})
    to_addr = tx.get("to", "")
    if not to_addr:
        return {"error": "no route"}

    receive = None
    out = quote.get("details", {}).get("currencyOut", {})
    if out.get("amount"):
        receive = int(out["amount"]) / 1e18

    return {
        "to": to_addr,
        "data": tx.get("data", ""),
        "value": int(tx.get("value", "0")),
        "gas_limit": None,
        "gas_price": None,
        "approve_to": to_addr if token else None,
        "receive": receive,
        "receive_sym": "ETH",
        "dest_chain_id": to_chain_id,
        "note": None,
    }


# ─── LiFi (Lens GHO, which Relay does not support) ────────────────

# LiFi addresses some chains by short key rather than numeric id.
_LIFI_KEYS = {232: "lns"}


def _lifi(cfg, to_chain_id, amount_wei, sender, token=None):
    params = {
        "fromChain": _LIFI_KEYS.get(cfg["id"], str(cfg["id"])),
        "toChain": str(to_chain_id),
        "fromToken": token or NATIVE,
        "toToken": NATIVE,
        "fromAmount": str(amount_wei),
        "fromAddress": sender,
    }
    try:
        r = requests.get("https://li.quest/v1/quote", params=params, timeout=15)
    except Exception as e:
        return {"error": str(e)}
    if r.status_code != 200:
        return {"error": f"HTTP {r.status_code}: {r.text[:100]}"}

    quote = r.json()
    tx = quote.get("transactionRequest", {})
    if not tx.get("to"):
        return {"error": "no route"}

    def _to_int(v):
        if isinstance(v, str):
            return int(v, 16) if v.startswith("0x") else int(v)
        return int(v)

    receive = None
    est = quote.get("estimate", {})
    if est.get("toAmount"):
        receive = int(est["toAmount"]) / 1e18

    return {
        "to": tx["to"],
        "data": tx.get("data", ""),
        "value": _to_int(tx.get("value", 0)),
        "gas_limit": _to_int(tx["gasLimit"]) if tx.get("gasLimit") else None,
        "gas_price": _to_int(tx["gasPrice"]) if tx.get("gasPrice") else None,
        "approve_to": tx["to"] if token else None,
        "receive": receive,
        "receive_sym": "ETH",
        "dest_chain_id": to_chain_id,
        "note": quote.get("toolDetails", {}).get("name"),
    }


# ─── Polygon zkEVM native bridge (to Ethereum L1) ─────────────────

# Relay/LiFi do not route Polygon zkEVM, so use its canonical bridge.
# bridgeAsset(uint32 destNetwork, address dest, uint256 amount,
#             address token, bool forceUpdate, bytes permit)
_ZKEVM_BRIDGE = "0x2a3DD3EB832aF982ec71669E178424b10Dca2EDe"
_ZKEVM_SIG = "bridgeAsset(uint32,address,uint256,address,bool,bytes)"


def _native_zkevm(cfg, to_chain_id, amount_wei, sender, token=None):
    if token:
        return {"error": "zkEVM native bridge handles native ETH only"}

    digest = Web3.keccak(text=_ZKEVM_SIG).hex()
    selector = (digest[2:] if digest.startswith("0x") else digest)[:8]

    addr = sender[2:].lower()
    calldata = "0x" + selector
    calldata += format(0, "064x")                        # destNetwork = 0 (Ethereum)
    calldata += "0" * 24 + addr                          # dest = sender
    calldata += format(amount_wei, "064x")               # amount
    calldata += "0" * 24 + NATIVE[2:]                    # token = native
    calldata += format(1, "064x")                        # forceUpdate = true
    calldata += format(192, "064x")                      # offset to permit bytes
    calldata += format(0, "064x")                        # permit length = 0

    return {
        "to": _ZKEVM_BRIDGE,
        "data": calldata,
        "value": amount_wei,
        "gas_limit": None,
        "gas_price": None,
        "approve_to": None,
        "receive": None,
        "receive_sym": "ETH",
        "dest_chain_id": 1,  # lands on Ethereum L1, not the target chain
        "note": "lands on Ethereum L1 (~30 min); then re-run collect to bridge L1 -> target",
    }


_ADAPTERS = {
    "relay": _relay,
    "lifi": _lifi,
    "native_zkevm": _native_zkevm,
}


def get_quote(cfg, to_chain_id, amount_wei, sender, token=None):
    """Dispatch to the bridge adapter configured for this chain."""
    adapter = _ADAPTERS.get(cfg.get("bridge"))
    if adapter is None:
        return {"error": f"no bridge adapter for {cfg.get('bridge')}"}
    return adapter(cfg, to_chain_id, amount_wei, sender, token=token)
