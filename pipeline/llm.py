"""Thin client for any OpenAI-compatible chat endpoint (Gemini, Groq, a local server).

Set DOCU_MOCK_LLM=1 to replay canned responses from tests/fixtures, useful for
testing the pipeline without spending free-tier quota.
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

import requests

from .config import ROOT, env, load_config


class LLMError(RuntimeError):
    pass


def _provider() -> dict:
    cfg = load_config()["llm"]
    name = cfg["provider"]
    if name not in cfg["providers"]:
        raise LLMError(f"llm.provider '{name}' isn't defined under llm.providers in config.yaml")
    return {"name": name, **cfg["providers"][name], "temperature": cfg["temperature"],
            "timeout": cfg["timeout_seconds"]}


def max_input_chars() -> int:
    return int(_provider().get("max_input_chars", 30000))


def chat(system: str, user: str, *, task: str, temperature: float | None = None) -> str:
    """Send one system+user exchange and return the raw text reply.

    `task` names the call ("factsheet", "script") so the mock can pick a fixture.
    """
    if os.getenv("DOCU_MOCK_LLM") == "1":
        return _mock(task)

    p = _provider()
    key = env(p["api_key_env"])
    if not key:
        raise LLMError(f"Missing {p['api_key_env']} in .env (needed for provider '{p['name']}')")

    url = p["base_url"].rstrip("/") + "/chat/completions"
    payload = {
        "model": p["model"],
        "temperature": p["temperature"] if temperature is None else temperature,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
    }
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}

    # Free tiers rate-limit hard. Back off on 429/5xx instead of dying.
    for attempt in range(5):
        resp = requests.post(url, json=payload, headers=headers, timeout=p["timeout"])
        if resp.status_code == 200:
            return resp.json()["choices"][0]["message"]["content"]
        if resp.status_code in (429, 500, 502, 503, 504):
            wait = min(60, 5 * 2 ** attempt)
            print(f"  LLM busy ({resp.status_code}), retrying in {wait}s...")
            time.sleep(wait)
            continue
        raise LLMError(f"{p['name']} returned {resp.status_code}: {resp.text[:500]}")
    raise LLMError(f"{p['name']} kept rate-limiting. Wait a bit or switch provider in config.yaml.")


def extract_json(text: str):
    """Pull the JSON object out of a model reply, tolerating ```json fences and chatter."""
    fenced = re.search(r"```(?:json)?\s*(\{.*\}|\[.*\])\s*```", text, re.DOTALL)
    candidate = fenced.group(1) if fenced else text
    start = min((i for i in (candidate.find("{"), candidate.find("[")) if i != -1), default=-1)
    if start == -1:
        raise LLMError("Model reply had no JSON in it")
    end = max(candidate.rfind("}"), candidate.rfind("]"))
    try:
        return json.loads(candidate[start:end + 1])
    except json.JSONDecodeError as e:
        raise LLMError(f"Model returned broken JSON: {e}") from e


def _mock(task: str) -> str:
    path = Path(ROOT / "tests" / "fixtures" / f"llm_{task}.json")
    if not path.exists():
        raise LLMError(f"No mock fixture for task '{task}' at {path}")
    return "Sure, here it is:\n```json\n" + path.read_text(encoding="utf-8") + "\n```"
