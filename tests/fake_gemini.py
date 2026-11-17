"""Імітація Gemini API (generateContent та interactions), текст і зображення, для тестів без мережі."""
from __future__ import annotations

import asyncio
import base64
import io
import threading
import time

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from PIL import Image

KEY = "AIzaSy-test-0123456789abcdefghijklmnop"
SIZES_1K = {"16:9": (1376, 768), "1:1": (1024, 1024), "3:2": (1264, 848), "2:3": (848, 1264), "4:3": (1200, 896),
            "3:4": (896, 1200), "4:5": (928, 1152), "5:4": (1152, 928), "9:16": (768, 1376), "21:9": (1584, 672)}


def make_png(ratio: str = "16:9", size: str = "1K", color=(60, 40, 110)) -> bytes:
    w, h = SIZES_1K.get(ratio, (1024, 1024))
    k = {"1K": 1, "2K": 2, "4K": 4}.get(size, 1)
    im = Image.new("RGB", (w * k, h * k), color)
    for y in range(0, h * k, 8):                      # смуги, щоб це було «справжнє» зображення
        shade = int(120 * y / (h * k))
        im.paste((color[0] + shade, color[1] + shade // 2, color[2]), (0, y, w * k, y + 4))
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return buf.getvalue()


class FakeGemini:
    def __init__(self, port: int):
        self.port = port
        self.calls: list[dict] = []
        # Черга сценаріїв для наступних викликів; порожня — звичайна вдала відповідь.
        #   ("status", 503, "текст") | ("noimage", "NO_IMAGE") | ("slow", секунд) | ("thought",) | ("block", "SAFETY")
        self.script: list[tuple] = []          # для запитів на зображення
        self.text_script: list[tuple] = []     # для текстових запитів
        # відповіді на текстові запити (вибір за системною інструкцією)
        self.text_reply = "Доброго ранку, друзі! ☀️\n\nНехай цей день буде світлим.\n\n💛 Усмішки вам і тепла."
        self.marks_reply = ("Доброго ранку, друзі! ☀️\n\n**Нехай цей день буде світлим.**\n\n💛 Усмішки вам і тепла.")
        self.prompt_reply = ("Soft watercolor sunrise over calm hills, warm golden light, gentle pink and amber "
                             "gradients, airy mist, serene low-detail upper area, horizontal composition.")
        self.from_text_reply = ("A quiet lakeside clearing at first light, two smooth stones at the water edge, "
                                "copper leaves drifting on the surface, warm low sun behind bare birches, "
                                "calm empty sky in the upper third, serene and grateful mood.")
        self.theme_reply = "THEME: gratitude and quiet morning light\nSYMBOL: a single white feather"
        self.style_reply = "Тон теплий і сердечний, звертання «друзі», 3 абзаци, емодзі на початку абзаців."
        self.lock = threading.Lock()
        self.app = self._app()
        self.server = None

    def _next(self, what: str = "image"):
        q = self.script if what == "image" else self.text_script
        with self.lock:
            return q.pop(0) if q else None

    def _app(self) -> FastAPI:
        app = FastAPI()

        def wants_image(body: dict) -> bool:
            cfg = (body.get("generationConfig") or {}).get("responseModalities") or []
            return "IMAGE" in cfg or (body.get("response_format") or {}).get("type") == "image"

        def text_for(body: dict) -> str:
            if "system_instruction" in body:                      # interactions
                sys_prompt = body["system_instruction"]
            else:
                sys_prompt = " ".join(p.get("text", "") for p in (body.get("systemInstruction") or {}).get("parts", []))
            low = sys_prompt.lower()
            if "аналізуєш стиль" in low:
                return self.style_reply
            if "THEME:" in sys_prompt and "SYMBOL:" in sys_prompt:
                return self.theme_reply
            if "генератора зображень gemini" in low:              # промт за текстом посту
                return self.from_text_reply
            if "промт" in low and "зображен" in low:
                return self.prompt_reply
            if "двома зірочками" in low:
                return self.marks_reply
            return self.text_reply

        async def handle(request: Request, api: str, model: str):
            body = await request.json()
            key = request.headers.get("x-goog-api-key", "")
            what = "image" if wants_image(body) else "text"
            with self.lock:
                self.calls.append({"api": api, "model": model, "body": body, "key": key, "what": what,
                                   "url": str(request.url), "revision": request.headers.get("api-revision", "")})
            if key != KEY:
                return JSONResponse({"error": {"code": 400, "status": "INVALID_ARGUMENT",
                                               "message": "API key not valid. Please pass a valid API key."}}, 400)
            step = self._next(what)
            if step and step[0] == "status":
                return JSONResponse({"error": {"code": step[1], "message": step[2] if len(step) > 2 else "error",
                                               "status": "UNAVAILABLE"}}, step[1])
            if step and step[0] == "slow":
                await asyncio.sleep(step[1])
            if what == "text":
                if step and step[0] == "notext":
                    return JSONResponse({"candidates": [{"content": {"parts": []},
                                                         "finishReason": step[1] if len(step) > 1 else "MAX_TOKENS"}]})
                out = text_for(body)
                if api == "interactions":
                    return JSONResponse({"id": "t1", "status": "completed", "steps": [
                        {"type": "model_output", "content": [{"type": "text", "text": out}]}]})
                parts = [{"text": out}]
                if step and step[0] == "thought":
                    parts.insert(0, {"thought": True, "text": "думаю над відповіддю"})
                return JSONResponse({"candidates": [{"content": {"role": "model", "parts": parts},
                                                     "finishReason": "STOP"}],
                                     "usageMetadata": {"totalTokenCount": 220}})
            if api == "interactions":
                fmt = body.get("response_format") or {}
                png = base64.b64encode(make_png(fmt.get("aspect_ratio", "1:1"), fmt.get("image_size", "1K"))).decode()
                return JSONResponse({"id": "i1", "status": "completed", "steps": [
                    {"type": "model_output", "content": [{"type": "image", "mime_type": "image/png", "data": png}]}]})
            cfg = (body.get("generationConfig") or {}).get("imageConfig") or {}
            ratio, size = cfg.get("aspectRatio", "1:1"), cfg.get("imageSize", "1K")
            if step and step[0] == "block":
                return JSONResponse({"promptFeedback": {"blockReason": step[1]}})
            if step and step[0] == "noimage":
                return JSONResponse({"candidates": [{"content": {"parts": [{"text": "I can't draw that."}]},
                                                     "finishReason": step[1]}]})
            parts = [{"inlineData": {"mimeType": "image/png", "data": base64.b64encode(make_png(ratio, size)).decode()}}]
            if step and step[0] == "thought":       # чернетка-«думка» перед остаточним зображенням
                draft = base64.b64encode(make_png("1:1", "1K", (10, 90, 30))).decode()
                parts.insert(0, {"thought": True, "inlineData": {"mimeType": "image/png", "data": draft}})
            return JSONResponse({"candidates": [{"content": {"role": "model", "parts": parts}, "finishReason": "STOP"}],
                                 "usageMetadata": {"totalTokenCount": 1300}})

        @app.post("/v1beta/models/{name:path}")
        async def generate_content(name: str, request: Request):
            model, _, action = name.partition(":")
            if action != "generateContent":
                return JSONResponse({"error": {"code": 404, "message": "unknown method"}}, 404)
            if not model.startswith("gemini-"):
                return JSONResponse({"error": {"code": 404, "status": "NOT_FOUND",
                                               "message": f"models/{model} is not found"}}, 404)
            return await handle(request, "generateContent", model)

        @app.post("/v1beta/interactions")
        async def interactions(request: Request):
            body = await request.json()
            return await handle(request, "interactions", body.get("model", ""))

        return app

    def start(self):
        cfg = uvicorn.Config(self.app, host="127.0.0.1", port=self.port, log_level="warning")
        self.server = uvicorn.Server(cfg)
        threading.Thread(target=self.server.run, daemon=True).start()
        for _ in range(100):
            if self.server.started:
                return
            time.sleep(0.05)
        raise RuntimeError("fake gemini не стартував")

    def stop(self):
        if self.server:
            self.server.should_exit = True

    def mark(self) -> int:
        with self.lock:
            return len(self.calls)

    def of(self, what: str, since: int = 0) -> list[dict]:
        with self.lock:
            return [c for c in self.calls[since:] if c["what"] == what]

    def since(self, n: int) -> list[dict]:
        with self.lock:
            return self.calls[n:]
