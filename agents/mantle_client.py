"""
agents/mantle_client.py — Mantle LLM client (Bedrock-backed, OpenAI-compatible API)

Adapted from ghostvendor pattern. Uses AWS-internal certificate (verify=False).
MANTLE_API_KEY and MANTLE_BASE_URL must be set in the environment.
"""

from __future__ import annotations

import logging
import os
import re

import urllib3
from openai import APIError, OpenAI

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
os.environ.setdefault("PYTHONHTTPSVERIFY", "0")

logger = logging.getLogger(__name__)

MANTLE_BASE_URL = os.environ.get("MANTLE_BASE_URL", "https://bedrock-mantle.us-east-1.api.aws/v1")

# Model IDs
_QWEN_235B = "qwen.qwen3-235b-a22b-2507"  # reasoning — SQL fix proposals (App 1)
_DEEPSEEK = "deepseek.v3.2"  # patch generator — spec/API patches (App 2)

# Fallbacks
_QWEN_FALLBACK = "nvidia.nemotron-super-3-120b"
_DEEPSEEK_FALLBACK = _QWEN_235B

_client: OpenAI | None = None


def _get_client() -> OpenAI:
    global _client
    if _client is None:
        _client = OpenAI(
            base_url=MANTLE_BASE_URL,
            api_key=os.environ["MANTLE_API_KEY"],
            default_headers={"openai-project": "default"},
            http_client=__import__("httpx").Client(verify=False),
        )
    return _client


def _call(
    model: str, system: str, user: str, temperature: float = 0.0, max_tokens: int = 8192
) -> str:
    response = _get_client().chat.completions.create(
        model=model,
        temperature=temperature,
        max_tokens=max_tokens,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    )
    return response.choices[0].message.content


def _call_with_fallback(
    primary: str,
    fallback: str,
    system: str,
    user: str,
    temperature: float = 0.0,
    max_tokens: int = 8192,
) -> str:
    try:
        return _call(primary, system, user, temperature, max_tokens=max_tokens)
    except APIError as exc:
        logger.warning("%s failed (%s), falling back to %s", primary, exc, fallback)
        return _call(fallback, system, user, temperature, max_tokens=max_tokens)


def ultra(system: str, user: str, temperature: float = 0.0, max_tokens: int = 8192) -> str:
    """QWEN 235B — reasoning model for SQL fix proposals (App 1)."""
    return _call_with_fallback(
        _QWEN_235B, _QWEN_FALLBACK, system, user, temperature, max_tokens=max_tokens
    )


def deepseek_pro(system: str, user: str, temperature: float = 0.0, max_tokens: int = 16384) -> str:
    """DeepSeek v3 — patch generator for spec/API patches (App 2)."""
    return _call_with_fallback(
        _DEEPSEEK, _DEEPSEEK_FALLBACK, system, user, temperature, max_tokens=max_tokens
    )


def strip_llm_wrapper(raw: str) -> str:
    """Strip <think> blocks and markdown code fences from model output."""
    if not raw:
        return raw
    raw = raw.strip()
    raw = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL).strip()
    if raw.startswith("```"):
        parts = raw.split("```")
        inner = parts[1] if len(parts) > 1 else ""
        if inner.startswith("json"):
            inner = inner[4:]
        raw = inner.strip()
    return raw
