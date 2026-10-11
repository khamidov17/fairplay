"""Pluggable AI provider for drafts (grading, transcripts, question variations).

FAIRPLAY_AI_PROVIDER = anthropic | openai | none (default). "openai" means any
OpenAI-compatible endpoint (OpenAI, DeepSeek, Qwen/DashScope, Moonshot ...), set
FAIRPLAY_AI_BASE_URL accordingly. Every output is a draft a teacher approves.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import re
from typing import Any

import httpx

log = logging.getLogger("fairplay.ai")


class AIUnavailable(RuntimeError):
    """AI is off or failed; callers use their deterministic fallback."""


def provider() -> str:
    return (os.environ.get("FAIRPLAY_AI_PROVIDER") or "none").strip().lower()


def enabled() -> bool:
    return provider() in ("anthropic", "openai") and bool(os.environ.get("FAIRPLAY_AI_KEY"))


_transport: httpx.AsyncBaseTransport | None = None


def set_transport(t: httpx.AsyncBaseTransport | None) -> None:
    """Tests inject a fake transport."""
    global _transport
    _transport = t


def _parse_json(text: str) -> dict:
    text = text.strip()
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise AIUnavailable("AI did not return JSON")
    return json.loads(m.group(0))


def _mime(img: bytes) -> str:
    if img[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if img[:3] == b"GIF":
        return "image/gif"
    if img[:4] == b"RIFF" and img[8:12] == b"WEBP":
        return "image/webp"
    return "image/jpeg"


async def complete_json(system: str, prompt: str, images: list[bytes] | None = None,
                        max_tokens: int = 800) -> dict[str, Any]:
    """Ask the configured model for one JSON object. Raises AIUnavailable when off or failing."""
    if not enabled():
        raise AIUnavailable("AI is off")
    images = images or []
    key = os.environ["FAIRPLAY_AI_KEY"]
    model = os.environ.get("FAIRPLAY_AI_MODEL")
    try:
        async with httpx.AsyncClient(timeout=60, transport=_transport) as client:
            if provider() == "anthropic":
                content: list[dict] = [
                    {"type": "image", "source": {"type": "base64", "media_type": _mime(i),
                                                 "data": base64.b64encode(i).decode()}} for i in images]
                content.append({"type": "text", "text": prompt})
                base = os.environ.get("FAIRPLAY_AI_BASE_URL") or "https://api.anthropic.com"
                r = await client.post(f"{base.rstrip('/')}/v1/messages", headers={
                    "x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
                    json={"model": model or "claude-sonnet-5-5", "max_tokens": max_tokens, "system": system,
                          "messages": [{"role": "user", "content": content}]})
                r.raise_for_status()
                text = "".join(b.get("text", "") for b in r.json().get("content", []))
            else:
                parts: list[dict] = [{"type": "text", "text": prompt}] + [
                    {"type": "image_url", "image_url": {
                        "url": f"data:{_mime(i)};base64,{base64.b64encode(i).decode()}"}} for i in images]
                base = os.environ.get("FAIRPLAY_AI_BASE_URL") or "https://api.openai.com/v1"
                r = await client.post(f"{base.rstrip('/')}/chat/completions", headers={
                    "Authorization": f"Bearer {key}", "content-type": "application/json"},
                    json={"model": model or "gpt-4o-mini", "max_tokens": max_tokens,
                          "messages": [{"role": "system", "content": system},
                                       {"role": "user", "content": parts}]})
                r.raise_for_status()
                text = r.json()["choices"][0]["message"]["content"]
        return _parse_json(text)
    except AIUnavailable:
        raise
    except Exception as e:  # network, HTTP, JSON
        log.warning("AI call failed: %s", e)
        raise AIUnavailable(str(e)) from e
