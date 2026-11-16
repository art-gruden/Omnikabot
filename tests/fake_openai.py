"""Імітація OpenAI API (chat/completions, images/generations, models) для тестів без мережі."""
from __future__ import annotations

import base64
import io
import threading
import time

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from PIL import Image

KEY = "sk-test-0123456789abcdefghij"

MODELS = ["gpt-5.4-mini", "gpt-5.4", "gpt-5.4-nano", "gpt-image-1.5", "gpt-image-1", "gpt-image-1-mini"]


def make_png(size: str) -> bytes:
    w, h = (int(x) for x in size.split("x"))
    im = Image.new("RGB", (w, h), (46, 24, 72))
    for y in range(h):                       # простий градієнт, щоб картинка була «справжня»
        k = y / h
        for x in range(0, w, 64):
            im.paste((int(40 + 180 * k), int(20 + 90 * k), int(90 + 120 * (1 - k))), (x, y, min(x + 64, w), y + 1))
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return buf.getvalue()


class FakeOpenAI:
    def __init__(self, port: int):
        self.port = port
        self.calls: list[dict] = []
        # {"chat": (429, "текст", скільки_разів)} — скільки разів поспіль повертати помилку
        self.fail_next: dict[str, tuple] = {}
        self.text_reply = "Доброго ранку, друзі! ☀️\n\nНехай цей день буде світлим.\n\n💛 Усмішки вам і тепла."
        self.prompt_reply = ("Soft watercolor sunrise over calm hills, warm golden light, gentle pink and amber "
                             "gradients, airy mist, serene low-detail center, horizontal composition.")
        self.theme_reply = "THEME: gratitude and quiet morning light\nSYMBOL: a single white feather"
        self.style_reply = "Тон теплий і сердечний, звертання «друзі», 3 абзаци, емодзі на початку абзаців."
        self.lock = threading.Lock()
        self.app = self._app()
        self.server = None

    def _app(self) -> FastAPI:
        app = FastAPI()

        @app.post("/v1/chat/completions")
        async def chat(request: Request):
            body = await request.json()
            auth = request.headers.get("authorization", "")
            with self.lock:
                self.calls.append({"what": "chat", "body": body, "auth": auth})
            if auth != f"Bearer {KEY}":
                return JSONResponse({"error": {"message": "Incorrect API key provided"}}, 401)
            fail = self._fail("chat")
            if fail:
                return JSONResponse({"error": {"message": fail[1]}}, fail[0])
            if "max_tokens" in body:          # нові моделі такого не приймають
                return JSONResponse({"error": {"message": "Unsupported parameter: 'max_tokens'"}}, 400)
            sys_prompt = body["messages"][0]["content"]
            if "опиши стиль" in sys_prompt.lower() or "аналізуєш стиль" in sys_prompt.lower():
                out = self.style_reply
            elif "THEME:" in sys_prompt and "SYMBOL:" in sys_prompt:
                out = self.theme_reply
            elif "промт" in sys_prompt.lower() and "зображен" in sys_prompt.lower():
                out = self.prompt_reply
            else:
                out = self.text_reply
            return JSONResponse({"id": "c1", "choices": [{"index": 0, "finish_reason": "stop",
                                                          "message": {"role": "assistant", "content": out}}],
                                 "usage": {"total_tokens": 100}})

        @app.post("/v1/images/generations")
        async def images(request: Request):
            body = await request.json()
            auth = request.headers.get("authorization", "")
            with self.lock:
                self.calls.append({"what": "image", "body": body, "auth": auth})
            if auth != f"Bearer {KEY}":
                return JSONResponse({"error": {"message": "Incorrect API key provided"}}, 401)
            fail = self._fail("image")
            if fail:
                return JSONResponse({"error": {"message": fail[1]}}, fail[0])
            size = body.get("size") or "1024x1024"
            data = base64.b64encode(make_png(size)).decode()
            return JSONResponse({"created": int(time.time()), "data": [{"b64_json": data}]})

        @app.get("/v1/models")
        async def models(request: Request):
            with self.lock:
                self.calls.append({"what": "models", "body": {}, "auth": request.headers.get("authorization", "")})
            if request.headers.get("authorization") != f"Bearer {KEY}":
                return JSONResponse({"error": {"message": "Incorrect API key provided"}}, 401)
            return JSONResponse({"data": [{"id": m, "object": "model"} for m in MODELS]})

        return app

    def _fail(self, what: str):
        with self.lock:
            f = self.fail_next.get(what)
            if not f:
                return None
            times = (f[2] if len(f) > 2 else 1) - 1
            if times <= 0:
                self.fail_next.pop(what, None)
            else:
                self.fail_next[what] = (f[0], f[1], times)
            return f

    def start(self):
        cfg = uvicorn.Config(self.app, host="127.0.0.1", port=self.port, log_level="warning")
        self.server = uvicorn.Server(cfg)
        threading.Thread(target=self.server.run, daemon=True).start()
        for _ in range(100):
            if self.server.started:
                return
            time.sleep(0.05)
        raise RuntimeError("fake openai не стартував")

    def stop(self):
        if self.server:
            self.server.should_exit = True

    def of(self, what: str) -> list[dict]:
        with self.lock:
            return [c for c in self.calls if c["what"] == what]

    def mark(self) -> int:
        with self.lock:
            return len(self.calls)

    def since(self, n: int) -> list[dict]:
        with self.lock:
            return self.calls[n:]
