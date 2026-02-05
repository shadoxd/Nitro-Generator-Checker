# Discord Tip Wallet Bot (LTC)

This repository now includes a **Discord tip bot starter** with custom permissions similar to tip.cc flows.

## What this bot does

Implemented in `tip_wallet_bot.py`:

- **Server wallet** (guild-level pooled wallet).
- **User wallets** (created automatically on first use/tip).
- `.bal` command for user balance.
- `.tip @user <amount>` for user-to-user tips.
- `.servertip @user <amount>` for tipping from server wallet.
- Role-based permissions that match your twist:
  - **Staff**: can deposit and tip, but **cannot withdraw**.
  - **Family**: can deposit, withdraw, and tip.
  - **Admin/Owner**: can configure roles and full server-wallet controls.

## Commands

- `.setuproles @staffRole @familyRole` — admin/owner only
- `.bal`
- `.deposit <amount>`
- `.withdraw <amount>` (blocked for staff unless also family/admin)
- `.tip @user <amount>`
- `.serverbal`
- `.serverdeposit <amount>`
- `.serverwithdraw <amount>` (admin/family only)
- `.servertip @user <amount>`
- `.recommended`

## Quick setup

1. Install dependencies:
   ```bash
   pip install -U discord.py
   ```
2. Set bot token:
   ```bash
   export DISCORD_BOT_TOKEN="your_token_here"
   ```
3. Run:
   ```bash
   python tip_wallet_bot.py
   ```

Optional environment variables:
- `TIPBOT_DB_PATH` (default: `tipbot.db`)
- `TIPBOT_PREFIX` (default: `.`)

## Notes for production

- Current deposit/withdraw commands update internal balances immediately.
- For real LTC custody, connect these flows to a proper LTC wallet service and transaction confirmation pipeline.
- Add anti-abuse checks (rate limits, min/max amounts, AML/KYC policy where needed).
