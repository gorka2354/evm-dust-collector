#!/usr/bin/env python3
"""Render docs/demo.png — an illustrative terminal screenshot for the README.

This is EXAMPLE output with placeholder addresses (0x1111...). It uses the same
format strings as dust_collector/core.py so the layout matches a real run. No
personal data, no real wallets.
"""

import os
from PIL import Image, ImageDraw, ImageFont

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "docs", "demo.png")

# ── Example data (illustrative dust scattered across chains) ──
BAL = [  # (chain, symbol, balance, usd)
    ("polygon",  "POL",  12.400000, 4.96),
    ("arbitrum", "ETH",   0.001842, 3.64),
    ("base",     "USDC",  2.310000, 2.31),
    ("optimism", "ETH",   0.000931, 1.84),
    ("scroll",   "ETH",   0.000402, 0.79),
]
EST = [  # (chain, symbol, usd, gas, bridge, profit, action)
    ("polygon",  "POL",  4.96, 0.01, 0.30, 4.65, "bridge native"),
    ("arbitrum", "ETH",  3.64, 0.02, 0.30, 3.32, "bridge native"),
    ("base",     "USDC", 2.31, 0.00, 0.00, 2.31, "already on target"),
    ("optimism", "ETH",  1.84, 0.02, 0.30, 1.52, "bridge native"),
    ("scroll",   "ETH",  0.79, 0.03, 0.30, 0.46, "bridge native"),
]


def build_lines():
    L = []
    L.append("$ python collect.py estimate")
    L.append("")
    L.append("[*] EVM Dust Collector - target: base (chain_id=8453)")
    L.append("[*] Keys never leave this machine. See SECURITY.md.")
    L.append("")
    L.append("[*] Prices updated from CoinGecko")
    L.append("[*] Loaded 4 wallet(s)")
    L.append("")
    L.append("#" * 60)
    L.append("  WALLET [1/4]: 0x1111...1111")
    L.append("#" * 60)
    L.append("")
    L.append("[*] Scanning balances across all chains...")
    L.append("")
    L.append("=" * 65)
    L.append(f"  {'Chain':<14} {'Asset':<8} {'Balance':>14} {'USD':>10}")
    L.append("-" * 65)
    total = 0.0
    for chain, sym, bal, usd in BAL:
        total += usd
        L.append(f"  {chain:<14} {sym:<8} {bal:>14.6f} {usd:>9.2f}$")
    L.append("-" * 65)
    L.append(f"  {'TOTAL':<14} {'':8} {'':>14} {total:>9.2f}$")
    L.append("=" * 65)
    L.append("")
    L.append("[*] Estimating gas costs...")
    L.append("")
    L.append(f"{'Chain':<14} {'Asset':<8} {'Value':>8} {'Gas':>8} {'Bridge':>8} {'Profit':>8} Action")
    L.append("-" * 80)
    tp = 0.0
    for chain, sym, usd, gas, br, profit, action in EST:
        tp += profit
        L.append(f"  {chain:<12} {sym:<8} {usd:>7.2f}$ {gas:>7.2f}$ {br:>7.2f}$ "
                 f"{profit:>+7.2f}$ [OK] {action}")
    L.append("-" * 80)
    L.append(f"  Estimated total profit: {tp:>+.2f}$")
    L.append("  Target chain: base")
    return L


# ── Colors (GitHub dark palette) ──
BG = (13, 17, 23)
BAR = (22, 27, 34)
FG = (201, 209, 217)
GREEN = (63, 185, 80)
BLUE = (88, 166, 255)
YELLOW = (210, 153, 34)
WHITE = (240, 246, 252)
DIM = (110, 118, 129)


def color_for(line):
    s = line.strip()
    if s.startswith("$"):
        return GREEN
    if "[OK]" in line:
        return GREEN
    if s.startswith("[*]"):
        return BLUE
    if s.startswith("#") or "WALLET" in line:
        return YELLOW
    if s and set(s) <= set("=-"):
        return DIM
    if "TOTAL" in line or "profit" in line.lower():
        return WHITE
    return FG


def main():
    lines = build_lines()
    fs = 21
    lh = 29
    pad = 26
    bar_h = 42
    font = ImageFont.truetype("C:/Windows/Fonts/consola.ttf", fs)
    bold = ImageFont.truetype("C:/Windows/Fonts/consolab.ttf", fs)

    width = pad * 2 + int(max(font.getlength(ln) for ln in lines)) + 8
    height = bar_h + pad + lh * len(lines) + pad

    img = Image.new("RGB", (width, height), BG)
    d = ImageDraw.Draw(img)

    # Title bar with traffic-light dots
    d.rectangle([0, 0, width, bar_h], fill=BAR)
    for i, c in enumerate([(255, 95, 86), (255, 189, 46), (39, 201, 63)]):
        d.ellipse([18 + i * 22, bar_h // 2 - 7, 32 + i * 22, bar_h // 2 + 7], fill=c)
    title = "collect.py  -  EVM Dust Collector"
    d.text((width // 2 - bold.getlength(title) // 2, bar_h // 2 - fs // 2),
           title, font=bold, fill=DIM)

    # Body
    y = bar_h + pad
    for ln in lines:
        col = color_for(ln)
        use = bold if (col is WHITE or ln.strip().startswith("$")) else font
        d.text((pad, y), ln, font=use, fill=col)
        y += lh

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    img.save(OUT)
    print(f"wrote {OUT}  ({width}x{height})")


if __name__ == "__main__":
    main()
