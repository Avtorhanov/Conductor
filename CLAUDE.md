# Дирижёр — контекст для Claude Code

Оркестратор диагностики и восстановления прошивок микросхем. Python 3.9+, из библиотек только PyYAML.
Внешние инструменты (flashrom, probe-rs, minipro, sigrok, binwalk…) вызываются отдельными процессами через плагины и не встраиваются.

## В начале каждой сессии
1. Прочитай `docs/STATUS.md`: текущее состояние, что не проверено на железе, следующие шаги.
2. Полный контекст и правила — `docs/SESSION_PROMPT.md` (Блок 1). Принципы — `PRINCIPLES.md`. Руководство разработчика — `docs/MANUAL.md`, часть 2. Установка — `docs/INSTALL.md`.

## Команды
- Тесты: `python3 -m unittest -v`. Они должны проходить до любого коммита.
- Учебный режим без железа: `python3 -m dirizher sim reset` и `python3 -m dirizher --sim run`.
- Список плагинов, профилей и навыков: `python3 -m dirizher plugins`, `profiles`, `skills`.
- Самопроверка: `python3 -m dirizher doctor [--heal]`. Справка: `python3 -m dirizher help [слово]`.
- Интерфейсы: `python3 -m dirizher web` (браузер: ПК, телефон, экран прибора), `python3 -m dirizher` (мастер в терминале).

## Карта кода
- `dirizher/core/pipeline.py` — Conductor, сценарий из 7 шагов. Запись — только через `Conductor.apply()`.
- `dirizher/core/plugin.py` — интерфейс плагина: `available / probe / read / write`.
- `dirizher/plugins/` — лезвия (`flashrom_spi`, `simulator`, `probe_rs`, `openocd`, `avrdude`, `esptool_flash`, `minipro`, `bp5_i2c`, `_template`); общие помощники — `_common.py`. Помощник-процесс BP5 — `dirizher/helpers/`, замер sigrok — `dirizher/extras/`. Карточки — `docs/INTEGRATIONS.md`.
- `dirizher/core/checks.py` — проверки (`@register`). `core/safety.py` — условия записи.
- `core/storage.py` — копии по SHA-256. `core/memory.py` — SQLite. `core/learning.py` — обучение.
- `core/ui.py` — весь ввод-вывод. `profiles/` — база знаний (YAML).
- `dirizher/core/params.py` — карта параметров; `Conductor.read_params/tune` — сценарий «Настройка».
- `dirizher/core/skills.py` — навыки `skills/<id>/` и сторож кода; образец навыка — `skills/modbus_rtu/`.
- `dirizher/workshop/` — капот (`hood.py`), навыки из шаблонов, справка (`help/*.md`), самопроверка (`doctor.py`).
- `dirizher/workshop/catalog.py` + `catalog/engines|adapters/*.yaml` — справочник: движки (авторы, лицензии, принцип) и переходники (рисунки — данными: wiring, package, header, chain). Навык может принести карточки в `skills/<id>/catalog/`.
- `tools/build_pages.py` + `tools/pages_demo.js` — демо для GitHub Pages: записывает ходы настоящего ядра на симуляторе; публикует `.github/workflows/pages.yml`.
- `dirizher/interface/` — слой поверх ядра: `guide.yaml` + `guide.py` (Штурман: вопросы, подсказки, уроки), `web.py` + `static/index.html` (браузер), `wizard.py` (терминал), `builder.py` (конструктор профиля). Ядро о нём не знает: интерфейс подставляет свой `ui`.
- `dirizher/interface/playground.yaml` + `playground.py` + `static/playground.html` — учебная площадка (тренажёр по маршрутам: данные, проверка, текстовый режим, страница; `python3 -m dirizher playground [--export файл]`). Строки «сеансов вживую» сверяются с ядром в `tests/test_playground.py`.

## Жёсткие правила
- `probe()` только читает. Никакой записи без копии «до», сверки, копии «после» и подтверждения словом.
- Новый инструмент — это плагин, новый чип или плата — это профиль. Ядро меняется только при необходимости, с объяснением.
- Копии и данные в `data/` никогда не удаляются и не правятся. Не добавлять обходов подтверждения и автозаписи.
- Если формат вывода программы, ID чипа или распиновка не проверены, ставь комментарий `ПРОВЕРИТЬ НА ЖЕЛЕЗЕ` и делай запись в STATUS.
- Каждая возможность — с тестами без железа: симулятор, `AutoUI`, подставная программа в `PATH`.
- Тексты для пользователя — по-русски; идентификаторы — по-английски.
- Никаких обходов защиты от чтения и проверки подписей прошивок.

## В конце сессии
Обнови `docs/STATUS.md` (включая журнал сессий) и `CHANGELOG.md`, прогони тесты, перечисли, что осталось проверить на железе.
