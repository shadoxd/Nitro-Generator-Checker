"""Discord tip bot with server wallet + role-based permissions.

This is an MVP reference implementation inspired by tip.cc style commands,
with custom rules:
- Server wallet: Administrators (or owner) can deposit/withdraw/tip from it.
- Staff: can deposit to server wallet and tip users, but cannot withdraw.
- Family: can deposit, withdraw, and tip from their personal wallet.
- Users: can receive tips, check balance, and withdraw their personal balance.

Important:
- On-chain deposits/withdrawals are mocked as ledger entries. Integrate an LTC
  wallet provider for production use.
"""

from __future__ import annotations

import os
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Optional

import discord
from discord.ext import commands

DB_PATH = os.getenv("TIPBOT_DB_PATH", "tipbot.db")
PREFIX = os.getenv("TIPBOT_PREFIX", ".")
TOKEN = os.getenv("DISCORD_BOT_TOKEN", "")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class LedgerEvent:
    guild_id: int
    user_id: Optional[int]
    event_type: str
    amount: str
    note: str


class WalletStore:
    def __init__(self, path: str) -> None:
        self.path = path
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with closing(self._connect()) as conn, conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS guild_wallets (
                    guild_id INTEGER PRIMARY KEY,
                    server_balance TEXT NOT NULL DEFAULT '0'
                );

                CREATE TABLE IF NOT EXISTS user_wallets (
                    guild_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    balance TEXT NOT NULL DEFAULT '0',
                    total_deposit TEXT NOT NULL DEFAULT '0',
                    total_withdraw TEXT NOT NULL DEFAULT '0',
                    PRIMARY KEY (guild_id, user_id)
                );

                CREATE TABLE IF NOT EXISTS role_config (
                    guild_id INTEGER PRIMARY KEY,
                    staff_role_id INTEGER,
                    family_role_id INTEGER
                );

                CREATE TABLE IF NOT EXISTS ledger (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    guild_id INTEGER NOT NULL,
                    user_id INTEGER,
                    event_type TEXT NOT NULL,
                    amount TEXT NOT NULL,
                    note TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                """
            )

    def ensure_guild(self, guild_id: int) -> None:
        with closing(self._connect()) as conn, conn:
            conn.execute(
                "INSERT OR IGNORE INTO guild_wallets (guild_id, server_balance) VALUES (?, '0')",
                (guild_id,),
            )
            conn.execute(
                "INSERT OR IGNORE INTO role_config (guild_id, staff_role_id, family_role_id) VALUES (?, NULL, NULL)",
                (guild_id,),
            )

    def ensure_user(self, guild_id: int, user_id: int) -> None:
        with closing(self._connect()) as conn, conn:
            conn.execute(
                "INSERT OR IGNORE INTO user_wallets (guild_id, user_id, balance, total_deposit, total_withdraw) VALUES (?, ?, '0', '0', '0')",
                (guild_id, user_id),
            )

    def set_roles(self, guild_id: int, staff_role_id: Optional[int], family_role_id: Optional[int]) -> None:
        with closing(self._connect()) as conn, conn:
            conn.execute(
                "UPDATE role_config SET staff_role_id = ?, family_role_id = ? WHERE guild_id = ?",
                (staff_role_id, family_role_id, guild_id),
            )

    def get_roles(self, guild_id: int) -> sqlite3.Row:
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT staff_role_id, family_role_id FROM role_config WHERE guild_id = ?",
                (guild_id,),
            ).fetchone()
            if row is None:
                raise RuntimeError("Guild config missing")
            return row

    def get_server_balance(self, guild_id: int) -> Decimal:
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT server_balance FROM guild_wallets WHERE guild_id = ?",
                (guild_id,),
            ).fetchone()
            if row is None:
                return Decimal("0")
            return Decimal(row["server_balance"])

    def get_user_balance(self, guild_id: int, user_id: int) -> Decimal:
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT balance FROM user_wallets WHERE guild_id = ? AND user_id = ?",
                (guild_id, user_id),
            ).fetchone()
            if row is None:
                return Decimal("0")
            return Decimal(row["balance"])

    def _log(self, conn: sqlite3.Connection, event: LedgerEvent) -> None:
        conn.execute(
            "INSERT INTO ledger (guild_id, user_id, event_type, amount, note, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (event.guild_id, event.user_id, event.event_type, event.amount, event.note, now_iso()),
        )

    def server_deposit(self, guild_id: int, user_id: int, amount: Decimal) -> None:
        with closing(self._connect()) as conn, conn:
            current = Decimal(
                conn.execute(
                    "SELECT server_balance FROM guild_wallets WHERE guild_id = ?",
                    (guild_id,),
                ).fetchone()["server_balance"]
            )
            updated = current + amount
            conn.execute(
                "UPDATE guild_wallets SET server_balance = ? WHERE guild_id = ?",
                (str(updated), guild_id),
            )
            self._log(conn, LedgerEvent(guild_id, user_id, "server_deposit", str(amount), "server wallet credited"))

    def server_withdraw(self, guild_id: int, user_id: int, amount: Decimal) -> bool:
        with closing(self._connect()) as conn, conn:
            current = Decimal(
                conn.execute(
                    "SELECT server_balance FROM guild_wallets WHERE guild_id = ?",
                    (guild_id,),
                ).fetchone()["server_balance"]
            )
            if current < amount:
                return False
            updated = current - amount
            conn.execute(
                "UPDATE guild_wallets SET server_balance = ? WHERE guild_id = ?",
                (str(updated), guild_id),
            )
            self._log(conn, LedgerEvent(guild_id, user_id, "server_withdraw", str(amount), "server wallet debited"))
            return True

    def user_deposit(self, guild_id: int, user_id: int, amount: Decimal) -> None:
        self.ensure_user(guild_id, user_id)
        with closing(self._connect()) as conn, conn:
            row = conn.execute(
                "SELECT balance, total_deposit FROM user_wallets WHERE guild_id = ? AND user_id = ?",
                (guild_id, user_id),
            ).fetchone()
            new_balance = Decimal(row["balance"]) + amount
            new_total_deposit = Decimal(row["total_deposit"]) + amount
            conn.execute(
                "UPDATE user_wallets SET balance = ?, total_deposit = ? WHERE guild_id = ? AND user_id = ?",
                (str(new_balance), str(new_total_deposit), guild_id, user_id),
            )
            self._log(conn, LedgerEvent(guild_id, user_id, "user_deposit", str(amount), "personal wallet credited"))

    def user_withdraw(self, guild_id: int, user_id: int, amount: Decimal) -> bool:
        self.ensure_user(guild_id, user_id)
        with closing(self._connect()) as conn, conn:
            row = conn.execute(
                "SELECT balance, total_withdraw FROM user_wallets WHERE guild_id = ? AND user_id = ?",
                (guild_id, user_id),
            ).fetchone()
            balance = Decimal(row["balance"])
            if balance < amount:
                return False
            new_balance = balance - amount
            new_total_withdraw = Decimal(row["total_withdraw"]) + amount
            conn.execute(
                "UPDATE user_wallets SET balance = ?, total_withdraw = ? WHERE guild_id = ? AND user_id = ?",
                (str(new_balance), str(new_total_withdraw), guild_id, user_id),
            )
            self._log(conn, LedgerEvent(guild_id, user_id, "user_withdraw", str(amount), "personal wallet debited"))
            return True

    def transfer_user_to_user(self, guild_id: int, from_user_id: int, to_user_id: int, amount: Decimal) -> bool:
        self.ensure_user(guild_id, from_user_id)
        self.ensure_user(guild_id, to_user_id)
        with closing(self._connect()) as conn, conn:
            sender = conn.execute(
                "SELECT balance FROM user_wallets WHERE guild_id = ? AND user_id = ?",
                (guild_id, from_user_id),
            ).fetchone()
            sender_balance = Decimal(sender["balance"])
            if sender_balance < amount:
                return False
            receiver = conn.execute(
                "SELECT balance FROM user_wallets WHERE guild_id = ? AND user_id = ?",
                (guild_id, to_user_id),
            ).fetchone()
            conn.execute(
                "UPDATE user_wallets SET balance = ? WHERE guild_id = ? AND user_id = ?",
                (str(sender_balance - amount), guild_id, from_user_id),
            )
            conn.execute(
                "UPDATE user_wallets SET balance = ? WHERE guild_id = ? AND user_id = ?",
                (str(Decimal(receiver["balance"]) + amount), guild_id, to_user_id),
            )
            self._log(conn, LedgerEvent(guild_id, from_user_id, "tip_sent", str(amount), f"to:{to_user_id}"))
            self._log(conn, LedgerEvent(guild_id, to_user_id, "tip_received", str(amount), f"from:{from_user_id}"))
            return True

    def transfer_server_to_user(self, guild_id: int, by_user_id: int, to_user_id: int, amount: Decimal) -> bool:
        self.ensure_user(guild_id, to_user_id)
        with closing(self._connect()) as conn, conn:
            server_balance = Decimal(
                conn.execute(
                    "SELECT server_balance FROM guild_wallets WHERE guild_id = ?",
                    (guild_id,),
                ).fetchone()["server_balance"]
            )
            if server_balance < amount:
                return False
            user_row = conn.execute(
                "SELECT balance FROM user_wallets WHERE guild_id = ? AND user_id = ?",
                (guild_id, to_user_id),
            ).fetchone()
            conn.execute(
                "UPDATE guild_wallets SET server_balance = ? WHERE guild_id = ?",
                (str(server_balance - amount), guild_id),
            )
            conn.execute(
                "UPDATE user_wallets SET balance = ? WHERE guild_id = ? AND user_id = ?",
                (str(Decimal(user_row["balance"]) + amount), guild_id, to_user_id),
            )
            self._log(conn, LedgerEvent(guild_id, by_user_id, "server_tip_sent", str(amount), f"to:{to_user_id}"))
            self._log(conn, LedgerEvent(guild_id, to_user_id, "server_tip_received", str(amount), f"from_server_by:{by_user_id}"))
            return True


def parse_amount(value: str) -> Decimal:
    try:
        amount = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError("Invalid amount.") from exc
    if amount <= 0:
        raise ValueError("Amount must be greater than 0.")
    return amount.quantize(Decimal("0.00000001"))


intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix=PREFIX, intents=intents)
store = WalletStore(DB_PATH)


def has_role(member: discord.Member, role_id: Optional[int]) -> bool:
    if role_id is None:
        return False
    return any(r.id == role_id for r in member.roles)


def is_admin_or_owner(member: discord.Member) -> bool:
    return member.guild_permissions.administrator or member.id == member.guild.owner_id


def is_family(member: discord.Member, family_role_id: Optional[int]) -> bool:
    return has_role(member, family_role_id)


def is_staff(member: discord.Member, staff_role_id: Optional[int]) -> bool:
    return has_role(member, staff_role_id)


@bot.event
async def on_ready() -> None:
    print(f"Logged in as {bot.user} ({bot.user.id})")


@bot.command(name="setuproles")
@commands.guild_only()
async def setup_roles(ctx: commands.Context, staff_role: discord.Role, family_role: discord.Role) -> None:
    if not isinstance(ctx.author, discord.Member) or not is_admin_or_owner(ctx.author):
        await ctx.reply("Only server owner/admin can configure roles.")
        return

    store.ensure_guild(ctx.guild.id)
    store.set_roles(ctx.guild.id, staff_role.id, family_role.id)
    await ctx.reply(f"Configured roles: staff={staff_role.mention}, family={family_role.mention}")


@bot.command(name="serverbal")
@commands.guild_only()
async def server_bal(ctx: commands.Context) -> None:
    store.ensure_guild(ctx.guild.id)
    bal = store.get_server_balance(ctx.guild.id)
    await ctx.reply(f"Server wallet balance: **{bal} LTC**")


@bot.command(name="bal")
@commands.guild_only()
async def user_bal(ctx: commands.Context) -> None:
    store.ensure_guild(ctx.guild.id)
    store.ensure_user(ctx.guild.id, ctx.author.id)
    bal = store.get_user_balance(ctx.guild.id, ctx.author.id)
    await ctx.reply(f"Your balance: **{bal} LTC**")


@bot.command(name="serverdeposit")
@commands.guild_only()
async def server_deposit(ctx: commands.Context, amount: str) -> None:
    assert isinstance(ctx.author, discord.Member)
    store.ensure_guild(ctx.guild.id)
    roles = store.get_roles(ctx.guild.id)

    if not (is_admin_or_owner(ctx.author) or is_staff(ctx.author, roles["staff_role_id"]) or is_family(ctx.author, roles["family_role_id"])):
        await ctx.reply("Only admin/staff/family can deposit to server wallet.")
        return

    parsed = parse_amount(amount)
    store.server_deposit(ctx.guild.id, ctx.author.id, parsed)
    await ctx.reply(f"Deposited **{parsed} LTC** into server wallet.")


@bot.command(name="serverwithdraw")
@commands.guild_only()
async def server_withdraw(ctx: commands.Context, amount: str) -> None:
    assert isinstance(ctx.author, discord.Member)
    store.ensure_guild(ctx.guild.id)
    roles = store.get_roles(ctx.guild.id)

    if not (is_admin_or_owner(ctx.author) or is_family(ctx.author, roles["family_role_id"])):
        await ctx.reply("Only admin/family can withdraw from server wallet.")
        return

    parsed = parse_amount(amount)
    ok = store.server_withdraw(ctx.guild.id, ctx.author.id, parsed)
    if not ok:
        await ctx.reply("Insufficient server wallet balance.")
        return

    await ctx.reply(f"Withdrew **{parsed} LTC** from server wallet.")


@bot.command(name="deposit")
@commands.guild_only()
async def user_deposit(ctx: commands.Context, amount: str) -> None:
    parsed = parse_amount(amount)
    store.ensure_guild(ctx.guild.id)
    store.user_deposit(ctx.guild.id, ctx.author.id, parsed)
    await ctx.reply(f"Deposited **{parsed} LTC** to your wallet.")


@bot.command(name="withdraw")
@commands.guild_only()
async def user_withdraw(ctx: commands.Context, amount: str) -> None:
    assert isinstance(ctx.author, discord.Member)
    store.ensure_guild(ctx.guild.id)
    roles = store.get_roles(ctx.guild.id)

    # Staff-only users are blocked from withdrawing.
    if is_staff(ctx.author, roles["staff_role_id"]) and not is_family(ctx.author, roles["family_role_id"]) and not is_admin_or_owner(ctx.author):
        await ctx.reply("Staff cannot withdraw. Ask family/admin if needed.")
        return

    parsed = parse_amount(amount)
    ok = store.user_withdraw(ctx.guild.id, ctx.author.id, parsed)
    if not ok:
        await ctx.reply("Insufficient balance.")
        return

    await ctx.reply(f"Withdrew **{parsed} LTC** from your wallet.")


@bot.command(name="tip")
@commands.guild_only()
async def tip(ctx: commands.Context, user: discord.Member, amount: str) -> None:
    parsed = parse_amount(amount)
    if user.id == ctx.author.id:
        await ctx.reply("You cannot tip yourself.")
        return

    store.ensure_guild(ctx.guild.id)
    ok = store.transfer_user_to_user(ctx.guild.id, ctx.author.id, user.id, parsed)
    if not ok:
        await ctx.reply("Insufficient balance.")
        return

    await ctx.reply(f"{ctx.author.mention} tipped {user.mention} **{parsed} LTC**")


@bot.command(name="servertip")
@commands.guild_only()
async def server_tip(ctx: commands.Context, user: discord.Member, amount: str) -> None:
    assert isinstance(ctx.author, discord.Member)
    store.ensure_guild(ctx.guild.id)
    roles = store.get_roles(ctx.guild.id)

    if not (is_admin_or_owner(ctx.author) or is_staff(ctx.author, roles["staff_role_id"]) or is_family(ctx.author, roles["family_role_id"])):
        await ctx.reply("Only admin/staff/family can tip from server wallet.")
        return

    parsed = parse_amount(amount)
    ok = store.transfer_server_to_user(ctx.guild.id, ctx.author.id, user.id, parsed)
    if not ok:
        await ctx.reply("Insufficient server wallet balance.")
        return

    await ctx.reply(f"Server wallet tipped {user.mention} **{parsed} LTC** (by {ctx.author.mention}).")


@bot.command(name="recommended")
async def recommended_commands(ctx: commands.Context) -> None:
    await ctx.reply(
        "Recommended tip.cc-like commands implemented here: "
        "`.bal`, `.deposit`, `.withdraw`, `.tip`, `.serverbal`, `.serverdeposit`, `.serverwithdraw`, `.servertip`, `.setuproles`."
    )


if __name__ == "__main__":
    if not TOKEN:
        raise RuntimeError("Set DISCORD_BOT_TOKEN env var before running.")
    bot.run(TOKEN)
