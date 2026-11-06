"""`sweep` on the command line: always plans first, and sends only on --live and a typed yes."""

import pytest

from dust_collector import cli

WALLET = "0x9858EfFD232B4033E47d90003D41EC34EcaEda94"  # account 0 of the public BIP-39 test phrase
DESTINATION = "0x" + "ab" * 20


@pytest.fixture
def calls(tmp_path, monkeypatch):
    """Records each sweep_all call's `live` flag; nothing touches a network."""
    made = []
    monkeypatch.setattr(cli, "update_prices", lambda: None)
    monkeypatch.setattr(cli.sweep, "sweep_all", lambda wallets, to, live=False, chains=None: made.append((live, chains and sorted(chains))) or [])
    monkeypatch.setattr(cli.sweep, "print_sweep", lambda rows, live: 0)
    monkeypatch.delenv("MAIN_WALLET", raising=False)
    env = tmp_path / ".env"
    env.write_text(f"{WALLET}=\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    return made


def answer(monkeypatch, reply):
    monkeypatch.setattr("builtins.input", lambda prompt="": reply)


def test_without_live_it_only_plans(calls):
    assert cli.main(["--to", DESTINATION, "sweep"]) == 0
    assert [live for live, _ in calls] == [False]


def test_live_sends_nothing_unless_yes_is_typed(calls, monkeypatch):
    answer(monkeypatch, "y")
    assert cli.main(["--to", DESTINATION, "--live", "sweep"]) == 1
    assert [live for live, _ in calls] == [False]


def test_live_sends_after_the_plan_and_a_typed_yes(calls, monkeypatch):
    answer(monkeypatch, "yes")
    assert cli.main(["--to", DESTINATION, "--live", "sweep"]) == 0
    assert [live for live, _ in calls] == [False, True]


def test_yes_flag_skips_the_question_but_not_the_plan(calls, monkeypatch):
    monkeypatch.setattr("builtins.input", lambda prompt="": pytest.fail("asked although --yes was given"))
    assert cli.main(["--to", DESTINATION, "--live", "--yes", "sweep"]) == 0
    assert [live for live, _ in calls] == [False, True]


def test_chains_narrow_the_sweep_and_an_unknown_one_stops_it(calls):
    assert cli.main(["--to", DESTINATION, "--chains", "zksync, arbitrum", "sweep"]) == 0
    assert calls == [(False, ["arbitrum", "zksync"])]
    assert cli.main(["--to", DESTINATION, "--chains", "arbitrum,nochain", "sweep"]) == 1
    assert len(calls) == 1


def test_no_destination_no_sweep(calls):
    assert cli.main(["sweep"]) == 1
    assert calls == []
