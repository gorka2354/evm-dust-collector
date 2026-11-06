"""Sweep: what leaves each wallet, where it goes, and what is left behind.

Chains are faked; signing is real, with keys of the public BIP-39 test phrase.
"""

import pytest
from eth_abi import decode
from eth_account import Account
from web3 import Web3

from dust_collector import sweep
from dust_collector.prices import PRICES
from dust_collector.wallets import derive_from_seed

TEST_PHRASE = "abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon about"
WALLET, KEY = derive_from_seed(TEST_PHRASE, 0)
OTHER, OTHER_KEY = derive_from_seed(TEST_PHRASE, 1)
DESTINATION = Web3.to_checksum_address("0x" + "ab" * 20)
TOKEN = Web3.to_checksum_address("0x" + "cd" * 20)
GWEI = 10 ** 9
ETHER = 10 ** 18
UPPER_BOUND = bytes(Web3.keccak(text="getL1FeeUpperBound(uint256)")[:4])
L1_FEE = bytes(Web3.keccak(text="getL1Fee(bytes)")[:4])


class FakeEth:
    """Answers like a node, and records what would be broadcast."""

    def __init__(self, balance, token_balance=0, base_fee=GWEI, tip=0, oracle=None, l1_fee=0, gas=21_000, token_gas=50_000):
        self.balance = balance
        self.token_balance = token_balance
        self.base_fee = base_fee
        self.max_priority_fee = tip
        self.gas_price = base_fee
        self.oracle = oracle
        self.l1_fee = l1_fee
        self.gas = gas
        self.token_gas = token_gas
        self.chain_id = 1
        self.nonce = 5
        self.in_flight = 0
        self.mined = True
        self.receipt_status = 1
        self.upper_bound_missing = False
        self.calls = []
        self.sent = []
        self.account = Account

    def get_balance(self, address):
        return self.balance

    def get_block(self, tag):
        return {"baseFeePerGas": self.base_fee}

    def get_code(self, address):
        return b"\x01" if self.oracle and address.lower() == self.oracle.lower() else b""

    def call(self, tx):
        if tx["to"].lower() == TOKEN.lower():
            return self.token_balance.to_bytes(32, "big")
        self.calls.append(bytes(tx["data"]))
        if self.upper_bound_missing and tx["data"][:4] == UPPER_BOUND:
            raise ValueError("execution reverted")
        return self.l1_fee.to_bytes(32, "big")

    def estimate_gas(self, tx):
        gas = self.token_gas if tx.get("data") else self.gas
        if isinstance(gas, Exception):
            raise gas
        return gas

    def get_transaction_count(self, address, block="latest"):
        return self.nonce + (self.in_flight if block == "pending" else 0)

    def send_raw_transaction(self, raw):
        sender = Account.recover_transaction(raw)
        tx = self.decoded(raw)
        if tx["value"] + tx["gas"] * tx["maxFeePerGas"] + self.l1_fee > self.balance:
            raise ValueError("insufficient funds for gas * price + value")  # what a node answers
        self.sent.append((sender, raw))
        self.balance -= tx["value"] + tx["gas"] * self.base_fee + self.l1_fee
        if Web3.to_checksum_address(tx["to"]) == TOKEN:
            self.token_balance = 0
        return bytes(32)

    @staticmethod
    def decoded(raw):
        try:  # eth-account 0.13+
            from eth_account.typed_transactions import TypedTransaction
        except ImportError:  # eth-account 0.11, what web3 6.x pulls in
            from eth_account._utils.typed_transactions import TypedTransaction
        return TypedTransaction.from_bytes(raw).as_dict()

    def wait_for_transaction_receipt(self, tx_hash, timeout):
        if not self.mined:
            raise TimeoutError("not mined")
        return type("Receipt", (), {"status": self.receipt_status})()


class FakeW3:
    def __init__(self, eth):
        self.eth = eth


CHAIN = {"plain": {"id": 1, "rpc": "x", "native": "ETH", "decimals": 18}}


@pytest.fixture(autouse=True)
def prices(monkeypatch):
    monkeypatch.setitem(PRICES, "ETH", 2000.0)
    monkeypatch.setitem(PRICES, "USDC", 1.0)
    monkeypatch.setattr(sweep, "TOKENS", {"plain": [{"symbol": "USDC", "address": TOKEN, "decimals": 6}]})


def run(eth, live=False, key=KEY):
    return sweep.sweep_wallet(WALLET, key, DESTINATION, live=live, chains=CHAIN, w3_for=lambda cfg: FakeW3(eth))


def test_the_amount_is_the_balance_less_the_worst_fee():
    assert sweep.native_send_amount(10 ** 15, 21_000, GWEI, 5) == 10 ** 15 - 21_000 * GWEI - 5
    assert sweep.native_send_amount(1_000, 21_000, GWEI, 0) == 0


@pytest.mark.parametrize("value, fee, worth", [(0.04, 0.0, False), (0.05, 0.0, True), (1.0, 0.25, True), (1.0, 0.26, False)])
def test_a_balance_moves_only_when_worth_it_after_its_fee(value, fee, worth):
    assert sweep.worth_sending(value, fee) is worth


def test_the_token_transfer_is_the_standard_call():
    data = sweep.transfer_data(DESTINATION, 5)
    assert data[:4] == bytes(Web3.keccak(text="transfer(address,uint256)")[:4])
    assert data[4:36] == bytes.fromhex(DESTINATION[2:].lower().rjust(64, "0"))
    assert int.from_bytes(data[36:], "big") == 5


def test_a_dry_run_plans_the_native_balance_less_its_fee_and_sends_nothing():
    eth = FakeEth(balance=10 ** 15)
    [row] = run(eth)
    gas = int(21_000 * 1.2)
    assert row["status"] == "planned"
    assert row["amount"] == pytest.approx((10 ** 15 - gas * int(GWEI * 1.3)) / ETHER)
    assert eth.sent == []


def test_where_rollups_bill_an_l1_fee_it_is_left_for_too():
    eth = FakeEth(balance=10 ** 15, oracle=sweep.OP_STACK_ORACLE, l1_fee=7 * 10 ** 9)
    [row] = run(eth)
    gas = int(21_000 * 1.2)
    assert row["amount"] == pytest.approx((10 ** 15 - gas * int(GWEI * 1.3) - 7 * 10 ** 9) / ETHER)
    assert eth.calls == [UPPER_BOUND + sweep.TX_SIZE_BYTES.to_bytes(32, "big")]


def test_an_op_stack_chain_before_fjord_doubles_the_fee_of_a_transaction_sized_blob():
    eth = FakeEth(balance=1, oracle=sweep.OP_STACK_ORACLE, l1_fee=7)
    eth.upper_bound_missing = True
    assert sweep.l1_fee_upper_bound(FakeW3(eth)) == 14
    assert eth.calls[-1][:4] == L1_FEE
    assert len(decode(["bytes"], eth.calls[-1][4:])[0]) == sweep.TX_SIZE_BYTES


def test_scroll_doubles_the_fee_of_a_transaction_sized_blob():
    eth = FakeEth(balance=1, oracle=sweep.SCROLL_ORACLE, l1_fee=7)
    assert sweep.l1_fee_upper_bound(FakeW3(eth)) == 14
    assert eth.calls[-1][:4] == L1_FEE
    assert len(decode(["bytes"], eth.calls[-1][4:])[0]) == sweep.TX_SIZE_BYTES


def test_a_chain_without_an_oracle_bills_no_l1_fee():
    assert sweep.l1_fee_upper_bound(FakeW3(FakeEth(balance=1))) == 0


def test_a_planned_token_transfer_leaves_its_fee_out_of_the_native_amount():
    eth = FakeEth(balance=10 ** 15, token_balance=5_000_000)
    rows = run(eth)
    token_fee = int(50_000 * 1.2) * int(GWEI * 1.3)
    native_fee = int(21_000 * 1.2) * int(GWEI * 1.3)
    assert [r["status"] for r in rows] == ["planned", "planned"]
    assert rows[1]["amount"] == pytest.approx((10 ** 15 - token_fee - native_fee) / ETHER)


def test_a_reverted_transfer_is_reported_as_reverted(capsys):
    eth = FakeEth(balance=10 ** 15)
    eth.receipt_status = 0
    rows = run(eth, live=True)
    assert [r["status"] for r in rows] == ["reverted"]
    sweep.print_sweep(rows, live=True)
    assert "[REVERTED] plain" in capsys.readouterr().out


def test_dust_smaller_than_it_is_worth_moving_stays():
    [row] = run(FakeEth(balance=10 ** 13))
    assert row["status"] == "not worth it"


@pytest.mark.parametrize("balance", [5 * 10 ** 13, 10 ** 14])  # $0.03 left after the fee; a fee of half the value
def test_native_dust_that_would_cover_its_fee_still_stays_when_not_worth_it(balance):
    eth = FakeEth(balance=balance)
    [row] = run(eth, live=True)
    assert row["status"] == "not worth it"
    assert eth.sent == []


def test_an_empty_wallet_has_nothing_to_report():
    assert run(FakeEth(balance=0)) == []


def test_a_token_without_gas_beside_it_is_reported_and_left():
    eth = FakeEth(balance=10, token_balance=5_000_000)
    rows = run(eth)
    assert [r["status"] for r in rows if r["symbol"] == "USDC"] == ["no gas"]


def test_a_live_sweep_sends_the_token_then_the_rest_of_the_native_balance_to_the_destination_only():
    eth = FakeEth(balance=10 ** 15, token_balance=5_000_000)
    rows = run(eth, live=True)
    assert [r["status"] for r in rows] == ["sent", "sent"]
    assert len(eth.sent) == 2
    (token_sender, token_raw), (native_sender, native_raw) = eth.sent
    assert token_sender == native_sender == WALLET
    token_tx = FakeEth.decoded(token_raw)
    assert Web3.to_checksum_address(token_tx["to"]) == TOKEN
    assert token_tx["value"] == 0
    assert bytes(token_tx["data"]) == sweep.transfer_data(DESTINATION, 5_000_000)
    native_tx = FakeEth.decoded(native_raw)
    assert Web3.to_checksum_address(native_tx["to"]) == DESTINATION
    assert bytes(native_tx["data"]) == b""
    assert native_tx["nonce"] == token_tx["nonce"] + 1
    assert native_tx["value"] > 0


def test_a_wallet_with_a_transaction_in_flight_is_left_alone():
    eth = FakeEth(balance=10 ** 15, token_balance=5_000_000)
    eth.in_flight = 1
    rows = run(eth, live=True)
    assert [r["status"] for r in rows] == ["in flight"]
    assert eth.sent == []


def test_a_token_transfer_not_mined_in_time_is_reported_and_the_native_balance_waits():
    eth = FakeEth(balance=10 ** 15, token_balance=5_000_000)
    eth.mined = False
    rows = run(eth, live=True)
    assert [(r["symbol"], r["status"]) for r in rows] == [("USDC", "pending")]
    assert rows[0]["tx"].startswith("0x")
    assert len(eth.sent) == 1


def test_a_token_that_cannot_be_moved_doesnt_hold_up_the_native_balance():
    eth = FakeEth(balance=10 ** 15, token_balance=5_000_000, token_gas=ValueError("execution reverted"))
    rows = run(eth, live=True)
    assert [r["status"].split(":")[0] for r in rows] == ["error", "sent"]
    assert len(eth.sent) == 1


def test_a_sent_transaction_is_still_reported_when_a_later_step_fails():
    eth = FakeEth(balance=10 ** 15, token_balance=5_000_000, gas=ConnectionError("node down"))
    rows = run(eth, live=True)
    assert [r["status"].split(":")[0] for r in rows] == ["sent", "error"]
    assert rows[0]["tx"].startswith("0x")


def test_the_fee_ceiling_stays_close_to_the_base_fee_so_little_is_stranded():
    max_fee, fields = sweep.fee_params(FakeW3(FakeEth(balance=1, base_fee=10 * GWEI, tip=GWEI)))
    assert max_fee == 13 * GWEI + GWEI
    assert fields == {"maxFeePerGas": max_fee, "maxPriorityFeePerGas": GWEI}


def test_a_chain_without_a_base_fee_uses_its_gas_price():
    eth = FakeEth(balance=1, base_fee=0)
    eth.gas_price = 3 * GWEI
    assert sweep.fee_params(FakeW3(eth)) == (3 * GWEI, {"gasPrice": 3 * GWEI})


def test_one_chain_failing_doesnt_stop_the_others():
    class Broken:
        @property
        def eth(self):
            raise ConnectionError("node down")

    chains = {"down": {"id": 2, "rpc": "x", "native": "ETH", "decimals": 18}, **CHAIN}
    good = FakeEth(balance=10 ** 15)
    rows = sweep.sweep_wallet(WALLET, KEY, DESTINATION, chains=chains, w3_for=lambda cfg: Broken() if cfg["id"] == 2 else FakeW3(good))
    assert [r["status"].split(":")[0] for r in rows] == ["error", "planned"]


def test_the_destination_cannot_be_one_of_the_wallets():
    with pytest.raises(ValueError):
        sweep.sweep_all([{"address": WALLET, "key": KEY}], WALLET)


def test_a_live_sweep_needs_every_key():
    with pytest.raises(ValueError):
        sweep.sweep_all([{"address": WALLET, "key": KEY}, {"address": OTHER, "key": None}], DESTINATION, live=True, chains=CHAIN)


def test_a_chain_whose_node_is_down_or_on_another_chain_is_skipped_for_every_wallet(capsys):
    class Broken:
        @property
        def eth(self):
            raise ConnectionError("node down")

    other = FakeEth(balance=10 ** 15)
    other.chain_id = 999
    good = FakeEth(balance=10 ** 15)
    nodes = {1: FakeW3(good), 2: Broken(), 3: FakeW3(other)}
    chains = {**CHAIN,
              "down": {"id": 2, "rpc": "x", "native": "ETH", "decimals": 18},
              "mislabelled": {"id": 3, "rpc": "x", "native": "ETH", "decimals": 18}}
    rows = sweep.sweep_all([{"address": WALLET, "key": KEY}, {"address": OTHER, "key": OTHER_KEY}], DESTINATION,
                           live=True, chains=chains, w3_for=lambda cfg: nodes[cfg["id"]])
    out = capsys.readouterr().out
    assert {r["chain"] for r in rows} == {"plain"}
    assert "down: node unreachable" in out
    assert "mislabelled: node answers for chain 999, not 3" in out
    assert other.sent == []


def test_many_wallets_are_planned_together():
    eth = FakeEth(balance=10 ** 15)
    rows = sweep.sweep_all([{"address": WALLET, "key": None}, {"address": OTHER, "key": None}], DESTINATION, chains=CHAIN, w3_for=lambda cfg: FakeW3(eth))
    assert sorted(r["wallet"] for r in rows) == sorted([WALLET, OTHER])
    assert all(r["status"] == "planned" for r in rows)
