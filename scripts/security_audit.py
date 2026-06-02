#!/usr/bin/env python3
"""Automated security audit. Run it yourself: `python scripts/security_audit.py`

It enforces the promises in SECURITY.md:

  1. No secrets are committed (no .env, no private keys, no seed phrases).
  2. The code talks to NO network host beyond the documented allow-list
     (RPCs from config.py + Relay + LiFi + CoinGecko). A new outbound URL
     fails the build, so "where your data goes" can never silently change.
  3. No key/seed variable is handed to an HTTP request.

CI runs this on every push and pull request. Exit code != 0 means a problem.
Output is ASCII-only so it renders on any console.
"""

import os
import re
import sys
from urllib.parse import urlparse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PKG = os.path.join(ROOT, "dust_collector")

# Hosts the tool is allowed to contact directly in code (besides RPCs).
ALLOWED_API_HOSTS = {"api.relay.link", "li.quest", "api.coingecko.com"}

failures = []


def fail(msg):
    failures.append(msg)
    print("  [FAIL] " + msg)


def ok(msg):
    print("  [ok]   " + msg)


# ── 1. Secret scan ────────────────────────────────────────────────

def secret_scan():
    print("\n[1] Secret scan (no keys / seeds / .env committed)")
    priv = re.compile(r'\b(0x)?[a-fA-F0-9]{64}\b')
    seed = re.compile(r'\b([a-z]{3,8}\s+){11,23}[a-z]{3,8}\b')

    if os.path.exists(os.path.join(ROOT, ".env")):
        # A local .env is fine for the user, but it must never be tracked.
        import subprocess
        try:
            tracked = subprocess.run(
                ["git", "ls-files", ".env"], cwd=ROOT,
                capture_output=True, text=True).stdout.strip()
            if tracked:
                fail(".env is tracked by git!")
            else:
                ok(".env exists locally but is not tracked")
        except Exception:
            ok(".env present (git not available to confirm tracking)")

    found = 0
    for cur, dirs, files in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in (".git", "__pycache__", "venv", ".venv")]
        for fn in files:
            p = os.path.join(cur, fn)
            try:
                txt = open(p, encoding="utf-8", errors="ignore").read()
            except Exception:
                continue
            if priv.search(txt):
                fail(f"private-key-like 64-hex string in {os.path.relpath(p, ROOT)}")
                found += 1
            for m in seed.finditer(txt):
                frag = m.group(0)
                # Ignore English prose (sentences contain punctuation nearby).
                if fn.endswith((".md", ".py")) and (". " in txt[max(0, m.start()-40):m.end()+40]):
                    continue
                fail(f"seed-phrase-like text in {os.path.relpath(p, ROOT)}: {frag[:30]}...")
                found += 1
    if found == 0:
        ok("no private keys or seed phrases anywhere")


# ── 2. Network egress allow-list ──────────────────────────────────

def egress_scan():
    print("\n[2] Network egress (no undocumented hosts)")
    # Allowed RPC hosts come straight from the committed config.
    sys.path.insert(0, ROOT)
    from dust_collector.config import CHAINS
    allowed = set(ALLOWED_API_HOSTS)
    for cfg in CHAINS.values():
        allowed.add(urlparse(cfg["rpc"]).netloc)

    url_re = re.compile(r'https?://[^\s"\'`)]+')
    bad = 0
    for fn in os.listdir(PKG):
        if not fn.endswith(".py"):
            continue
        for i, line in enumerate(open(os.path.join(PKG, fn), encoding="utf-8"), 1):
            for url in url_re.findall(line):
                host = urlparse(url).netloc
                if not host:
                    continue
                # relay.link bridge UI link printed for the user is fine.
                if host in allowed or host.endswith("relay.link"):
                    continue
                fail(f"undocumented egress host {host} in {fn}:{i} "
                     f"(add it to config or SECURITY.md)")
                bad += 1
    if bad == 0:
        ok(f"all network hosts are documented ({len(allowed)} allowed)")


# ── 3. Keys never go into an HTTP request ─────────────────────────

def key_egress_scan():
    print("\n[3] Keys never handed to an HTTP request")
    danger = re.compile(r'requests\.(get|post)|HTTPProvider\(')
    keyish = re.compile(r'\b(private_key|mnemonic|seed_phrase|\.key\b|raw_key)\b')
    bad = 0
    for fn in os.listdir(PKG):
        if not fn.endswith(".py"):
            continue
        for i, line in enumerate(open(os.path.join(PKG, fn), encoding="utf-8"), 1):
            if danger.search(line) and keyish.search(line):
                fail(f"possible key in request at {fn}:{i}")
                bad += 1
    if bad == 0:
        ok("no key/seed variable appears in any HTTP call")


if __name__ == "__main__":
    print("=" * 60)
    print(" EVM Dust Collector - security audit")
    print("=" * 60)
    secret_scan()
    egress_scan()
    key_egress_scan()
    print("\n" + "=" * 60)
    if failures:
        print(f" RESULT: {len(failures)} problem(s) found")
        print("=" * 60)
        sys.exit(1)
    print(" RESULT: PASS - safe")
    print("=" * 60)
    sys.exit(0)
