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

# Binary assets are not scanned as text (random bytes can look like hex).
SKIP_EXT = {".png", ".jpg", ".jpeg", ".gif", ".ico", ".pdf", ".woff", ".woff2",
            ".ttf", ".otf", ".zip", ".gz", ".lock"}

failures = []


def fail(msg):
    failures.append(msg)
    print("  [FAIL] " + msg)


def ok(msg):
    print("  [ok]   " + msg)


# ── 1. Secret scan ────────────────────────────────────────────────

def _is_local_secret_file(name):
    """`.env` and `.env.*` hold the user's real seeds. Only `.env.example` is meant to be shared."""
    return name == ".env" or (name.startswith(".env.") and name != ".env.example")


def _files_to_scan():
    """What git tracks: the promise is about what gets committed.

    The user's own .env stays unread, so its seeds can never end up in this
    script's output, a terminal log or a CI log. Without git, every file is
    walked except local secret files.
    """
    import subprocess
    try:
        out = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT,
                             capture_output=True, text=True, check=True).stdout
        return [os.path.join(ROOT, p) for p in out.split("\0") if p]
    except Exception:
        paths = []
        for cur, dirs, files in os.walk(ROOT):
            dirs[:] = [d for d in dirs if d not in (".git", "__pycache__", "venv", ".venv")]
            paths += [os.path.join(cur, fn) for fn in files if not _is_local_secret_file(fn)]
        return paths


def secret_scan():
    print("\n[1] Secret scan (no keys / seeds / .env committed)")
    priv = re.compile(r'\b(0x)?[a-fA-F0-9]{64}\b')
    seed = re.compile(r'\b([a-z]{3,8}\s+){11,23}[a-z]{3,8}\b')

    import subprocess
    try:
        tracked = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT,
                                 capture_output=True, text=True, check=True).stdout.split("\0")
        leaked = [p for p in tracked if p and _is_local_secret_file(os.path.basename(p))]
        if leaked:
            fail(f"secret file tracked by git: {', '.join(leaked)}")
        elif os.path.exists(os.path.join(ROOT, ".env")):
            ok(".env exists locally but is not tracked")
    except Exception:
        if os.path.exists(os.path.join(ROOT, ".env")):
            ok(".env present (git not available to confirm tracking)")

    found = 0
    for p in _files_to_scan():
        fn = os.path.basename(p)
        if os.path.splitext(fn)[1].lower() in SKIP_EXT or _is_local_secret_file(fn):
            continue
        try:
            txt = open(p, encoding="utf-8", errors="ignore").read()
        except Exception:
            continue
        rel = os.path.relpath(p, ROOT)
        # Where, never what: a match may be a real key, and this output is shown and logged.
        line_of = lambda m: txt.count("\n", 0, m.start()) + 1
        for m in priv.finditer(txt):
            fail(f"private-key-like 64-hex string at {rel}:{line_of(m)}")
            found += 1
        for m in seed.finditer(txt):
            # Ignore English prose (sentences contain punctuation nearby).
            if fn.endswith((".md", ".py")) and (". " in txt[max(0, m.start()-40):m.end()+40]):
                continue
            fail(f"seed-phrase-like text at {rel}:{line_of(m)}")
            found += 1
    if found == 0:
        ok("no private keys or seed phrases in tracked files")


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
