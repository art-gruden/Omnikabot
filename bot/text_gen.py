"""Текстові запити бота (оформлення постів, промти, опис стилю) — через Gemini або OpenAI.

Провайдер задається в .env:
    TEXT_PROVIDER=gemini | openai     (без значення: gemini, коли є GEMINI_API_KEY, інакше openai)
    GEMINI_TEXT_MODEL=gemini-3.5-flash

Інтерфейс однаковий для обох: await provider.chat(system, user, max_tokens) -> str.
Помилки обох провайдерів приходять як TextError.
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Callable

from . import gemini_api as GA
from . import openai_api as O

log = logging.getLogger("textcard-bot.text")

DEFAULT_GEMINI_TEXT_MODEL = "gemini-3.5-flash"


class TextError(RuntimeError):
    """Помилка текстового запиту з повідомленням для користувача."""


class TextProvider:
    name = ""
    label = ""

    @property
    def model(self) -> str:
        raise NotImplementedError

    def ready(self) -> bool:
        raise NotImplementedError

    def not_ready_hint(self) -> str:
        raise NotImplementedError

    async def chat(self, system: str, user: str, max_tokens: int = 1000) -> str:
        raise NotImplementedError


class OpenAITextProvider(TextProvider):
    name, label = "openai", "ChatGPT"

    def __init__(self, cfg: Callable[[], dict]):
        self._cfg = cfg            # ключ і модель задаються в меню бота, тож читаємо їх на кожен виклик

    @property
    def model(self) -> str:
        return self._cfg()["text_model"]

    def ready(self) -> bool:
        return bool(self._cfg()["key"])

    def not_ready_hint(self) -> str:
        return "Потрібен ключ OpenAI: меню → Адміністрування → OpenAI"

    async def chat(self, system: str, user: str, max_tokens: int = 1000) -> str:
        c = self._cfg()
        client = O.OpenAIClient(c["key"], c["base_url"], c["text_model"], c["image_model"])
        try:
            return await client.chat(system, user, max_tokens=max_tokens)
        except O.OpenAIError as e:
            raise TextError(str(e)) from None


class GeminiTextProvider(TextProvider):
    name, label = "gemini", "Gemini"

    def __init__(self):
        self._model = GA.ALIASES.get(GA.env("GEMINI_TEXT_MODEL").lower(),
                                     GA.env("GEMINI_TEXT_MODEL") or DEFAULT_GEMINI_TEXT_MODEL)
        self.timeout = GA.env_num("GEMINI_TEXT_TIMEOUT", 90)
        self.attempts = max(1, int(GA.env_num("TEXT_ATTEMPTS", 2)))
        self.retry_delay = GA.env_num("TEXT_RETRY_DELAY", 1.5)
        self.thinking = GA.env("GEMINI_THINKING").lower()     # minimal | low | medium | high; порожньо — не задаємо

    @property
    def model(self) -> str:
        return self._model

    def ready(self) -> bool:
        return bool(GA.api_key())

    def not_ready_hint(self) -> str:
        return "Потрібен ключ Gemini: GEMINI_API_KEY у .env (./deploy.sh gemini)"

    def request(self, system: str, user: str, max_tokens: int) -> tuple[str, dict, bool]:
        # запас на «роздуми» моделі: інакше відповідь може обірватися на MAX_TOKENS
        budget = max(1024, max_tokens * 3)
        if GA.api_mode() == "interactions":
            cfg: dict = {"max_output_tokens": budget, "temperature": 0.9}
            if self.thinking:
                cfg["thinking_level"] = self.thinking
            return (f"{GA.base_url()}/interactions",
                    {"model": self._model, "system_instruction": system, "input": user, "generation_config": cfg}, True)
        cfg = {"maxOutputTokens": budget, "temperature": 0.9}
        if self.thinking:
            cfg["thinkingLevel"] = self.thinking.upper()
        return (f"{GA.base_url()}/models/{self._model}:generateContent",
                {"systemInstruction": {"parts": [{"text": system}]},
                 "contents": [{"role": "user", "parts": [{"text": user}]}],
                 "generationConfig": cfg}, False)

    async def chat(self, system: str, user: str, max_tokens: int = 1000) -> str:
        if not self.ready():
            raise TextError("Ключ Gemini не заданий: додайте GEMINI_API_KEY у .env.")
        url, payload, interactions = self.request(system, user, max_tokens)
        last: GA.GeminiError | None = None
        for attempt in range(1, self.attempts + 1):
            try:
                body = await GA.post(url, payload, timeout=self.timeout, model=self._model,
                                     model_env="GEMINI_TEXT_MODEL", interactions=interactions)
                text = GA.find_text(body).strip()
                if not text:
                    raise GA.GeminiError("Gemini не повернув тексту"
                                         + (f" ({GA.finish_reason(body)})." if GA.finish_reason(body) else "."),
                                         retryable=True)
                return text
            except GA.GeminiError as e:
                last = e
                log.info("текст через Gemini, спроба %d з %d: %s", attempt, self.attempts, e)
                if not e.retryable or attempt == self.attempts:
                    break
                await asyncio.sleep(self.retry_delay * attempt)
        raise TextError(str(last))


def provider_name() -> str:
    name = os.environ.get("TEXT_PROVIDER", "").strip().lower()
    if name in ("gemini", "openai"):
        return name
    if name:
        log.warning("невідомий TEXT_PROVIDER=%s — використовую openai", name)
        return "openai"
    return "gemini" if GA.api_key() else "openai"


def make_provider(openai_cfg: Callable[[], dict]) -> TextProvider:
    return GeminiTextProvider() if provider_name() == "gemini" else OpenAITextProvider(openai_cfg)
