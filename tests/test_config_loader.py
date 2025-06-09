import json
from config_loader import Config


def test_config_validation(tmp_path):
    cfg_path = tmp_path / "cfg.json"
    sample = {
        "Token": "tok",
        "Webhook": None,
        "BotBlacklist": [],
        "WebhookNotification": True,
        "UserAgents": [],
        "DeviceIds": [],
        "NitroSettings": {"max_snipes": 5},
        "DMMessage": "hi",
        "GiveawayBlacklist": [],
    }
    cfg_path.write_text(json.dumps(sample))
    cfg = Config.load(str(cfg_path))
    assert cfg.token == "tok"
