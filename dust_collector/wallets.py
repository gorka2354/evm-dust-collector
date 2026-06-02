"""Single .env wallet loader (previously reimplemented in 5 scripts).

Supported line formats (one wallet per line):

    word1 word2 ... word12                  # seed phrase, address derived
    0xADDRESS word1 word2 ... word12        # address + seed phrase
    0xADDRESS=0xPRIVATEKEY                   # address + raw private key

Lines that are blank or start with '#' are ignored.

NOTE: seed phrases and private keys are read straight off disk and used
only locally to sign transactions. They are never written anywhere or sent
over the network. See SECURITY.md.
"""

import os

from eth_account import Account

# Required to derive keys from a mnemonic; this is a local-only operation.
Account.enable_unaudited_hdwallet_features()

_SEED_LENGTHS = (12, 15, 18, 21, 24)


def default_env_path():
    """`.env` in the current working directory."""
    return os.path.join(os.getcwd(), ".env")


def derive_from_seed(mnemonic):
    """Return (address, private_key_hex) for a BIP-39 seed phrase."""
    acct = Account.from_mnemonic(mnemonic.strip())
    return acct.address, acct.key.hex()


def parse_line(line):
    """Parse one .env line into {"address", "key"} or return None.

    `key` is None for an address-only line (scan works without a key).
    """
    line = line.strip()
    if not line or line.startswith("#"):
        return None

    parts = line.split()

    # 0xADDRESS word1 word2 ... (address + seed phrase)
    if (parts[0].startswith("0x") and len(parts[0]) == 42
            and len(parts) - 1 in _SEED_LENGTHS):
        given_addr = parts[0]
        derived_addr, key = derive_from_seed(" ".join(parts[1:]))
        if derived_addr.lower() != given_addr.lower():
            # Non-standard derivation path: trust the address the user wrote,
            # but warn so a typo in the seed does not silently use a wrong key.
            print(f"  [!] {given_addr[:10]}...: seed derives {derived_addr[:10]}... "
                  f"(using given address with derived key)")
        return {"address": given_addr, "key": key}

    # word1 word2 ... (seed phrase only, address derived)
    if len(parts) in _SEED_LENGTHS and "=" not in line:
        addr, key = derive_from_seed(line)
        return {"address": addr, "key": key}

    # 0xADDRESS=0xPRIVATEKEY
    if "=" in line:
        addr, key = line.split("=", 1)
        addr, key = addr.strip(), key.strip()
        if addr.startswith("0x") and len(addr) == 42:
            if key and not key.startswith("0x"):
                key = "0x" + key
            return {"address": addr, "key": key or None}

    return None


def load_wallets(env_path=None):
    """Load all wallets from a .env file. Returns a list of {"address","key"}."""
    env_path = env_path or default_env_path()
    wallets = []
    if not os.path.exists(env_path):
        return wallets
    with open(env_path, "r", encoding="utf-8") as f:
        for lineno, raw in enumerate(f, 1):
            try:
                w = parse_line(raw)
            except Exception as e:
                print(f"  [!] .env line {lineno}: {str(e)[:60]}")
                continue
            if w:
                wallets.append(w)
    return wallets
