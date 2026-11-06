# Security & "is this a scam?"

This tool asks for **seed phrases / private keys**. That is exactly what a
wallet drainer asks for too — so do **not** trust this README. Trust the code.
Everything below is verifiable in a couple of minutes with `grep`.

**The one-sentence claim:** your private keys and seed phrases are used only
to *sign transactions on your own machine*. They are never written to disk,
never logged, and never put into any network request.

---

## Prove it yourself (2 minutes)

### 1. Every line that touches a key

```bash
grep -rn -E "sign_transaction|from_mnemonic|key_from_seed|\.key|enable_unaudited" dust_collector/
```

You get exactly two places that *use* a key:

| Location | What it does | Local only? |
|----------|--------------|-------------|
| `wallets.py` `seed_from_mnemonic(...)` + `key_from_seed(...)` | derive address+key from your seed | yes — pure crypto |
| `core.py` `w3.eth.account.sign_transaction(tx, key)` | sign a transaction | yes — pure crypto |

The line right after signing broadcasts **only the signed bytes**, never the key:

```python
signed  = w3.eth.account.sign_transaction(tx, key)   # local
tx_hash = w3.eth.send_raw_transaction(raw_tx(signed)) # raw_tx(signed) = signed bytes only
```

### 2. Every request that leaves your machine

```bash
grep -rn -E "requests\.(get|post)|HTTPProvider" dust_collector/
```

There are only **four** outbound destinations, and **none of them receive a key**:

| Destination | Why | What it receives |
|-------------|-----|------------------|
| Your RPC nodes (`web3compat.py`) | read balances, broadcast signed txs | your **public address**, signed tx bytes |
| `api.relay.link/quote` (`bridges.py`) | get a bridge route | your **public address** + amount |
| `li.quest/v1/quote` (`bridges.py`) | bridge route for Lens | your **public address** + amount |
| `api.coingecko.com` (`prices.py`) | USD prices for "is this worth bridging?" | **nothing about you** — just token names |

Your address is already public on every block explorer, so sharing it with a
bridge API leaks nothing new. **No telemetry, no analytics, no "phone home."**

### 3. Watch the traffic live

Run the tool through a proxy and look at every byte it sends:

```bash
pip install mitmproxy
mitmproxy            # in one terminal
HTTPS_PROXY=http://127.0.0.1:8080 python collect.py estimate
```

You will see RPC calls, relay/li.quest quotes and a CoinGecko call — and your
seed phrase / private key in **none** of them.

### 4. The safest first run needs no key at all

```bash
python collect.py scan        # reads balances only — never asks for a key
```

`scan`, `audit` and `estimate` are fully read-only. A key is required *only*
for `collect`, `withdraw` and `distribute`, and even then only to sign locally.

No message the tool prints repeats a line of your `.env`. A line it can't use
is reported by its line number and word count only, because the libraries'
own errors can quote a seed phrase back.

---

## Recommendations before you trust it with real funds

1. **Read the diff, pin a commit.** Don't `git pull` blindly later — review
   changes. Pin the exact commit you audited.
2. **Use a fresh virtual environment** so you know which packages are installed:
   ```bash
   python -m venv venv && source venv/bin/activate   # Windows: venv\Scripts\activate
   pip install -r requirements.txt
   ```
3. **Test with one low-value wallet first.** Run `scan`, then `estimate`, then
   `collect` (dry run), and only then `collect --live`.
4. **Dry run is the default.** Nothing is sent without `--live`. Without `--yes`
   every transaction asks for confirmation.
5. **`.env` is git-ignored.** Your keys live only in your local `.env`. Keep it
   that way; never commit it.

---

## Reporting a vulnerability

Open an issue (no secrets, please) or contact the maintainer privately. This is
unaudited, best-effort software — see [DISCLAIMER.md](DISCLAIMER.md).
