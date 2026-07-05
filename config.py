import json
import os

import yaml

_ENV_LINE_RE_COMMENT_PREFIXES = ("#",)


def load_env(path: str = ".env") -> None:
    if not os.path.exists(path):
        return
    with open(path, "r") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith(_ENV_LINE_RE_COMMENT_PREFIXES):
                continue
            if "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            os.environ.setdefault(key, value)


def get_required_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(
            f"Missing required environment variable: {name}. "
            f"Copy .env.example to .env and fill it in."
        )
    return value


def load_config(path: str = "config.yaml") -> dict:
    with open(path, "r") as f:
        return yaml.safe_load(f)


def load_rules(path: str = "rules.json") -> dict:
    with open(path, "r") as f:
        return json.load(f)
