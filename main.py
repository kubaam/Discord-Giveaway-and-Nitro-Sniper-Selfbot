import os
import sys
import asyncio
import json
import re
import aiohttp
import discord
import datetime
import logging
import random
import aiofiles
import dataclasses
from discord.ext import commands
from tenacity import retry, stop_after_attempt, wait_exponential
from logging.handlers import RotatingFileHandler
from typing import Optional, Dict, List, Union
import platform
from collections import defaultdict

###################################################################
# Logging Setup
###################################################################

LOG_RESET_INTERVAL = 10  # seconds
log_counters = defaultdict(int)
last_reset_time = datetime.datetime.now()
log_lock = asyncio.Lock()

def _current_time() -> datetime.datetime:
    return datetime.datetime.now()

async def rate_limited_log(content: str, notify_everyone: bool = False, save: bool = False, level: int = logging.INFO):
    """
    Logs a message up to 3 times within LOG_RESET_INTERVAL seconds.
    Prevents log spamming.
    """
    global last_reset_time
    async with log_lock:
        now = _current_time()
        if (now - last_reset_time).total_seconds() > LOG_RESET_INTERVAL:
            log_counters.clear()
            last_reset_time = now

        log_counters[content] += 1
        if log_counters[content] <= 3:
            await _log(content, notify_everyone, save, level)

async def _log(content: str, notify_everyone: bool = False, save: bool = False, level: int = logging.INFO):
    """
    Formats and outputs a log message.
    """
    formatted_content = f"[!] LOG: {content}"
    if save:
        logging.log(level, formatted_content)
    if notify_everyone:
        formatted_content = f"@everyone {formatted_content}"
    print(formatted_content)

###################################################################
# Configuration
###################################################################

@dataclasses.dataclass
class Config:
    """Holds the user's configuration settings, loaded from a JSON file."""
    token: str
    webhook: Optional[str]
    bot_blacklist: List[str]
    webhook_notification: bool
    user_agents: List[str]
    device_ids: List[str]
    nitro_settings: Dict[str, Union[int, float]]
    giveaway_blacklist: List[str]  # <<-- NEW: list of blacklisted words for giveaways

    @staticmethod
    def load_config(filepath: str = "config.json") -> "Config":
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                data = json.load(f)

            return Config(
                token=data.get("Token", [None])[0],
                webhook=data.get("Webhook"),
                bot_blacklist=data.get("BotBlacklist", []),
                webhook_notification=data.get("WebhookNotification", True),
                user_agents=data.get("UserAgents", []),
                device_ids=data.get("DeviceIds", []),
                nitro_settings=data.get("NitroSettings", {}),
                giveaway_blacklist=data.get("GiveawayBlacklist", [])  # load from config
            )

        except (FileNotFoundError, json.JSONDecodeError) as e:
            _log(f"Configuration file error: {e}. Exiting...", save=True, level=logging.ERROR)
            sys.exit(1)

config = Config.load_config()

###################################################################
# Logging Handlers
###################################################################

log_formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s', 
                                  datefmt='%Y-%m-%d %H:%M:%S')
log_file_handler = RotatingFileHandler(
    'logs.txt',
    maxBytes=5 * 1024 * 1024,
    backupCount=5,
    encoding='utf-8'
)
log_file_handler.setFormatter(log_formatter)
logging.basicConfig(level=logging.INFO, handlers=[log_file_handler])

###################################################################
# Global Constants (from config)
###################################################################
ALL_USER_AGENTS = config.user_agents
DEVICE_IDS = config.device_ids
NITRO_SETTINGS = config.nitro_settings
GIVEAWAY_BLACKLIST = config.giveaway_blacklist  # convenience

###################################################################
# Utility Functions
###################################################################

def clear_console():
    """Clears the console, Windows or POSIX."""
    os.system("cls" if os.name == "nt" else "clear")

async def restart_script():
    """
    Attempts to restart the script by re-invoking sys.executable with the current sys.argv.
    If unsuccessful, exits the program.
    """
    await rate_limited_log("Restarting script due to hard error...", save=True)
    try:
        os.execv(sys.executable, [sys.executable] + sys.argv)
    except OSError as e:
        await rate_limited_log(f"Failed to restart script: {e}", save=True, level=logging.ERROR)
        sys.exit(1)

def is_blacklisted(user_id: int, client: discord.Client) -> bool:
    return str(user_id) in config.bot_blacklist or (client.user and user_id == client.user.id)

###################################################################
# Discord Client Setup (Self-Bot)
###################################################################

client = commands.Bot(command_prefix=";", help_command=None, self_bot=True)

###################################################################
# Webhook Notification
###################################################################

async def send_webhook_notification(
    title: str,
    description: str,
    content: str = "",
    color: int = 0xFF8C7E,
    footer_text: str = "Giveaway Sniper",
    avatar_url: str = "https://i.imgur.com/44N46up.gif"
):
    if config.webhook_notification and config.webhook:
        data = {
            "content": content,
            "embeds": [{
                "title": title,
                "description": description,
                "color": color,
                "footer": {"text": footer_text}
            }],
            "username": "Giveaway Sniper",
            "avatar_url": avatar_url
        }

        async with aiohttp.ClientSession() as session:
            try:
                async with session.post(config.webhook, json=data) as response:
                    if response.status not in (200, 204):
                        await rate_limited_log(
                            f"Failed to send webhook notification: {response.status} - {await response.text()}",
                            save=True, 
                            level=logging.ERROR
                        )
            except aiohttp.ClientError as e:
                await rate_limited_log(
                    f"Failed to send webhook notification (network error): {e}",
                    save=True,
                    level=logging.ERROR
                )

###################################################################
# Info / Notification Senders
###################################################################

async def nitro_info(elapsed: str, code: str, status: str):
    await rate_limited_log(f"Elapsed: {elapsed}, Code: {code}, Status: {status}")
    content = "@everyone" if status.lower() == "successfully redeemed" else ""
    await send_webhook_notification(
        title="🔑 Nitro Code Redemption Status",
        description=(
            f"**Elapsed Time:** `{elapsed}` seconds\n"
            f"**Code:** `{code}`\n"
            f"**Status:** {status}"
        ),
        content=content,
        color=0x3498DB,
        footer_text="Nitro Sniper | Status Update",
        avatar_url="https://i.imgur.com/nitro-icon.png"
    )

async def giveaway_info(message: discord.Message, action: str, location: str, author: str):
    await rate_limited_log(f"Sniped a giveaway! {action} | Location: {location} | Author: {author}")
    await send_webhook_notification(
        title="🎁 Giveaway Sniped!",
        description=(
            f"**Action Taken:** {action}\n"
            f"**Location:** `{location}`\n"
            f"**Hosted by:** {author}\n"
            f"[Click Here to View Message]({message.jump_url})"
        ),
        color=0xF1C40F,
        footer_text="Giveaway Sniper | Action Report",
        avatar_url="https://i.imgur.com/giveaway-icon.png"
    )

async def bot_connected_info(user: discord.User):
    await rate_limited_log(f"Bot successfully connected: {user} (ID: {user.id})")
    await send_webhook_notification(
        title="✅ Bot Connected",
        description=(
            f"The bot is now connected and operational.\n\n"
            f"**User:** `{user}`\n"
            f"**ID:** `{user.id}`"
        ),
        color=0x2ECC71,
        footer_text="Connection Status",
        avatar_url="https://i.imgur.com/connected-icon.png"
    )

###################################################################
# Giveaway Win Detection
###################################################################

async def detect_giveaway_win(message: discord.Message):
    """
    Detects if the self-bot (client.user) has won a giveaway.
    Adjusted to handle <@!ID> mentions, embed fields, and optional mention requirement.
    """
    if not (client.user and message.guild and message.author):
        return

    # Keywords that might indicate a win
    keywords = ["won", "winner", "congratulations", "victory", "congrats"]

    def contains_keywords(text: str) -> bool:
        return any(k in text.lower() for k in keywords)

    content_lower = message.content.lower()

    # Check whether user is mentioned natively
    user_mentioned = (client.user in message.mentions)

    # Fallback string-based mention check in case the API misses certain mention styles
    if not user_mentioned:
        mention_strs = [f"<@{client.user.id}>", f"<@!{client.user.id}>"]
        user_mentioned = any(m in message.content for m in mention_strs)

    mention_required = True

    # Check direct content
    content_detected = contains_keywords(content_lower)
    if mention_required:
        content_detected = content_detected and user_mentioned

    # Check embed(s)
    embed_detected = False
    for e in message.embeds:
        data = e.to_dict()

        embed_text = f"{data.get('title', '')}\n{data.get('description', '')}"
        if "fields" in data:
            for field in data["fields"]:
                embed_text += f"\n{field.get('name', '')}\n{field.get('value', '')}"

        if contains_keywords(embed_text.lower()):
            if mention_required:
                mention_in_embed_text = any(m in embed_text for m in [f"<@{client.user.id}>", f"<@!{client.user.id}>"])
                if user_mentioned or mention_in_embed_text:
                    embed_detected = True
                    break
            else:
                embed_detected = True
                break

    if content_detected or embed_detected:
        location = f"Server: {message.guild.name} | Channel: {message.channel.name}"
        author = message.author.name
        jump_url = message.jump_url

        await rate_limited_log(
            f"Detected Giveaway Win! Location: {location} | Author: {author} [Click Here]({jump_url})",
            notify_everyone=True,
            save=True,
            level=logging.INFO
        )
        await send_webhook_notification(
            title="🏆 Giveaway Win",
            description=(
                f"**Congratulations!** You've won a giveaway!\n"
                f"**Location:** `{location}`\n"
                f"**Hosted by:** {author}\n"
                f"[Click Here to View Winning Message]({jump_url})"
            ),
            content="@everyone",
            color=0x2ECC71,
            footer_text="Giveaway Win | Notification",
            avatar_url="https://i.imgur.com/win-icon.png"
        )

###################################################################
# Nitro Code Sniping
###################################################################

async def check_nitro_codes(message: discord.Message):
    content_lower = message.content.lower()
    if not any(x in content_lower for x in ["discord.gift/", "discordapp.com/gifts/", "discord.com/gifts/"]):
        return

    codes = re.findall(r"discord(?:\.gift|\.com/gifts|\.app\.com/gifts)/([a-zA-Z0-9]+)", message.content)
    tried_codes_path = "tried-nitro-codes.txt"

    if not os.path.exists(tried_codes_path):
        async with aiofiles.open(tried_codes_path, "w", encoding="utf-8") as fp:
            await fp.write("[]")

    async with aiofiles.open(tried_codes_path, "r", encoding="utf-8") as fp:
        try:
            used_codes = json.loads(await fp.read())
        except json.JSONDecodeError:
            used_codes = []

    async with aiofiles.open(tried_codes_path, "w", encoding="utf-8") as fp:
        for code in codes:
            if len(code) in [16, 24] and code not in used_codes:
                used_codes.append(code)
                await fp.write(json.dumps(used_codes))
                try:
                    await redeem_nitro_code(config.token, code)
                except Exception as e:
                    await rate_limited_log(f"Error redeeming Nitro code {code}: {e}", level=logging.ERROR)

###################################################################
# Nitro Redemption Logic
###################################################################

async def redeem_nitro_code(token: str, code: str):
    url = f"https://discord.com/api/v10/entitlements/gift-codes/{code}/redeem"
    headers = {
        "Authorization": token,
        "User-Agent": random.choice(ALL_USER_AGENTS) if ALL_USER_AGENTS else "Mozilla/5.0",
        "X-Super-Properties": random.choice(DEVICE_IDS) if DEVICE_IDS else "",
        "X-Fingerprint": random.choice(DEVICE_IDS) if DEVICE_IDS else "",
        "X-Debug-Options": "bugReporterEnabled",
        "Content-Type": "application/json"
    }

    start_time = _current_time()

    async with aiohttp.ClientSession() as session:
        try:
            async with session.post(url, headers=headers, json={}) as response:
                elapsed_seconds = (_current_time() - start_time).total_seconds()
                elapsed_str = f"{elapsed_seconds:.3f}"

                if response.status == 429:
                    res_json = await response.json()
                    retry_after = int(res_json.get("retry_after", 60))
                    await rate_limited_log(f"Rate limited, retrying in {retry_after} seconds...", level=logging.WARNING)
                    await asyncio.sleep(retry_after)
                    return await redeem_nitro_code(token, code)

                try:
                    res_json = await response.json()
                except Exception as e:
                    await rate_limited_log(f"Failed to parse response JSON: {e}", level=logging.ERROR)
                    return await nitro_info(elapsed_str, code, f"Failed to parse response JSON: {e}")

                if not isinstance(res_json, dict):
                    await rate_limited_log("Unexpected JSON structure received.", level=logging.ERROR)
                    return await nitro_info(elapsed_str, code, "Unexpected JSON structure")

                message_lower = res_json.get("message", "").lower()

                if "unknown gift code" in message_lower:
                    await rate_limited_log(f"Invalid Nitro code: {code}", level=logging.WARNING)
                    await nitro_info(elapsed_str, code, "Invalid Nitro code")

                elif "subscription_plan" in res_json:
                    await rate_limited_log(f"Successfully redeemed Nitro code: {code}", level=logging.INFO)
                    await nitro_info(elapsed_str, code, "Successfully redeemed")

                elif "this gift has been redeemed already" in message_lower:
                    await rate_limited_log(f"Code already redeemed: {code}", level=logging.INFO)
                    await nitro_info(elapsed_str, code, "Code already redeemed")

                elif "retry_after" in res_json:
                    r_wait = float(res_json["retry_after"])
                    await rate_limited_log(f"Rate limited, retrying in {r_wait} seconds...", level=logging.WARNING)
                    await asyncio.sleep(r_wait)
                    return await redeem_nitro_code(token, code)

                else:
                    await rate_limited_log(f"Unexpected response while redeeming Nitro code {code}: {res_json}", level=logging.ERROR)
                    await nitro_info(elapsed_str, code, f"Unexpected response: {res_json}")

        except aiohttp.ClientError as e:
            await rate_limited_log(f"Network error while redeeming Nitro code {code}: {e}", level=logging.ERROR)

###################################################################
# Giveaway Reaction Handling
###################################################################

async def handle_giveaway_reaction(message: discord.Message):
    """
    Handles participation in a giveaway by:
    - Checking for blacklisted words
    - Clicking a giveaway button if available
    - Reacting with the 🎉 emoji if no button is found.
    """

    # 1) Skip reacting if it's a webhook message or own message (no log)
    if message.webhook_id is not None or (client.user and message.author.id == client.user.id):
        return

    # 2) Check for blacklisted words in content or embed
    msg_text = message.content.lower()
    for w in GIVEAWAY_BLACKLIST:
        if w.lower() in msg_text:
            return  # skip entire reaction if blacklisted word is present

    # Also check embed
    for e in message.embeds:
        edata = e.to_dict()
        text_block = (edata.get("title", "") + edata.get("description", "")).lower()
        if "fields" in edata:
            for field in edata["fields"]:
                text_block += (field.get("name", "") + field.get("value", "")).lower()
        for w in GIVEAWAY_BLACKLIST:
            if w.lower() in text_block:
                return  # skip due to blacklisted word

    # 3) Add a random delay for “human-like” timing
    delay = random.uniform(10, 20)
    await asyncio.sleep(delay)

    try:
        location = f"Server: {message.guild.name} | Channel: {message.channel.name}"
        author = message.author.name

        # Check if the message has components (buttons)
        if message.components:
            for row in message.components:
                for component in getattr(row, 'children', []):
                    if (
                        hasattr(component, 'type') and
                        component.type == discord.ComponentType.button and
                        component.style in (
                            discord.ButtonStyle.primary,
                            discord.ButtonStyle.success
                        )
                    ):
                        try:
                            await component.click()
                            await giveaway_info(message, "Clicked Button", location, author)
                            return
                        except discord.HTTPException as e:
                            # Skip if missing permissions (50013) or unknown message (10008)
                            if e.code in (50013, 10008):
                                return
                            await rate_limited_log(f"Error clicking giveaway button: {e}", level=logging.ERROR)

        # Fallback: React with "🎉"
        await message.add_reaction("🎉")
        await giveaway_info(message, "Reacted with Emoji", location, author)

    except discord.HTTPException as e:
        if e.code in (50013, 10008):
            return
        await rate_limited_log(f"HTTP error handling giveaway reaction: {e}", level=logging.ERROR)
    except Exception as e:
        await rate_limited_log(f"Unexpected error handling giveaway reaction: {e}", level=logging.ERROR)

###################################################################
# Giveaway Message Checker
###################################################################

async def check_giveaway_message(message: discord.Message):
    """
    Determines if a message is likely a giveaway and attempts to snipe it.
    First checks blacklisted words, then standard giveaway keywords.
    """
    # 1) Quick skip if blacklisted word is found
    #    (We do a deeper check in handle_giveaway_reaction, but a quick fail here is also okay.)
    text_lower = message.content.lower()
    for w in GIVEAWAY_BLACKLIST:
        if w.lower() in text_lower:
            return  # skip if any blacklisted word is in content

    for e in message.embeds:
        edata = e.to_dict()
        text_block = (edata.get("title", "") + edata.get("description", "")).lower()
        if "fields" in edata:
            for field in edata["fields"]:
                text_block += (field.get("name", "") + field.get("value", "")).lower()
        for w in GIVEAWAY_BLACKLIST:
            if w.lower() in text_block:
                return  # skip if blacklisted

    # 2) Standard giveaway keywords
    giveaway_keywords = [
        "giveaway", "ends at", "hosted by", ":gift:", ":tada:",
        "**giveaway**", "🎉", "winners:", "entries:", "ends:"
    ]

    def contains_keyword(text: str) -> bool:
        return any(keyword in text.lower() for keyword in giveaway_keywords)

    content_or_embed_has_giveaway = (
        contains_keyword(message.content) or
        any(
            contains_keyword(e.to_dict().get("description", "")) or
            contains_keyword(e.to_dict().get("title", "")) or
            any(contains_keyword(v) for v in e.to_dict().values() if isinstance(v, str))
            for e in message.embeds
        )
    )

    if content_or_embed_has_giveaway:
        await handle_giveaway_reaction(message)

###################################################################
# Event: on_ready
###################################################################

@client.event
async def on_ready():
    user = client.user
    await bot_connected_info(user)

###################################################################
# Event: on_message
###################################################################

@client.event
async def on_message(message: discord.Message):
    # 1) Skip messages from ourselves (the self-bot).
    if message.author == client.user:
        return

    # 2) Skip if the user is blacklisted.
    if is_blacklisted(message.author.id, client):
        return

    # 3) Always check Nitro codes (from any user).
    await check_nitro_codes(message)

    # 4) **Only** check giveaways if the message is from a bot.
    if message.author.bot:
        await check_giveaway_message(message)
        await detect_giveaway_win(message)

    # 5) Process commands if using commands.Bot
    await client.process_commands(message)


###################################################################
# Main Entry Point
###################################################################

def main():
    if platform.system() == "Windows":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    try:
        client.run(config.token, reconnect=True)
    except discord.errors.LoginFailure:
        rate_limited_log("Invalid token or 2FA required. Check your config.json.", save=True, level=logging.ERROR)
    except Exception as e:
        rate_limited_log(f"An error occurred while starting the bot: {e}", save=True, level=logging.ERROR)
        asyncio.run(restart_script())

if __name__ == "__main__":
    # Ensure stdout is in UTF-8 mode
    sys.stdout = open(sys.stdout.fileno(), mode="w", encoding="utf-8", buffering=1)
    main()
