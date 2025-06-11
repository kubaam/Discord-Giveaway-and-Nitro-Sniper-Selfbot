#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Giveaway & Nitro Sniper Self-Bot

Using discord.py-self v2.0.1+. This build includes a monkey patch that safely
ignores ``application: None`` in ``THREAD_LIST_SYNC`` events.
"""

import os
import sys
import time
import signal
import asyncio
import json
import orjson
import re
import aiohttp
import discord
import base64
import datetime
import logging
import random
import secrets
import subprocess
import aiofiles
from discord.ext import commands
from config_loader import Config, BASE_DIR
from logging.handlers import RotatingFileHandler
import argparse
from typing import (
    Any,
    Coroutine,
    Dict,
    List,
    Optional,
    Set,
)
import platform
from collections import defaultdict

# ----------------------------
# Monkey-patch Message to drop None application fields
# ----------------------------
from discord.message import Message as _Message


_orig_message_init = _Message.__init__


def _patched_message_init(self, *args, **kwargs):
    """Drop ``application: None`` keys from the raw payload to avoid errors."""

    # Locate the payload dict (usually kwargs['data'] or args[2])
    payload = None
    if "data" in kwargs and isinstance(kwargs["data"], dict):
        payload = kwargs["data"]
    elif len(args) >= 3 and isinstance(args[2], dict):
        payload = args[2]
    # Remove application if explicitly None
    if payload is not None and payload.get("application") is None:
        payload.pop("application", None)
    # Call original initializer
    return _orig_message_init(self, *args, **kwargs)


_Message.__init__ = _patched_message_init

# ----------------------------
# Global Constants & Paths
# ----------------------------
LOG_RESET_INTERVAL = 10  # seconds to reset identical-msg counters
RATE_LIMIT_THRESHOLD = 3  # max prints per identical msg per interval
WEBHOOK_RATE_LIMIT_INTERVAL = 1  # sec between webhook posts

TRIED_CODES_PATH = os.path.join(BASE_DIR, "tried-nitro-codes.txt")
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")

# Discord API error codes for lookup
ERROR_CODES: Dict[int, str] = {
    10001: "Unknown Account",
    10002: "Unknown Application",
    10003: "Unknown Channel",
    10004: "Unknown Guild",
    10005: "Unknown Integration",
    10006: "Unknown Invite",
    10007: "Unknown Member",
    10008: "Unknown Message",
    10009: "Unknown Permission Overwrite",
    10010: "Unknown Provider",
    10011: "Unknown Role",
    10012: "Unknown Token",
    10013: "Unknown User",
    10014: "Unknown Emoji",
    10015: "Unknown Webhook",
    10016: "Unknown Webhook Service",
    10017: "Unknown Connection",
    10020: "Unknown Session",
    10021: "Unknown Asset",
    10023: "Unknown approval form",
    10026: "Unknown Ban",
    10027: "Unknown SKU",
    10028: "Unknown Store Listing",
    10029: "Unknown Entitlement",
    10030: "Unknown Build",
    10031: "Unknown Lobby",
    10032: "Unknown Branch",
    10033: "Unknown Store Directory Layout",
    10035: "Unknown Price Tier",
    10036: "Unknown Redistributable",
    10038: "Unknown Gift Code",
    10039: "Unknown Team",
    40001: "Unauthorized",
    40003: "You are opening direct messages too fast",
    50008: "Cannot send messages in a non-text channel",
    50012: "Invalid OAuth State",
    50013: "Missing Permissions",
    50014: "Invalid authentication token",
    50050: "This gift has been redeemed already.",
    50054: "Cannot self-redeem this gift",
    60003: "Two-factor is required for this operation",
}


class RateLimitError(Exception):
    """Raised when the API responds with HTTP 429."""

    retry_after: float

    def __init__(self, retry_after: float) -> None:
        super().__init__(f"Rate limited for {retry_after}s")
        self.retry_after = retry_after


# ----------------------------
# Rate-limited Logger
# ----------------------------
class AppLogger:
    """Helper to throttle repeated log messages and avoid spam."""

    def __init__(self) -> None:
        self.counters: Dict[str, int] = defaultdict(int)
        self.last_reset = time.monotonic()
        self.lock = asyncio.Lock()

    async def rate_limited_log(
        self,
        msg: str,
        notify_everyone: bool = False,
        save: bool = False,
        level: int = logging.INFO,
    ) -> None:
        async with self.lock:
            now = time.monotonic()
            if now - self.last_reset > LOG_RESET_INTERVAL:
                self.counters.clear()
                self.last_reset = now
            self.counters[msg] += 1
            if self.counters[msg] <= RATE_LIMIT_THRESHOLD:
                await self._log(msg, notify_everyone, save, level)

    async def _log(self, msg: str, notify: bool, save: bool, level: int) -> None:
        text = f"[!] {msg}"
        if notify:
            text = "@everyone " + text
        if save:
            logging.log(level, text)
        print(text)


app_logger = AppLogger()


# ----------------------------
# Webhook Notifier
# ----------------------------
class WebhookNotifier:
    def __init__(self) -> None:
        self.last_time = 0.0
        self.lock = asyncio.Lock()

    async def _send(
        self, payload: dict, session: aiohttp.ClientSession, url: str
    ) -> None:
        async with self.lock:
            now = time.monotonic()
            diff = now - self.last_time
            if diff < WEBHOOK_RATE_LIMIT_INTERVAL:
                await asyncio.sleep(WEBHOOK_RATE_LIMIT_INTERVAL - diff)
            self.last_time = time.monotonic()
        try:
            async with session.post(url, json=payload) as resp:
                if resp.status not in (200, 204):
                    txt = await resp.text()
                    await app_logger.rate_limited_log(
                        f"Webhook failed {resp.status}: {txt[:200]}",
                        save=True,
                        level=logging.ERROR,
                    )
        except aiohttp.ClientError as e:
            await app_logger.rate_limited_log(
                f"Webhook error: {e}", save=True, level=logging.ERROR
            )

    async def send(
        self,
        title: str,
        description: str,
        config: "Config",
        session: aiohttp.ClientSession,
        content: str = "",
        color: int = 0xFF8C7E,
        footer: str = "Giveaway Sniper",
        avatar_url: str = "https://i.imgur.com/44N46up.gif",
    ) -> None:
        if not (config.webhook_notification and config.webhook):
            return
        payload = {
            "content": content,
            "embeds": [
                {
                    "title": title,
                    "description": description,
                    "color": color,
                    "footer": {"text": footer},
                }
            ],
            "username": "Giveaway Sniper",
            "avatar_url": avatar_url,
        }
        await self._send(payload, session, config.webhook)


webhook_notifier = WebhookNotifier()


# ----------------------------
# Configuration Loading (initialized in main)
# ----------------------------

config: Config

# ----------------------------
# Concurrency Control
# ----------------------------
nitro_semaphore: asyncio.Semaphore = asyncio.Semaphore(1)


def update_concurrency() -> None:
    global nitro_semaphore
    max_snipes = int(config.nitro_settings.get("max_snipes", 5))
    nitro_semaphore = asyncio.Semaphore(max_snipes)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Discord Giveaway & Nitro Sniper")
    parser.add_argument(
        "-c", "--config", default=CONFIG_PATH, help="Path to config JSON"
    )
    parser.add_argument("-t", "--token", help="Override token from config")
    return parser.parse_args()


# ----------------------------
# Reload config on SIGHUP
# ----------------------------
def _reload(sig=None, frame=None):
    global config
    try:
        config = Config.load(CONFIG_PATH)
        update_concurrency()
        update_retry_settings()
        asyncio.create_task(
            app_logger.rate_limited_log("Config reloaded", level=logging.INFO)
        )
    except Exception as e:
        asyncio.create_task(
            app_logger.rate_limited_log(f"Reload error: {e}", level=logging.ERROR)
        )


if hasattr(signal, "SIGHUP"):
    signal.signal(signal.SIGHUP, _reload)

# ----------------------------
# HTTP Session Factory
# ----------------------------
http_session: Optional[aiohttp.ClientSession] = None


async def get_session() -> aiohttp.ClientSession:
    global http_session
    if http_session is None or http_session.closed:
        timeout = float(config.nitro_settings.get("request_timeout", 10))
        http_session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=timeout),
            trust_env=True,
            json_serialize=lambda v: orjson.dumps(v).decode(),
        )
    return http_session


# ----------------------------
# Nitro Codes Persistence
# ----------------------------
async def load_used_codes() -> Set[str]:
    if not os.path.exists(TRIED_CODES_PATH):
        return set()
    try:
        async with aiofiles.open(TRIED_CODES_PATH, "rb") as f:
            data = await f.read()
            return set(orjson.loads(data) or [])
    except Exception as e:
        await app_logger.rate_limited_log(f"Load codes error: {e}", level=logging.ERROR)
        return set()


async def save_used_codes(codes: Set[str]) -> None:
    try:
        async with aiofiles.open(TRIED_CODES_PATH, "wb") as f:
            await f.write(orjson.dumps(sorted(codes), option=orjson.OPT_INDENT_2))
    except Exception as e:
        await app_logger.rate_limited_log(f"Save codes error: {e}", level=logging.ERROR)


USED_CODES: Set[str] = set()


# ----------------------------
# Utilities
# ----------------------------
def clear_console() -> None:
    cmd = "cls" if platform.system() == "Windows" else "clear"
    try:
        subprocess.run([cmd], check=False)
    except FileNotFoundError:
        pass


async def restart_script() -> None:
    await app_logger.rate_limited_log("Restarting...", save=True, level=logging.WARNING)
    try:
        os.execv(sys.executable, [sys.executable] + sys.argv)
    except Exception as e:
        await app_logger.rate_limited_log(
            f"Restart failed: {e}", save=True, level=logging.CRITICAL
        )
        sys.exit(1)


def task_error_handler(task: asyncio.Task) -> None:
    try:
        if err := task.exception():
            asyncio.create_task(
                app_logger.rate_limited_log(f"Task error: {err}", level=logging.ERROR)
            )
    except asyncio.CancelledError:
        pass


def create_task_safe(coro: Coroutine[Any, Any, Any]) -> asyncio.Task:
    t = asyncio.create_task(coro)
    t.add_done_callback(task_error_handler)
    return t


async def human_sleep(base: float, jitter: float = 0.2) -> None:
    delay = random.SystemRandom().uniform(base * (1 - jitter), base * (1 + jitter))
    await asyncio.sleep(delay)


def random_super_properties() -> str:
    props = {
        "os": secrets.choice(["Windows", "Linux", "Mac OS X"]),
        "browser": "Chrome",
        "device": "",
        "browser_user_agent": (
            secrets.choice(config.user_agents) if config.user_agents else "Mozilla/5.0"
        ),
        "client_build_number": secrets.randbelow(10000) + 10000,
        "release_channel": "stable",
    }
    raw = orjson.dumps(props)
    return base64.b64encode(raw).decode()


def random_headers(token: str) -> Dict[str, str]:
    headers = {
        "Authorization": token,
        "User-Agent": (
            secrets.choice(config.user_agents) if config.user_agents else "Mozilla/5.0"
        ),
        "X-Super-Properties": random_super_properties(),
        "Content-Type": "application/json",
    }
    if config.device_ids:
        headers["X-Fingerprint"] = secrets.choice(config.device_ids)
    return headers


def extract_embed_text(embed: discord.Embed) -> str:
    d = embed.to_dict()
    parts = [d.get("title", ""), d.get("description", "")]
    parts += [f"{f['name']}\n{f['value']}" for f in d.get("fields", [])]
    return "\n".join(p for p in parts if p).lower()


def extract_first_button(message: discord.Message) -> Optional[Any]:
    for row in getattr(message, "components", []):
        for component in getattr(row, "children", []):
            if getattr(component, "type", None) == discord.ComponentType.button:
                return component
    return None


def contains_blacklisted(text: str, blacklist: List[str]) -> bool:
    tl = (text or "").lower()
    return any(term.lower() in tl for term in blacklist)


# ----------------------------
# Bot Setup
# ----------------------------
client = commands.Bot(command_prefix=";", help_command=None, self_bot=True)


def is_blacklisted(user_id: int) -> bool:
    return (client.user and user_id == client.user.id) or str(
        user_id
    ) in config.bot_blacklist


# ----------------------------
# Nitro Redemption with retries
# ----------------------------
NITRO_MAX_RETRIES = 3


def update_retry_settings() -> None:
    global NITRO_MAX_RETRIES
    NITRO_MAX_RETRIES = int(config.nitro_settings.get("max_retries", 3))


async def redeem_nitro_code(token: str, code: str) -> None:
    url = f"https://discord.com/api/v10/entitlements/gift-codes/{code}/redeem"
    headers = random_headers(token)
    session = await get_session()

    start = datetime.datetime.utcnow()
    attempt = 0
    while attempt < NITRO_MAX_RETRIES:
        attempt += 1
        async with nitro_semaphore:
            async with session.post(url, headers=headers, json={}) as resp:
                if resp.status == 429:
                    data = await resp.json()
                    retry_after = data.get("retry_after", 1)
                    await human_sleep(retry_after)
                    if attempt >= NITRO_MAX_RETRIES:
                        raise RateLimitError(retry_after)
                    continue

                try:
                    data = await resp.json()
                except Exception:
                    await app_logger.rate_limited_log(
                        f"JSON parse error for {code}", level=logging.ERROR
                    )
                    status = "Parse error"
                else:
                    msg = data.get("message", "").lower()
                    ec = data.get("code")
                    if 200 <= resp.status < 300:
                        status = "Successfully redeemed"
                    elif ec == 10038 or "unknown gift" in msg:
                        status = "Invalid code"
                    elif ec == 50050 or "already redeemed" in msg:
                        status = "Already redeemed"
                    else:
                        status = f"Unexpected ({ec}): {msg}"
                break
    elapsed = (datetime.datetime.utcnow() - start).total_seconds()
    await nitro_info(elapsed, code, status)


async def nitro_info(elapsed: float, code: str, status: str) -> None:
    await app_logger.rate_limited_log(f"Nitro {status} in {elapsed:.3f}s — {code}")
    session = await get_session()
    await webhook_notifier.send(
        title="🔑 Nitro Status",
        description=f"**Time:** `{elapsed:.3f}s`\n**Code:** `{code}`\n**Status:** {status}",
        config=config,
        session=session,
        content="@everyone" if "success" in status.lower() else "",
        color=0x3498DB,
        footer="Nitro Sniper",
    )


async def check_nitro_codes(message: discord.Message) -> None:
    codes = re.findall(
        r"(?:discord(?:\.gift|\.com/gifts|\.app\.com/gifts)/)([A-Za-z0-9]+)",
        message.content or "",
    )
    new = {c for c in codes if 16 <= len(c) <= 24 and c not in USED_CODES}
    if not new:
        return
    USED_CODES.update(new)
    await save_used_codes(USED_CODES)
    for c in new:
        create_task_safe(redeem_nitro_code(config.token, c))


# ----------------------------
# Giveaway Sniping
# ----------------------------
async def giveaway_info(message: discord.Message, action: str) -> None:
    guild = message.guild.name if message.guild else "DM"
    chan = message.channel.name if message.channel else "DM"
    host = message.author.name
    jump = getattr(message, "jump_url", "")
    notify = action.lower() == "won"

    await app_logger.rate_limited_log(
        f"Giveaway {action} on {guild}/{chan}", notify_everyone=notify
    )

    desc = (
        f"**Action:** {action}\n"
        f"**Server:** {guild}\n"
        f"**Channel:** {chan}\n"
        f"**Host:** {host}\n\n"
        f"[Jump to message]({jump})"
    )
    session = await get_session()
    await webhook_notifier.send(
        title="🏆 Giveaway Win" if notify else "🎁 Giveaway Sniped",
        description=desc,
        config=config,
        session=session,
        content="@everyone" if notify else "",
        color=0x2ECC71 if notify else 0xF1C40F,
        footer="Giveaway Sniper",
    )


async def handle_giveaway_reaction(message: discord.Message) -> None:
    if message.webhook_id or (client.user and message.author.id == client.user.id):
        return
    if contains_blacklisted(message.content or "", config.giveaway_blacklist):
        return
    if any(
        contains_blacklisted(extract_embed_text(e), config.giveaway_blacklist)
        for e in message.embeds
    ):
        return

    await asyncio.sleep(random.SystemRandom().uniform(30, 60))
    try:
        btn = extract_first_button(message)
        if btn:
            await btn.click()
            await giveaway_info(message, "Clicked")
        else:
            await message.add_reaction("🎉")
            await giveaway_info(message, "Reacted")
    except discord.HTTPException as e:
        if e.code not in (50013, 10008):
            await app_logger.rate_limited_log(
                f"Giveaway HTTP error: {e}", level=logging.ERROR
            )
    except Exception as e:
        await app_logger.rate_limited_log(f"Giveaway error: {e}", level=logging.ERROR)


async def check_giveaway_message(message: discord.Message) -> None:
    keywords = [
        "giveaway",
        "**giveaway**",
        "ends at",
        "ends:",
        "hosted by",
        ":gift:",
        ":tada:",
        "🎉",
        "winners:",
        "entries:",
    ]
    content = (message.content or "").lower()
    embeds = [extract_embed_text(e) for e in message.embeds]
    if any(k in content for k in keywords) or any(
        k in em for em in embeds for k in keywords
    ):
        create_task_safe(handle_giveaway_reaction(message))


async def detect_giveaway_win(message: discord.Message) -> None:
    if not (client.user and message.guild and message.author):
        return
    win_keywords = ["won", "winner", "congratulations", "victory", "congrats"]
    # message.content may be None for certain system messages
    content = (message.content or "").lower()
    mentioned = (
        any(m.id == client.user.id for m in message.mentions)
        or f"<@{client.user.id}>" in content
        or f"<@!{client.user.id}>" in content
    )
    if mentioned and any(k in content for k in win_keywords):
        await giveaway_info(message, "Won")

        host_user = None
        for embed in message.embeds:
            for field in getattr(embed, "fields", []):
                if "host" in field.name.lower():
                    m = re.search(r"<@!?(\d+)>", field.value)
                    if m:
                        uid = int(m.group(1))
                        host_user = message.guild.get_member(
                            uid
                        ) or await client.fetch_user(uid)
                    break
            if host_user:
                break

        if host_user and not host_user.bot and config.dm_message:
            try:
                await host_user.send(config.dm_message)
                await app_logger.rate_limited_log(f"DM sent to host {host_user}")
            except Exception as e:
                await app_logger.rate_limited_log(
                    f"DM host failed: {e}", level=logging.ERROR
                )


# ----------------------------
# Bot Event Handlers
# ----------------------------
@client.event
async def on_ready() -> None:
    global USED_CODES
    session = await get_session()
    USED_CODES = await load_used_codes()
    await app_logger.rate_limited_log(
        f"Ready as {client.user} in {len(client.guilds)} guilds", level=logging.INFO
    )
    await webhook_notifier.send(
        title="✅ Bot Connected",
        description=f"**User:** `{client.user}`\n**ID:** `{client.user.id}`",
        config=config,
        session=session,
        color=0x2ECC71,
        footer=f"discord.py-self {discord.__version__}",
    )


@client.event
async def on_message(message: discord.Message) -> None:
    if message.author == client.user or is_blacklisted(message.author.id):
        return
    create_task_safe(check_nitro_codes(message))
    if message.author.bot:
        create_task_safe(check_giveaway_message(message))
        create_task_safe(detect_giveaway_win(message))
    await client.process_commands(message)


@client.event
async def on_disconnect() -> None:
    if http_session and not http_session.closed:
        await http_session.close()
    await app_logger.rate_limited_log("HTTP session closed", level=logging.INFO)


# ----------------------------
# Logging Configuration
# ----------------------------
formatter = logging.Formatter(
    "%(asctime)s - %(levelname)s - %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
)
file_handler = RotatingFileHandler(
    os.path.join(BASE_DIR, "logs.txt"),
    maxBytes=5 * 1024 * 1024,
    backupCount=5,
    encoding="utf-8",
)
file_handler.setFormatter(formatter)
console_handler = logging.StreamHandler(sys.stdout)
console_handler.setFormatter(formatter)

logging.basicConfig(
    level=logging.INFO, handlers=[file_handler, console_handler], encoding="utf-8"
)


# ----------------------------
# Main Entry Point
# ----------------------------
def main() -> None:
    args = parse_args()
    global CONFIG_PATH, config
    CONFIG_PATH = args.config
    config = Config.load(CONFIG_PATH)
    if args.token:
        config.token = args.token
    else:
        config.token = os.getenv("DISCORD_TOKEN", config.token)
    update_concurrency()
    update_retry_settings()
    if platform.system() == "Windows":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    try:
        client.run(config.token, reconnect=True)
    except discord.LoginFailure:
        asyncio.run(
            app_logger.rate_limited_log(
                "Invalid token", save=True, level=logging.CRITICAL
            )
        )
        sys.exit(1)
    except Exception as e:
        asyncio.run(
            app_logger.rate_limited_log(
                f"Fatal: {e}", save=True, level=logging.CRITICAL
            )
        )
        asyncio.run(restart_script())


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except AttributeError:
        pass
    main()
