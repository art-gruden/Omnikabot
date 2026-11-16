# Готові промти картинок

Бот не складає промт картинки з нуля: він бере готовий із цих файлів і підставляє дату, сезон і тему посту.

| Файл | Що всередині |
|---|---|
| `morning-image-prompts-24.json` | 8 категорій по 3 варіанти: `calendar`, `cozy`, `nature`, `heart`, `daily_energy`, `intention`, `affirmation`, `special` |
| `birthday-image-prompts.json` | 3 фони для привітань із днем народження (`birthday_3` — ювілейний) |

## Яка рубрика які категорії бере

| Рубрика бота | Категорії |
|---|---|
| 🎂 День народження (`/birthday`) | `birthday` |
| 🌅 Ранкове завантаження (`/morning`) і «Новий фон» під звичайною листівкою | `affirmation` |
| ☀️ Ранкове привітання «Омніка» (`/omnika`) | `calendar`, `daily_energy`, `nature`, `cozy` |
| 💗 Єдність сердець (`/unity`) | `heart`, `intention` |

Відповідність задано в `KIND_CATEGORIES` у `bot/image_prompts.py`. Категорія `special` (особлива дата) поки не прив'язана до жодної рубрики: її промти потребують полів `EVENT_NAME` і `EVENT_SYMBOL`, які має вводити людина.

## Поля, які підставляє бот

| Поле | Звідки |
|---|---|
| `DATE_ISO`, `DATE_UK`, `WEEKDAY_UK` | сьогоднішня дата за часовим поясом бота |
| `SEASON`, `SEASON_DETAILS`, `SEASON_PALETTE`, `SEASONAL_BOTANICALS`, `SEASONAL_FRUIT` | таблиця `SEASONS` за місяцем |
| `DAY_THEME`, `THEME_SYMBOL` | GPT за текстом посту (один короткий запит); без ключа OpenAI — типова тема категорії |
| `CREATIVE_ANCHOR` | набір `ANCHORS` для сезону; останні використані пропускаються |
| `RECENT_VISUALS` | сюжети чотирьох останніх виданих промтів рубрики |

## Як змінити чи додати промт

Відредагуйте JSON: бот перечитує файли сам, перезапуск не потрібен (у Docker — перезберіть образ або змонтуйте папку й задайте `IMAGE_PROMPTS_DIR`). Обов'язкові ключі запису: `id`, `category_id`, `category`, `variant`, `name`, `prompt`. Для нового `id` додайте короткий опис сюжету у `VISUAL_TAGS`, інакше в `RECENT_VISUALS` піде сам `id`. Перевірка: `python3 tests/test_prompts.py`.

Подивитися, що вийде на дату: `python -m bot.image_prompts omnika 2026-11-17 gratitude and calm`.
