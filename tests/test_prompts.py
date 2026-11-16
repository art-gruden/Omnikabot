"""Тести готових промтів картинок (без мережі): python3 tests/test_prompts.py"""
from __future__ import annotations

import datetime as dt
import json
import sys
import tempfile
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from bot import content as C  # noqa: E402
from bot import image_prompts as IP  # noqa: E402

LIB = IP.load_library()
D = dt.date(2026, 11, 17)


def run(kind, days, **kw):
    """Імітує щоденну видачу: повертає список виданих промтів, історію веде як бот."""
    last, recent, out = {}, [], []
    for i in range(days):
        ip = IP.build(kind, D + dt.timedelta(days=i), last_used=last, recent=recent, **kw)
        last[ip.prompt_id] = float(i)
        recent.insert(0, (ip.prompt_id, ip.anchor))
        out.append(ip)
    return out


class Library(unittest.TestCase):
    def test_files(self):
        main = json.loads((IP.PROMPTS_DIR / IP.PROMPT_FILES[0]).read_text(encoding="utf-8"))
        self.assertEqual(len(main["prompts"]), 24)
        self.assertEqual(len({p["category_id"] for p in main["prompts"]}), 8)
        self.assertEqual(len(LIB), 27)
        self.assertEqual(set(IP.VISUAL_TAGS), set(LIB))

    def test_every_kind_has_prompts(self):
        self.assertEqual(set(IP.KIND_CATEGORIES), set(C.KINDS))
        cats = {p["category_id"] for p in LIB.values()}
        for kind, wanted in IP.KIND_CATEGORIES.items():
            self.assertTrue(set(wanted) <= cats, kind)
            self.assertEqual({p["category_id"] for p in IP.candidates(LIB, kind)}, set(wanted))

    def test_all_prompts_fill_in_every_month(self):
        for kind in IP.KIND_CATEGORIES:
            for p in IP.candidates(LIB, kind):
                for month in range(1, 13):
                    ip = IP.build(kind, dt.date(2026, month, 28), lib={p["id"]: p}, allow_explicit=True)
                    self.assertNotRegex(ip.prompt, r"\{\{|\}\}")
                    self.assertTrue(any(v in ip.prompt for v in IP.SEASONS[month][:4]), (p["id"], month))

    def test_missing_library_is_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(IP.PromptError):
                IP.load_library(tmp)
            (Path(tmp) / IP.PROMPT_FILES[0]).write_text("{не json", encoding="utf-8")
            with self.assertRaises(IP.PromptError):
                IP.load_library(tmp)


class Build(unittest.TestCase):
    def test_date_and_season(self):
        ip = IP.build("omnika", D, lib=LIB)
        self.assertIn("2026-11-17 (вівторок)", ip.prompt)
        self.assertIn("“17 листопада”", ip.prompt)
        self.assertIn("late autumn", ip.prompt)
        self.assertIn("none yet", ip.prompt)

    def test_theme_from_post_or_default(self):
        ip = IP.build("unity", D, theme="gratitude and calm", lib=LIB)
        self.assertIn("its meaning or mood is gratitude and calm.", ip.prompt)
        self.assertEqual(ip.symbol, "a single white feather")
        ip = IP.build("unity", D, lib=LIB)
        self.assertEqual(ip.theme, IP.DEFAULT_THEME[ip.category_id])

    def test_theme_is_sanitized(self):
        ip = IP.build("omnika", D, theme="вдячність {{DATE_ISO}}\n і спокій " + "x" * 400, lib=LIB)
        self.assertNotIn("{{", ip.prompt)
        self.assertNotIn("\n", ip.prompt)
        self.assertLessEqual(len(ip.theme), 160)

    def test_parse_theme_reply(self):
        self.assertEqual(IP.parse_theme_reply("THEME: gratitude, calm.\nSYMBOL: a white feather"),
                         ("gratitude, calm", "a white feather"))
        self.assertEqual(IP.parse_theme_reply("**Theme** — quiet joy\n- symbol: \"a candle\""), ("quiet joy", "a candle"))
        self.assertEqual(IP.parse_theme_reply("не знаю"), ("", ""))

    def test_rotation_without_repeats(self):
        for kind, cats in IP.KIND_CATEGORIES.items():
            n = len(IP.candidates(LIB, kind)) - (1 if kind == "birthday" else 0)
            ids = [ip.prompt_id for ip in run(kind, n * 2, lib=LIB)]
            self.assertEqual(len(set(ids[:n])), n, kind)            # коло без повторів
            self.assertEqual(ids[n:], ids[:n], kind)                # далі — найдавніший
        self.assertNotIn("birthday_3", [ip.prompt_id for ip in run("birthday", 10, lib=LIB)])

    def test_recent_visuals_and_anchors(self):
        out = run("omnika", 6, lib=LIB)
        for prev in out[2:5]:
            self.assertIn(IP.VISUAL_TAGS[prev.prompt_id], out[5].prompt)
        self.assertNotIn(IP.VISUAL_TAGS[out[0].prompt_id], out[5].prompt)   # у промт ідуть лише 4 останні
        self.assertEqual(len({ip.anchor for ip in out[:5]}), 5)             # деталь-якір не повторюється поспіль

    def test_exclude_and_explicit(self):
        a = IP.build("unity", D, lib=LIB)
        b = IP.build("unity", D, lib=LIB, exclude=(a.prompt_id,))
        self.assertNotEqual(a.prompt_id, b.prompt_id)
        self.assertEqual(IP.build("birthday", D, lib=LIB, variant=3).prompt_id, "birthday_3")
        self.assertEqual(IP.build("morning", D, lib=LIB, variant=3).prompt_id, "affirmation_3")
        self.assertEqual(IP.build("birthday", D, lib=LIB, allow_explicit=True,
                                  last_used={"birthday_1": 1.0, "birthday_2": 2.0}).prompt_id, "birthday_3")


class Links(unittest.TestCase):
    def test_roundtrip(self):
        ip = IP.build("omnika", D, theme="gratitude, quiet confidence", lib=LIB)
        url, short = IP.button_url(ip.prompt)
        self.assertTrue(url.startswith("https://chatgpt.com/?q=") and not short)
        self.assertEqual(parse_qs(urlparse(url).query)["q"][0], ip.prompt)
        self.assertNotIn(" ", url)

    def test_shortening(self):
        ip = IP.build("omnika", D, theme="gratitude, quiet confidence", lib=LIB)
        url, short = IP.button_url(ip.prompt, IP.SAFE_BUTTON_URL)
        self.assertTrue(short)
        self.assertLessEqual(len(url), IP.SAFE_BUTTON_URL)
        q = parse_qs(urlparse(url).query)["q"][0]
        self.assertIn("Produce a single 16:9 landscape image", q)
        self.assertIn("No text, numbers, letters", q)
        self.assertEqual(IP.button_url(ip.prompt, 300), (None, True))

    def test_keyboard_url_button(self):
        from bot.main import kb
        m = kb([[("🌐 Відкрити в ChatGPT", "https://chatgpt.com/?q=x"), ("📋 Скопіювати промт", "p:copy:ab12")]])
        row = m.inline_keyboard[0]
        self.assertEqual((row[0].url, row[0].callback_data), ("https://chatgpt.com/?q=x", None))
        self.assertEqual((row[1].url, row[1].callback_data), (None, "p:copy:ab12"))


if __name__ == "__main__":
    unittest.main(verbosity=1)
