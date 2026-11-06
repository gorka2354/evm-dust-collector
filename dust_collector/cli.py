"""Command line interface — one entry point for every operation.

    python -m dust_collector scan                 # read-only, no key needed
    python -m dust_collector audit                # fast parallel native scan
    python -m dust_collector estimate             # scan + gas/profit estimate
    python -m dust_collector collect              # bridge to target (dry run)
    python -m dust_collector collect --live --yes # bridge for real
    python -m dust_collector withdraw --to 0x..   # target chain -> main wallet
    python -m dust_collector distribute --live    # seed gas for ERC20 swaps
"""

import argparse
import os
import sys

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
    p.add_argument("--chains", help="sweep: only these chains, comma-separated (default: all)")
    p.add_argument("command", nargs="?", default="scan",
                   choices=["scan", "audit", "estimate", "collect",
                            "withdraw", "distribute", "sweep"],
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

    # ── sweep: every wallet's dust to one address, chain by chain ──
    if args.command == "sweep":
        to = _resolve_to(args)
        if not to:
            print("\n[!] No destination. Use --to 0x... or set MAIN_WALLET env var.")
            return 1
        chains = CHAINS
        if args.chains:
            names = [name.strip() for name in args.chains.split(",") if name.strip()]
            unknown = [name for name in names if name not in CHAINS]
            if unknown:
                print(f"[!] Unknown chain(s): {', '.join(unknown)}")
                return 1
            chains = {name: CHAINS[name] for name in names}
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
        if not args.yes and input(f"\nSend all of the above to {to}? Type yes: ").strip().lower() != "yes":
            print("Cancelled, nothing was sent.")
            return 1
        try:
            rows = sweep.sweep_all(wallets, to, live=True, chains=chains)
        except ValueError as e:
            print(f"[!] {e}")
            return 1
        sweep.print_sweep(rows, live=True)
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
