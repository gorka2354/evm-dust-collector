"""fund: gas from one wallet to the ones whose tokens can't move without it, then sweep takes the tokens.

A fake chain with a balance per address; signing is real, with keys of the public BIP-39 test phrase.
"""

import time

import pytest
from eth_account import Account
from web3 import Web3

from dust_collector import cli, sweep
from dust_collector.prices import PRICES
from dust_collector.wallets import derive_from_seed

TEST_PHRASE = "abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon about"
(FUNDER, FUNDER_KEY), (A, A_KEY), (B, B_KEY) = (derive_from_seed(TEST_PHRASE, i) for i in range(3))
DESTINATION = Web3.to_checksum_address("0x" + "ab" * 20)
TOKEN = Web3.to_checksum_address("0x" + "cd" * 20)
GWEI = 10 ** 9
CHAIN = {"plain": {"id": 1, "rpc": "x", "native": "ETH", "decimals": 18}}
BALANCE_OF = bytes(Web3.keccak(text="balanceOf(address)")[:4])


def decoded(raw):
    try:  # eth-account 0.13+
        from eth_account.typed_transactions import TypedTransaction
    except ImportError:  # eth-account 0.11
        from eth_account._utils.typed_transactions import TypedTransaction
    return TypedTransaction.from_bytes(raw).as_dict()


class Chain:
    """A node with a native balance and a token balance per address."""

    chain_id = 1
    max_priority_fee = 0
    account = Account

    def __init__(self, native, tokens, base_fee=GWEI):
        self.native = dict(native)
        self.tokens = dict(tokens)
        self.base_fee = base_fee
        self.gas_price = base_fee
        self.nonces = {}
        self.sent = []

    def get_balance(self, address):
        return self.native.get(address, 0)

    def get_block(self, tag):
        return {"baseFeePerGas": self.base_fee, "timestamp": int(time.time())}

    def get_code(self, address):
        return b""

    def call(self, tx):
        data = bytes(tx["data"])
        if data[:4] == BALANCE_OF:
            return self.tokens.get(Web3.to_checksum_address("0x" + data[-20:].hex()), 0).to_bytes(32, "big")
        return (1).to_bytes(32, "big")  # transfer() answers true

    def estimate_gas(self, tx):
        return 50_000 if tx.get("data") else 21_000

    def get_transaction_count(self, address, block="latest"):
        return self.nonces.get(address, 0)

    def send_raw_transaction(self, raw):
        sender = Account.recover_transaction(raw)
        tx = decoded(raw)
        if tx["value"] + tx["gas"] * tx["maxFeePerGas"] > self.get_balance(sender):
            raise ValueError("insufficient funds for gas * price + value")
        assert tx["nonce"] == self.nonces.get(sender, 0), "nonce out of order"
        self.nonces[sender] = tx["nonce"] + 1
        self.native[sender] -= tx["value"] + tx["gas"] * self.base_fee
        to = Web3.to_checksum_address(tx["to"])
        if to == TOKEN:
            data = bytes(tx["data"])
            recipient = Web3.to_checksum_address("0x" + data[16:36].hex())
            amount = int.from_bytes(data[36:68], "big")
            self.tokens[sender] -= amount
            self.tokens[recipient] = self.tokens.get(recipient, 0) + amount
        else:
            self.native[to] = self.native.get(to, 0) + tx["value"]
        self.sent.append((sender, to, tx["value"]))
        return Web3.keccak(raw)

    def get_transaction_receipt(self, tx_hash):
        return type("Receipt", (), {"status": 1})()

    def get_transaction(self, tx_hash):
        return {"hash": tx_hash}


class W3:
    def __init__(self, eth):
        self.eth = eth


@pytest.fixture(autouse=True)
def setup(monkeypatch):
    monkeypatch.setitem(PRICES, "ETH", 2000.0)
    monkeypatch.setitem(PRICES, "USDC", 1.0)
    monkeypatch.setattr(sweep, "TOKENS", {"plain": [{"symbol": "USDC", "address": TOKEN, "decimals": 6}]})
    monkeypatch.setattr(sweep, "RECEIPT_POLL", 0)


def wallets(*keys):
    return [{"address": derive_from_seed(TEST_PHRASE, i)[0], "key": derive_from_seed(TEST_PHRASE, i)[1]} for i in keys]


def test_the_need_covers_every_stuck_token_twice_less_the_gas_already_there():
    rows = [{"wallet": A, "chain": "plain", "status": "no gas", "fee_wei": 100, "native_wei": 30, "usd": 1.0},
            {"wallet": A, "chain": "plain", "status": "no gas", "fee_wei": 50, "native_wei": 30, "usd": 2.0},
            {"wallet": B, "chain": "plain", "status": "planned", "usd": 5.0}]
    assert sweep.funding_needs(rows) == [{"wallet": A, "chain": "plain", "need_wei": 150 * sweep.FUND_MARGIN - 30, "usd": 3.0}]


def test_top_ups_free_the_stuck_tokens_and_the_sweep_then_takes_them():
    node = Chain(native={FUNDER: 10 ** 16}, tokens={A: 5_000_000, B: 3_000_000})
    w3_for = lambda cfg: W3(node)  # noqa: E731
    plan = sweep.fund_all(wallets(0, 1, 2), FUNDER, DESTINATION, chains=CHAIN, w3_for=w3_for)
    assert sorted((r["wallet"], r["status"]) for r in plan) == [(A, "planned"), (B, "planned")]
    assert node.sent == []

    sweep.fund_all(wallets(0, 1, 2), FUNDER, DESTINATION, live=True, chains=CHAIN, w3_for=w3_for)
    assert {(s, t) for s, t, _ in node.sent} == {(FUNDER, A), (FUNDER, B)}

    rows = sweep.sweep_all(wallets(1, 2), DESTINATION, live=True, chains=CHAIN, w3_for=w3_for)
    assert sorted((r["wallet"], r["symbol"], r["status"]) for r in rows if r["symbol"] == "USDC") == [(A, "USDC", "sent"), (B, "USDC", "sent")]
    assert node.tokens[DESTINATION] == 8_000_000


def test_a_funder_without_enough_sends_nothing_it_cannot_cover():
    node = Chain(native={FUNDER: 10 ** 9}, tokens={A: 5_000_000})
    rows = sweep.fund_all(wallets(0, 1), FUNDER, DESTINATION, live=True, chains=CHAIN, w3_for=lambda cfg: W3(node))
    assert [r["status"] for r in rows] == ["funder short"]
    assert node.sent == []


def test_tokens_not_worth_their_top_up_get_none():
    node = Chain(native={FUNDER: 10 ** 16}, tokens={A: 10_000})  # one cent of USDC
    rows = sweep.fund_all(wallets(0, 1), FUNDER, DESTINATION, live=True, chains=CHAIN, w3_for=lambda cfg: W3(node))
    assert [r["status"] for r in rows] in ([], ["not worth it"])
    assert node.sent == []


def test_the_funder_keeps_what_its_own_stuck_tokens_need():
    token_fee = int(50_000 * 1.2) * int(GWEI * 1.3)
    own_need = token_fee * sweep.FUND_MARGIN
    # Enough for its own tokens' need plus a little: not for A's top-up as well.
    node = Chain(native={FUNDER: own_need + token_fee}, tokens={FUNDER: 5_000_000, A: 5_000_000})
    rows = sweep.fund_all(wallets(0, 1), FUNDER, DESTINATION, live=True, chains=CHAIN, w3_for=lambda cfg: W3(node))
    assert [(r["wallet"], r["status"]) for r in rows] == [(A, "funder short")]
    assert node.sent == []


def test_a_funder_with_stuck_tokens_of_its_own_never_sends_to_itself():
    node = Chain(native={FUNDER: 0}, tokens={FUNDER: 5_000_000, A: 5_000_000})
    rows = sweep.fund_all(wallets(0, 1), FUNDER, DESTINATION, live=True, chains=CHAIN, w3_for=lambda cfg: W3(node))
    assert [r["wallet"] for r in rows] == [A]
    assert node.sent == []


def test_the_funder_must_be_a_loaded_wallet_with_its_key():
    with pytest.raises(ValueError):
        sweep.fund_all([{"address": A, "key": A_KEY}], FUNDER, DESTINATION, chains=CHAIN, w3_for=lambda cfg: W3(Chain({}, {})))
    with pytest.raises(ValueError):
        sweep.fund_all([{"address": FUNDER, "key": None}], FUNDER, DESTINATION, chains=CHAIN, w3_for=lambda cfg: W3(Chain({}, {})))


@pytest.fixture
def funds(tmp_path, monkeypatch):
    made = []

    def fake_fund_all(wallets, funder, to, live=False, chains=None):
        made.append(live)
        return [{"chain": "plain", "wallet": A, "symbol": "ETH", "amount": 0.001, "usd": 2.0, "fee_usd": 0.0,
                 "status": "sent" if live else "planned", "tx": "0x" + "ef" * 32 if live else None, "frees_usd": 5.0}]

    monkeypatch.setattr(cli, "update_prices", lambda: None)
    monkeypatch.setattr(cli.sweep, "fund_all", fake_fund_all)
    monkeypatch.setattr(cli.sweep, "print_sweep", lambda rows, live, title="SWEEP": 0)
    monkeypatch.delenv("MAIN_WALLET", raising=False)
    (tmp_path / ".env").write_text(f"{TEST_PHRASE}\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    return made


def test_fund_plans_only_without_live(funds):
    assert cli.main(["--from", FUNDER, "--to", DESTINATION, "fund"]) == 0
    assert funds == [False]


def test_fund_sends_after_the_plan_and_a_typed_yes(funds, monkeypatch, tmp_path):
    monkeypatch.setattr("builtins.input", lambda prompt="": "yes")
    assert cli.main(["--from", FUNDER, "--to", DESTINATION, "--live", "fund"]) == 0
    assert funds == [False, True]
    assert list(tmp_path.glob("sweep-*.csv"))


def test_fund_needs_a_funder_and_a_destination(funds):
    assert cli.main(["--to", DESTINATION, "fund"]) == 1
    assert cli.main(["--from", FUNDER, "fund"]) == 1
    assert funds == []
