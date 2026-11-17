"""Низькорівневий клієнт Gemini API: спільний для тексту (text_gen.py) і зображень (image_gen.py).

Два інтерфейси Google:
  generateContent — POST /v1beta/models/<модель>:generateContent (типовий тут);
  interactions    — POST /v1beta/interactions (новіший; вмикається GEMINI_API=interactions).

Ключ передається заголовком x-goog-api-key і ніколи не потрапляє в URL чи лог.
"""
from __future__ import annotations

import json
import logging
import os

import httpx

log = logging.getLogger("textcard-bot.gemini")

BASE_URL_DEFAULT = "https://generativelanguage.googleapis.com/v1beta"
API_REVISION = "2026-05-20"          # заголовок Api-Revision для interactions
RETRY_STATUS = {408, 429, 500, 502, 503, 504}

# назви-псевдоніми моделей -> ID у Gemini API
ALIASES = {
    # зображення
    "nano-banana": "gemini-2.5-flash-image",
    "nano-banana-2": "gemini-3.1-flash-image",
    "nano-banana-2-lite": "gemini-3.1-flash-lite-image",
    "nano-banana-2.1": "gemini-nano-banana-2.1",
    "nano-banana-pro": "gemini-3-pro-image",
    # текст
    "flash": "gemini-3.5-flash",
    "flash-3": "gemini-3-flash-preview",
}


class GeminiError(RuntimeError):
    """Помилка Gemini із текстом для користувача; retryable — чи має сенс повторити."""

    def __init__(self, message: str, *, retryable: bool = False, status: int = 0):
        super().__init__(message)
        self.retryable = retryable
        self.status = status


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def env_num(name: str, default: float) -> float:
    try:
        return float(env(name) or default)
    except ValueError:
        return default


def api_key() -> str:
    return env("GEMINI_API_KEY")


def base_url() -> str:
    return (env("GEMINI_BASE_URL") or BASE_URL_DEFAULT).rstrip("/")


def api_mode() -> str:
    return "interactions" if env("GEMINI_API").lower() == "interactions" else "generateContent"


def parse_model(value: str, default_size: str = "1K") -> tuple[str, str]:
    """«nano-banana-2-1k» -> («gemini-3.1-flash-image», «1K»). Справжній ID моделі лишається як є."""
    import re
    value = (value or "").strip() or "nano-banana-2"
    size = (default_size or "1K").upper()
    m = re.fullmatch(r"(?i)(.+?)-(512|[124]k)", value)
    if m:
        value, size = m.group(1), m.group(2).upper()
    return ALIASES.get(value.lower(), value), size


def explain(status: int, body: str, model: str, model_env: str = "GEMINI_MODEL") -> str:
    """Зрозуміле повідомлення про помилку Gemini (без ключа й без тіла відповіді цілком)."""
    msg = ""
    try:
        msg = ((json.loads(body).get("error") or {}).get("message") or "")[:300]
    except (ValueError, AttributeError):
        msg = (body or "")[:300]
    if status in (401, 403) or "API key not valid" in msg or "API_KEY_INVALID" in (body or ""):
        return "Gemini не прийняв ключ. Перевірте GEMINI_API_KEY у .env."
    if status == 404:
        return f"Gemini не знає моделі «{model}» (404). Перевірте {model_env} у .env."
    if status == 429:
        return "Gemini: перевищено ліміт запитів або квоту (429). Спробуйте за хвилину."
    if status >= 500:
        return f"Gemini тимчасово недоступний ({status})."
    return f"Gemini повернув помилку {status}: {msg}"


async def post(url: str, payload: dict, *, timeout: float, model: str, model_env: str = "GEMINI_MODEL",
               interactions: bool = False) -> dict:
    """POST із ключем у заголовку. Повертає розібраний JSON або кидає GeminiError."""
    key = api_key()
    if not key:
        raise GeminiError("Ключ Gemini не заданий: додайте GEMINI_API_KEY у .env.")
    headers = {"x-goog-api-key": key, "Content-Type": "application/json"}
    if interactions:
        headers["Api-Revision"] = env("GEMINI_API_REVISION") or API_REVISION
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(timeout, connect=20)) as cli:
            r = await cli.post(url, json=payload, headers=headers)
    except httpx.TimeoutException:
        raise GeminiError(f"Gemini не відповів за {timeout:.0f} с.", retryable=True) from None
    except httpx.HTTPError as e:
        raise GeminiError(f"Не вдалося звернутися до Gemini: {type(e).__name__}.", retryable=True) from None
    if r.status_code >= 300:
        raise GeminiError(explain(r.status_code, r.text, model, model_env),
                          retryable=r.status_code in RETRY_STATUS, status=r.status_code)
    try:
        body = r.json()
    except ValueError:
        raise GeminiError("Gemini повернув несподівану відповідь.", retryable=True) from None
    if isinstance(body, dict):
        block = (body.get("promptFeedback") or {}).get("blockReason")
        if block:
            raise GeminiError(f"Gemini відхилив запит через правила вмісту ({block}).")
    return body


def finish_reason(body) -> str:
    try:
        return body["candidates"][0].get("finishReason", "") or ""
    except (KeyError, IndexError, TypeError, AttributeError):
        return (body or {}).get("status", "") if isinstance(body, dict) else ""


def find_images(node, out: list | None = None) -> list[tuple[str, str]]:
    """Усі зображення у відповіді (base64, mime), у порядку появи; чернетки-«думки» пропускаються."""
    out = [] if out is None else out
    if isinstance(node, dict):
        if node.get("thought") is True:
            return out
        inline = node.get("inlineData") or node.get("inline_data")
        if isinstance(inline, dict) and inline.get("data"):
            out.append((inline["data"], inline.get("mimeType") or inline.get("mime_type") or "image/png"))
        elif node.get("type") == "image" and isinstance(node.get("data"), str):
            out.append((node["data"], node.get("mime_type") or node.get("mimeType") or "image/png"))
        for k, v in node.items():
            if k not in ("inlineData", "inline_data"):
                find_images(v, out)
    elif isinstance(node, list):
        for v in node:
            find_images(v, out)
    return out


def find_text(node, out: list | None = None) -> str:
    """Текст із відповіді обох інтерфейсів; «думки» моделі пропускаються."""
    out = [] if out is None else out
    if isinstance(node, dict):
        if node.get("thought") is True or node.get("type") == "thought":
            return ""
        t = node.get("text")
        if isinstance(t, str) and node.get("type") in (None, "text", "output_text"):
            out.append(t)
        for k, v in node.items():
            if k not in ("text", "promptFeedback", "usageMetadata", "safetyRatings"):
                find_text(v, out)
    elif isinstance(node, list):
        for v in node:
            find_text(v, out)
    return "\n".join(x.strip() for x in out if x and x.strip())
