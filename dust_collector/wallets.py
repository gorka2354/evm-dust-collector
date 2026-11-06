"""Single .env wallet loader (previously reimplemented in 5 scripts).

Supported line formats (one wallet per line):

    word1 word2 ... word12                  # seed phrase: its first account
    0xADDRESS word1 word2 ... word12        # address + the seed phrase it came from
    0xADDRESS=0xPRIVATEKEY                   # address + raw private key
    0xPRIVATEKEY                             # raw private key: its address
    0xADDRESS=                               # address only

Lines that are blank or start with '#' are ignored.

An address-only line takes its key from a seed phrase or private key on
another line of the same file, if that phrase or key makes the address. So a
list of addresses and the phrases behind them can be pasted together in any
order, whether every wallet has a phrase of its own or all of them come from
one. A phrase or key that gives a listed address its key is used for that
alone; one that matches no listed address stands for its own first account.

A key is attached only where it makes exactly that address, never by guess:
signing with another account's key would act on another wallet.

NOTE: seed phrases and private keys are read straight off disk and used
only locally to sign transactions. They are never written anywhere or sent
over the network, and no message here ever repeats a line from the file.
See SECURITY.md.
"""

import os
import re
from functools import lru_cache

from eth_account import Account
from eth_account.hdaccount import key_from_seed, seed_from_mnemonic

# Required to derive keys from a mnemonic; this is a local-only operation.
Account.enable_unaudited_hdwallet_features()

_SEED_LENGTHS = (12, 15, 18, 21, 24)
_ADDRESS = re.compile(r"0x[0-9a-fA-F]{40}")
_PRIVATE_KEY = re.compile(r"(0x)?[0-9a-fA-F]{64}")

# Wallets made from one phrase sit at m/44'/60'/0'/0/N. Up to this many
# accounts of one phrase are searched for listed addresses. With many phrases
# the search is shallower, never under MIN_ACCOUNT_DEPTH: each phrase is then
# usually a wallet of its own, at account 0.
MAX_ACCOUNT_INDEX = 200
MIN_ACCOUNT_DEPTH = 5
_ACCOUNT_PATH = "m/44'/60'/0'/0/{}"
_EXPECTED = ("expected a seed phrase, 0xADDRESS + seed phrase, "
             "0xADDRESS=PRIVATEKEY, a private key, or 0xADDRESS=")


class WalletLineError(ValueError):
    """A line that can't be used. Its message never contains the line itself."""


def default_env_path():
    """`.env` in the current working directory."""
    return os.path.join(os.getcwd(), ".env")


@lru_cache(maxsize=None)
def _seed(mnemonic):
    return seed_from_mnemonic(mnemonic, "")


@lru_cache(maxsize=None)
def derive_from_seed(mnemonic, index=0):
    """Return (address, private_key_hex) for account `index` of a BIP-39 seed phrase."""
    acct = Account.from_key(key_from_seed(_seed(mnemonic.strip()), _ACCOUNT_PATH.format(index)))
    return acct.address, "0x" + bytes(acct.key).hex()


def key_for_address(mnemonic, address):
    """The key of the account of this phrase that has this address."""
    wanted = address.lower()
    for index in range(MAX_ACCOUNT_INDEX):
        derived, key = derive_from_seed(mnemonic, index)
        if derived.lower() == wanted:
            return key
    raise WalletLineError(f"the seed phrase on this line does not make {address[:10]}... "
                          f"in its first {MAX_ACCOUNT_INDEX} accounts")


def _from_key(key):
    key = key if key.startswith("0x") else "0x" + key
    return Account.from_key(key).address, key


def _parse(line):
    """(wallet, secret). The secret, a phrase or key, is set only for a line that names no address."""
    parts = line.split()

    # 0xADDRESS word1 word2 ... (address + seed phrase)
    if _ADDRESS.fullmatch(parts[0]) and len(parts) - 1 in _SEED_LENGTHS:
        return {"address": parts[0], "key": key_for_address(" ".join(parts[1:]), parts[0])}, None

    # word1 word2 ... (seed phrase only: its first account)
    if len(parts) in _SEED_LENGTHS and "=" not in line:
        phrase = " ".join(parts)
        addr, key = derive_from_seed(phrase)
        return {"address": addr, "key": key}, phrase

    # 0xPRIVATEKEY (a key alone: its own address)
    if len(parts) == 1 and _PRIVATE_KEY.fullmatch(line):
        addr, key = _from_key(line)
        return {"address": addr, "key": key}, key

    # 0xADDRESS=0xPRIVATEKEY, or 0xADDRESS= for an address alone
    if "=" in line:
        addr, key = (part.strip() for part in line.split("=", 1))
        if _ADDRESS.fullmatch(addr):
            if not key:
                return {"address": addr, "key": None}, None
            derived, key = _from_key(key)
            if derived.lower() != addr.lower():
                raise WalletLineError(f"the private key on this line is not the key of {addr[:10]}...")
            return {"address": addr, "key": key}, None

    raise WalletLineError(f"{len(parts)} word(s) not understood: {_EXPECTED} "
                          f"(a seed phrase has {', '.join(map(str, _SEED_LENGTHS))} words)")


def _read_line(line):
    line = line.strip()
    if not line or line.startswith("#"):
        return None, None
    try:
        return _parse(line)
    except WalletLineError:
        raise
    except Exception:
        # Errors from the key libraries can quote their input, which is a seed or key here.
        raise WalletLineError("not a valid seed phrase or private key") from None


def parse_line(line):
    """Parse one .env line into {"address", "key"} or return None.

    `key` is None for an address-only line (scan works without a key).
    Raises WalletLineError, whose message never includes the line.
    """
    return _read_line(line)[0]


def _attach_keys(wallets, secrets):
    """Give each keyless wallet the key of a phrase or key that makes its address.

    Returns the secrets that gave a key.
    """
    wanted = {w["address"].lower(): w for w in wallets if not w["key"]}
    used, phrases = set(), []
    for secret in dict.fromkeys(secrets):
        if " " in secret:
            phrases.append(secret)
            continue
        address, key = _from_key(secret)
        wallet = wanted.pop(address.lower(), None)
        if wallet:
            wallet["key"] = key
            used.add(secret)
    depth = max(MIN_ACCOUNT_DEPTH, MAX_ACCOUNT_INDEX // max(len(phrases), 1))
    for index in range(depth):
        if not wanted:
            break
        for phrase in phrases:
            address, key = derive_from_seed(phrase, index)
            wallet = wanted.pop(address.lower(), None)
            if wallet:
                wallet["key"] = key
                used.add(phrase)
    return used


def resolve_wallets(entries):
    """Wallets from parsed lines [(wallet, secret)], one per address, in file order."""
    listed = {}
    for w, secret in entries:
        if secret is None:
            seen = listed.setdefault(w["address"].lower(), w)
            if seen is not w and w["key"] and not seen["key"]:
                seen["key"] = w["key"]
    used = _attach_keys(listed.values(), [secret for _, secret in entries if secret])
    wallets, included = [], set()
    for w, secret in entries:
        address = w["address"].lower()
        if address in included or secret in used:
            continue
        included.add(address)
        wallets.append(listed.get(address, w))
    return wallets


def load_wallets(env_path=None):
    """Load all wallets from a .env file. Returns a list of {"address","key"}."""
    env_path = env_path or default_env_path()
    if not os.path.exists(env_path):
        return []
    entries = []
    with open(env_path, "r", encoding="utf-8-sig") as f:
        for lineno, raw in enumerate(f, 1):
            try:
                w, secret = _read_line(raw)
            except WalletLineError as e:
                print(f"  [!] .env line {lineno}: {e}")
                continue
            if w:
                entries.append((w, secret))
    return resolve_wallets(entries)
