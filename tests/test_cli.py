"""`sweep` on the command line: always plans first, then sends only what the plan listed.

The live run needs --live and a typed yes (or --yes), covers only the planned
wallet/chain pairs, leaves out wallets without a key, and writes every hash
to a CSV report.
"""

import csv

import pytest
from web3 import Web3

from dust_collector import cli

TEST_PHRASE = "abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon about"
WALLET = "0x9858EfFD232B4033E47d90003D41EC34EcaEda94"  # account 0 of the public test phrase: has a key
KEYLESS = Web3.to_checksum_address("0x" + "12" * 20)   # listed without a key
DESTINATION = "0x" + "ab" * 20


def planned(wallet, chain):
    return {"chain": chain, "wallet": wallet, "symbol": "ETH", "amount": 0.001, "usd": 2.0, "fee_usd": 0.001,
            "status": "planned", "tx": None}


class Sweeps:
    """Stands in for sweep_all: records each call and answers with `plan`, or 'sent' for a live run."""

    def __init__(self):
        self.calls = []
        self.plan = [planned(WALLET, "arbitrum"), planned(KEYLESS, "zksync"),
                     {**planned(WALLET, "base"), "status": "not worth it"}]

    def __call__(self, wallets, to, live=False, chains=None, only=None):
        self.calls.append({"live": live, "wallets": sorted(w["address"] for w in wallets),
                           "chains": sorted(chains) if chains is not None else None, "only": only})
        if not live:
            return list(self.plan)
        return [{**planned(a, c), "status": "sent", "tx": "0x" + "cd" * 32} for a, c in sorted(only)]


@pytest.fixture
def sweeps(tmp_path, monkeypatch):
    fake = Sweeps()
    monkeypatch.setattr(cli, "update_prices", lambda: None)
    monkeypatch.setattr(cli.sweep, "sweep_all", fake)
    monkeypatch.setattr(cli.sweep, "print_sweep", lambda rows, live: 0)
    monkeypatch.delenv("MAIN_WALLET", raising=False)
    (tmp_path / ".env").write_text(f"{TEST_PHRASE}\n{KEYLESS}=\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    return fake


def answer(monkeypatch, reply):
    monkeypatch.setattr("builtins.input", lambda prompt="": reply)


def test_without_live_it_only_plans(sweeps):
    assert cli.main(["--to", DESTINATION, "sweep"]) == 0
    assert [c["live"] for c in sweeps.calls] == [False]


def test_live_sends_nothing_unless_yes_is_typed(sweeps, monkeypatch):
    answer(monkeypatch, "y")
    assert cli.main(["--to", DESTINATION, "--live", "sweep"]) == 1
    assert [c["live"] for c in sweeps.calls] == [False]


def test_live_sends_only_the_planned_pairs_from_wallets_with_a_key(sweeps, monkeypatch, capsys, tmp_path):
    answer(monkeypatch, "yes")
    assert cli.main(["--to", DESTINATION, "--live", "sweep"]) == 0
    plan, live = sweeps.calls
    assert plan["live"] is False and live["live"] is True
    assert live["wallets"] == [WALLET]
    assert live["chains"] == ["arbitrum"]
    assert live["only"] == {(WALLET, "arbitrum")}
    assert f"no key and stay out: {KEYLESS[:10]}..." in capsys.readouterr().out
    [report] = list(tmp_path.glob("sweep-*.csv"))
    rows = list(csv.DictReader(report.open(encoding="utf-8")))
    assert [(r["wallet"], r["chain"], r["status"], r["tx"]) for r in rows] == [(WALLET, "arbitrum", "sent", "0x" + "cd" * 32)]


def test_nothing_planned_nothing_asked_nothing_sent(sweeps, monkeypatch):
    sweeps.plan = [{**planned(WALLET, "base"), "status": "not worth it"}]
    monkeypatch.setattr("builtins.input", lambda prompt="": pytest.fail("asked with nothing to send"))
    assert cli.main(["--to", DESTINATION, "--live", "sweep"]) == 0
    assert [c["live"] for c in sweeps.calls] == [False]


def test_yes_flag_skips_the_question_but_not_the_plan(sweeps, monkeypatch):
    monkeypatch.setattr("builtins.input", lambda prompt="": pytest.fail("asked although --yes was given"))
    assert cli.main(["--to", DESTINATION, "--live", "--yes", "sweep"]) == 0
    assert [c["live"] for c in sweeps.calls] == [False, True]


def test_chains_narrow_the_sweep_and_an_unknown_one_stops_it(sweeps):
    assert cli.main(["--to", DESTINATION, "--chains", "zksync, arbitrum", "sweep"]) == 0
    assert sweeps.calls[0]["chains"] == ["arbitrum", "zksync"]
    assert cli.main(["--to", DESTINATION, "--chains", "arbitrum,nochain", "sweep"]) == 1
    assert len(sweeps.calls) == 1


def test_no_destination_no_sweep(sweeps):
    assert cli.main(["sweep"]) == 1
    assert sweeps.calls == []
