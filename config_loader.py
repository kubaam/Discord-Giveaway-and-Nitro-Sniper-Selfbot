from __future__ import annotations

import logging
import os
import sys
from typing import Dict, List, Optional, Union

import orjson

from pydantic import BaseModel, Field, HttpUrl, ValidationError

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")


class NitroSettingsModel(BaseModel):
    max_snipes: int = Field(default=5, ge=1)
    cooldown_time: int = Field(default=300, ge=0)
    request_timeout: float = Field(default=10.0, gt=0)
    max_retries: int = Field(default=3, ge=1)


class ConfigModel(BaseModel):
    Token: Union[List[str], str]
    Webhook: Optional[HttpUrl] = None
    WebhookNotification: bool = True
    BotBlacklist: List[str] = []
    UserAgents: List[str] = []
    DeviceIds: List[str] = []
    NitroSettings: NitroSettingsModel = NitroSettingsModel()
    DMMessage: str = ""
    GiveawayBlacklist: List[str] = []


class Config:
    def __init__(self, model: ConfigModel):
        token = model.Token
        if isinstance(token, list):
            token = token[0] if token else ""
        self.token: str = token
        self.webhook: Optional[str] = str(model.Webhook) if model.Webhook else None
        self.bot_blacklist = model.BotBlacklist
        self.webhook_notification = model.WebhookNotification
        self.user_agents = model.UserAgents
        self.device_ids = model.DeviceIds
        self.nitro_settings: Dict[str, Union[int, float]] = model.NitroSettings.dict()
        self.giveaway_blacklist = model.GiveawayBlacklist
        self.dm_message = model.DMMessage

    @staticmethod
    def load(path: str = CONFIG_PATH) -> "Config":
        """Load configuration from JSON using fast orjson parser."""
        try:
            with open(path, "rb") as f:
                data = orjson.loads(f.read())
            model = ConfigModel.parse_obj(data)
            return Config(model)
        except ValidationError as e:
            logging.critical(f"Invalid config: {e}")
            sys.exit(1)
        except Exception as e:
            logging.critical(f"Config load failed: {e}")
            sys.exit(1)
