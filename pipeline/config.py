from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
PROMPTS_DIR = ROOT / "prompts"
EPISODES_DIR = ROOT / "episodes"

load_dotenv(ROOT / ".env")


@lru_cache(maxsize=1)
def load_config(path: str | None = None) -> dict:
    cfg_path = Path(path) if path else ROOT / "config.yaml"
    with open(cfg_path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def env(name: str, default: str | None = None) -> str | None:
    value = os.getenv(name, default)
    return value if value else default


def user_agent() -> str:
    # Wikimedia blocks generic agents. Put a real contact in .env.
    return env("WIKI_USER_AGENT", "DocuPipeline/0.1 (contact: set WIKI_USER_AGENT in .env)")


def load_prompt(name: str) -> str:
    return (PROMPTS_DIR / name).read_text(encoding="utf-8")
