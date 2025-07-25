# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from datetime import datetime

from pathlib import Path
from random import SystemRandom

import requests

from python_ghost_cursor.playwright_async import create_cursor
from playwright.async_api import async_playwright
from playwright_stealth import stealth

# --- универсальный импорт исключений Playwright (+ таймаут) ------------------
try:  # Playwright ≥ 1.43.0
    from playwright.async_api import Error as PlaywrightError
    from playwright.async_api import TimeoutError as PWTimeoutError
except ImportError:  # более старые версии
    from playwright.async_api import PlaywrightError  # type: ignore
    from playwright.async_api import TimeoutError as PWTimeoutError  # type: ignore
# ------------------------------------------------------------------------------

from samokat_config import CFG, load_cfg
from utils import RunContext, log, make_log_file, version_check


_REQUIRED = {
    "playwright": ("1.44", "2.0"),
    "playwright-stealth": ("2.0.0", "3.0"),
}

version_check(_REQUIRED)


_rnd = SystemRandom()  # единый генератор на весь скрипт


def _to_bool(val: str | bool) -> bool:
    if isinstance(val, bool):
        return val
    return str(val).lower() in {"1", "true", "yes", "on"}


try:
    from pathlib import Path

    params = json.load(sys.stdin)
    json_headless_raw = params.get("headless")
    if json_headless_raw is not None:
        try:
            ctx_json_headless = _to_bool(json_headless_raw)
            if isinstance(
                json_headless_raw, str
            ) and json_headless_raw.strip().lower() not in {
                "1",
                "true",
                "yes",
                "on",
                "0",
                "false",
                "no",
                "off",
            }:
                raise ValueError
        except ValueError:
            print('{"error":"bad headless value"}')
            sys.exit(1)
    else:
        ctx_json_headless = None
    user_phone = params.get("user_phone", "")
    logs_dir = os.path.join(os.path.dirname(__file__), "Logs")
    os.makedirs(logs_dir, exist_ok=True)
    log_file = make_log_file(logs_dir, user_phone)
    log_start = os.path.getsize(log_file) if os.path.exists(log_file) else 0

    webhook_url = params.get("Webhook", "")
    cli_args = sys.argv[1:]
    overrides = {}
    cli_proxy = None
    for arg in cli_args:
        if arg.startswith("--proxy="):
            cli_proxy = arg.split("=", 1)[1]
        elif "=" in arg:
            k, v = arg.split("=", 1)
            overrides[k] = v
    json_proxy = params.get("proxy", "").strip() or None
    proxy_url = cli_proxy or json_proxy or None
    ctx = RunContext(
        cli_overrides=overrides,
        cli_proxy=cli_proxy,
        proxy_url=proxy_url,
        log_file=log_file,
        log_start_pos=log_start,
        json_headless=ctx_json_headless,
    )
    log(f"[INFO] Получены параметры: {params}", ctx)
    load_cfg(base_dir=Path(__file__).parent, cli_overrides=overrides, ctx=ctx)
    if proxy_url:
        log(f"[INFO] Proxy enabled: {proxy_url}", ctx)
    if ctx.json_headless is not None:
        log(f"[INFO] headless overridden by JSON → {ctx.json_headless}", ctx)
except Exception as e:
    print(f"[ERROR] Не удалось получить JSON из stdin: {e}")
    sys.exit(1)

# === Load configuration values ===
# Configuration values are read directly from CFG at call sites

# Дополнительные параметры из полученного JSON
phone_from_Avito = params.get("phone_from_Avito", "")
phone_for_form = phone_from_Avito if phone_from_Avito else user_phone

user_age = params.get("user_age", "")
user_city = params.get("user_city", "")
user_name = params.get("user_name", "")
birth_date = params.get("birth_date", "")
user_gender = params.get("user_gender", "")
user_courier_type = params.get("user_courier_type", "")

if sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")
if sys.stderr.encoding.lower() != "utf-8":
    sys.stderr.reconfigure(encoding="utf-8")


# === P-6. Единый фильтр для блокировки «лишних» запросов ===
async def should_abort(route, ctx: RunContext):
    """
    Абортирует:
      • любые ресурсы с resource_type in {"image", "media"}
      • запросы, у которых в header-ах:
            status 204/206  ИЛИ  Content-Length < 512
    Всё остальное пропускает.
    """
    req = route.request
    r_type = req.resource_type
    headers = req.headers
    clen = int(headers.get("content-length", "1024"))
    status = int(headers.get(":status", 200))

    url = req.url
    must_abort = (
        (r_type in ("image", "media"))
        or (status in (204, 206))
        or (clen < 512)
        or any(p in url for p in CFG["BLOCK_PATTERNS"])
    )

    if not must_abort:
        await route.continue_()
        return

    if not ctx.first_abort_logged:
        log(
            f"[INFO] ABORT {req.method} {req.url} "
            f"({r_type}, len≈{headers.get('content-length','?')})",
            ctx,
        )
        ctx.first_abort_logged = True

    await route.abort()


# Основная рабочая директория
WORK_DIR = os.path.dirname(__file__)


async def get_form_position(page, selector="div.form-wrapper"):
    el = await page.query_selector(selector)
    if not el:
        return None
    box = await el.bounding_box()
    if not box:
        return None
    return {"top": box["y"], "center": box["y"] + box["height"] / 2}


def send_webhook(result, webhook_url, ctx: RunContext):
    if webhook_url:
        e = None
        for attempt in range(3):
            try:
                resp = requests.post(
                    webhook_url, json=result, timeout=CFG["WEBHOOK_TIMEOUT"]
                )
                if 200 <= resp.status_code < 300:
                    return
                raise Exception(f"Status {resp.status_code}")
            except Exception as exc:
                e = exc
                log(f"[WARN] webhook fail, retry {attempt + 1} in 60s: {e}", ctx)
                time.sleep(60)
        log(f"[FATAL] webhook 3rd fail: {e}", ctx)


# ==== тайм-зона по крупным городам РФ ====
TZ_BY_CITY = {
    "Москва": "Europe/Moscow",
    "Санкт-Петербург": "Europe/Moscow",
    "Нижний Новгород": "Europe/Moscow",
    "Казань": "Europe/Moscow",
    "Воронеж": "Europe/Moscow",
    "Ростов-на-Дону": "Europe/Moscow",
    "Волгоград": "Europe/Volgograd",
    "Самара": "Europe/Samara",  # UTC+4
    "Екатеринбург": "Asia/Yekaterinburg",  # UTC+5
    "Челябинск": "Asia/Yekaterinburg",
    "Уфа": "Asia/Yekaterinburg",
    "Пермь": "Asia/Yekaterinburg",
    "Омск": "Asia/Omsk",  # UTC+6
    "Новосибирск": "Asia/Novosibirsk",  # UTC+7
    "Красноярск": "Asia/Krasnoyarsk",
}
tz = TZ_BY_CITY.get(user_city, "Europe/Moscow")

# === fingerprint values ===
offset = _rnd.choice([-1, 0, 1])
if offset == -1:
    tz = "Europe/Kaliningrad"
elif offset == 1:
    tz = "Europe/Samara"
else:
    tz = "Europe/Moscow"

FP_PLATFORM = "Win32"
FP_DEVICE_MEMORY = 8
FP_HARDWARE_CONCURRENCY = 8
FP_LANGUAGES = ["ru-RU", "ru"]
FP_ACCEPT_LANGUAGE = "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7"
FP_WEBGL_VENDOR = "Intel Inc."
FP_WEBGL_RENDERER = "Intel Iris OpenGL Engine"
log(f"[INFO] Platform: {FP_PLATFORM}", ctx)
log(f"[INFO] Device Memory: {FP_DEVICE_MEMORY}", ctx)
log(f"[INFO] Hardware Concurrency: {FP_HARDWARE_CONCURRENCY}", ctx)
log(f"[INFO] Languages: {FP_ACCEPT_LANGUAGE}", ctx)
log(f"[INFO] Timezone: {tz}", ctx)
log(f"[INFO] WebGL Vendor: {FP_WEBGL_VENDOR}", ctx)
log(f"[INFO] WebGL Renderer: {FP_WEBGL_RENDERER}", ctx)


# ====================================================================================
# Этап 3.
# ====================================================================================


def _human_delay() -> float:
    """Возвращает задержку перед следующим символом, s."""
    base = _rnd.lognormvariate(
        CFG["HUMAN_DELAY_μ"], CFG["HUMAN_DELAY_σ"]
    )  # медиана ≈0.14
    return max(0.09, min(base, 0.22))


async def human_type(page, selector: str, text: str, ctx: RunContext):
    """Вводит `text` в элемент `selector` максимально «по-человечески»."""
    await page.focus(selector)
    n = len(text)
    coef = 0.6 if n <= 3 else 1.15 if n > 20 else 1.0
    total = 0.0

    for char in text:
        delay = _human_delay() * coef
        if not char.isdigit() and _rnd.random() < CFG["TYPO_PROB"]:
            await page.keyboard.type(char, delay=0)
            await asyncio.sleep(delay)
            await page.keyboard.press("Backspace")
            total += delay
        await page.keyboard.type(char, delay=0)
        await asyncio.sleep(delay)
        total += delay

    log(f'[DEBUG] typing "{text}" len={n} total_time={total:.2f}', ctx)


async def fill_full_name(page, name, ctx: RunContext, retries=3):
    for attempt in range(retries):
        try:
            input_box = page.get_by_placeholder("ФИО")
            await human_move_cursor(page, input_box, ctx)
            await input_box.click()
            await page.wait_for_timeout(100)
            await input_box.fill("")
            await human_type(page, 'input[name="user_name"]', name, ctx)
            value = await input_box.input_value()
            if value.strip() == name.strip():
                return True
            await page.wait_for_timeout(200)
        except Exception as e:
            log(f"[WARN] fill_full_name attempt {attempt+1} failed: {e}", ctx)
    log("[ERROR] Не удалось заполнить поле ФИО", ctx)
    return False


async def fill_city(page, city, ctx: RunContext, retries=3):
    city_lower = city.strip().lower()
    for attempt in range(retries):
        try:
            input_box = page.get_by_placeholder("Выберите город").nth(0)
            await human_move_cursor(page, input_box, ctx)
            await input_box.click()
            await page.wait_for_timeout(100)
            await input_box.fill("")
            part_city = get_partial_city(city)
            await human_type_city_autocomplete(
                page, 'input[name="user_city"]', part_city, ctx
            )

            try:
                await page.wait_for_selector(
                    ".form-list-item", timeout=CFG["SELECT_ITEM_TIMEOUT"]
                )
            except PWTimeoutError:
                pass

            try:
                items = await page.query_selector_all(".form-list-item")
                for it in items:
                    txt = (await it.inner_text()).strip().lower()
                    if txt == city_lower:
                        await it.click()
                        break
            except Exception:
                pass

            value = await input_box.input_value()
            if value.strip().lower() == city_lower:
                return True
            await page.wait_for_timeout(200)
        except Exception as e:
            log(f"[WARN] fill_city attempt {attempt+1} failed: {e}", ctx)
    log("[ERROR] Не удалось выбрать город", ctx)
    return False


async def fill_phone(page, phone, ctx: RunContext, retries=3):
    for attempt in range(retries):
        try:
            input_box = page.get_by_placeholder("+7 (900) 000 00-00")
            await human_move_cursor(page, input_box, ctx)
            await input_box.click()
            await page.wait_for_timeout(100)
            await input_box.fill("")
            await human_type(page, 'input[name="user_phone"]', phone, ctx)
            value = await input_box.input_value()
            if value.strip() == phone.strip():
                return True
            await page.wait_for_timeout(200)
        except Exception as e:
            log(f"[WARN] fill_phone attempt {attempt+1} failed: {e}", ctx)
    log("[ERROR] Не удалось заполнить поле Телефон", ctx)
    return False


async def fill_gender(page, gender, ctx: RunContext, retries=3):
    for attempt in range(retries):
        try:
            input_box = page.get_by_placeholder("Выберите пол")
            await human_move_cursor(page, input_box, ctx)
            await input_box.click()
            await page.wait_for_timeout(100)
            try:
                await page.get_by_text(gender, exact=True).click(
                    timeout=CFG["SELECT_ITEM_TIMEOUT"]
                )
            except Exception:
                value = await input_box.input_value()
                if value.strip().lower() == gender.strip().lower():
                    return True
                continue
            value = await input_box.input_value()
            if value.strip().lower() == gender.strip().lower():
                return True
            await page.wait_for_timeout(200)
        except Exception as e:
            log(f"[WARN] fill_gender attempt {attempt+1} failed: {e}", ctx)
    log("[ERROR] Не удалось выбрать пол", ctx)
    return False


async def fill_age(page, age, ctx: RunContext, retries=3):
    for attempt in range(retries):
        try:
            input_box = page.get_by_placeholder("0")
            await human_move_cursor(page, input_box, ctx)
            await input_box.click()
            await page.wait_for_timeout(100)
            await input_box.fill("")
            await human_type(page, 'input[name="user_age"]', age, ctx)
            value = await input_box.input_value()
            if value.strip() == age.strip():
                return True
            await page.wait_for_timeout(200)
        except Exception as e:
            log(f"[WARN] fill_age attempt {attempt+1} failed: {e}", ctx)
    log("[ERROR] Не удалось заполнить поле Возраст", ctx)
    return False


async def fill_courier_type(page, courier_type, ctx: RunContext, retries=3):
    for attempt in range(retries):
        try:
            input_box = page.get_by_placeholder("Выберите тип")
            await human_move_cursor(page, input_box, ctx)
            await input_box.click()
            await page.wait_for_timeout(100)
            try:
                await page.get_by_text(courier_type, exact=True).click(
                    timeout=CFG["SELECT_ITEM_TIMEOUT"]
                )
            except Exception:
                value = await input_box.input_value()
                if value.strip().lower() == courier_type.strip().lower():
                    return True
                continue
            value = await input_box.input_value()
            if value.strip().lower() == courier_type.strip().lower():
                return True
            await page.wait_for_timeout(200)
        except Exception as e:
            log(f"[WARN] fill_courier_type attempt {attempt+1} failed: {e}", ctx)
    log("[ERROR] Не удалось выбрать тип курьера", ctx)
    return False


async def fill_policy_checkbox(page, ctx: RunContext, retries=3):
    for attempt in range(retries):
        try:
            checkbox = await page.query_selector(".form-policy-checkbox")
            if checkbox:
                await checkbox.click()
                # Проверка: SVG-галочка появляется в DOM после клика
                checked = await page.query_selector(".form-policy-checkbox svg")
                if checked:
                    return True
            await page.wait_for_timeout(200)
        except Exception as e:
            log(f"[WARN] fill_policy_checkbox attempt {attempt+1} failed: {e}", ctx)
    log("[ERROR] Не удалось поставить галочку политики", ctx)
    return False


def get_partial_city(city_name):
    min_percent = 70
    max_percent = 85
    percent = _rnd.randint(min_percent, max_percent)
    cut = max(1, int(len(city_name) * percent / 100))
    return city_name[:cut]


async def human_type_city_autocomplete(page, selector: str, text: str, ctx: RunContext):
    """То же, что human_type, но без опечаток (важно для автодополнения); задержки – такие же, лог тот же."""
    await page.focus(selector)
    n = len(text)
    coef = 0.6 if n <= 3 else 1.15 if n > 20 else 1.0
    total = 0.0

    for char in text:
        delay = _human_delay() * coef
        await page.keyboard.type(char, delay=0)
        await asyncio.sleep(delay)
        total += delay

    log(f'[DEBUG] typing "{text}" len={n} total_time={total:.2f}', ctx)


async def human_move_cursor(page, el, ctx: RunContext):
    """Плавно ведёт курсор к случайной точке внутри элемента ``el``."""
    b = await el.bounding_box()
    if not b:
        return
    target_x = b["x"] + _rnd.uniform(8, b["width"] - 8)
    target_y = b["y"] + _rnd.uniform(8, b["height"] - 8)

    cur = page.mouse.position
    pivots = []
    if _rnd.random() < 0.7:
        pivots.append(
            (
                (cur[0] + target_x) / 2 + _rnd.uniform(-15, 15),
                (cur[1] + target_y) / 2 + _rnd.uniform(-15, 15),
            )
        )
    pivots.append((target_x, target_y))
    cur_x, cur_y = cur
    for x, y in pivots:
        await page.mouse.move(x, y, steps=_rnd.randint(8, 20))
        cur_x, cur_y = x, y
    await page.mouse.move(
        cur_x + _rnd.uniform(-4, 4),
        cur_y + _rnd.uniform(-3, 3),
        steps=3,
    )
    sel = (
        await el.get_attribute("name")
        or await el.get_attribute("placeholder")
        or await el.evaluate("el => el.className")
        or "element"
    )
    log(f"[INFO] Курсор к {sel} ({int(target_x)},{int(target_y)})", ctx)


async def emulate_user_reading(page, total_time, ctx: RunContext):
    start_time = asyncio.get_event_loop().time()
    blocks = [".hero", ".about", ".benefits", ".form-wrapper"]
    height = await page.evaluate("document.body.scrollHeight")
    current_y = 0

    while asyncio.get_event_loop().time() - start_time < total_time:
        action = _rnd.choices(
            ["scroll_down", "scroll_up", "pause", "mouse_wiggle", "to_block"],
            weights=[0.48, 0.14, 0.20, 0.06, 0.12],
        )[0]

        if action == "scroll_down":
            step = _rnd.choice(
                [
                    _rnd.randint(*CFG["SCROLL_STEP"]["down1"]),
                    _rnd.randint(*CFG["SCROLL_STEP"]["down2"]),
                ]
            )
            current_y = min(current_y + step, height - 1)
            if step >= 400:
                parts = _rnd.randint(3, 6)
                for _ in range(parts):
                    await page.mouse.wheel(0, step / parts)
                    await asyncio.sleep(_rnd.uniform(0.06, 0.12))
            else:
                await page.mouse.wheel(0, step)
            log(f"[INFO] wheel вниз на {step}", ctx)
            await asyncio.sleep(_rnd.uniform(0.7, 1.7))
        elif action == "scroll_up":
            step = _rnd.randint(*CFG["SCROLL_STEP"]["up"])
            current_y = max(current_y - step, 0)
            await page.mouse.wheel(0, -step)
            log(f"[INFO] wheel вверх на {step}", ctx)
            await asyncio.sleep(_rnd.uniform(0.5, 1.1))
        elif action == "pause":
            t = _rnd.uniform(1.2, 3.8)
            log(f"[INFO] Пауза {t:.1f}", ctx)
            await asyncio.sleep(t)
        elif action == "mouse_wiggle":
            x = _rnd.randint(100, 1200)
            y = _rnd.randint(100, 680)
            dx = _rnd.randint(-10, 10)
            dy = _rnd.randint(-8, 8)
            await page.mouse.move(x, y, steps=_rnd.randint(4, 10))
            await asyncio.sleep(_rnd.uniform(0.08, 0.18))
            await page.mouse.move(x + dx, y + dy, steps=2)
            log(f"[INFO] Мышь дрожит ({x},{y})", ctx)
            await asyncio.sleep(_rnd.uniform(0.10, 0.22))
        else:
            sel = _rnd.choice(blocks)
            el = await page.query_selector(sel)
            if el:
                box = await el.bounding_box()
                if box:
                    x = box["x"] + _rnd.uniform(12, box["width"] - 12)
                    y = box["y"] + _rnd.uniform(12, box["height"] - 12)
                    await page.mouse.move(x, y, steps=_rnd.randint(10, 22))
                    log(f"[INFO] Мышь на {sel} ({int(x)},{int(y)})", ctx)
                    await asyncio.sleep(_rnd.uniform(0.4, 1.3))


# --- 000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000 ---
# --- 000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000 ---
# --- 000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000 ---


# ====================================================================================
# Плавный, крупный и потом мелкий скролл к форме, без телепортов
# ====================================================================================


async def smooth_scroll_to_form(page, ctx: RunContext):
    form = await page.query_selector("div.form-wrapper")
    if not form:
        log("[WARN] div.form-wrapper не найден", ctx)
        return

    while True:
        b = await form.bounding_box()
        if not b:
            log("[WARN] Не удалось получить bounding_box формы", ctx)
            return

        form_top = b["y"]
        viewport_height = await page.evaluate("window.innerHeight")
        scroll_y = await page.evaluate("window.scrollY")

        if 0 <= form_top < viewport_height // 4:
            log(
                f"[SCROLL] Форма видна: form_top={form_top}, viewport_height={viewport_height}",
                ctx,
            )
            break

        # --- HUMAN-LIKE SCROLL STEP + PAUSE ---
        direction = 1 if form_top > 0 else -1
        if direction > 0:
            step = _rnd.choice(
                [
                    _rnd.randint(*CFG["SCROLL_STEP"]["down1"]),
                    _rnd.randint(*CFG["SCROLL_STEP"]["down2"]),
                ]
            )
        else:
            step = _rnd.randint(*CFG["SCROLL_STEP"]["up"])
        new_y = scroll_y + direction * step
        if step >= 400:
            parts = _rnd.randint(3, 6)
            for _ in range(parts):
                await page.mouse.wheel(0, direction * step / parts)
                await asyncio.sleep(_rnd.uniform(0.06, 0.12))
        else:
            await page.mouse.wheel(0, direction * step)
        log(f"[SCROLL] wheel {'down' if direction>0 else 'up'} {step}", ctx)

        # Остановки возле блоков с текстом
        text_blocks = [".hero", ".about", ".benefits", ".form-wrapper"]
        for sel in text_blocks:
            el = await page.query_selector(sel)
            if el:
                b2 = await el.bounding_box()
                if b2 and abs(b2["y"] - new_y) < 60:
                    pause_t = _rnd.uniform(1.2, 3.2)
                    log(f"[INFO] Пауза у блока {sel} {pause_t:.1f} сек", ctx)
                    await asyncio.sleep(pause_t)
                    break

        await asyncio.sleep(_rnd.uniform(0.8, 2.1))

        # Дальше стандартные шаги
        diff = form_top - viewport_height // 4
        abs_diff = abs(diff)
        if abs_diff > 400:
            step = CFG["SCROLL_STEP"]["fine"][0]
        elif abs_diff > 120:
            step = CFG["SCROLL_STEP"]["fine"][1]
        elif abs_diff > 40:
            step = CFG["SCROLL_STEP"]["fine"][2]
        else:
            step = CFG["SCROLL_STEP"]["fine"][3]

        if diff > 0:
            new_y = scroll_y + step
            delta = step
        else:
            new_y = scroll_y - step
            delta = -step
        if abs(delta) >= 400:
            parts = _rnd.randint(3, 6)
            for _ in range(parts):
                await page.mouse.wheel(0, delta / parts)
                await asyncio.sleep(_rnd.uniform(0.06, 0.12))
        else:
            await page.mouse.wheel(0, delta)
        log(
            f"[SCROLL] wheel small: scroll_y={scroll_y} → {new_y}, form_top={form_top}, step={step}",
            ctx,
        )
        await asyncio.sleep(0.06)


async def run_browser(ctx: RunContext):
    async with async_playwright() as p:

        launch_kwargs = {
            "headless": (
                ctx.json_headless if ctx.json_headless is not None else CFG["HEADLESS"]
            ),
            "channel": "chrome",
            "args": [
                "--incognito",
                "--disable-gpu",
                "--no-sandbox",
                "--disable-dev-shm-usage",
            ],
        }
        if ctx.proxy_url:
            launch_kwargs["proxy"] = {"server": ctx.proxy_url}

        browser = await p.chromium.launch(**launch_kwargs)

        context = await browser.new_context(
            user_agent=CFG["UA"],
            locale=FP_LANGUAGES[0],
            timezone_id=tz,
            viewport={"width": 1366, "height": 768},
            extra_http_headers={"Accept-Language": FP_ACCEPT_LANGUAGE},
        )

        # apply playwright-stealth anti-bot measures
        await stealth.Stealth().apply_stealth_async(context)

        await context.add_init_script(
            'Object.defineProperty(navigator,"webdriver",{get:()=>undefined})'
        )
        await context.add_init_script(
            f'Object.defineProperty(navigator, "platform", {{get: () => "{FP_PLATFORM}"}})'
        )
        await context.add_init_script(
            f'Object.defineProperty(navigator, "deviceMemory", {{get: () => {FP_DEVICE_MEMORY}}})'
        )
        await context.add_init_script(
            f'Object.defineProperty(navigator, "hardwareConcurrency", {{get: () => {FP_HARDWARE_CONCURRENCY}}})'
        )
        await context.add_init_script(
            f'Object.defineProperty(navigator, "languages", {{get: () => {json.dumps(FP_LANGUAGES)}}})'
        )
        await context.add_init_script(
            f"""
const getParameter = WebGLRenderingContext.prototype.getParameter;
WebGLRenderingContext.prototype.getParameter = function(parameter){{
    if (parameter === 37445) return "{FP_WEBGL_VENDOR}";
    if (parameter === 37446) return "{FP_WEBGL_RENDERER}";
    return getParameter.call(this, parameter);
}};
if (window.WebGL2RenderingContext) {{
    const getParameter2 = WebGL2RenderingContext.prototype.getParameter;
    WebGL2RenderingContext.prototype.getParameter = function(parameter){{
        if (parameter === 37445) return "{FP_WEBGL_VENDOR}";
        if (parameter === 37446) return "{FP_WEBGL_RENDERER}";
        return getParameter2.call(this, parameter);
    }};
}}
"""
        )
        log("[INFO] Patched WebGL2 getParameter", ctx)
        page = await context.new_page()
        cursor = create_cursor(page)

        async def _abort(route):
            await should_abort(route, ctx)

        await context.route("**/*", _abort)

        error_msg = ""

        try:
            # --- загрузка страницы с таймаут-отловом ---
            try:
                await page.goto(
                    "https://go.cpatrafficpoint.ru/click?o=3&a=103",
                    wait_until="domcontentloaded",
                    timeout=CFG["PAGE_GOTO_TIMEOUT"],
                )
                log("[INFO] Страница загружена", ctx)
            except PWTimeoutError:
                error_msg = f"Timeout {CFG['PAGE_GOTO_TIMEOUT'] // 1000} сек. при загрузке лендинга"
                log(f"[ERROR] {error_msg}", ctx)
                raise
            except PlaywrightError as e:
                if "has been closed" in str(e) or "TargetClosedError" in str(e):
                    ctx.browser_closed_manually = True
                    sys.exit(0)
                raise

            # Проверка на 503 ошибку
            status = await page.evaluate("document.documentElement.innerText")
            if (
                "503" in status
                or "Сервис временно недоступен" in status
                or "Service Unavailable" in status
            ):
                error_msg = "Ошибка 503: сайт временно недоступен"
                log(f"[ERROR] {error_msg}", ctx)
                raise Exception(error_msg)

            try:
                await page.wait_for_selector(
                    "div.form-wrapper", timeout=CFG["FORM_WRAPPER_TIMEOUT"]
                )
                log("[INFO] Контент формы загружен (div.form-wrapper найден)", ctx)
            except Exception as e:
                log(f"[WARN] div.form-wrapper не найден: {e}", ctx)

            # измеряем фактическое время полной загрузки
            load_ms = await page.evaluate(
                "() => { const t = performance.timing; return (t.loadEventEnd || t.domContentLoadedEventEnd) - t.navigationStart; }"
            )
            load_sec = max(load_ms / 1000, 0)

            min_read = 1.5  # минимум «чтения», сек
            base_read = _rnd.uniform(10, 25)
            total_time = max(min_read, base_read - max(0, 7 - load_sec))

            log(f"[INFO] Имитация “чтения” лендинга: {total_time:.1f} сек", ctx)

            await emulate_user_reading(page, total_time, ctx)

            # ====================================================================================
            # Быстрые крупные скроллы к форме, без телепортов
            # ====================================================================================
            await smooth_scroll_to_form(page, ctx)

            # ====================================================================================
            # Этап 4. Клик мышью по каждому полю, заполнение всех полей, установка галочки
            # ====================================================================================
            try:

                log("[INFO] Вводим ФИО через fill_full_name", ctx)
                await fill_full_name(page, user_name, ctx)

                log("[INFO] Вводим город через fill_city", ctx)
                await fill_city(page, user_city, ctx)
                await asyncio.sleep(0.3)

                log("[INFO] Вводим телефон через fill_phone", ctx)
                await fill_phone(page, phone_for_form, ctx)
                await asyncio.sleep(0.1)

                log("[INFO] Вводим пол через fill_gender", ctx)
                await fill_gender(page, user_gender, ctx)
                await asyncio.sleep(0.1)

                log("[INFO] Вводим возраст через fill_age", ctx)
                await fill_age(page, user_age, ctx)
                await asyncio.sleep(0.1)

                log("[INFO] Вводим тип курьера через fill_courier_type", ctx)
                await fill_courier_type(page, user_courier_type, ctx)
                await asyncio.sleep(0.1)

                log("[INFO] Ставим галочку политики через fill_policy_checkbox", ctx)
                await fill_policy_checkbox(page, ctx)
                await asyncio.sleep(0.1)

                log(
                    "[INFO] Все поля формы заполнены и чекбокс отмечен. Ожидание завершено.",
                    ctx,
                )

            except Exception as e:
                log(f"[ERROR] Ошибка на этапе заполнения формы: {e}", ctx)

            # ====================================================================================
            # Этап 5. Скриншот формы после заполнения и проверка заполненности
            # ====================================================================================
            screenshot_dir = os.path.join(WORK_DIR, "Media", "Screenshots")
            os.makedirs(screenshot_dir, exist_ok=True)
            date_str = datetime.now().strftime("%d.%m.%Y")
            base = f"{date_str}-{user_phone}"
            idx = 0
            while True:
                suffix = "" if idx == 0 else f"({idx})"
                screenshot_name = f"{base}{suffix}.png"
                path = os.path.join(screenshot_dir, screenshot_name)
                if not os.path.exists(path):
                    break
                idx += 1
            await page.screenshot(path=path, full_page=False)
            ctx.screenshot_path = path
            log(f"[INFO] Скриншот формы сохранён: {ctx.screenshot_path}", ctx)

            await asyncio.sleep(1.5)

            try:
                values = await page.evaluate(
                    """
                () => {
                    const getSelectText = (inputName, fallback="") => {
                        const inp = document.querySelector('input[name="'+inputName+'"]');
                        if (inp && inp.value) return inp.value;
                        // если input пустой, ищем div с выбранным текстом рядом с input
                        const wrap = inp ? inp.closest('.form-select, .select-wrapper, .select') : null;
                        if (wrap) {
                            const sel = wrap.querySelector('.form-select__selected, .selected, [data-selected], .form-list-item.selected');
                            if (sel && sel.textContent) return sel.textContent.trim();
                        }
                        return fallback;
                    };
                    return {
                        name:    document.querySelector('input[name="user_name"]')?.value || "",
                        city:    document.querySelector('input[name="user_city"]')?.value || "",
                        phone:   document.querySelector('input[name="user_phone"]')?.value || "",
                        gender:  getSelectText("user_gender"),
                        age:     document.querySelector('input[name="user_age"]')?.value || "",
                        courier: getSelectText("user_courier_type")
                    }
                }
                """
                )
                required_fields = {
                    "Имя": values["name"],
                    "Город": values["city"],
                    "Телефон": values["phone"],
                    "Пол": values["gender"],
                    "Возраст": values["age"],
                    "Тип курьера": values["courier"],
                }

                empty_fields = [k for k, v in required_fields.items() if not v.strip()]
                if empty_fields:
                    log(f"[ERROR] Не заполнены поля: {', '.join(empty_fields)}", ctx)
                    error_msg = f"Не заполнены поля: {', '.join(empty_fields)}"
                else:
                    error_msg = ""
            except Exception as e:
                log(f"[ERROR] Ошибка при проверке/скриншоте: {e}", ctx)
                error_msg = str(e)

            if error_msg:
                # Пропускаем клик по кнопке, сразу идём в finally
                pass
            else:
                # ====================================================================================
                # Этап 6. Ghost-cursor доводит курсор до кнопки + нативный клик + извлечение utm_term
                # ====================================================================================
                scroll_step = _rnd.choice(
                    [
                        _rnd.randint(*CFG["SCROLL_STEP"]["down1"]),
                        _rnd.randint(*CFG["SCROLL_STEP"]["down2"]),
                    ]
                )
                current_y = await page.evaluate("window.scrollY")
                new_y = current_y + scroll_step
                await page.evaluate(f"window.scrollTo(0, {new_y})")
                await asyncio.sleep(_rnd.uniform(0.7, 1.7))
                try:
                    button_selector = "button.btn_submit"
                    old_url = page.url
                    try:
                        await cursor.click(button_selector)
                        log("[INFO] Кнопка 'Оставить заявку' успешно нажата", ctx)
                    except Exception as e:
                        log(
                            f"[ERROR] Не удалось кликнуть по кнопке 'Оставить заявку': {e}",
                            ctx,
                        )
                        raise

                    try:
                        # Ждём редирект (10 сек)
                        await page.wait_for_function(
                            f'document.location.href !== "{old_url}"',
                            timeout=CFG["REDIRECT_TIMEOUT"],
                        )
                        log("[INFO] URL изменился — заявка успешно отправлена", ctx)

                        modal_selector = "div.modal-message-content"
                        modal = await page.query_selector(modal_selector)

                        if modal is None:
                            try:
                                await page.wait_for_selector(
                                    modal_selector,
                                    timeout=CFG["MODAL_SELECTOR_TIMEOUT"],
                                )
                                log(
                                    "[INFO] Всплывающее окно подтверждения появилось после ожидания",
                                    ctx,
                                )
                            except Exception as e:
                                html_content = await page.content()
                                log(
                                    f"[ERROR] Всплывающее окно подтверждения НЕ появилось после ожидания: {e}",
                                    ctx,
                                )
                                log(f"[DEBUG HTML CONTENT]: {html_content[:2000]}", ctx)
                                raise
                        else:
                            log(
                                "[INFO] Всплывающее окно подтверждения уже было на экране",
                                ctx,
                            )

                        log("[INFO] Всплывающее окно подтверждения появилось", ctx)
                        await asyncio.sleep(_rnd.uniform(1, 4))
                        # ====== Этап 7: извлечение utm_term из финального URL ======
                        try:
                            final_url = page.url
                            log(
                                f"[DEBUG] Финальный URL для поиска utm_term: {final_url}",
                                ctx,
                            )
                            ctx.postback = None
                            if "utm_term=" in final_url:
                                start = final_url.find("utm_term=") + len("utm_term=")
                                end = final_url.find("&", start)
                                if end == -1:
                                    ctx.postback = final_url[start:]
                                else:
                                    ctx.postback = final_url[start:end]
                                log(f"[INFO] POSTBACK: {ctx.postback}", ctx)
                            else:
                                log("[ERROR] utm_term не найден в ссылке", ctx)
                                raise Exception("utm_term not found in final URL")
                        except Exception as e:
                            log(
                                f"[ERROR] Ошибка на этапе извлечения utm_term: {e}", ctx
                            )
                    except Exception as e:
                        if page.url == old_url:
                            log("[ERROR] Редирект после клика не произошел", ctx)
                            raise Exception("Редирект после клика не произошел")
                        else:
                            log(
                                f"[ERROR] Всплывающее окно подтверждения НЕ появилось: {e}",
                                ctx,
                            )
                except Exception as e:
                    log(
                        f"[ERROR] Ошибка при клике по кнопке 'Оставить заявку': {e}",
                        ctx,
                    )

        finally:
            await context.close()
            await browser.close()


async def main(ctx: RunContext):
    await asyncio.wait_for(run_browser(ctx), timeout=CFG["RUN_TIMEOUT"])


if __name__ == "__main__":
    try:
        asyncio.run(main(ctx))
    except Exception as e:  # любая непойманная ошибка
        log(f"[FATAL] {e}", ctx)
        fatal = {"error": f"UNCAUGHT {e.__class__.__name__}: {e}"}
        print(json.dumps(fatal, ensure_ascii=False))
        sys.exit(1)


# ====================================================================================
# Этап 8. Возврат данных во Flask (результаты выполнения)
# ====================================================================================
error_lines = []
with open(ctx.log_file, encoding="utf-8") as f:
    f.seek(ctx.log_start_pos)
    for line in f:
        if "[ERROR]" in line:
            error_lines.append(line.strip())
all_errors = error_lines
error_msg = "\n".join(all_errors).strip()

result = {"phone": user_phone}


if error_msg:
    result["error"] = error_msg
    if ctx.screenshot_path:
        result["screenshot"] = ctx.screenshot_path
else:
    result["POSTBACK"] = ctx.postback


if not ctx.browser_closed_manually:
    send_webhook(result, webhook_url, ctx)
print(json.dumps(result, ensure_ascii=False))
