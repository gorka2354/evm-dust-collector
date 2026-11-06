"""Scan, estimate, collect, withdraw and distribute — the moving parts.

Wallet addresses are passed in explicitly (no module-level globals, no
hard-coded personal addresses). Private keys are used only to sign locally.
"""

import time
import concurrent.futures

from web3 import Web3

from . import bridges
from .config import (CHAINS, TOKENS, ERC20_ABI, MIN_PROFIT_USD, SEND_PERCENT,
                     chain_name_for_id)
from .prices import to_usd, price_of, token_usd
from .web3compat import get_w3, raw_tx


# ─── Helpers ──────────────────────────────────────────────────────

def fmt(amount, decimals=18):
    return amount / (10 ** decimals)


def get_gas_params(w3):
    """EIP-1559 params when the chain supports it, else legacy gasPrice."""
    try:
        latest = w3.eth.get_block("latest")
        base_fee = latest.get("baseFeePerGas", 0)
        if base_fee and base_fee > 0:
            try:
                max_priority = w3.eth.max_priority_fee
            except Exception:
                max_priority = base_fee // 5
            return {"maxFeePerGas": int(base_fee * 2.5) + max_priority,
                    "maxPriorityFeePerGas": max_priority}
    except Exception:
        pass
    return {"gasPrice": w3.eth.gas_price}


def build_tx(w3, from_addr, to_addr, value, data, nonce, chain_id, gas=300000):
    tx = {
        "from": from_addr,
        "to": Web3.to_checksum_address(to_addr),
        "value": value,
        "data": data,
        "gas": gas,
        "nonce": nonce,
        "chainId": chain_id,
    }
    tx.update(get_gas_params(w3))
    return tx


def _send(w3, tx, key, wait_timeout=180):
    """Sign locally and broadcast. Returns the receipt."""
    signed = w3.eth.account.sign_transaction(tx, key)
    tx_hash = w3.eth.send_raw_transaction(raw_tx(signed))
    print(f"    TX: {tx_hash.hex()}")
    return w3.eth.wait_for_transaction_receipt(tx_hash, timeout=wait_timeout)


# ─── Scanner (one wallet, full: native + ERC20) ───────────────────

def scan_wallet(wallet, chains=None):
    """Scan native + token balances across all chains for one wallet."""
    wallet = Web3.to_checksum_address(wallet)
    chains = chains or CHAINS
    results = []

    for chain_name, cfg in chains.items():
        try:
            w3 = get_w3(cfg["rpc"])
            native_bal = w3.eth.get_balance(wallet)
            if native_bal > 0:
                human = fmt(native_bal, cfg["decimals"])
                results.append({
                    "chain": chain_name, "chain_id": cfg["id"],
                    "symbol": cfg["native"], "address": None,
                    "balance_raw": native_bal, "balance": human,
                    "decimals": cfg["decimals"], "usd": to_usd(cfg["native"], human),
                    "type": "native",
                })
        except Exception as e:
            print(f"  [!] {chain_name}: RPC error ({str(e)[:40]})")
            continue

        for token in TOKENS.get(chain_name, []):
            try:
                contract = w3.eth.contract(
                    address=Web3.to_checksum_address(token["address"]), abi=ERC20_ABI)
                bal = contract.functions.balanceOf(wallet).call()
                if bal > 0:
                    human = fmt(bal, token["decimals"])
                    results.append({
                        "chain": chain_name, "chain_id": cfg["id"],
                        "symbol": token["symbol"], "address": token["address"],
                        "balance_raw": bal, "balance": human,
                        "decimals": token["decimals"], "usd": token_usd(token, human),
                        "type": "erc20",
                    })
            except Exception:
                pass

    return results


def print_balances(results):
    results_sorted = sorted(results, key=lambda x: x["usd"], reverse=True)
    total = sum(r["usd"] for r in results_sorted)

    print("\n" + "=" * 65)
    print(f"  {'Chain':<14} {'Asset':<8} {'Balance':>14} {'USD':>10}")
    print("-" * 65)
    for r in results_sorted:
        if r["usd"] < 0.01:
            continue
        print(f"  {r['chain']:<14} {r['symbol']:<8} {r['balance']:>14.6f} {r['usd']:>9.2f}$")
    print("-" * 65)
    print(f"  {'TOTAL':<14} {'':8} {'':>14} {total:>9.2f}$")
    print("=" * 65)
    return results_sorted


# ─── Fast audit (many addresses, native only, parallel) ───────────

def _audit_one(w3_rpc, native, addr):
    try:
        w3 = get_w3(w3_rpc, timeout=8)
        bal = w3.eth.get_balance(Web3.to_checksum_address(addr))
        if bal > 0:
            human = fmt(bal)
            usd = to_usd(native, human)
            if usd >= 0.01:
                return (native, human, usd)
    except Exception:
        pass
    return None


def fast_audit(addresses, chains=None, max_workers=20):
    """Concurrent native-balance sweep across all chains for many wallets."""
    chains = chains or CHAINS
    tasks = []
    for chain_name, cfg in chains.items():
        for addr in addresses:
            tasks.append((chain_name, cfg["rpc"], cfg["native"], addr))

    print(f"FAST AUDIT: {len(addresses)} wallets x {len(chains)} chains "
          f"= {len(tasks)} checks", flush=True)
    print("=" * 60, flush=True)

    results = {}
    done = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(_audit_one, rpc, native, addr): (chain_name, addr)
                   for chain_name, rpc, native, addr in tasks}
        for fut in concurrent.futures.as_completed(futures):
            done += 1
            if done % 500 == 0:
                print(f"  ...{done}/{len(tasks)} done", flush=True)
            r = fut.result()
            if r:
                chain_name, addr = futures[fut]
                native, human, usd = r
                results.setdefault(chain_name, []).append((addr, native, human, usd))

    grand_total = 0
    print(f"\n{'='*60}")
    for chain_name in sorted(results, key=lambda c: -sum(x[3] for x in results[c])):
        entries = results[chain_name]
        chain_total = sum(e[3] for e in entries)
        if chain_total < 0.05:
            continue
        grand_total += chain_total
        native = entries[0][1]
        print(f"\n  {chain_name} ({native}): ${chain_total:.2f}  [{len(entries)} wallets]")
        for addr, nat, human, usd in sorted(entries, key=lambda x: -x[3])[:5]:
            print(f"    {addr[:8]}...{addr[-4:]}: {human:.6f} {nat} (${usd:.2f})")
        if len(entries) > 5:
            print(f"    ... and {len(entries)-5} more")

    print(f"\n{'='*60}")
    print(f"  GRAND TOTAL: ${grand_total:.2f}")
    print(f"  Wallets: {len(addresses)}   Chains with balance: {len(results)}")
    print("=" * 60)
    return results


# ─── Estimator ────────────────────────────────────────────────────

def estimate(results, target_id):
    """Estimate gas + bridge cost per balance; return the profitable ones."""
    print("\n[*] Estimating gas costs...")
    estimates = []

    for r in results:
        if r["usd"] < 0.10:
            continue
        cfg = CHAINS[r["chain"]]

        if cfg["id"] == target_id and r["type"] == "native":
            estimates.append({**r, "gas_usd": 0, "action": "already on target",
                              "profitable": True})
            continue

        try:
            w3 = get_w3(cfg["rpc"])
            gas_price = w3.eth.gas_price
            gas_limit = 21000 if r["type"] == "native" else 65000
            gas_cost_usd = to_usd(cfg["native"], fmt(gas_price * gas_limit))
            bridge_fee_usd = 0.30
            total_cost = gas_cost_usd + bridge_fee_usd
            profit = r["usd"] - total_cost
            estimates.append({
                **r, "gas_usd": gas_cost_usd, "bridge_fee_usd": bridge_fee_usd,
                "total_cost": total_cost, "profit": profit,
                "profitable": profit > MIN_PROFIT_USD,
                "action": "bridge native" if r["type"] == "native" else "swap+bridge",
            })
        except Exception as e:
            print(f"  [!] {r['chain']}: gas estimation failed ({str(e)[:40]})")

    print(f"\n{'Chain':<14} {'Asset':<8} {'Value':>8} {'Gas':>8} {'Bridge':>8} {'Profit':>8} Action")
    print("-" * 80)
    total_profit = 0
    for e in sorted(estimates, key=lambda x: x.get("profit", 0), reverse=True):
        mark = "OK" if e.get("profitable") else "SKIP"
        profit = e.get("profit", e["usd"])
        print(f"  {e['chain']:<12} {e['symbol']:<8} {e['usd']:>7.2f}$ "
              f"{e.get('gas_usd', 0):>7.2f}$ {e.get('bridge_fee_usd', 0):>7.2f}$ "
              f"{profit:>+7.2f}$ [{mark}] {e.get('action', '')}")
        if e.get("profitable"):
            total_profit += profit
    print("-" * 80)
    print(f"  Estimated total profit: {total_profit:>+.2f}$")
    print(f"  Target chain: {chain_name_for_id(target_id)}\n")

    return [e for e in estimates if e.get("profitable")]


# ─── Collect (bridge everything to the target chain) ──────────────

def collect(wallet, key, profitable, target_id, live=False, auto_confirm=False):
    """Bridge each profitable balance to the target chain. DRY RUN by default."""
    wallet = Web3.to_checksum_address(wallet)
    print(f"\n{'='*60}")
    print(f"  EXECUTION MODE: {'LIVE' if live else 'DRY RUN'}")
    print(f"  Target chain: {chain_name_for_id(target_id)} (chain_id={target_id})")
    print(f"{'='*60}")

    if live and not key:
        print("\n[!] ERROR: No private key for this wallet. Add it to .env.")
        return

    nonce_cache = {}

    for est in profitable:
        chain = est["chain"]
        cfg = CHAINS[chain]

        if cfg["id"] == target_id and est["type"] == "native":
            print(f"\n[=] {chain}/{est['symbol']}: already on target, skipping")
            continue

        is_erc20 = est["type"] == "erc20"
        print(f"\n[>] {chain}/{est['symbol']}: {est['balance']:.6f} (~${est['usd']:.2f})"
              + (" [ERC20]" if is_erc20 else ""))

        w3 = get_w3(cfg["rpc"])
        nonce = nonce_cache.get(chain, w3.eth.get_transaction_count(wallet))

        if is_erc20:
            send_amount = est["balance_raw"]
            send_human = est["balance"]
            origin_token = est["address"]
            print(f"    Token: {origin_token}")
            print(f"    Send:  {send_human:.6f} {est['symbol']} (~${est['usd']:.2f})")
        else:
            balance = w3.eth.get_balance(wallet)
            origin_token = None
            send_amount = int(balance * SEND_PERCENT)
            if send_amount <= 0:
                print("    [!] Balance too low. Skipping.")
                continue
            send_human = fmt(send_amount)
            print(f"    Balance: {fmt(balance):.6f} {cfg['native']}")
            print(f"    Send:    {send_human:.6f} ({SEND_PERCENT*100:.0f}%)")

        print("    Fetching bridge quote...")
        q = bridges.get_quote(cfg, target_id, send_amount, wallet, token=origin_token)
        if "error" in q:
            print(f"    [!] Bridge quote failed: {q['error']}")
            print(f"    [i] Bridge manually via https://relay.link/bridge")
            continue

        recv = q.get("receive")
        recv_str = f"~{recv:.6f}" if isinstance(recv, (int, float)) else "?"
        print(f"    Bridge to:   {q['to'][:10]}...{q['to'][-6:]}")
        print(f"    You receive: {recv_str} {q['receive_sym']} on {chain_name_for_id(q['dest_chain_id'])}")
        if q.get("note"):
            print(f"    Note: {q['note']}")

        if not live:
            print("    [DRY RUN] Not sent. Use --live to execute.")
            continue

        if not auto_confirm:
            if input(f"    Confirm bridge {send_human:.6f} {est['symbol']} from {chain}? [y/N]: ").lower() != "y":
                print("    Skipped by user.")
                continue
        else:
            print(f"    Auto-confirmed: bridge {send_human:.6f} {est['symbol']} from {chain}")

        try:
            # ERC20: approve the bridge spender first if needed.
            if is_erc20 and q.get("approve_to"):
                token_contract = w3.eth.contract(
                    address=Web3.to_checksum_address(origin_token), abi=ERC20_ABI)
                allowance = token_contract.functions.allowance(
                    wallet, Web3.to_checksum_address(q["approve_to"])).call()
                if allowance < send_amount:
                    print(f"    Approving {est['symbol']}...")
                    approve_tx = token_contract.functions.approve(
                        Web3.to_checksum_address(q["approve_to"]), send_amount
                    ).build_transaction({
                        "from": wallet, "gas": 80000, "nonce": nonce,
                        "chainId": cfg["id"], **get_gas_params(w3),
                    })
                    _send(w3, approve_tx, key, wait_timeout=90)
                    print("    [OK] Approved")
                    nonce += 1
                    time.sleep(3)

            # Build the bridge tx. Honour a quote-supplied legacy gasPrice
            # (LiFi); otherwise use the chain's EIP-1559 params.
            tx = {
                "from": wallet, "to": Web3.to_checksum_address(q["to"]),
                "value": q["value"], "data": q["data"],
                "nonce": nonce, "chainId": cfg["id"],
                "gas": q.get("gas_limit") or 500000,
            }
            if q.get("gas_price"):
                tx["gasPrice"] = q["gas_price"]
            else:
                tx.update(get_gas_params(w3))

            if not q.get("gas_limit"):
                try:
                    est_gas = w3.eth.estimate_gas({
                        "from": wallet, "to": tx["to"],
                        "value": q["value"], "data": q["data"]})
                    tx["gas"] = int(est_gas * 1.3)
                    print(f"    Gas: {est_gas} (using {tx['gas']})")
                except Exception as e:
                    print(f"    [!] Gas est. failed ({str(e)[:50]}), using {tx['gas']}")

            receipt = _send(w3, tx, key)
            if receipt.status == 1:
                print(f"    [OK] Success! Gas: {receipt.gasUsed}")
            else:
                print(f"    [FAIL] Reverted. Gas: {receipt.gasUsed}")
            nonce_cache[chain] = nonce + 1
            time.sleep(3)
        except Exception as e:
            print(f"    [!] Error: {e}")

    print(f"\n{'='*60}")
    print("  Collect complete.")
    if not live:
        print("  This was a DRY RUN. No transactions were sent.")
    print(f"{'='*60}\n")


# ─── Withdraw (target chain -> main wallet) ───────────────────────

def withdraw(wallet, key, to_address, amount_usd, target_id, live=False):
    target_name = chain_name_for_id(target_id)
    cfg = CHAINS[target_name]
    w3 = get_w3(cfg["rpc"])
    wallet = Web3.to_checksum_address(wallet)
    to_addr = Web3.to_checksum_address(to_address)

    balance = w3.eth.get_balance(wallet)
    balance_usd = to_usd(cfg["native"], fmt(balance))

    print(f"\n{'='*60}")
    print(f"  WITHDRAW")
    print(f"  From:    {wallet} ({target_name})")
    print(f"  To:      {to_addr}")
    print(f"  Balance: {fmt(balance):.6f} {cfg['native']} (~${balance_usd:.2f})")
    print(f"{'='*60}")

    # Leave 21000 gas + a small buffer for the L1 data fee on L2s.
    gas_cost = w3.eth.gas_price * 21000 + 5_000_000_000_000
    if amount_usd and amount_usd > 0:
        send_amount = int((amount_usd / price_of(cfg["native"])) * 1e18)
        if send_amount > balance:
            print(f"  [!] Requested ${amount_usd:.2f} but only ${balance_usd:.2f} available")
            send_amount = balance - gas_cost
    else:
        send_amount = balance - gas_cost

    if send_amount <= 0:
        print("  [!] Balance too low for gas. Skipping.")
        return

    send_human = fmt(send_amount)
    print(f"  Send:    {send_human:.6f} {cfg['native']} (~${to_usd(cfg['native'], send_human):.2f})")
    print(f"  Gas:     {fmt(balance - send_amount):.6f} {cfg['native']}")

    if not live:
        print("\n  [DRY RUN] Use --live to send for real.")
        return
    if not key:
        print("\n  [!] No private key for this wallet.")
        return

    nonce = w3.eth.get_transaction_count(wallet)
    tx = build_tx(w3, wallet, to_addr, send_amount, b"", nonce, cfg["id"], gas=21000)
    try:
        tx["gas"] = int(w3.eth.estimate_gas(
            {"from": wallet, "to": to_addr, "value": send_amount}) * 1.2)
    except Exception:
        tx["gas"] = 21000

    receipt = _send(w3, tx, key, wait_timeout=120)
    if receipt.status == 1:
        print(f"  [OK] Sent! Remaining: {fmt(w3.eth.get_balance(wallet)):.6f} {cfg['native']}")
    else:
        print("  [FAIL] Transaction reverted")


def withdraw_all(wallets, to_address, target_id, live=False):
    """Send every wallet's full target-chain balance to one address."""
    target_name = chain_name_for_id(target_id)
    print(f"\nFAST WITHDRAW -> {to_address}  ({len(wallets)} wallets on {target_name})")
    print("=" * 55)
    attempted = 0
    for i, w in enumerate(wallets, 1):
        if not w.get("key"):
            print(f"  [{i}] {w['address'][:8]}...: no key, skipping")
            continue
        attempted += 1
        withdraw(w["address"], w["key"], to_address, 0, target_id, live=live)
    print("=" * 55)
    print(f"Done ({attempted} wallets attempted; per-wallet results above).")


# ─── Distribute gas to wallets that hold ERC20 but no gas ─────────

def distribute_gas(wallets, key, target_id, live=False):
    target_name = chain_name_for_id(target_id)
    cfg = CHAINS[target_name]
    w3 = get_w3(cfg["rpc"])

    source = Web3.to_checksum_address(wallets[0]["address"])
    source_bal = w3.eth.get_balance(source)

    print(f"\n{'='*60}")
    print(f"  DISTRIBUTE GAS ({target_name})")
    print(f"  Source:  {source}")
    print(f"  Balance: {fmt(source_bal):.6f} {cfg['native']} (~${to_usd(cfg['native'], fmt(source_bal)):.2f})")
    print(f"{'='*60}")

    gas_per_wallet = int(0.00005 * 1e18)  # ~0.00005 ETH
    recipients = []

    for w in wallets[1:]:
        addr = Web3.to_checksum_address(w["address"])
        bal = w3.eth.get_balance(addr)
        has_tokens = False
        for token in TOKENS.get(target_name, []):
            try:
                contract = w3.eth.contract(
                    address=Web3.to_checksum_address(token["address"]), abi=ERC20_ABI)
                tok_bal = contract.functions.balanceOf(addr).call()
                if tok_bal > 0 and token_usd(token, fmt(tok_bal, token["decimals"])) > 0.10:
                    has_tokens = True
                    break
            except Exception:
                pass
        if has_tokens and bal < gas_per_wallet:
            recipients.append(addr)
            print(f"  -> {addr[:10]}...: needs gas (has ERC20, only {fmt(bal):.6f} ETH)")

    if not recipients:
        print("\n  No wallets need gas distribution.")
        return

    total_needed = gas_per_wallet * len(recipients)
    print(f"\n  Recipients: {len(recipients)}   Per wallet: {fmt(gas_per_wallet):.6f} ETH")
    print(f"  Total needed: {fmt(total_needed):.6f} ETH")

    if source_bal < total_needed + 5_000_000_000_000:
        print(f"  [!] Source balance too low! Need {fmt(total_needed):.6f} ETH")
        return
    if not live:
        print("\n  [DRY RUN] Use --live to send for real.")
        return

    nonce = w3.eth.get_transaction_count(source)
    for addr in recipients:
        tx = build_tx(w3, source, addr, gas_per_wallet, b"", nonce, cfg["id"], gas=21000)
        try:
            _send(w3, tx, key, wait_timeout=60)
            nonce += 1
            time.sleep(1)
        except Exception as e:
            print(f"  [!] {addr[:10]}...: {e}")

    print(f"\n  [OK] Distributed! Source remaining: {fmt(w3.eth.get_balance(source)):.6f} ETH")
