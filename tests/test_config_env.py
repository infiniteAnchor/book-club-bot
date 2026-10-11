"""ENV defaults and Discord token selection for self-hosted setups."""
import importlib
import os
from unittest import mock

import config


def _load(env):
    """Reload config with exactly these variables (no .env file)."""
    keys = ("ENV", "DISCORD_TOKEN", "DISCORD_TOKEN_DEV")
    base = {k: v for k, v in os.environ.items() if k not in keys}
    with mock.patch.dict(os.environ, {**base, **env}, clear=True), \
         mock.patch("dotenv.load_dotenv"):
        return importlib.reload(config)


def teardown_module():
    importlib.reload(config)


def test_env_defaults_to_prod_token():
    cfg = _load({"DISCORD_TOKEN": "prod-token", "DISCORD_TOKEN_DEV": "dev-token"})
    assert cfg.ENV == "prod"
    assert cfg.DISCORD_TOKEN == "prod-token"


def test_dev_prefers_dev_token():
    cfg = _load({"ENV": "dev", "DISCORD_TOKEN": "prod-token", "DISCORD_TOKEN_DEV": "dev-token"})
    assert cfg.DISCORD_TOKEN == "dev-token"


def test_dev_falls_back_to_main_token():
    cfg = _load({"ENV": "dev", "DISCORD_TOKEN": "prod-token"})
    assert cfg.DISCORD_TOKEN == "prod-token"


def test_secret_values_include_both_discord_tokens():
    cfg = _load({"DISCORD_TOKEN": "prod-token", "DISCORD_TOKEN_DEV": "dev-token"})
    assert {"prod-token", "dev-token"} <= set(cfg.SECRET_VALUES)
