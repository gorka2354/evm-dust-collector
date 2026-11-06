"""Command line interface — one entry point for every operation.

    python -m dust_collector scan                 # read-only, no key needed
    python -m dust_collector audit                # fast parallel native scan
    python -m dust_collector estimate             # scan + gas/profit estimate
    python -m dust_collector collect              # bridge to target (dry run)
    python -m dust_collector collect --live --yes # bridge for real
    python -m dust_collector withdraw --to 0x..   # target chain -> main wallet
    python -m dust_collector distribute --live    # seed gas for ERC20 swaps
    python -m dust_collector sweep --to 0x..      # plan sending all dust to one address
    python -m dust_collector sweep --to 0x.. --live
"""

import argparse
import os
import sys

from web3 import Web3

from . import core
from .config import CHAINS, TARGET_CHAIN_ID, chain_name_for_id
from .prices import update_prices
from .wallets import load_wallets, default_env_path
from . import sweep


def _resolve_wallets(args):
    """Return a list of {"address","key"} from --wallet or the .env file."""
    if args.wallet:
        pk = os.environ.get("PRIVATE_KEY", "")
        if pk and not pk.startswith("0x"):
            pk = "0x" + pk
        return [{"address": args.wallet, "key": pk or None}]
    return load_wallets(args.env)


def _resolve_to(args):
    """Withdraw destination: --to flag or MAIN_WALLET env var."""
    to = args.to or os.environ.get("MAIN_WALLET", "")
    return to.strip() or None


def _chosen_chains(args):
    """The chains --chains names (all of them by default), or None after saying which name is unknown."""
    if not args.chains:
        return CHAINS
    names = [name.strip() for name in args.chains.split(",") if name.strip()]
    unknown = [name for name in names if name not in CHAINS]
    if unknown:
        print(f"[!] Unknown chain(s): {', '.join(unknown)}")
        return None
    return {name: CHAINS[name] for name in names}


def build_parser():
    p = argparse.ArgumentParser(
        prog="dust_collector",
        description="Sweep small EVM balances from many wallets into one.")
    p.add_argument("--env", default=None, help="path to .env (default: ./.env)")
    p.add_argument("--wallet", help="single wallet address (skip .env)")
    p.add_argument("--target", type=int, default=TARGET_CHAIN_ID,
                   help=f"target chain id (default {TARGET_CHAIN_ID} = Base)")
    p.add_argument("--live", action="store_true", help="actually send transactions")
    p.add_argument("--yes", "-y", action="store_true", help="auto-confirm transactions")
    p.add_argument("--to", help="withdraw destination address (or MAIN_WALLET env)")
    p.add_argument("--amount", type=float, default=0,
                   help="USD amount to withdraw (0 = max)")
    p.add_argument("--chains", help="sweep/fund: only these chains, comma-separated (default: all)")
    p.add_argument("--from", dest="funder", help="fund: the wallet that pays the gas top-ups")
    p.add_argument("command", nargs="?", default="scan",
                   choices=["scan", "audit", "estimate", "collect",
                            "withdraw", "distribute", "sweep", "fund"],
                   help="operation to run (default: scan)")
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)

    if args.target not in {c["id"] for c in CHAINS.values()}:
        print(f"[!] Unknown target chain id {args.target}. "
              f"Pick one of the configured chains.")
        return 1

    print(f"\n[*] EVM Dust Collector - target: {chain_name_for_id(args.target)} "
          f"(chain_id={args.target})")
    print("[*] Keys never leave this machine. See SECURITY.md.\n")

    update_prices()

    wallets = _resolve_wallets(args)
    if not wallets:
        print(f"\n[!] No wallets found in {args.env or default_env_path()}")
        print("    Add wallets to .env (see .env.example) or use --wallet 0x...")
        return 1
    keyed = sum(1 for w in wallets if w.get("key"))
    print(f"[*] Loaded {len(wallets)} wallet(s), {keyed} with a key")

    # ── audit: fast parallel native scan across all wallets ──
    if args.command == "audit":
        core.fast_audit([w["address"] for w in wallets])
        return 0

    # ── distribute: gas from first wallet to the rest ──
    if args.command == "distribute":
        core.distribute_gas(wallets, wallets[0].get("key"), args.target, live=args.live)
        return 0

    # ── fund: gas from one wallet to those whose tokens can't move without it ──
    if args.command == "fund":
        to = _resolve_to(args)
        if not to or not args.funder:
            print("\n[!] fund needs --from 0x... (a wallet in the .env, with its key) and --to 0x... "
                  "(where the sweep will send, for the gas estimates).")
            return 1
        chains = _chosen_chains(args)
        if chains is None:
            return 1
        print(f"\n[*] Planning gas top-ups from {args.funder} ...")
        try:
            plan = sweep.fund_all(wallets, args.funder, to, live=False, chains=chains)
        except ValueError as e:
            print(f"[!] {e}")
            return 1
        sweep.print_sweep(plan, live=False, title="GAS TOP-UPS")
        freed = sum(r["frees_usd"] for r in plan if r["status"] == "planned")
        print(f"  Tokens these top-ups free: ${freed:.2f}")
        if not args.live:
            print("\n  Dry run: nothing was sent. Add --live to send.")
            return 0
        if not any(r["status"] == "planned" for r in plan):
            print("\n  Nothing to send.")
            return 0
        if not args.yes and input(f"\nSend these top-ups from {args.funder}? Type yes: ").strip().lower() != "yes":
            print("Cancelled, nothing was sent.")
            return 1
        try:
            report = sweep.new_report()
        except OSError as e:
            print(f"[!] Can't create the report file here ({e}). Nothing was sent.")
            return 1
        rows = sweep.fund_all(wallets, args.funder, to, live=True, chains=chains)
        sweep.print_sweep(rows, live=True, title="GAS TOP-UPS")
        sweep.write_report(rows, report)
        print(f"\n  Every top-up with its hash: {report}")
        print("  Next: run sweep again; the freed tokens are in its plan now.")
        return 0

    # ── sweep: every wallet's dust to one address, chain by chain ──
    if args.command == "sweep":
        to = _resolve_to(args)
        if not to:
            print("\n[!] No destination. Use --to 0x... or set MAIN_WALLET env var.")
            return 1
        chains = _chosen_chains(args)
        if chains is None:
            return 1
        print(f"\n[*] Planning the sweep of {len(wallets)} wallet(s) to {to} ...")
        try:
            plan = sweep.sweep_all(wallets, to, live=False, chains=chains)
        except ValueError as e:
            print(f"[!] {e}")
            return 1
        sweep.print_sweep(plan, live=False)
        if not args.live:
            print("\n  Dry run: nothing was sent. Add --live to send.")
            return 0

        # The live run touches only what this plan lists, from wallets that can sign.
        by_address = {Web3.to_checksum_address(w["address"]): w for w in wallets}
        planned = {(r["wallet"], r["chain"]) for r in plan if r["status"] == "planned"}
        keyless = sorted({a for a, _ in planned if not by_address[a].get("key")})
        if keyless:
            shown = ", ".join(a[:10] + "..." for a in keyless[:8]) + (" ..." if len(keyless) > 8 else "")
            print(f"\n[!] {len(keyless)} wallet(s) with planned transfers have no key and stay out: {shown}")
        planned = {(a, c) for a, c in planned if by_address[a].get("key")}
        if not planned:
            print("\n  Nothing to send.")
            return 0
        senders = [w for a, w in by_address.items() if any(a == p for p, _ in planned)]
        question = f"\nSend the planned transfers from {len(senders)} wallet(s) to {to}? Type yes: "
        if not args.yes and input(question).strip().lower() != "yes":
            print("Cancelled, nothing was sent.")
            return 1
        # The report file is made before the first transfer: if this folder
        # can't take it, the run stops while nothing has gone.
        try:
            report = sweep.new_report()
        except OSError as e:
            print(f"[!] Can't create the report file here ({e}). Nothing was sent.")
            return 1
        try:
            rows = sweep.sweep_all(senders, to, live=True, chains={c: chains[c] for _, c in planned}, only=planned)
        except ValueError as e:
            print(f"[!] {e}")
            return 1
        sweep.print_sweep(rows, live=True)
        try:
            sweep.write_report(rows, report)
        except OSError as e:
            print(f"\n[!] Could not write {report} ({e}). Every transfer with its hash:")
            for r in rows:
                if r["tx"]:
                    print(f"  {r['chain']} {r['wallet']} {r['symbol']} {r['status']} {r['tx']}")
            return 1
        print(f"\n  Every transfer with its hash: {report}")
        return 0

    # ── withdraw across all wallets ──
    if args.command == "withdraw":
        to = _resolve_to(args)
        if not to:
            print("\n[!] No destination. Use --to 0x... or set MAIN_WALLET env var.")
            return 1
        if len(wallets) == 1:
            core.withdraw(wallets[0]["address"], wallets[0].get("key"),
                          to, args.amount, args.target, live=args.live)
        else:
            core.withdraw_all(wallets, to, args.target, live=args.live)
        return 0

    # ── scan / estimate / collect: per wallet ──
    for i, w in enumerate(wallets, 1):
        print(f"\n{'#'*60}\n  WALLET [{i}/{len(wallets)}]: {w['address']}\n{'#'*60}")
        print("\n[*] Scanning balances across all chains...")
        results = core.print_balances(core.scan_wallet(w["address"]))

        if args.command in ("estimate", "collect"):
            profitable = core.estimate(results, args.target)
            if args.command == "collect":
                if profitable:
                    core.collect(w["address"], w.get("key"), profitable,
                                 args.target, live=args.live, auto_confirm=args.yes)
                else:
                    print("\n[!] No profitable operations found.")
        elif args.command == "scan":
            print("\nNext: estimate | collect | collect --live --yes | "
                  "withdraw --to 0x...")

    return 0


if __name__ == "__main__":
    sys.exit(main())
