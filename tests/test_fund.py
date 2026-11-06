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
        self.txs = []           # every accepted transaction, decoded
        self.value_gas = {}     # recipient -> gas a transfer with value costs (an EIP-7702 account, say)
        self.on_send = None     # called after each accepted transaction: fees can move

    def get_balance(self, address):
        return self.native.get(address, 0)

    def get_block(self, tag):
        return {"baseFeePerGas": self.base_fee, "timestamp": int(time.time())}

    def get_code(self, address):
        return b"\xef\x01\x00" if address in self.value_gas else b""  # what a delegated EIP-7702 account shows

    def call(self, tx):
        data = bytes(tx["data"])
        if data[:4] == BALANCE_OF:
            return self.tokens.get(Web3.to_checksum_address("0x" + data[-20:].hex()), 0).to_bytes(32, "big")
        return (1).to_bytes(32, "big")  # transfer() answers true

    def estimate_gas(self, tx):
        if tx.get("value", 0) > self.get_balance(tx["from"]):
            raise ValueError("failed with 33554432 gas: insufficient funds for transfer")  # as a real node does
        if tx.get("data"):
            return 50_000
        return self.value_gas.get(tx["to"], 21_000) if tx.get("value") else 21_000

    def get_transaction_count(self, address, block="latest"):
        return self.nonces.get(address, 0)

    def send_raw_transaction(self, raw):
        sender = Account.recover_transaction(raw)
        tx = decoded(raw)
        if tx["value"] + tx["gas"] * tx["maxFeePerGas"] > self.get_balance(sender):
            raise ValueError("insufficient funds for gas * price + value")
        if tx["maxFeePerGas"] < self.base_fee:
            raise ValueError("max fee per gas less than block base fee")
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
        self.txs.append(tx)
        if self.on_send:
            self.on_send(self)
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


def stuck(wallet, gas, usd, native=30, fee_usd=0.01):
    return {"wallet": wallet, "chain": "plain", "status": "no gas", "gas": gas, "native_wei": native,
            "usd": usd, "fee_usd": fee_usd}


def test_the_top_up_covers_every_stuck_token_twice_at_the_fees_of_the_moment_less_the_gas_there():
    rows = [stuck(A, 100, 1.0), stuck(A, 50, 2.0), {"wallet": B, "chain": "plain", "status": "planned", "usd": 5.0}]
    [need] = sweep.funding_needs(rows)
    assert need == {"wallet": A, "chain": "plain", "gas": 150, "transfers": 2, "native_wei": 30, "usd": 3.0}
    assert sweep.top_up(need, max_fee=10, l1_fee=7) == (150 * 10 + 2 * 7) * sweep.FUND_MARGIN - 30
    assert sweep.top_up(need, max_fee=30, l1_fee=7) == (150 * 30 + 2 * 7) * sweep.FUND_MARGIN - 30


def test_a_token_without_any_price_never_earns_a_top_up():
    assert sweep.funding_needs([stuck(A, 100, 0.0)]) == []


def test_tokens_the_sweep_would_leave_anyway_free_nothing_even_together():
    # Two tokens of 4 cents: each under the sweep's minimum, so funding them moves nothing.
    assert sweep.funding_needs([stuck(A, 100, 0.04), stuck(A, 100, 0.04)]) == []


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


def test_an_empty_funder_is_told_how_much_to_send_before_anything_else(capsys):
    node = Chain(native={}, tokens={A: 5_000_000, B: 3_000_000})
    plan = sweep.fund_all(wallets(0, 1, 2), FUNDER, DESTINATION, chains=CHAIN, w3_for=lambda cfg: W3(node))
    assert sorted((r["wallet"], r["status"]) for r in plan) == [(A, "funder short"), (B, "funder short")]
    token_fee = int(50_000 * 1.2) * int(GWEI * 1.3)
    hop = int(21_000 * 1.2) * int(GWEI * 1.3)
    wanted = 2 * (token_fee * sweep.FUND_MARGIN + hop)
    shown = sweep._up(wanted / 10 ** 18)
    assert float(shown) >= wanted / 10 ** 18
    assert f"the funder needs {shown} ETH in all, holds 0: send it {shown} ETH more" in capsys.readouterr().out


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


def test_a_token_the_sweep_would_move_still_gets_no_top_up_costing_too_much_of_it():
    # $1 of USDC: its own $0.16 fee passes the sweep's rule, but a top-up of twice that plus the
    # funder's own fee comes to $0.38, over a quarter of what it frees.
    node = Chain(native={FUNDER: 10 ** 16}, tokens={A: 1_000_000})
    rows = sweep.fund_all(wallets(0, 1), FUNDER, DESTINATION, live=True, chains=CHAIN, w3_for=lambda cfg: W3(node))
    assert [(r["wallet"], r["status"]) for r in rows] == [(A, "not worth it")]
    assert node.sent == []


def test_the_funder_keeps_what_its_own_stuck_tokens_need():
    token_fee = int(50_000 * 1.2) * int(GWEI * 1.3)
    own_need = token_fee * sweep.FUND_MARGIN
    # Enough for its own tokens' need plus a little: not for A's top-up as well.
    node = Chain(native={FUNDER: own_need + token_fee}, tokens={FUNDER: 5_000_000, A: 5_000_000})
    rows = sweep.fund_all(wallets(0, 1), FUNDER, DESTINATION, live=True, chains=CHAIN, w3_for=lambda cfg: W3(node))
    assert [(r["wallet"], r["status"]) for r in rows] == [(A, "funder short")]
    assert node.sent == []


def test_a_top_up_is_priced_at_the_gas_price_it_is_sent_at():
    node = Chain(native={FUNDER: 10 ** 16}, tokens={A: 5_000_000})
    plan = sweep.sweep_all([{"address": A, "key": None}], DESTINATION, chains=CHAIN, w3_for=lambda cfg: W3(node))
    [need] = sweep.funding_needs(plan)
    node.base_fee = node.gas_price = 3 * GWEI  # gas got dearer after the plan
    rows = []
    sweep.fund_chain(W3(node), "plain", CHAIN["plain"], FUNDER, FUNDER_KEY, [need], True, rows)
    [(sender, to, value)] = node.sent
    assert (sender, to) == (FUNDER, A)
    assert value == int(50_000 * 1.2) * int(3 * GWEI * 1.3) * sweep.FUND_MARGIN


def triple_the_gas_price(node):
    node.base_fee = node.gas_price = 3 * GWEI


def test_gas_dearer_after_one_top_up_prices_the_next_one_afresh():
    node = Chain(native={FUNDER: 10 ** 16}, tokens={A: 5_000_000, B: 5_000_000})
    node.on_send = triple_the_gas_price
    rows = sweep.fund_all(wallets(0, 1, 2), FUNDER, DESTINATION, live=True, chains=CHAIN, w3_for=lambda cfg: W3(node))
    assert [r["status"] for r in rows] == ["sent", "sent"]
    first, second = node.txs
    assert second["maxFeePerGas"] >= int(3 * GWEI * 1.3)
    assert second["value"] == int(50_000 * 1.2) * int(3 * GWEI * 1.3) * sweep.FUND_MARGIN


def test_gas_dearer_after_one_top_up_cannot_eat_into_the_funders_reserve():
    token_fee = int(50_000 * 1.2) * int(GWEI * 1.3)
    hop = int(21_000 * 1.2) * int(GWEI * 1.3)
    reserve = token_fee * sweep.FUND_MARGIN  # the funder's own token, at 1 gwei
    # At 1 gwei: room for its reserve and both top-ups. At 3 gwei the second no longer fits.
    node = Chain(native={FUNDER: reserve + 2 * (token_fee * sweep.FUND_MARGIN + hop)},
                 tokens={FUNDER: 5_000_000, A: 5_000_000, B: 5_000_000})
    node.on_send = triple_the_gas_price
    rows = sweep.fund_all(wallets(0, 1, 2), FUNDER, DESTINATION, live=True, chains=CHAIN, w3_for=lambda cfg: W3(node))
    assert [r["status"] for r in rows] == ["sent", "funder short"]
    own_need_now = int(50_000 * 1.2) * int(3 * GWEI * 1.3)
    assert node.native[FUNDER] >= own_need_now


def test_a_top_up_to_an_account_with_code_gets_the_gas_its_value_costs():
    node = Chain(native={FUNDER: 10 ** 16}, tokens={A: 5_000_000})
    node.value_gas[A] = 79_495  # an EIP-7702 account whose receive path stores when value comes in
    sweep.fund_all(wallets(0, 1), FUNDER, DESTINATION, live=True, chains=CHAIN, w3_for=lambda cfg: W3(node))
    [tx] = node.txs
    assert tx["gas"] >= 79_495


def test_a_top_up_whose_gas_with_its_value_costs_too_much_of_what_it_frees_is_not_sent():
    # $2 of USDC: $0.38 to top up on the value-free estimate, $0.56 once the value's gas is in: over $0.50.
    node = Chain(native={FUNDER: 10 ** 16}, tokens={A: 2_000_000})
    node.value_gas[A] = 79_495
    rows = sweep.fund_all(wallets(0, 1), FUNDER, DESTINATION, live=True, chains=CHAIN, w3_for=lambda cfg: W3(node))
    assert [(r["wallet"], r["status"]) for r in rows] == [(A, "not worth it")]
    assert node.sent == []


def test_an_empty_funders_plan_for_a_recipient_with_code_asks_for_enough(capsys):
    node = Chain(native={}, tokens={A: 5_000_000})
    node.value_gas[A] = 79_495
    sweep.fund_all(wallets(0, 1), FUNDER, DESTINATION, chains=CHAIN, w3_for=lambda cfg: W3(node))
    line = next(l for l in capsys.readouterr().out.splitlines() if "the funder needs" in l)
    wanted = float(line.split("needs ")[1].split(" ")[0])
    node.native[FUNDER] = int(wanted * 10 ** 18) + 1  # exactly what the plan asked for
    rows = sweep.fund_all(wallets(0, 1), FUNDER, DESTINATION, live=True, chains=CHAIN, w3_for=lambda cfg: W3(node))
    assert [(r["wallet"], r["status"]) for r in rows] == [(A, "sent")]


def test_a_node_showing_the_old_balance_after_a_top_up_cannot_spend_the_reserve():
    node = Chain(native={FUNDER: 0}, tokens={A: 5_000_000, B: 5_000_000})
    each = int(50_000 * 1.2) * int(GWEI * 1.3) * sweep.FUND_MARGIN + int(21_000 * 1.2) * int(GWEI * 1.3)
    node.native[FUNDER] = each + each // 2  # enough for one top-up, not two
    seen = node.native[FUNDER]
    stale = W3(node)
    original = node.get_balance
    node.get_balance = lambda address: seen if address == FUNDER else original(address)  # never catches up
    plan = sweep.sweep_all([{"address": A, "key": None}, {"address": B, "key": None}], DESTINATION,
                           chains=CHAIN, w3_for=lambda cfg: stale)
    rows = []
    sweep.fund_chain(stale, "plain", CHAIN["plain"], FUNDER, FUNDER_KEY, sweep.funding_needs(plan), True, rows)
    assert sorted(r["status"] for r in rows) == ["funder short", "sent"]


def test_the_funder_can_be_the_destination_too():
    node = Chain(native={FUNDER: 10 ** 16}, tokens={A: 5_000_000})
    rows = sweep.fund_all(wallets(0, 1), FUNDER, FUNDER, live=True, chains=CHAIN, w3_for=lambda cfg: W3(node))
    assert [(r["wallet"], r["status"]) for r in rows] == [(A, "sent")]


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


@pytest.mark.parametrize("amount, shown", [(0.000269491, "0.0002695"), (1.25163, "1.252"), (1.25121, "1.252"), (0.0001, "0.0001000"),
                                           (2.14694e-06, "0.000002147"), (12.0, "12.00")])
def test_amounts_to_send_are_rounded_up_never_down(amount, shown):
    assert sweep._up(amount) == shown
    assert float(shown) >= amount


def test_fund_plans_only_without_live(funds):
    assert cli.main(["--from", FUNDER, "--to", DESTINATION, "fund"]) == 0
    assert funds == [False]


def test_fund_sends_after_the_plan_and_a_typed_yes(funds, monkeypatch, tmp_path):
    monkeypatch.setattr("builtins.input", lambda prompt="": "yes")
    assert cli.main(["--from", FUNDER, "--to", DESTINATION, "--live", "fund"]) == 0
    assert funds == [False, True]
    assert list(tmp_path.glob("sweep-*.csv"))


def test_fund_prints_every_hash_when_the_report_cannot_be_written(funds, monkeypatch, capsys):
    def refuse(rows, path):
        raise PermissionError("access denied")

    monkeypatch.setattr(cli.sweep, "write_report", refuse)
    assert cli.main(["--from", FUNDER, "--to", DESTINATION, "--live", "--yes", "fund"]) == 1
    assert funds == [False, True]
    assert "0x" + "ef" * 32 in capsys.readouterr().out


def test_fund_needs_a_funder_and_a_destination(funds):
    assert cli.main(["--to", DESTINATION, "fund"]) == 1
    assert cli.main(["--from", FUNDER, "fund"]) == 1
    assert funds == []
