"""Sweep: what leaves each wallet, where it goes, and what is left behind.

Chains are faked; signing is real, with keys of the public BIP-39 test phrase.
"""

import time

import pytest
from eth_abi import decode
from eth_account import Account
from web3 import Web3
from web3.exceptions import TransactionNotFound

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
SHIPPED_RECEIPT_POLL = sweep.RECEIPT_POLL  # read before any fixture patches it
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
        self.receipt_refusals = 0
        self.receipt_status = 1
        self.upper_bound_missing = False
        self.token_ratio = None      # set: the oracle bills the L1 fee in the native token, like Mantle's
        self.block_age = 0           # seconds since the newest block
        self.transfer_answer = 1     # what the token's transfer() returns
        self.token_moves = True      # whether a mined token transfer empties the balance
        self.lost_reply = False      # take the transaction, then lose the reply
        self.l1_fee_next = None      # the L1 fee once a transaction is mined
        self.mined_when_read = None  # a pending transaction is mined as the nonce is read, leaving this
        self.known = set()
        self.calls = []
        self.sent = []
        self.account = Account

    def get_balance(self, address):
        return self.balance

    def get_block(self, tag):
        return {"baseFeePerGas": self.base_fee, "timestamp": int(time.time()) - self.block_age}

    def get_code(self, address):
        return b"\x01" if self.oracle and address.lower() == self.oracle.lower() else b""

    def call(self, tx):
        if tx["to"].lower() == TOKEN.lower():
            if bytes(tx["data"][:4]) == sweep.TRANSFER_SELECTOR:
                return self.transfer_answer.to_bytes(32, "big")
            return self.token_balance.to_bytes(32, "big")
        if bytes(tx["data"][:4]) == sweep.TOKEN_RATIO:
            if self.token_ratio is None:
                raise ValueError("execution reverted")  # a standard OP-stack oracle has no tokenRatio()
            return self.token_ratio.to_bytes(32, "big")
        self.calls.append(bytes(tx["data"]))
        if self.upper_bound_missing and tx["data"][:4] == UPPER_BOUND:
            raise ValueError("execution reverted")
        return self.l1_fee.to_bytes(32, "big")

    def estimate_gas(self, tx):
        gas = self.token_gas if tx.get("data") else self.gas
        if callable(gas):
            gas = gas(tx["value"])
        if isinstance(gas, Exception):
            raise gas
        return gas

    def get_transaction_count(self, address, block="latest"):
        if self.mined_when_read is not None:
            self.balance, self.mined_when_read = self.mined_when_read, None
            self.nonce, self.in_flight = self.nonce + self.in_flight, 0
        return self.nonce + (self.in_flight if block == "pending" else 0)

    def send_raw_transaction(self, raw):
        sender = Account.recover_transaction(raw)
        tx = self.decoded(raw)
        if tx["value"] + tx["gas"] * tx["maxFeePerGas"] + self.l1_fee > self.balance:
            raise ValueError("insufficient funds for gas * price + value")  # what a node answers
        self.sent.append((sender, raw))
        self.known.add("0x" + bytes(Web3.keccak(raw)).hex())
        self.balance -= tx["value"] + tx["gas"] * self.base_fee + self.l1_fee
        if Web3.to_checksum_address(tx["to"]) == TOKEN and self.token_moves:
            self.token_balance = 0
        if self.l1_fee_next is not None:
            self.l1_fee = self.l1_fee_next
        if self.lost_reply:
            raise TimeoutError("read timed out")
        return Web3.keccak(raw)

    def get_transaction(self, tx_hash):
        if tx_hash not in self.known:
            raise LookupError("transaction not found")
        return {"hash": tx_hash}

    @staticmethod
    def decoded(raw):
        try:  # eth-account 0.13+
            from eth_account.typed_transactions import TypedTransaction
        except ImportError:  # eth-account 0.11, what web3 6.x pulls in
            from eth_account._utils.typed_transactions import TypedTransaction
        return TypedTransaction.from_bytes(raw).as_dict()

    def get_transaction_receipt(self, tx_hash):
        if self.receipt_refusals:
            self.receipt_refusals -= 1
            raise ConnectionError("403 Client Error: Forbidden")  # a public node that has had enough
        if not self.mined:
            raise TransactionNotFound("transaction not found")  # what web3 raises for a null receipt
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
    monkeypatch.setattr(sweep, "RECEIPT_TIMEOUT", 0.2)  # a never-mined transaction gives up quickly here
    monkeypatch.setattr(sweep, "RECEIPT_POLL", 0)


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


def test_mantle_bills_the_l1_fee_in_its_own_token_at_the_oracles_ratio():
    eth = FakeEth(balance=1, oracle=sweep.OP_STACK_ORACLE, l1_fee=7)
    eth.token_ratio = 5066
    assert sweep.l1_fee_upper_bound(FakeW3(eth), {"l1_fee_in_native": True}) == 7 * 5066


def test_a_ratio_on_a_chain_not_marked_for_it_is_ignored():
    eth = FakeEth(balance=1, oracle=sweep.OP_STACK_ORACLE, l1_fee=7)
    eth.token_ratio = 2 ** 256 - 1  # an oracle answering tokenRatio() with nonsense
    assert sweep.l1_fee_upper_bound(FakeW3(eth), {"id": 10}) == 7


@pytest.mark.parametrize("stamp, usable", [(0, False), (None, True)])
def test_a_zero_timestamp_is_the_oldest_block_and_a_missing_one_cannot_be_judged(monkeypatch, stamp, usable):
    eth = FakeEth(balance=1)
    monkeypatch.setattr(eth, "get_block", lambda tag: {"baseFeePerGas": GWEI, "timestamp": stamp})
    up, _ = sweep.reachable_chains(CHAIN, lambda cfg: FakeW3(eth))
    assert ("plain" in up) is usable


def test_a_receipt_refused_by_the_node_in_use_is_read_from_another_endpoint():
    first, second = FakeEth(balance=10 ** 15), FakeEth(balance=0)
    first.receipt_refusals = 10 ** 9  # banned for good
    nodes = {"https://first": FakeW3(first), "https://second": FakeW3(second)}
    chains = {"plain": {**CHAIN["plain"], "rpc": "https://first", "fallback_rpcs": ["https://second"]}}
    rows = sweep.sweep_all([{"address": WALLET, "key": KEY}], DESTINATION, live=True, chains=chains,
                           w3_for=lambda cfg: nodes[cfg["rpc"]])
    assert [r["status"] for r in rows] == ["sent"]
    assert len(first.sent) == 1 and second.sent == []


def test_an_endpoint_with_no_new_block_for_hours_is_not_used(capsys):
    stuck, fresh = FakeEth(balance=10 ** 15), FakeEth(balance=10 ** 15)
    stuck.block_age = 125 * 86400
    up, down = sweep.reachable_chains({"plain": {**CHAIN["plain"], "rpc": "https://stuck"}}, lambda cfg: FakeW3(stuck))
    assert up == {} and "looks halted" in down["plain"]
    up, down = sweep.reachable_chains({"plain": {**CHAIN["plain"], "rpc": "https://stuck", "fallback_rpcs": ["https://fresh"]}},
                                      lambda cfg: FakeW3(stuck if cfg["rpc"] == "https://stuck" else fresh))
    assert up["plain"]["rpc"] == "https://fresh" and down == {}


def test_a_chain_without_an_oracle_bills_no_l1_fee():
    assert sweep.l1_fee_upper_bound(FakeW3(FakeEth(balance=1))) == 0


def test_a_planned_token_transfer_leaves_its_fee_out_of_the_native_amount():
    eth = FakeEth(balance=10 ** 15, token_balance=5_000_000)
    rows = run(eth)
    token_fee = int(50_000 * 1.2) * int(GWEI * 1.3)
    native_fee = int(21_000 * 1.2) * int(GWEI * 1.3)
    assert [r["status"] for r in rows] == ["planned", "planned"]
    assert rows[1]["amount"] == pytest.approx((10 ** 15 - token_fee - native_fee) / ETHER)


def test_a_reply_lost_after_the_node_took_the_transaction_keeps_its_hash():
    eth = FakeEth(balance=10 ** 15)
    eth.lost_reply = True
    [row] = run(eth, live=True)
    assert row["status"] == "sent"
    assert row["tx"] in eth.known


def test_a_transaction_the_node_refused_is_an_error_with_its_hash():
    eth = FakeEth(balance=10 ** 15)

    def refuse(raw):
        raise ValueError("nonce too low")

    eth.send_raw_transaction = refuse
    [row] = run(eth, live=True)
    assert row["status"] == "error: nonce too low"
    assert row["tx"].startswith("0x") and len(row["tx"]) == 66


def test_a_token_whose_transfer_answers_false_is_not_sent():
    eth = FakeEth(balance=10 ** 15, token_balance=5_000_000)
    eth.transfer_answer = 0
    rows = run(eth, live=True)
    assert [(r["symbol"], r["status"]) for r in rows] == [("USDC", "refused"), ("ETH", "sent")]
    assert len(eth.sent) == 1


def test_a_mined_token_transfer_that_left_the_balance_is_reported_not_moved():
    eth = FakeEth(balance=10 ** 15, token_balance=5_000_000)
    eth.token_moves = False
    rows = run(eth, live=True)
    assert [(r["symbol"], r["status"]) for r in rows] == [("USDC", "not moved"), ("ETH", "sent")]


def test_the_native_transfer_after_a_token_uses_the_l1_fee_of_that_moment():
    eth = FakeEth(balance=10 ** 15, token_balance=5_000_000, oracle=sweep.OP_STACK_ORACLE, l1_fee=7 * 10 ** 9)
    eth.l1_fee_next = 5 * 10 ** 13
    rows = run(eth, live=True)
    assert [r["status"] for r in rows] == ["sent", "sent"]


def test_native_gas_is_estimated_for_a_real_amount_not_one_wei():
    eth = FakeEth(balance=10 ** 15, gas=lambda value: 21_187 if value == 1 else 43_504)
    run(eth, live=True)
    [(_, raw)] = eth.sent
    assert FakeEth.decoded(raw)["gas"] >= 43_504


def test_a_transaction_mined_while_the_wallet_is_read_leaves_no_stale_balance():
    eth = FakeEth(balance=10 ** 15)
    eth.in_flight = 1
    eth.mined_when_read = 5 * 10 ** 14
    [row] = run(eth, live=True)
    assert row["status"] == "sent"
    assert row["amount"] == pytest.approx((5 * 10 ** 14 - int(21_000 * 1.2) * int(GWEI * 1.3)) / ETHER)


def test_every_unsure_transfer_is_printed_with_its_hash(capsys):
    def row(status, tx):
        return {"chain": "plain", "wallet": WALLET, "symbol": "ETH", "amount": 0.1, "usd": 1.0, "fee_usd": 0.0,
                "status": status, "tx": tx}

    sweep.print_sweep([row("pending", "0x" + "01" * 32), row("error: lost", "0x" + "02" * 32),
                       row("not moved", "0x" + "03" * 32), row("sent", "0x" + "04" * 32)], live=True)
    out = capsys.readouterr().out
    assert "[PENDING] plain" in out and "0x" + "01" * 32 in out
    assert "[ERROR] plain" in out and "0x" + "02" * 32 in out
    assert "[NOT MOVED] plain" in out and "0x" + "03" * 32 in out
    assert "0x" + "04" * 32 not in out  # a confirmed one goes to the report file, not the screen


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


def test_a_wrapped_token_is_valued_as_what_it_wraps(monkeypatch):
    monkeypatch.setattr(sweep, "TOKENS", {"plain": [{"symbol": "WETH", "address": TOKEN, "decimals": 18, "price_of": "ETH"}]})
    rows = run(FakeEth(balance=10 ** 15, token_balance=10 ** 15))  # 0.001 WETH
    [weth] = [r for r in rows if r["symbol"] == "WETH"]
    assert weth["status"] == "planned"
    assert weth["usd"] == pytest.approx(2.0)


@pytest.mark.parametrize("base_fee, status", [(GWEI // 10, "planned"), (GWEI, "not worth it")])
def test_a_token_without_a_price_moves_only_while_its_transfer_is_cheap(monkeypatch, base_fee, status):
    # 60k gas at 1.3x base fee: $0.0156 at 0.1 gwei, $0.156 at 1 gwei, against a $0.10 ceiling.
    monkeypatch.setattr(sweep, "TOKENS", {"plain": [{"symbol": "LP", "address": TOKEN, "decimals": 18}]})
    rows = run(FakeEth(balance=10 ** 15, token_balance=10 ** 12, base_fee=base_fee))
    assert [r["status"] for r in rows if r["symbol"] == "LP"] == [status]


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


def test_a_node_refusing_receipt_reads_for_a_while_still_ends_in_sent():
    eth = FakeEth(balance=10 ** 15)
    eth.receipt_refusals = 5
    [row] = run(eth, live=True)
    assert row["status"] == "sent"


def test_no_receipt_read_starts_after_the_deadline(monkeypatch):
    class Clock:
        now = 0.0

        def monotonic(self):
            return self.now

        def sleep(self, seconds):
            self.now += seconds

    clock = Clock()
    asked_at = []

    class Node:
        def get_transaction_receipt(self, tx_hash):
            asked_at.append(clock.now)
            clock.now += 0.5  # each read takes a while
            raise LookupError("transaction not found")

    monkeypatch.setattr(sweep, "time", clock)
    monkeypatch.setattr(sweep, "RECEIPT_TIMEOUT", 10)
    monkeypatch.setattr(sweep, "RECEIPT_POLL", 4)
    assert sweep._receipt(FakeW3(Node()), "0x" + "00" * 32) is None
    assert asked_at and max(asked_at) < 10
    assert clock.now <= 10


def test_receipts_are_polled_gently():
    # web3's own wait polls every 0.1 s, which got public nodes to ban this machine.
    # The fixture speeds polling up for these tests, so check the value as shipped.
    assert SHIPPED_RECEIPT_POLL >= 1


def test_a_chain_whose_first_rpc_refuses_is_swept_through_its_fallback(capsys):
    class Broken:
        @property
        def eth(self):
            raise ConnectionError("403 Client Error: Forbidden")

    good = FakeEth(balance=10 ** 15)
    nodes = {"https://first": Broken(), "https://second": FakeW3(good)}
    chains = {"plain": {**CHAIN["plain"], "rpc": "https://first", "fallback_rpcs": ["https://second"]}}
    rows = sweep.sweep_all([{"address": WALLET, "key": KEY}], DESTINATION, live=True, chains=chains,
                           w3_for=lambda cfg: nodes[cfg["rpc"]])
    assert [r["status"] for r in rows] == ["sent"]
    assert len(good.sent) == 1
    assert "plain: using https://second" in capsys.readouterr().out


def test_a_fallback_on_another_chain_is_not_used():
    wrong = FakeEth(balance=10 ** 15)
    wrong.chain_id = 999
    right = FakeEth(balance=10 ** 15)
    nodes = {"https://first": FakeW3(wrong), "https://second": FakeW3(right)}
    up, down = sweep.reachable_chains({"plain": {**CHAIN["plain"], "rpc": "https://first", "fallback_rpcs": ["https://second"]}},
                                      lambda cfg: nodes[cfg["rpc"]])
    assert up["plain"]["rpc"] == "https://second" and down == {}


def test_when_no_chain_answers_nothing_is_swept_rather_than_everything(capsys):
    class Broken:
        @property
        def eth(self):
            raise ConnectionError("node down")

    rows = sweep.sweep_all([{"address": WALLET, "key": KEY}], DESTINATION, live=True,
                           chains={"down": {"id": 2, "rpc": "x", "native": "ETH", "decimals": 18}},
                           w3_for=lambda cfg: Broken())
    assert rows == []
    assert "No chain left to sweep" in capsys.readouterr().out


def test_no_chains_means_none_not_all():
    assert sweep.sweep_wallet(WALLET, KEY, DESTINATION, chains={}, w3_for=lambda cfg: pytest.fail("touched a chain")) == []


def test_only_the_listed_wallet_and_chain_pairs_are_touched():
    eth = FakeEth(balance=10 ** 15)
    chains = {**CHAIN, "other": {"id": 1, "rpc": "x", "native": "ETH", "decimals": 18}}
    rows = sweep.sweep_all([{"address": WALLET, "key": KEY}, {"address": OTHER, "key": OTHER_KEY}], DESTINATION,
                           live=True, chains=chains, w3_for=lambda cfg: FakeW3(eth), only={(WALLET, "other")})
    assert [(r["wallet"], r["chain"], r["status"]) for r in rows] == [(WALLET, "other", "sent")]
    assert [sender for sender, _ in eth.sent] == [WALLET]


def test_a_sent_token_keeps_its_hash_when_the_check_after_it_fails():
    eth = FakeEth(balance=10 ** 15, token_balance=5_000_000)
    original_call = eth.call

    def flaky_call(tx):
        if eth.sent and bytes(tx["data"][:4]) == sweep.BALANCE_OF_SELECTOR:
            raise TimeoutError("read timed out")
        return original_call(tx)

    eth.call = flaky_call
    rows = run(eth, live=True)
    assert [(r["symbol"], r["status"]) for r in rows] == [("USDC", "sent"), ("ETH", "sent")]
    assert rows[0]["tx"] in eth.known


def test_each_report_gets_a_new_file_and_never_overwrites_an_earlier_one(tmp_path):
    first = sweep.new_report(str(tmp_path), stamp="20261106-120000")
    sweep.write_report([{"chain": "plain", "wallet": WALLET, "symbol": "ETH", "amount": 1, "usd": 1, "fee_usd": 0,
                         "status": "sent", "tx": "0x" + "ab" * 32}], first)
    second = sweep.new_report(str(tmp_path), stamp="20261106-120000")
    assert second != first
    assert ("0x" + "ab" * 32) in open(first, encoding="utf-8").read()
    assert open(second, encoding="utf-8").read().strip() == ",".join(sweep.REPORT_FIELDS)


def test_the_report_lists_every_row_with_its_hash(tmp_path):
    rows = [{"chain": "plain", "wallet": WALLET, "symbol": "ETH", "amount": 0.1, "usd": 200.0, "fee_usd": 0.01,
             "status": "sent", "tx": "0x" + "ab" * 32}]
    path = tmp_path / "report.csv"
    sweep.write_report(rows, str(path))
    lines = path.read_text(encoding="utf-8").splitlines()
    assert lines[0] == ",".join(sweep.REPORT_FIELDS)
    assert lines[1].endswith("sent," + "0x" + "ab" * 32)


def test_many_wallets_are_planned_together():
    eth = FakeEth(balance=10 ** 15)
    rows = sweep.sweep_all([{"address": WALLET, "key": None}, {"address": OTHER, "key": None}], DESTINATION, chains=CHAIN, w3_for=lambda cfg: FakeW3(eth))
    assert sorted(r["wallet"] for r in rows) == sorted([WALLET, OTHER])
    assert all(r["status"] == "planned" for r in rows)
