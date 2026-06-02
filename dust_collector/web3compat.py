"""web3.py 6.x / 7.x compatibility shims.

web3 v7 made two breaking changes that this tool hits:
  * `geth_poa_middleware` was removed in favour of `ExtraDataToPOAMiddleware`
  * `SignedTransaction.rawTransaction` was renamed to `raw_transaction`

These helpers let the same code run on both major versions, so a fresh
`pip install web3` (which today pulls 7.x) does not break on import.
"""

from web3 import Web3

# POA middleware moved between v6 and v7.
try:                                                        # web3 v6
    from web3.middleware import geth_poa_middleware as _poa_middleware
except ImportError:                                         # web3 v7
    try:
        from web3.middleware import ExtraDataToPOAMiddleware as _poa_middleware
    except ImportError:
        _poa_middleware = None


def get_w3(rpc, timeout=15):
    """Build a Web3 HTTP provider with POA support (Polygon, BSC, etc.)."""
    w3 = Web3(Web3.HTTPProvider(rpc, request_kwargs={"timeout": timeout}))
    if _poa_middleware is not None:
        try:
            w3.middleware_onion.inject(_poa_middleware, layer=0)
        except Exception:
            pass
    return w3


def raw_tx(signed):
    """Return the raw signed-transaction bytes on either web3 version."""
    if hasattr(signed, "raw_transaction"):   # web3 v7
        return signed.raw_transaction
    return signed.rawTransaction             # web3 v6
