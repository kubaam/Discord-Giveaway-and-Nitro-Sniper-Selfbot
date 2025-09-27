# --- Core Libraries ---
import os
import sys
import time
import json
import re
import asyncio
import logging
import random
import base64
import datetime
from collections import defaultdict, deque
from logging.handlers import RotatingFileHandler
from typing import (
    Any,
    Callable,
    Coroutine,
    Dict,
    List,
    Optional,
    Set,
    Tuple,
    Union,
)

# --- Third-Party Libraries ---
# Install required libraries:
# pip install "discord.py-self==2.0.1" aiohttp pydantic tenacity orjson aiofiles multidict
try:
    import aiohttp
    import discord
    import orjson  # High-performance JSON library
    import aiofiles
    from multidict import CIMultiDictProxy
    from discord.ext import commands
    from pydantic import (
        BaseModel,
        Field,
        ValidationError,
        field_validator,
    )
    from tenacity import (
        retry,
        stop_after_attempt,
        wait_exponential,
        retry_if_exception_type,
    )
except ImportError:
    print("One or more required libraries are not installed.")
    print('Please run: pip install "discord.py-self==2.0.1" aiohttp pydantic tenacity orjson aiofiles multidict')
    sys.exit(1)


# ===================================================================================================
# 1. CONFIGURATION & DATA MODELS (Pydantic)
# ===================================================================================================
# Defines the structure of the `config.json` file, ensuring all settings are valid at startup.

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
TRIED_CODES_PATH = os.path.join(BASE_DIR, "tried-nitro-codes.json")
LOG_PATH = os.path.join(BASE_DIR, "sniper.log")


class AccountModel(BaseModel):
    """Configuration for a single Discord account."""
    token: str = Field(..., description="Discord account token.")
    is_main: bool = Field(False, description="Mark one account as main for receiving sniped gifts.")
    is_feeder: bool = Field(False, description="Mark accounts as feeders to snipe but not redeem gifts.")
    proxy_url: Optional[str] = Field(None, description="URL for the proxy to use with this account.")
    user_agent: str = Field(..., description="The User-Agent string for this account's client profile.")
    device_id: str = Field(..., description="The X-Fingerprint/Device ID for this account's profile.")


class NitroSettingsModel(BaseModel):
    """Settings specific to the Nitro sniper."""
    max_concurrent_snipes: int = Field(default=5, ge=1, description="Max parallel Nitro redemption attempts.")
    request_timeout: float = Field(default=8.0, gt=0, description="Timeout in seconds for Nitro HTTP requests.")
    max_retries: int = Field(default=3, ge=1, description="Max retries on transient network errors.")


class GiveawaySettingsModel(BaseModel):
    """Settings specific to the Giveaway sniper."""
    min_delay_sec: float = Field(default=2.5, ge=0, description="Minimum delay before entering a giveaway.")
    max_delay_sec: float = Field(default=7.0, ge=0, description="Maximum delay for entering a giveaway.")
    dm_message: str = Field("", description="Message to send the host upon winning a giveaway.")
    global_blacklist_keywords: List[str] = Field(default=[], description="Keywords that will disqualify a giveaway in any server.")
    server_specific_rules: Dict[int, Dict[str, List[str]]] = Field(
        default={}, description='Server ID-keyed rules, e.g., {"server_id": {"whitelist": ["nitro"], "blacklist": []}}'
    )


class InviteSniperSettingsModel(BaseModel):
    """Settings for the Invite Sniper module."""
    enabled: bool = Field(False, description="Enable/disable the invite sniper.")
    min_member_count: int = Field(default=50, description="Minimum member count to join a server.")
    max_member_count: int = Field(default=50000, description="Maximum member count to join a server.")
    server_blacklist_ids: List[int] = Field(default=[], description="List of server IDs to never join.")
    max_joins_per_hour: int = Field(5, description="Rate limit for joining new servers to avoid detection.")


class ConfigModel(BaseModel):
    """The root configuration model."""
    accounts: List[AccountModel]
    webhook_url: Optional[str] = Field(None, description="Discord webhook for notifications.")
    webhook_notifications: bool = Field(True, description="Master switch for all webhook notifications.")
    bot_author_blacklist: List[int] = Field(default=[], description="List of bot author IDs to ignore messages from.")
    nitro_settings: NitroSettingsModel = Field(default_factory=NitroSettingsModel)
    giveaway_settings: GiveawaySettingsModel = Field(default_factory=GiveawaySettingsModel)
    invite_sniper_settings: InviteSniperSettingsModel = Field(default_factory=InviteSniperSettingsModel)

    @field_validator("accounts")
    def validate_accounts(cls, v: List[AccountModel]) -> List[AccountModel]:
        """Validate account roles to ensure a sane configuration."""
        if not v:
            raise ValueError("Configuration must contain at least one account.")
        main_accounts = sum(1 for acc in v if acc.is_main)
        if main_accounts != 1:
            raise ValueError('Exactly one account must be marked as "is_main: true"')
        return v


# ===================================================================================================
# 2. CORE SERVICES
# ===================================================================================================
# Foundational classes that manage logging, notifications, API requests, and rate limits.

class RateLimitedLogger:
    """A logger that throttles repeated messages to prevent console/log spam."""
    def __init__(self, level=logging.INFO):
        self._logger = logging.getLogger(__name__)
        self._logger.setLevel(level)

        # File Handler
        file_handler = RotatingFileHandler(LOG_PATH, maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8")
        file_handler.setFormatter(
            logging.Formatter("%(asctime)s [%(levelname)-5.5s] --- %(message)s", "%Y-%m-%d %H:%M:%S")
        )
        self._logger.addHandler(file_handler)

        # Console Handler
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setFormatter(logging.Formatter("[%(asctime)s] %(message)s", "%H:%M:%S"))
        self._logger.addHandler(console_handler)

        self.log_counters = defaultdict(int)
        self.last_reset_time = time.monotonic()
        self.lock = asyncio.Lock()
        self.LOG_RESET_INTERVAL = 15  # seconds
        self.RATE_LIMIT_THRESHOLD = 3  # max prints per interval

    async def log(self, msg: str, level: int = logging.INFO, *, suppress_repetition: bool = True) -> None:
        """Logs a message, optionally suppressing it if repeated too frequently."""
        if not suppress_repetition:
            self._logger.log(level, msg)
            return

        async with self.lock:
            now = time.monotonic()
            if now - self.last_reset_time > self.LOG_RESET_INTERVAL:
                self.log_counters.clear()
                self.last_reset_time = now

            self.log_counters[msg] += 1
            if self.log_counters[msg] <= self.RATE_LIMIT_THRESHOLD:
                self._logger.log(level, msg)


logger = RateLimitedLogger()


class WebhookNotifier:
    """Manages sending formatted notifications to a Discord webhook."""
    def __init__(self, config: ConfigModel, session: aiohttp.ClientSession):
        self.config = config
        self.session = session
        self.lock = asyncio.Lock()
        self.last_sent_time = 0.0
        self.WEBHOOK_INTERVAL = 1.2  # seconds between posts

    async def send(self, title: str, description: str, color: int, content: str = "", footer: Optional[str] = None) -> None:
        """Constructs and sends an embed to the configured webhook."""
        if not (self.config.webhook_notifications and self.config.webhook_url):
            return

        payload = {
            "content": content,
            "embeds": [{
                "title": title,
                "description": description,
                "color": color,
                "timestamp": datetime.datetime.utcnow().isoformat(),
            }],
            "username": "Sniper Bot",
            "avatar_url": "https://i.imgur.com/44N46up.gif",
        }
        if footer:
            payload["embeds"][0]["footer"] = {"text": footer}

        async with self.lock:
            # Simple rate limiting for the webhook itself
            now = time.monotonic()
            if (delta := now - self.last_sent_time) < self.WEBHOOK_INTERVAL:
                await asyncio.sleep(self.WEBHOOK_INTERVAL - delta)
            self.last_sent_time = time.monotonic()

            try:
                async with self.session.post(str(self.config.webhook_url), json=payload) as resp:
                    if resp.status not in (200, 204):
                        txt = await resp.text()
                        await logger.log(
                            f"Webhook send failed with status {resp.status}: {txt[:200]}",
                            logging.ERROR,
                        )
            except aiohttp.ClientError as e:
                await logger.log(f"Webhook client error: {e}", logging.ERROR)


class ClientProfileManager:
    """Generates and manages consistent client profiles for stealth."""
    def __init__(self, account: AccountModel):
        self.account = account
        self.super_properties = self._generate_super_properties()

    def _generate_super_properties(self) -> str:
        """Generates a consistent X-Super-Properties header."""
        # This should be derived from a realistic client build
        properties = {
            "os": "Windows", "browser": "Chrome", "device": "", "system_locale": "en-US",
            "browser_user_agent": self.account.user_agent,
            "browser_version": "117.0.0.0",  # Example, should be consistent with UA
            "os_version": "10", "referrer": "", "referring_domain": "", "referrer_current": "",
            "referring_domain_current": "", "release_channel": "stable",
            "client_build_number": 202930,  # Example, find a recent one
            "client_event_source": None,
        }
        encoded = orjson.dumps(properties)
        return base64.b64encode(encoded).decode("utf-8")

    def get_headers(self) -> Dict[str, str]:
        """Returns a complete, consistent set of headers for an API request."""
        return {
            "Authorization": self.account.token, "Accept": "*/*", "Accept-Language": "en-US",
            "Connection": "keep-alive", "Content-Type": "application/json",
            "Sec-Fetch-Dest": "empty", "Sec-Fetch-Mode": "cors", "Sec-Fetch-Site": "same-origin",
            "User-Agent": self.account.user_agent, "X-Super-Properties": self.super_properties,
            "X-Fingerprint": self.account.device_id, "X-Discord-Locale": "en-US",
        }


class RateLimitGovernor:
    """Proactively manages rate limits for all API endpoints."""
    def __init__(self):
        self.buckets: Dict[str, Tuple[asyncio.Lock, float, int]] = {}
        self.global_lock = asyncio.Lock()
        self.global_reset = 0.0
        self.lock = asyncio.Lock()

    async def wait_for_bucket(self, bucket_id: str) -> None:
        """Pauses execution if the specified rate limit bucket is exhausted."""
        async with self.global_lock:
            now = time.monotonic()
            if self.global_reset > now:
                await asyncio.sleep(self.global_reset - now)

        async with self.lock:
            if bucket_id not in self.buckets:
                self.buckets[bucket_id] = (asyncio.Lock(), 0.0, 1)
            bucket_lock, reset_at, remaining = self.buckets[bucket_id]

        async with bucket_lock:
            now = time.monotonic()
            if remaining < 1 and reset_at > now:
                await logger.log(
                    f"Rate limit on bucket {bucket_id} hit. Sleeping for {reset_at - now:.2f}s", logging.WARNING,
                )
                await asyncio.sleep(reset_at - now)

    async def update_bucket(self, bucket_id: Optional[str], headers: CIMultiDictProxy) -> None:
        """Updates a bucket's state from API response headers."""
        if not bucket_id:
            return

        if headers.get("X-RateLimit-Global"):
            retry_after = float(headers.get("Retry-After", 1.0))
            async with self.global_lock:
                self.global_reset = time.monotonic() + retry_after
            return

        try:
            remaining = int(headers.get("X-RateLimit-Remaining", 1))
            reset_after = float(headers.get("X-RateLimit-Reset-After", 0))
        except (ValueError, TypeError):
            return

        async with self.lock:
            if bucket_id not in self.buckets:
                self.buckets[bucket_id] = (asyncio.Lock(), 0.0, 1)

            bucket_lock, _, _ = self.buckets[bucket_id]
            self.buckets[bucket_id] = (bucket_lock, time.monotonic() + reset_after, remaining)


# ===================================================================================================
# 3. API WORKER & QUEUE
# ===================================================================================================

class APIJob:
    """Represents a job to be executed by an APIConsumer."""
    def __init__(self, priority: int, coro: Coroutine, account_token: str, metadata: Dict[str, Any]):
        self.priority = priority
        self.coro = coro
        self.account_token = account_token
        self.metadata = metadata

    def __lt__(self, other):
        return self.priority < other.priority


class APIConsumer:
    """A worker that pulls jobs from the queue and executes them safely."""
    def __init__(
        self, queue: asyncio.PriorityQueue, governor: RateLimitGovernor,
        profile_managers: Dict[str, ClientProfileManager], session: aiohttp.ClientSession,
        webhook: WebhookNotifier, config: ConfigModel
    ):
        self.queue = queue
        self.governor = governor
        self.profile_managers = profile_managers
        self.session = session
        self.webhook = webhook
        self.config = config
        self.running = True

    async def run(self):
        """The main loop for the consumer worker."""
        await logger.log(f"API Consumer worker {id(self)} started.")
        while self.running:
            try:
                job: APIJob = await self.queue.get()
                await self.process_job(job)
                self.queue.task_done()
            except asyncio.CancelledError:
                self.running = False
                break
            except Exception as e:
                await logger.log(
                    f"Critical error in consumer worker: {e}", logging.CRITICAL, suppress_repetition=False
                )

    async def process_job(self, job: APIJob):
        """Handles a single job, including rate limiting and error handling."""
        endpoint = job.metadata.get("endpoint")
        if not endpoint:
            await logger.log(f"Job has no endpoint metadata: {job.metadata}", logging.ERROR)
            return

        profile = self.profile_managers.get(job.account_token)
        if not profile:
            await logger.log(
                f"No client profile found for token ending in ...{job.account_token[-4:]}", logging.ERROR,
            )
            return

        await self.governor.wait_for_bucket(endpoint)

        try:
            response = await job.coro(self.session, profile.get_headers())
            bucket_id = response.headers.get("X-RateLimit-Bucket")
            await self.governor.update_bucket(bucket_id, response.headers)
            await self.handle_response(response, job.metadata)
        except aiohttp.ClientError as e:
            await logger.log(f"HTTP Client Error for {endpoint}: {e}", logging.ERROR)
        except asyncio.TimeoutError:
            await logger.log(f"Request timeout for {endpoint}", logging.ERROR)
        except Exception as e:
            await logger.log(
                f"Unexpected error processing job for {endpoint}: {e}", logging.ERROR, suppress_repetition=False
            )

    async def handle_response(self, response: aiohttp.ClientResponse, metadata: Dict[str, Any]):
        """Processes the API response based on the job's metadata."""
        job_type = metadata.get("type")
        if job_type == "nitro_redeem":
            await self.handle_nitro_response(response, metadata)
        elif job_type == "giveaway_interact":
            await self.handle_giveaway_response(response, metadata)
        elif job_type == "invite_join":
            await self.handle_invite_response(response, metadata)

    async def handle_nitro_response(self, response: aiohttp.ClientResponse, metadata: Dict[str, Any]):
        """Specific logic for handling Nitro redemption responses."""
        code, start_time = metadata["code"], metadata["start_time"]
        elapsed = time.monotonic() - start_time
        status, color = "Failed", 0xE74C3C  # Red

        if 200 <= response.status < 300:
            status, color = "Successfully redeemed!", 0x2ECC71  # Green
            await logger.log(f"SUCCESSFULLY REDEEMED NITRO: {code} in {elapsed:.3f}s", logging.INFO, suppress_repetition=False)
        else:
            try:
                data = await response.json(loads=orjson.loads)
                message = data.get("message", "No message.")
                if "unknown gift code" in message.lower(): status = "Invalid Code"
                elif "already been redeemed" in message.lower(): status = "Already Redeemed"
                else: status = f"Failed ({response.status}): {message}"
                await logger.log(f"Nitro snipe for {code} failed: {status}", logging.INFO)
            except Exception:
                status = f"Failed with status {response.status} (non-JSON response)"
                await logger.log(f"Nitro snipe for {code} failed: {status}", logging.WARN)

        await self.webhook.send(
            title="🔑 Nitro Snipe Result",
            description=f"**Code:** `{code}`\n**Status:** {status}\n**Latency:** `{elapsed:.3f}s`",
            color=color, content="@everyone" if "success" in status.lower() else "",
            footer=f"Account: ...{metadata['token_suffix']}",
        )

    async def handle_giveaway_response(self, response: aiohttp.ClientResponse, metadata: Dict[str, Any]):
        """Specific logic for handling giveaway interaction responses."""
        guild_name = metadata.get("guild_name", "Unknown Server")
        channel_name = metadata.get("channel_name", "Unknown Channel")

        if 200 <= response.status < 300:
            await logger.log(f"Successfully entered giveaway in {guild_name}/#{channel_name}", logging.INFO)
            await self.webhook.send(
                title="🎁 Giveaway Entered",
                description=f"**Server:** `{guild_name}`\n**Channel:** `#{channel_name}`\n**Jump URL:** [Click Here]({metadata['jump_url']})",
                color=0x3498DB, footer=f"Account: ...{metadata['token_suffix']}",
            )
        else:
            await logger.log(f"Failed to enter giveaway in {guild_name}/#{channel_name}. Status: {response.status}", logging.WARN)

    async def handle_invite_response(self, response: aiohttp.ClientResponse, metadata: Dict[str, Any]):
        """Specific logic for handling server join responses."""
        invite_code = metadata['invite_code']
        if 200 <= response.status < 300:
            await logger.log(f"Successfully joined server with invite: {invite_code}", logging.INFO)
        else:
            await logger.log(f"Failed to join server with invite {invite_code}. Status: {response.status}", logging.WARN)


# ===================================================================================================
# 4. FEATURE IMPLEMENTATIONS
# ===================================================================================================

# --- Nitro Sniping ---
USED_NITRO_CODES: Set[str] = set()
NITRO_REGEX = re.compile(r"(?:discord\.gift/|discord\.com/gifts/|discordapp\.com/gifts/)([a-zA-Z0-9]{16,24})")

async def load_used_codes() -> Set[str]:
    """Loads previously tried Nitro codes from a file."""
    if not os.path.exists(TRIED_CODES_PATH): return set()
    try:
        async with aiofiles.open(TRIED_CODES_PATH, "rb") as f:
            content = await f.read()
            return set(orjson.loads(content))
    except Exception as e:
        await logger.log(f"Failed to load used codes: {e}", logging.ERROR)
        return set()

async def save_used_codes():
    """Saves the set of used Nitro codes to a file."""
    try:
        async with aiofiles.open(TRIED_CODES_PATH, "wb") as f:
            await f.write(orjson.dumps(list(USED_NITRO_CODES)))
    except Exception as e:
        await logger.log(f"Failed to save used codes: {e}", logging.ERROR)

def redeem_nitro_coro(code: str):
    """Returns a coroutine that attempts to redeem a Nitro code."""
    url = f"https://discord.com/api/v10/entitlements/gift-codes/{code}/redeem"
    @retry(
        stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=1, max=5),
        retry=retry_if_exception_type((aiohttp.ClientError, asyncio.TimeoutError)),
    )
    async def _coro(session: aiohttp.ClientSession, headers: Dict[str, str]) -> aiohttp.ClientResponse:
        return await session.post(url, headers=headers, json={}, timeout=8)
    return _coro


# --- Giveaway Sniping ---
def _encode_reaction_emoji_identifier(emoji: Any) -> Optional[str]:
    """Converts an emoji representation into the identifier expected by the HTTP API."""
    if emoji is None:
        return None

    if isinstance(emoji, str):
        return discord.http._uriquote(emoji)

    emoji_id = getattr(emoji, "id", None)
    emoji_name = getattr(emoji, "name", None)

    if emoji_id:
        if not emoji_name:
            return None
        prefix = "a:" if getattr(emoji, "animated", False) else ""
        return f"{prefix}{emoji_name}:{emoji_id}"

    if emoji_name:
        return discord.http._uriquote(emoji_name)

    return None


def _collect_existing_reaction_identifiers(message: discord.Message) -> List[str]:
    """Returns encoded reaction identifiers for reactions not made by the current user."""
    identifiers: List[str] = []
    for reaction in getattr(message, "reactions", []):
        if getattr(reaction, "me", False):
            continue

        count = getattr(reaction, "count", 0)
        if count is not None and count <= 0:
            continue

        identifier = _encode_reaction_emoji_identifier(getattr(reaction, "emoji", None))
        if identifier:
            identifiers.append(identifier)

    return identifiers


def create_smart_giveaway_entry_coro(message: discord.Message) -> Optional[Callable[[aiohttp.ClientSession, Dict[str, str]], Coroutine[Any, Any, aiohttp.ClientResponse]]]:
    """Returns a coroutine that tries to click a button, falling back to existing reactions."""
    first_button = next((child for comp in message.components for child in comp.children if isinstance(child, discord.Button)), None)
    available_reactions = _collect_existing_reaction_identifiers(message)

    if not first_button and not available_reactions:
        return None

    interaction_url = "https://discord.com/api/v10/interactions"

    async def _coro(session: aiohttp.ClientSession, headers: Dict[str, str]) -> aiohttp.ClientResponse:
        button_response: Optional[aiohttp.ClientResponse] = None
        if first_button:
            await logger.log(f"Attempting button click for giveaway in {message.guild.name}...", logging.DEBUG)
            payload = {
                "type": 3, "nonce": str(random.randint(10**18, 10**19 - 1)),
                "guild_id": message.guild.id, "channel_id": message.channel.id,
                "message_flags": 0, "message_id": message.id,
                "application_id": str(message.author.id), "session_id": "0",
                "data": {"component_type": first_button.type.value, "custom_id": first_button.custom_id},
            }
            try:
                button_response = await session.post(interaction_url, headers=headers, json=payload, timeout=10)
                if 200 <= button_response.status < 300:
                    await logger.log(f"Button click successful in {message.guild.name}.", logging.DEBUG)
                    return button_response

                response_text = await button_response.text()
                await logger.log(f"Button click in {message.guild.name} failed (Status {button_response.status}). Response: {response_text[:200]}. Falling back to reactions.", logging.WARNING)
            except Exception as e:
                await logger.log(f"Exception during button click in {message.guild.name}: {e}. Falling back to reactions.", logging.ERROR)
                if not available_reactions:
                    raise

        if not available_reactions:
            if button_response is None:
                raise RuntimeError("Button interaction failed and no reactions available to fall back on.")
            return button_response

        await logger.log(f"Attempting existing reactions for giveaway in {message.guild.name}...", logging.DEBUG)

        last_response: Optional[aiohttp.ClientResponse] = None
        for reaction_identifier in available_reactions:
            reaction_url = (
                f"https://discord.com/api/v10/channels/{message.channel.id}/messages/{message.id}/reactions/{reaction_identifier}/@me"
            )
            response = await session.put(reaction_url, headers=headers, timeout=10)
            if last_response is not None:
                await last_response.read()
            last_response = response

        if last_response is None:
            raise RuntimeError("No reactions were processed while attempting to join the giveaway.")
        return last_response

    return _coro


# --- Invite Sniping ---
INVITE_REGEX = re.compile(r"(?:discord\.gg/|discord\.com/invite/)([a-zA-Z0-9]+)")
JOINED_SERVERS_HISTORY = deque(maxlen=50) # Track recent joins for rate limiting

def join_server_coro(invite_code: str):
    """Returns a coroutine for joining a server via an invite code."""
    url = f"https://discord.com/api/v10/invites/{invite_code}"
    async def _coro(session: aiohttp.ClientSession, headers: Dict[str, str]) -> aiohttp.ClientResponse:
        return await session.post(url, headers=headers, json={}, timeout=15)
    return _coro


# ===================================================================================================
# 5. DISCORD BOT EVENT HANDLERS (Producers)
# ===================================================================================================

class SniperClient(commands.Bot):
    """Custom client class to hold shared resources."""
    def __init__(self, account_config: AccountModel, shared_config: ConfigModel, api_queue: asyncio.PriorityQueue, webhook: WebhookNotifier, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.account_config = account_config
        self.shared_config = shared_config
        self.api_queue = api_queue
        self.webhook = webhook
        self.main_account_token = next(acc.token for acc in shared_config.accounts if acc.is_main)

        self.add_listener(self.on_ready_sniper, "on_ready")
        self.add_listener(self.on_message_sniper, "on_message")

    async def on_ready_sniper(self):
        """Called when this specific client is connected and ready."""
        role = 'Main' if self.account_config.is_main else 'Feeder'
        await logger.log(f"Client ready: {self.user} ({self.user.id}) in {len(self.guilds)} guilds. Role: {role}")
        await self.webhook.send(
            title="✅ Client Connected",
            description=f"**User:** `{self.user}`\n**ID:** `{self.user.id}`\n**Role:** {role}",
            color=0x2ECC71,
        )

    async def on_message_sniper(self, message: discord.Message):
        """Processes every message seen by this client."""
        if not message.guild or not message.author or message.author == self.user: return
        if message.author.id in self.shared_config.bot_author_blacklist: return

        await self.check_for_nitro(message)
        if message.author.bot:
            await self.check_for_giveaway(message)
            await self.check_for_invite(message)
            await self.check_for_win(message)

    async def check_for_nitro(self, message: discord.Message):
        """Finds Nitro codes and queues redemption jobs."""
        codes = NITRO_REGEX.findall(message.content)
        if not codes: return

        redeem_token = self.main_account_token if self.account_config.is_feeder else self.account_config.token
        for code in codes:
            if code not in USED_NITRO_CODES:
                USED_NITRO_CODES.add(code)
                asyncio.create_task(save_used_codes())
                await logger.log(f"Nitro code found: {code}. Queuing redemption.", logging.INFO)
                
                job = APIJob(
                    priority=1, coro=redeem_nitro_coro(code), account_token=redeem_token,
                    metadata={
                        "type": "nitro_redeem", "endpoint": f"/entitlements/gift-codes/{code}/redeem",
                        "code": code, "start_time": time.monotonic(), "token_suffix": redeem_token[-4:],
                    },
                )
                await self.api_queue.put(job)

    async def check_for_giveaway(self, message: discord.Message):
        """Identifies potential giveaways and queues a smart interaction job."""
        settings = self.shared_config.giveaway_settings
        embed_text = "".join(orjson.dumps(e.to_dict()).decode('utf-8', 'ignore').lower() for e in message.embeds)
        full_text = message.content.lower() + embed_text

        # Filtering Logic
        if any(keyword.lower() in full_text for keyword in settings.global_blacklist_keywords): return

        server_rules = settings.server_specific_rules.get(message.guild.id)
        if server_rules:
            if any(k.lower() in full_text for k in server_rules.get("blacklist", [])): return
            if "whitelist" in server_rules and not any(k.lower() in full_text for k in server_rules["whitelist"]): return
        elif not any(kw in full_text for kw in ["giveaway", "win", "prize", "hosted by", "ends in", "🎉"]):
            return

        # Prevent re-entering reaction giveaways
        if any(r.emoji == "🎉" for r in message.reactions if r.me): return

        interaction_coro = create_smart_giveaway_entry_coro(message)
        if interaction_coro is None:
            return

        await logger.log(f"Giveaway found in {message.guild.name}. Queuing smart entry.", logging.DEBUG)

        delay = random.uniform(settings.min_delay_sec, settings.max_delay_sec)
        await asyncio.sleep(delay)

        job = APIJob(
            priority=5, coro=interaction_coro, account_token=self.account_config.token,
            metadata={
                "type": "giveaway_interact", "endpoint": "/interactions",
                "guild_name": message.guild.name, "channel_name": message.channel.name,
                "jump_url": message.jump_url, "token_suffix": self.account_config.token[-4:],
            },
        )
        await self.api_queue.put(job)

    async def check_for_win(self, message: discord.Message):
        """Detects if the user has won a giveaway."""
        if not self.user: return
        content = message.content.lower()
        mentioned = self.user.mention in content or f"<@!{self.user.id}>" in content

        if mentioned and any(kw in content for kw in ["congratulations", "won", "winner"]):
            await logger.log(f"Giveaway WIN detected in {message.guild.name}!", logging.INFO, suppress_repetition=False)
            await self.webhook.send(
                title="🏆 GIVEAWAY WON!",
                description=f"**Server:** `{message.guild.name}`\n**Channel:** `#{message.channel.name}`\n**Message:**\n>>> {message.content}\n\n[Jump to Win Message]({message.jump_url})",
                color=0x2ECC71, content=f"@everyone {self.user.mention}", footer=f"Account: {self.user.name}"
            )

    async def check_for_invite(self, message: discord.Message):
        """Finds server invites and queues join jobs if they meet criteria."""
        settings = self.shared_config.invite_sniper_settings
        if not settings.enabled: return

        now = time.monotonic()
        # Filter out joins that are too recent
        valid_history = [t for t in JOINED_SERVERS_HISTORY if now - 3600 < t]
        JOINED_SERVERS_HISTORY.clear()
        JOINED_SERVERS_HISTORY.extend(valid_history)
        if len(JOINED_SERVERS_HISTORY) >= settings.max_joins_per_hour: return

        for code in INVITE_REGEX.findall(message.content):
            job = APIJob(
                priority=10, coro=join_server_coro(code), account_token=self.account_config.token,
                metadata={
                    "type": "invite_join", "endpoint": f"/invites/{code}",
                    "invite_code": code, "token_suffix": self.account_config.token[-4:]
                }
            )
            await self.api_queue.put(job)
            JOINED_SERVERS_HISTORY.append(time.monotonic())
            await logger.log(f"Invite found: {code}. Queuing join job.", logging.DEBUG)


# ===================================================================================================
# 6. MAIN EXECUTION
# ===================================================================================================

async def main():
    """The main entry point for the application."""
    try:
        with open(CONFIG_PATH, "rb") as f:
            config_data = json.load(f)
            if 'giveaway_settings' in config_data and 'server_specific_rules' in config_data['giveaway_settings']:
                config_data['giveaway_settings']['server_specific_rules'] = {
                    int(k): v for k, v in config_data['giveaway_settings']['server_specific_rules'].items()
                }
            config = ConfigModel.model_validate(config_data)
    except (ValidationError, FileNotFoundError, json.JSONDecodeError) as e:
        await logger.log(f"Configuration error: {e}", logging.CRITICAL, suppress_repetition=False)
        sys.exit(1)

    # Initialize Shared Services
    api_queue = asyncio.PriorityQueue()
    governor = RateLimitGovernor()
    global_session = aiohttp.ClientSession(json_serialize=json.dumps) # Use standard json for aiohttp stability
    webhook = WebhookNotifier(config, global_session)
    
    global USED_NITRO_CODES
    USED_NITRO_CODES = await load_used_codes()
    await logger.log(f"Loaded {len(USED_NITRO_CODES)} used Nitro codes from file.", logging.INFO)

    profile_managers = {acc.token: ClientProfileManager(acc) for acc in config.accounts}

    # Start API Consumers
    consumer_tasks = [
        asyncio.create_task(
            APIConsumer(api_queue, governor, profile_managers, global_session, webhook, config).run()
        ) for _ in range(5) # Start 5 consumer workers for concurrency
    ]

    # Start Discord Clients
    client_tasks = []
    for account in config.accounts:
        client = SniperClient(
            account_config=account, shared_config=config, api_queue=api_queue, webhook=webhook,
            command_prefix="!sniper-impossible-prefix!", self_bot=True, proxy=account.proxy_url
        )
        client_tasks.append(client.start(account.token))

    await logger.log(f"Starting {len(config.accounts)} clients and {len(consumer_tasks)} workers.", logging.INFO, suppress_repetition=False)
    try:
        await asyncio.gather(*client_tasks, *consumer_tasks)
    except discord.LoginFailure as e:
        await logger.log(f"Login failed for one or more accounts: {e}", logging.CRITICAL, suppress_repetition=False)
    finally:
        await global_session.close()
        for task in consumer_tasks: task.cancel()
        await logger.log("Shutting down.", logging.INFO)


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
    except Exception as e:
        # Top-level exception handler for unexpected crashes
        asyncio.run(logger.log(f"FATAL UNHANDLED EXCEPTION: {e}", logging.CRITICAL, suppress_repetition=False))
