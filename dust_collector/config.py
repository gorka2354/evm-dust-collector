"""Single source of truth for chains, tokens and tunable constants.

Previously the chain list was duplicated (and had drifted) across
consolidate.py and fast_audit.py. Now everything reads from here.
"""

# Where everything is consolidated by default. Base has cheap gas and is
# supported by the Relay bridge from most chains.
TARGET_CHAIN_ID = 8453  # Base

# Economics
MIN_PROFIT_USD = 0.15   # skip a chain if net value after gas+bridge is below this
SEND_PERCENT = 0.82     # bridge 82% of a native balance, keep 18% for gas + L1 fees
SWEEP_MIN_USD = 0.05        # sweep: skip a balance worth less than this
SWEEP_MAX_FEE_SHARE = 0.25  # sweep: skip a balance whose transfer fee is more than this share of it

# fallback_rpcs (optional) are tried in order when a chain's rpc doesn't answer;
# public nodes ban an IP that polls them hard, often for an hour.
#
# bridge field picks the adapter in bridges.py:
#   "relay"        -> Relay Protocol  (default, most chains)
#   "lifi"         -> LiFi            (Lens GHO, which Relay does not support)
#   "native_zkevm" -> Polygon zkEVM native bridge (to Ethereum L1, then re-run)
#   None           -> scan only, no automated bridge route
CHAINS = {
    "ethereum":   {"id": 1,        "rpc": "https://ethereum-rpc.publicnode.com",            "native": "ETH",  "decimals": 18, "bridge": "relay",
                   "fallback_rpcs": ["https://eth.drpc.org", "https://1rpc.io/eth"]},
    "arbitrum":   {"id": 42161,    "rpc": "https://arbitrum-one-rpc.publicnode.com",        "native": "ETH",  "decimals": 18, "bridge": "relay",
                   "fallback_rpcs": ["https://arb1.arbitrum.io/rpc", "https://arbitrum.drpc.org"]},
    "optimism":   {"id": 10,       "rpc": "https://optimism-rpc.publicnode.com",            "native": "ETH",  "decimals": 18, "bridge": "relay",
                   "fallback_rpcs": ["https://mainnet.optimism.io", "https://optimism.drpc.org"]},
    "base":       {"id": 8453,     "rpc": "https://base-rpc.publicnode.com",                "native": "ETH",  "decimals": 18, "bridge": "relay",
                   "fallback_rpcs": ["https://mainnet.base.org", "https://base.drpc.org"]},
    "polygon":    {"id": 137,      "rpc": "https://polygon-bor-rpc.publicnode.com",         "native": "POL",  "decimals": 18, "bridge": "relay",
                   "fallback_rpcs": ["https://polygon.drpc.org", "https://1rpc.io/matic"]},
    "bsc":        {"id": 56,       "rpc": "https://bsc-rpc.publicnode.com",                 "native": "BNB",  "decimals": 18, "bridge": "relay",
                   "fallback_rpcs": ["https://bsc-dataseed.bnbchain.org", "https://bsc.drpc.org"]},
    "avalanche":  {"id": 43114,    "rpc": "https://api.avax.network/ext/bc/C/rpc",          "native": "AVAX", "decimals": 18, "bridge": "relay"},
    "fantom":     {"id": 250,      "rpc": "https://rpc.ftm.tools",                          "native": "FTM",  "decimals": 18, "bridge": "relay"},
    "zksync":     {"id": 324,      "rpc": "https://mainnet.era.zksync.io",                  "native": "ETH",  "decimals": 18, "bridge": "relay"},
    "linea":      {"id": 59144,    "rpc": "https://rpc.linea.build",                        "native": "ETH",  "decimals": 18, "bridge": "relay"},
    "scroll":     {"id": 534352,   "rpc": "https://rpc.scroll.io",                          "native": "ETH",  "decimals": 18, "bridge": "relay"},
    "gnosis":     {"id": 100,      "rpc": "https://rpc.gnosischain.com",                    "native": "xDAI", "decimals": 18, "bridge": "relay"},
    "mantle":     {"id": 5000,     "rpc": "https://rpc.mantle.xyz",                         "native": "MNT",  "decimals": 18, "bridge": "relay"},
    "celo":       {"id": 42220,    "rpc": "https://forno.celo.org",                         "native": "CELO", "decimals": 18, "bridge": "relay"},
    "zetachain":  {"id": 7000,     "rpc": "https://zetachain-evm.blockpi.network/v1/rpc/public", "native": "ZETA", "decimals": 18, "bridge": "relay"},
    "manta":      {"id": 169,      "rpc": "https://pacific-rpc.manta.network/http",         "native": "ETH",  "decimals": 18, "bridge": "relay"},
    "lens":       {"id": 232,      "rpc": "https://rpc.lens.xyz",                           "native": "GHO",  "decimals": 18, "bridge": "lifi"},
    "zora":       {"id": 7777777,  "rpc": "https://rpc.zora.energy",                        "native": "ETH",  "decimals": 18, "bridge": "relay"},
    "taiko":      {"id": 167000,   "rpc": "https://rpc.mainnet.taiko.xyz",                  "native": "ETH",  "decimals": 18, "bridge": "relay"},
    "polyzkevm":  {"id": 1101,     "rpc": "https://zkevm-rpc.com",                          "native": "ETH",  "decimals": 18, "bridge": "native_zkevm"},
    "abstract":   {"id": 2741,     "rpc": "https://api.mainnet.abs.xyz",                    "native": "ETH",  "decimals": 18, "bridge": "relay"},
    "cyber":      {"id": 7560,     "rpc": "https://cyber.alt.technology",                   "native": "ETH",  "decimals": 18, "bridge": "relay"},
    "blast":      {"id": 81457,    "rpc": "https://rpc.blast.io",                           "native": "ETH",  "decimals": 18, "bridge": "relay"},
    "mode":       {"id": 34443,    "rpc": "https://mainnet.mode.network",                   "native": "ETH",  "decimals": 18, "bridge": "relay"},
    "core":       {"id": 1116,     "rpc": "https://rpc.coredao.org",                        "native": "CORE", "decimals": 18, "bridge": "relay"},
    "gravity":    {"id": 1625,     "rpc": "https://rpc.gravity.xyz",                        "native": "G",    "decimals": 18, "bridge": "relay"},
    "sonic":      {"id": 146,      "rpc": "https://rpc.soniclabs.com",                      "native": "S",    "decimals": 18, "bridge": "relay"},
    "sei":        {"id": 1329,     "rpc": "https://evm-rpc.sei-apis.com",                   "native": "SEI",  "decimals": 18, "bridge": "relay"},
    "bob":        {"id": 60808,    "rpc": "https://rpc.gobob.xyz",                          "native": "ETH",  "decimals": 18, "bridge": "relay"},
    "worldchain": {"id": 480,      "rpc": "https://worldchain-mainnet.g.alchemy.com/public", "native": "ETH", "decimals": 18, "bridge": "relay"},
    "ink":        {"id": 57073,    "rpc": "https://rpc-gel.inkonchain.com",                 "native": "ETH",  "decimals": 18, "bridge": "relay"},
    "unichain":   {"id": 130,      "rpc": "https://mainnet.unichain.org",                   "native": "ETH",  "decimals": 18, "bridge": "relay"},
    "lisk":       {"id": 1135,     "rpc": "https://rpc.api.lisk.com",                       "native": "ETH",  "decimals": 18, "bridge": "relay"},
    "opbnb":      {"id": 204,      "rpc": "https://opbnb-mainnet-rpc.bnbchain.org",         "native": "BNB",  "decimals": 18, "bridge": None},
}

# ERC20 tokens worth scanning/sweeping per chain.
TOKENS = {
    "polygon": [
        {"symbol": "USDC",   "address": "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359", "decimals": 6},
        {"symbol": "USDC.e", "address": "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174", "decimals": 6},
        {"symbol": "USDT",   "address": "0xc2132D05D31c914a87C6611C10748AEb04B58e8F", "decimals": 6},
        {"symbol": "EURA",   "address": "0xE0B52e49357Fd4DAf2c15e02058DCE6BC0057db4", "decimals": 18},
    ],
    "base": [
        {"symbol": "USDC", "address": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913", "decimals": 6},
    ],
    "arbitrum": [
        {"symbol": "USDC.e", "address": "0xFF970A61A04b1cA14834A43f5dE4533eBDDB5CC8", "decimals": 6},
        {"symbol": "USDC",   "address": "0xaf88d065e77c8cC2239327C5EDb3A432268e5831", "decimals": 6},
        {"symbol": "USDT",   "address": "0xFd086bC7CD5C481DCC9C85ebE478A1C0b69FCbb9", "decimals": 6},
    ],
    "mantle": [
        {"symbol": "USDT",   "address": "0x201EBa5CC46D216Ce6DC03F6a759e8E766e956aE", "decimals": 6},
    ],
    "fantom": [
        {"symbol": "MIM",    "address": "0x82f0B8B456c1A451378467398982d4834b6829c1", "decimals": 18},
    ],
}

# Minimal ERC20 ABI: balanceOf / approve / allowance.
ERC20_ABI = [
    {"constant": True,  "inputs": [{"name": "", "type": "address"}], "name": "balanceOf", "outputs": [{"name": "", "type": "uint256"}], "type": "function"},
    {"constant": False, "inputs": [{"name": "spender", "type": "address"}, {"name": "amount", "type": "uint256"}], "name": "approve", "outputs": [{"name": "", "type": "bool"}], "type": "function"},
    {"constant": True,  "inputs": [{"name": "owner", "type": "address"}, {"name": "spender", "type": "address"}], "name": "allowance", "outputs": [{"name": "", "type": "uint256"}], "type": "function"},
]


def chain_name_for_id(chain_id):
    for name, cfg in CHAINS.items():
        if cfg["id"] == chain_id:
            return name
    return "unknown"
