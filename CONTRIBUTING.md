# Contributing

Thanks for helping out! This tool handles private keys and moves real money,
so the bar for changes is "obviously safe and easy to audit."

## Ground rules

1. **Keys stay local.** Nothing may put a private key or seed phrase into a
   file, a log, or a network request. The only places a key is used are
   `wallets.py` (`seed_from_mnemonic` / `key_from_seed`) and `core.py`
   (`sign_transaction`). Keep it that way, and never let a message quote a
   `.env` line: errors from the key libraries can contain the phrase.
2. **No new network egress without documenting it.** The tool talks only to
   the RPCs in `config.py`, Relay, LiFi and CoinGecko. If you add an endpoint,
   update `SECURITY.md` and the allow-list in `scripts/security_audit.py` in
   the same PR — otherwise CI fails.
3. **Never commit `.env`** or any real key. It's git-ignored; keep it so.
4. **Money logic is faithful by default.** Changes to amounts, gas reserves,
   `SEND_PERCENT`, nonce handling, or bridge parsing need a clear rationale.
5. **Printed output stays ASCII** (Windows consoles choke on `—`, `✓`, etc.).

## Before you open a PR

```bash
python scripts/security_audit.py        # must pass
python -m pytest                        # must pass
python -m py_compile dust_collector/*.py collect.py
python collect.py scan --wallet 0x...   # read-only smoke test
```

CI runs the same audit plus an import/compile check on both web3 6.x and 7.x.

## Adding a chain

Add one entry to `CHAINS` in `dust_collector/config.py` (id, rpc, native
symbol, decimals, and a `bridge` adapter). If it needs a price, add the symbol
to `PRICES` (and `COINGECKO_IDS` if it should refresh live) in `prices.py`.

## Adding a bridge

Implement an adapter in `dust_collector/bridges.py` that returns the
normalised quote dict documented at the top of that file, register it in
`_ADAPTERS`, and point chains at it via their `bridge` field.

## Style

Plain, readable Python. Match the surrounding code. No new dependencies unless
genuinely necessary (and explain why in the PR).
