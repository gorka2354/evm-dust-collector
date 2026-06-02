# Disclaimer

This is **unaudited, best-effort software** that moves real funds across
bridges. Read this before using it with anything you can't afford to lose.

- **You can lose money.** Bridging is inherently risky: a bridge can be slow,
  revert, mis-route, or in the worst case lose funds. This tool sends 82% of a
  native balance by default and trusts third-party bridge quotes
  (Relay, LiFi). Always dry-run first.
- **Use your own wallets only.** This tool is for consolidating dust from
  wallets *you control* into a wallet *you control*. Don't use it with keys
  that aren't yours.
- **No warranty.** Provided "as is" under the MIT License. The authors are not
  liable for lost funds, failed bridges, wrong-chain sends, or anything else.
- **Prices are approximate.** The USD figures are only used to decide whether a
  balance is worth bridging. They are not financial advice.
- **Third parties.** Bridge routes come from Relay and LiFi; balances and
  broadcasts go through public RPC endpoints; prices come from CoinGecko. Their
  availability and correctness are outside this tool's control.
- **You are responsible** for verifying the destination address, reviewing each
  transaction, and complying with the laws and taxes of your jurisdiction.

If you don't accept this, don't use the tool.
