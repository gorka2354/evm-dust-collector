"""The .env loader: the right key for each address, and no secret ever in a message.

The phrases here are public BIP-39 test vectors. No real wallet uses them.
"""

import pytest
from eth_account import Account

from dust_collector import wallets
from dust_collector.wallets import WalletLineError, derive_from_seed, load_wallets, parse_line

TEST_PHRASE = "abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon about"
OTHER_PHRASES = [
    "legal winner thank year wave sausage worth useful legal winner thank yellow",
    "letter advice cage absurd amount doctor acoustic avoid letter advice cage above",
    "zoo zoo zoo zoo zoo zoo zoo zoo zoo zoo zoo wrong",
]
FIRST = "0x9858EfFD232B4033E47d90003D41EC34EcaEda94"  # account 0 of the test phrase, a published vector
STRANGER = "0x" + "12" * 20
BAD_PHRASE = " ".join(["abandon"] * 11 + ["wrongword"])


def load(tmp_path, *lines):
    env = tmp_path / ".env"
    env.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return load_wallets(str(env))


def owns(wallet):
    return wallet["key"] is not None and Account.from_key(wallet["key"]).address.lower() == wallet["address"].lower()


def test_a_phrase_alone_is_its_first_account():
    w = parse_line(TEST_PHRASE)
    assert w["address"] == FIRST
    assert owns(w)


def test_an_address_with_its_phrase_finds_the_account_that_made_it():
    address, key = derive_from_seed(TEST_PHRASE, 7)
    w = parse_line(f"{address} {TEST_PHRASE}")
    assert w == {"address": address, "key": key}


def test_many_wallets_from_one_phrase_each_get_their_own_key():
    lines = [f"{derive_from_seed(TEST_PHRASE, i)[0]} {TEST_PHRASE}" for i in (0, 3, 42)]
    keys = [parse_line(line)["key"] for line in lines]
    assert len(set(keys)) == 3


def test_a_phrase_that_does_not_make_the_address_is_refused_not_guessed(monkeypatch):
    monkeypatch.setattr(wallets, "MAX_ACCOUNT_INDEX", 5)
    with pytest.raises(WalletLineError) as error:
        parse_line(f"{STRANGER} {TEST_PHRASE}")
    assert "abandon" not in str(error.value)


def test_a_misspelled_phrase_is_refused_without_repeating_it():
    with pytest.raises(WalletLineError) as error:
        parse_line(f"{FIRST} {BAD_PHRASE}")
    assert "abandon" not in str(error.value)
    assert "wrongword" not in str(error.value)


def test_a_private_key_must_belong_to_its_address():
    _, key = derive_from_seed(TEST_PHRASE, 1)
    with pytest.raises(WalletLineError) as error:
        parse_line(f"{FIRST}={key}")
    assert key[2:] not in str(error.value)
    _, own = derive_from_seed(TEST_PHRASE, 0)
    assert parse_line(f"{FIRST}={own[2:]}")["key"] == own


def test_a_private_key_alone_is_its_own_wallet():
    address, key = derive_from_seed(TEST_PHRASE, 2)
    assert parse_line(key) == {"address": address, "key": key}
    assert parse_line(key[2:]) == {"address": address, "key": key}


def test_an_address_alone_is_read_only():
    assert parse_line(f"{FIRST}=") == {"address": FIRST, "key": None}


def test_comments_and_blank_lines_are_skipped():
    assert parse_line("# a note") is None
    assert parse_line("   ") is None


def test_a_line_in_no_known_format_is_refused_by_its_word_count_only():
    numbered = "1. " + TEST_PHRASE
    with pytest.raises(WalletLineError) as error:
        parse_line(numbered)
    assert "13 word(s)" in str(error.value)
    assert "abandon" not in str(error.value)


def test_the_loader_reports_a_bad_line_by_number_and_never_prints_it(tmp_path, capsys):
    loaded = load(tmp_path, f"{FIRST}=", BAD_PHRASE, TEST_PHRASE)
    out = capsys.readouterr().out
    assert [w["address"] for w in loaded] == [FIRST]
    assert owns(loaded[0])
    assert ".env line 2" in out
    assert "abandon" not in out
    assert "wrongword" not in out


def test_listed_addresses_take_their_keys_from_one_phrase_pasted_below_them(tmp_path):
    addresses = [derive_from_seed(TEST_PHRASE, i)[0] for i in (3, 42, 7)]
    loaded = load(tmp_path, *[f"{a}=" for a in addresses], TEST_PHRASE)
    # Only the listed wallets: the phrase's own first account is not one of them.
    assert [w["address"] for w in loaded] == addresses
    assert all(owns(w) for w in loaded)


def test_each_listed_address_finds_its_own_phrase_among_many(tmp_path):
    addresses = [derive_from_seed(p, 0)[0] for p in OTHER_PHRASES]
    loaded = load(tmp_path, *[f"{a}=" for a in reversed(addresses)], *OTHER_PHRASES)
    assert [w["address"] for w in loaded] == list(reversed(addresses))
    assert all(owns(w) for w in loaded)


def test_one_phrase_is_searched_deep_enough_for_a_long_list(tmp_path):
    deep = derive_from_seed(TEST_PHRASE, 150)[0]
    [w] = load(tmp_path, f"{deep}=", TEST_PHRASE)
    assert w["address"] == deep and owns(w)


def test_a_listed_address_takes_the_key_of_a_private_key_line(tmp_path):
    address, key = derive_from_seed(TEST_PHRASE, 4)
    assert load(tmp_path, f"{address}=", key) == [{"address": address, "key": key}]


def test_an_address_no_line_makes_stays_read_only_and_unused_phrases_stay_wallets(tmp_path):
    loaded = load(tmp_path, f"{STRANGER}=", TEST_PHRASE)
    assert [w["address"] for w in loaded] == [STRANGER, FIRST]
    assert loaded[0]["key"] is None
    assert owns(loaded[1])


def test_the_same_wallet_twice_is_one_wallet_with_its_key(tmp_path):
    _, key = derive_from_seed(TEST_PHRASE, 0)
    loaded = load(tmp_path, f"{FIRST}=", f"{FIRST.lower()}={key}", f"{FIRST} {TEST_PHRASE}")
    assert loaded == [{"address": FIRST, "key": key}]


def test_a_file_saved_with_a_byte_order_mark_reads_the_same(tmp_path):
    env = tmp_path / ".env"
    env.write_text(f"{FIRST}=\n", encoding="utf-8-sig")
    assert load_wallets(str(env)) == [{"address": FIRST, "key": None}]
