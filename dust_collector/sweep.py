"""Sweep: move every wallet's dust to one address, on the chain where it is.

Bridging from each wallet costs a bridge fee per wallet and chain, a large share
of a $0.20 balance. A plain transfer costs a fraction of a cent on most L2s, and
what arrives can then be bridged once, from the destination.

Keys sign only two kinds of transaction here, both built in this file: a
native transfer to the destination, and an ERC-20 `transfer(destination,
balance)`. Nothing from an outside API goes into them.
"""

import concurrent.futures
import csv

from web3 import Web3

from .config import CHAINS, TOKENS, SWEEP_MAX_FEE_SHARE, SWEEP_MIN_USD
from .prices import to_usd
from .web3compat import get_w3, raw_tx

# Rollups that bill the L1 data fee on top of gas expose it through these.
OP_STACK_ORACLE = "0x420000000000000000000000000000000000000F"
SCROLL_ORACLE = "0x5300000000000000000000000000000000000002"
# A signed transfer is about 110 bytes, an ERC-20 transfer about 180.
TX_SIZE_BYTES = 200
RECEIPT_TIMEOUT = 180  # seconds to wait for a transaction to be mined

def _selector(signature):
    return bytes(Web3.keccak(text=signature)[:4])


TRANSFER_SELECTOR = _selector("transfer(address,uint256)")
BALANCE_OF_SELECTOR = _selector("balanceOf(address)")
L1_FEE_UPPER_BOUND = _selector("getL1FeeUpperBound(uint256)")
L1_FEE = _selector("getL1Fee(bytes)")


def _l1_fee_call(blob):
    """Calldata for getL1Fee(bytes blob)."""
    padded = blob.ljust((len(blob) + 31) // 32 * 32, b"\0")
    return L1_FEE + (32).to_bytes(32, "big") + len(blob).to_bytes(32, "big") + padded


def transfer_data(to, amount):
    """Calldata for ERC-20 transfer(to, amount)."""
    return TRANSFER_SELECTOR + bytes.fromhex(to[2:].lower().rjust(64, "0")) + amount.to_bytes(32, "big")


def native_send_amount(balance, gas, max_fee_per_gas, l1_fee):
    """What can leave once the worst-case fee is paid. Zero when nothing can."""
    return max(balance - gas * max_fee_per_gas - l1_fee, 0)


def worth_sending(value_usd, fee_usd):
    """Enough to bother with, and the fee doesn't eat too much of it."""
    return value_usd >= SWEEP_MIN_USD and fee_usd <= value_usd * SWEEP_MAX_FEE_SHARE


def fee_params(w3):
    """Max fee per gas, and the fields that go into the transaction.

    The ceiling is kept close to the base fee: on a native sweep, whatever the
    ceiling reserves above what is charged stays behind in the wallet.
    """
    block = w3.eth.get_block("latest")
    base = block.get("baseFeePerGas") or 0
    if base:
        try:
            tip = w3.eth.max_priority_fee
        except Exception:
            tip = 0
        max_fee = int(base * 1.3) + tip
        return max_fee, {"maxFeePerGas": max_fee, "maxPriorityFeePerGas": tip}
    price = w3.eth.gas_price
    return price, {"gasPrice": price}


def l1_fee_upper_bound(w3):
    """The L1 data fee a small transaction can be billed, or 0 where gas covers everything."""
    blob = b"\x01" * TX_SIZE_BYTES
    if w3.eth.get_code(Web3.to_checksum_address(OP_STACK_ORACLE)):
        oracle = Web3.to_checksum_address(OP_STACK_ORACLE)
        try:  # getL1FeeUpperBound(uint256), from the Fjord upgrade on
            return int.from_bytes(w3.eth.call({"to": oracle, "data": L1_FEE_UPPER_BOUND + TX_SIZE_BYTES.to_bytes(32, "big")}), "big")
        except Exception:  # older: the fee of a transaction-sized blob, doubled for margin
            return 2 * int.from_bytes(w3.eth.call({"to": oracle, "data": _l1_fee_call(blob)}), "big")
    if w3.eth.get_code(Web3.to_checksum_address(SCROLL_ORACLE)):
        return 2 * int.from_bytes(w3.eth.call({"to": Web3.to_checksum_address(SCROLL_ORACLE), "data": _l1_fee_call(blob)}), "big")
    return 0


def _token_balance(w3, token, wallet):
    data = BALANCE_OF_SELECTOR + bytes.fromhex(wallet[2:].lower().rjust(64, "0"))
    return int.from_bytes(w3.eth.call({"to": Web3.to_checksum_address(token), "data": data}), "big")


def _known(w3, tx_hash):
    try:
        return w3.eth.get_transaction(tx_hash) is not None
    except Exception:
        return False


def _send(w3, tx, key):
    """Sign locally, broadcast, wait. Returns (hash, status): sent, reverted, pending or "error: ...".

    The hash comes from the signed bytes, so it is kept even when the node's
    reply to the broadcast is lost.
    """
    signed = w3.eth.account.sign_transaction(tx, key)
    tx_hash = "0x" + bytes(signed.hash).hex()
    try:
        w3.eth.send_raw_transaction(raw_tx(signed))
    except Exception as e:
        # Refused, or taken with the reply lost: only the node can tell which.
        if not _known(w3, tx_hash):
            return tx_hash, f"error: {_short(e)}"
    try:
        receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=RECEIPT_TIMEOUT)
    except Exception:
        # Broadcast, not mined yet. The next run finds it in flight and leaves the chain alone.
        return tx_hash, "pending"
    return tx_hash, "sent" if receipt.status == 1 else "reverted"


def _short(error):
    # Errors from RPCs and web3 carry no key, but keep them short.
    return " ".join(str(error).split())[:70]


def sweep_chain(w3, chain, cfg, wallet, key, to, live, rows):
    """Plan, and if live send, one wallet's sweep on one chain. Appends one dict per balance to `rows`.

    Rows are appended as each step happens, so a transaction already sent is
    reported even when a later step on the chain fails.
    """
    native, decimals = cfg["native"], cfg["decimals"]

    def row(symbol, amount, dec, value_usd, fee_usd, status, tx=None):
        rows.append({"chain": chain, "wallet": wallet, "symbol": symbol, "amount": amount / 10 ** dec,
                     "usd": value_usd, "fee_usd": fee_usd, "status": status, "tx": tx})

    balance = w3.eth.get_balance(wallet)
    tokens = []
    for token in TOKENS.get(chain, []):
        try:
            held = _token_balance(w3, token["address"], wallet)
        except Exception:
            continue
        if held:
            tokens.append((token, held))
    if not balance and not tokens:
        return

    nonce = w3.eth.get_transaction_count(wallet, "pending")
    if nonce != w3.eth.get_transaction_count(wallet, "latest"):
        # Something sent earlier isn't mined yet, and the balance doesn't show its cost.
        row(native, balance, decimals, to_usd(native, balance / 10 ** decimals), 0, "in flight")
        return
    # Read again: a transaction mined while the nonces were read has changed it.
    balance = w3.eth.get_balance(wallet)

    max_fee, fee_fields = fee_params(w3)
    l1_fee = l1_fee_upper_bound(w3)
    sent = False

    for token, held in tokens:
        symbol, dec = token["symbol"], token["decimals"]
        contract = Web3.to_checksum_address(token["address"])
        value_usd = to_usd(symbol, held / 10 ** dec)
        data = transfer_data(to, held)
        try:
            gas = int(w3.eth.estimate_gas({"from": wallet, "to": contract, "data": data}) * 1.2)
            answer = bytes(w3.eth.call({"from": wallet, "to": contract, "data": data}))
        except Exception as e:
            row(symbol, held, dec, value_usd, 0, f"error: {_short(e)}")
            continue
        if len(answer) == 32 and not int.from_bytes(answer, "big"):
            # transfer() answers false without reverting: it would cost gas and move nothing.
            row(symbol, held, dec, value_usd, 0, "refused")
            continue
        fee = gas * max_fee + l1_fee
        fee_usd = to_usd(native, fee / 10 ** decimals)
        if fee > balance:
            row(symbol, held, dec, value_usd, fee_usd, "no gas")
            continue
        if not worth_sending(value_usd, fee_usd):
            row(symbol, held, dec, value_usd, fee_usd, "not worth it")
            continue
        if not live:
            row(symbol, held, dec, value_usd, fee_usd, "planned")
            balance -= fee
            continue
        tx = {"from": wallet, "to": contract, "value": 0, "data": data,
              "gas": gas, "nonce": nonce, "chainId": cfg["id"], **fee_fields}
        tx_hash, status = _send(w3, tx, key)
        if status == "sent" and _token_balance(w3, token["address"], wallet) >= held:
            status = "not moved"  # mined, yet the balance is still there
        row(symbol, held, dec, value_usd, fee_usd, status, tx_hash)
        if status not in ("sent", "reverted", "not moved"):
            return  # pending or unknown: the native balance waits until it settles
        nonce += 1
        sent = True
        balance = w3.eth.get_balance(wallet)

    if sent:
        # Fees move while a transfer is mined: the last one is built on fresh ones.
        max_fee, fee_fields = fee_params(w3)
        l1_fee = l1_fee_upper_bound(w3)

    if balance:
        # Estimated for half the balance rather than 1 wei: code at the destination
        # (an EIP-7702 account, say) can take another path for a real amount.
        gas = int(w3.eth.estimate_gas({"from": wallet, "to": to, "value": max(balance // 2, 1)}) * 1.2)
        amount = native_send_amount(balance, gas, max_fee, l1_fee)
        value_usd = to_usd(native, amount / 10 ** decimals)
        fee_usd = to_usd(native, (balance - amount) / 10 ** decimals)
        if not amount or not worth_sending(value_usd, fee_usd):
            row(native, balance, decimals, to_usd(native, balance / 10 ** decimals), fee_usd, "not worth it")
        elif not live:
            row(native, amount, decimals, value_usd, fee_usd, "planned")
        else:
            tx = {"from": wallet, "to": to, "value": amount, "data": b"", "gas": gas, "nonce": nonce,
                  "chainId": cfg["id"], **fee_fields}
            tx_hash, status = _send(w3, tx, key)
            row(native, amount, decimals, value_usd, fee_usd, status, tx_hash)


def sweep_wallet(wallet, key, to, live=False, chains=None, w3_for=None, only=None):
    """The given chains (all configured ones if None) for one wallet. Errors stay on their chain.

    `only`, if given, is the set of (wallet, chain) pairs that may be touched,
    so a live run acts on nothing its plan didn't list.
    """
    wallet = Web3.to_checksum_address(wallet)
    to = Web3.to_checksum_address(to)
    w3_for = w3_for or (lambda cfg: get_w3(cfg["rpc"], timeout=20))
    rows = []
    for chain, cfg in (CHAINS if chains is None else chains).items():
        if only is not None and (wallet, chain) not in only:
            continue
        try:
            sweep_chain(w3_for(cfg), chain, cfg, wallet, key, to, live, rows)
        except Exception as e:
            rows.append({"chain": chain, "wallet": wallet, "symbol": cfg["native"], "amount": 0, "usd": 0,
                         "fee_usd": 0, "status": f"error: {_short(e)}", "tx": None})
    return rows


def reachable_chains(chains, w3_for):
    """Split chains into those whose node answers with the configured chain id, and the rest with why."""
    def probe(item):
        name, cfg = item
        try:
            got = w3_for(cfg).eth.chain_id
        except Exception as e:
            return name, f"node unreachable ({_short(e)[:40]})"
        return name, None if got == cfg["id"] else f"node answers for chain {got}, not {cfg['id']}"

    with concurrent.futures.ThreadPoolExecutor(max_workers=16) as pool:
        verdicts = dict(pool.map(probe, chains.items()))
    up = {name: cfg for name, cfg in chains.items() if verdicts[name] is None}
    return up, {name: why for name, why in verdicts.items() if why}


def sweep_all(wallets, to, live=False, workers=8, chains=None, w3_for=None, only=None):
    """Sweep many wallets in parallel. Each wallet's own transactions stay in order.

    `chains` defaults to every configured chain; `only` limits the run to
    (wallet, chain) pairs, as sweep_wallet describes.
    """
    to = Web3.to_checksum_address(to)
    sources = {Web3.to_checksum_address(w["address"]) for w in wallets}
    if to in sources:
        raise ValueError("the destination is one of the wallets being swept")
    if live and any(not w.get("key") for w in wallets):
        missing = sum(1 for w in wallets if not w.get("key"))
        raise ValueError(f"{missing} wallet(s) have no key: a live sweep needs every key")
    w3_for = w3_for or (lambda cfg: get_w3(cfg["rpc"], timeout=20))
    chains, down = reachable_chains(CHAINS if chains is None else chains, w3_for)
    for name, why in sorted(down.items()):
        print(f"  [!] {name}: {why} - skipped")
    if not chains:
        print("  [!] No chain left to sweep.")
        return []
    rows = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(sweep_wallet, w["address"], w.get("key"), to, live, chains, w3_for, only) for w in wallets]
        for done, future in enumerate(concurrent.futures.as_completed(futures), 1):
            rows += future.result()
            if done % 10 == 0:
                print(f"  ...{done}/{len(wallets)} wallets", flush=True)
    return rows


def print_sweep(rows, live):
    """A summary per chain, then what didn't go and why."""
    moved = [r for r in rows if r["status"] in ("planned", "sent")]
    print(f"\n{'=' * 72}\n  SWEEP {'SENT' if live else 'PLAN (dry run, nothing sent)'}\n{'=' * 72}")
    by_chain = {}
    for r in moved:
        entry = by_chain.setdefault((r["chain"], r["symbol"]), {"usd": 0.0, "fee": 0.0, "count": 0})
        entry["usd"] += r["usd"]
        entry["fee"] += r["fee_usd"]
        entry["count"] += 1
    for (chain, symbol), e in sorted(by_chain.items(), key=lambda item: -item[1]["usd"]):
        print(f"  {chain:<12} {symbol:<7} {e['count']:>4} transfers  ${e['usd']:>8.2f}  gas ${e['fee']:.3f}")
    total = sum(e["usd"] for e in by_chain.values())
    fees = sum(e["fee"] for e in by_chain.values())
    print(f"  {'TOTAL':<20} {sum(e['count'] for e in by_chain.values()):>4} transfers  ${total:>8.2f}  gas ${fees:.3f}")
    skipped = {}
    for r in rows:
        if r["status"] not in ("planned", "sent", "pending"):
            status, _, detail = r["status"].partition(": ")
            entry = skipped.setdefault((r["chain"], r["symbol"], status), {"usd": 0.0, "count": 0, "detail": detail})
            entry["usd"] += r["usd"]
            entry["count"] += 1
    if skipped:
        print("\n  Left behind:")
        for (chain, symbol, status), e in sorted(skipped.items(), key=lambda item: -item[1]["usd"]):
            detail = f"  ({e['detail'][:50]})" if e["detail"] else ""
            print(f"  {chain:<12} {symbol:<7} {status:<13} {e['count']:>4}  ${e['usd']:.2f}{detail}")
    for r in rows:
        if r["tx"] and r["status"] != "sent":
            # Broadcast, or maybe broadcast: worth a look on an explorer before a re-run.
            label = r["status"].split(":")[0].upper()
            print(f"  [{label}] {r['chain']} {r['wallet'][:10]}... {r['symbol']}: {r['tx']}")
    print("=" * 72)
    return total


REPORT_FIELDS = ["chain", "wallet", "symbol", "amount", "usd", "fee_usd", "status", "tx"]


def write_report(rows, path):
    """Every row with its transaction hash, as CSV: what went where, to check on an explorer.

    Holds addresses, amounts and hashes only, all of them public on-chain.
    """
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=REPORT_FIELDS)
        writer.writeheader()
        for r in sorted(rows, key=lambda r: (r["chain"], r["wallet"], r["symbol"])):
            writer.writerow({field: r.get(field) for field in REPORT_FIELDS})
