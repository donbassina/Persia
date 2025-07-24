# -*- coding: utf-8 -*-
from random import SystemRandom
_rnd = SystemRandom()            # единый генератор на весь скрипт
import asyncio
from playwright.async_api import async_playwright
from playwright_stealth import stealth

# --- универсальный импорт исключений Playwright (+ таймаут) ------------------
try:                                    # Playwright ≥ 1.43.0
    from playwright.async_api import (
        Error as PlaywrightError,
        TimeoutError as PWTimeoutError,      # ← ДОБАВЛЕНО
    )
except ImportError:                     # более старые версии
    from playwright.async_api import PlaywrightError      # type: ignore
    from playwright.async_api import TimeoutError as PWTimeoutError   # type: ignore
# -----------------------------------------------------------------------------

import sys
import json

def _to_bool(val: str | bool) -> bool:
    if isinstance(val, bool):
        return val
    return str(val).lower() in {"1", "true", "yes", "on"}
import os
from datetime import datetime
from python_ghost_cursor.playwright_async import create_cursor

CLI_OVERRIDES: dict[str, str] = {}
CLI_PROXY: str | None = None
proxy_url: str | None = None
LOG_FILE: str | None = None
LOG_START_POS: int = 0
json_headless: bool | None = None

def make_log_file(phone):
    logs_dir = os.path.join(os.path.dirname(__file__), "Logs")
    os.makedirs(logs_dir, exist_ok=True)
    date_str = datetime.now().strftime("%d.%m.%Y")
    log_file = os.path.join(logs_dir, f"{date_str}-{phone}.txt")
    return log_file

def log(msg, LOG_FILE):
    ts_txt = f"{datetime.now()}  {msg}"
    if LOG_FILE:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(ts_txt + "\n")
    else:
        print(ts_txt, file=sys.stderr)

from samokat_config import CFG, load_cfg

try:
    from pathlib import Path
    params = json.load(sys.stdin)
    json_headless_raw = params.get("headless")
    if json_headless_raw is not None:
        try:
            json_headless = _to_bool(json_headless_raw)
            if isinstance(json_headless_raw, str) and json_headless_raw.strip().lower() not in {"1", "true", "yes", "on", "0", "false", "no", "off"}:
                raise ValueError
        except ValueError:
            print('{"error":"bad headless value"}')
            sys.exit(1)
    else:
        json_headless = None     # не передали в JSON
    user_phone = params.get("user_phone", "")
    LOG_FILE = make_log_file(user_phone)
    LOG_START_POS = os.path.getsize(LOG_FILE) if os.path.exists(LOG_FILE) else 0
    log(f"[INFO] Получены параметры: {params}", LOG_FILE)

    webhook_url = params.get("Webhook", "")
    cli_args = sys.argv[1:]
    overrides = {}
    CLI_PROXY = None
    for arg in cli_args:
        if arg.startswith("--proxy="):
            CLI_PROXY = arg.split("=", 1)[1]
        elif "=" in arg:
            k, v = arg.split("=", 1)
            overrides[k] = v
    JSON_PROXY = params.get("proxy", "").strip() or None
    proxy_url = CLI_PROXY or JSON_PROXY or None
    CLI_OVERRIDES = overrides
    load_cfg(base_dir=Path(__file__).parent, cli_overrides=overrides)
    if proxy_url:
        log(f"[INFO] Proxy enabled: {proxy_url}", LOG_FILE)
    if json_headless is not None:
        log(f"[INFO] headless overridden by JSON → {json_headless}", LOG_FILE)
except Exception as e:
    print(f"[ERROR] Не удалось получить JSON из stdin: {e}")
    sys.exit(1)

import requests
import time

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
    sys.stdout.reconfigure(encoding='utf-8')
if sys.stderr.encoding.lower() != "utf-8":
    sys.stderr.reconfigure(encoding='utf-8')







BROWSER_CLOSED_MANUALLY = False

# === P-6. Единый фильтр для блокировки «лишних» запросов ===
first_abort_logged = False        # замыкание для единственного лога

async def should_abort(route):
    """
    Абортирует:
      • любые ресурсы с resource_type in {"image", "media"}
      • запросы, у которых в header-ах:
            status 204/206  ИЛИ  Content-Length < 512
    Всё остальное пропускает.
    """
    global first_abort_logged
    req = route.request
    r_type = req.resource_type
    headers = req.headers
    clen = int(headers.get("content-length", "1024"))
    status = int(headers.get(":status", 200))

    url = req.url
    must_abort = (r_type in ("image", "media")) or \
                 (status in (204, 206)) or \
                 (clen < 512) or \
                 any(p in url for p in CFG["BLOCK_PATTERNS"])

    if not must_abort:
        await route.continue_()
        return

    if not first_abort_logged:
        log(f"[INFO] ABORT {req.method} {req.url} "
            f"({r_type}, len≈{headers.get('content-length','?')})", LOG_FILE)
        first_abort_logged = True

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
    return {
        "top": box["y"],
        "center": box["y"] + box["height"] / 2
    }




















def send_webhook(result, webhook_url):
    if webhook_url:
        e = None
        for attempt in range(3):
            try:
                resp = requests.post(webhook_url, json=result, timeout=CFG["WEBHOOK_TIMEOUT"])
                if 200 <= resp.status_code < 300:
                    return
                raise Exception(f"Status {resp.status_code}")
            except Exception as exc:
                e = exc
                log(f"[WARN] webhook fail, retry {attempt + 1} in 60s: {e}", LOG_FILE)
                time.sleep(60)
        log(f"[FATAL] webhook 3rd fail: {e}", LOG_FILE)





# ==== тайм-зона по крупным городам РФ ====
TZ_BY_CITY = {
    "Москва":            "Europe/Moscow",
    "Санкт-Петербург":   "Europe/Moscow",
    "Нижний Новгород":   "Europe/Moscow",
    "Казань":            "Europe/Moscow",
    "Воронеж":           "Europe/Moscow",
    "Ростов-на-Дону":    "Europe/Moscow",
    "Волгоград":         "Europe/Volgograd",
    "Самара":            "Europe/Samara",          # UTC+4
    "Екатеринбург":      "Asia/Yekaterinburg",     # UTC+5
    "Челябинск":         "Asia/Yekaterinburg",
    "Уфа":               "Asia/Yekaterinburg",
    "Пермь":             "Asia/Yekaterinburg",
    "Омск":              "Asia/Omsk",              # UTC+6
    "Новосибирск":       "Asia/Novosibirsk",       # UTC+7
    "Красноярск":        "Asia/Krasnoyarsk",
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
log(f"[INFO] Platform: {FP_PLATFORM}", LOG_FILE)
log(f"[INFO] Device Memory: {FP_DEVICE_MEMORY}", LOG_FILE)
log(f"[INFO] Hardware Concurrency: {FP_HARDWARE_CONCURRENCY}", LOG_FILE)
log(f"[INFO] Languages: {FP_ACCEPT_LANGUAGE}", LOG_FILE)
log(f"[INFO] Timezone: {tz}", LOG_FILE)
log(f"[INFO] WebGL Vendor: {FP_WEBGL_VENDOR}", LOG_FILE)
log(f"[INFO] WebGL Renderer: {FP_WEBGL_RENDERER}", LOG_FILE)



POSTBACK = None



screenshot_path = ""

# ====================================================================================
# Этап 3. 
# ====================================================================================

def _human_delay() -> float:
    """Возвращает задержку перед следующим символом, s."""
    base = _rnd.lognormvariate(CFG["HUMAN_DELAY_μ"], CFG["HUMAN_DELAY_σ"])      # медиана ≈0.14
    return max(0.09, min(base, 0.22))

async def human_type(page, selector: str, text: str):
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

    log(f'[DEBUG] typing "{text}" len={n} total_time={total:.2f}', LOG_FILE)



async def fill_full_name(page, name, retries=3):
    for attempt in range(retries):
        try:
            input_box = page.get_by_placeholder("ФИО")
            await human_move_cursor(page, input_box)
            await input_box.click()
            await page.wait_for_timeout(100)
            await input_box.fill("")
            await human_type(page, 'input[name="user_name"]', name)
            value = await input_box.input_value()
            if value.strip() == name.strip():
                return True
            await page.wait_for_timeout(200)
        except Exception as e:
            log(f"[WARN] fill_full_name attempt {attempt+1} failed: {e}", LOG_FILE)
    log("[ERROR] Не удалось заполнить поле ФИО", LOG_FILE)
    return False


async def fill_city(page, city, retries=3):
    city_lower = city.strip().lower()
    for attempt in range(retries):
        try:
            input_box = page.get_by_placeholder("Выберите город").nth(0)
            await human_move_cursor(page, input_box)
            await input_box.click()
            await page.wait_for_timeout(100)
            await input_box.fill("")
            part_city = get_partial_city(city)
            await human_type_city_autocomplete(page, 'input[name="user_city"]', part_city)

            try:
                await page.wait_for_selector('.form-list-item', timeout=CFG["SELECT_ITEM_TIMEOUT"])
            except PWTimeoutError:
                pass

            try:
                items = await page.query_selector_all('.form-list-item')
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
            log(f"[WARN] fill_city attempt {attempt+1} failed: {e}", LOG_FILE)
    log("[ERROR] Не удалось выбрать город", LOG_FILE)
    return False




async def fill_phone(page, phone, retries=3):
    for attempt in range(retries):
        try:
            input_box = page.get_by_placeholder("+7 (900) 000 00-00")
            await human_move_cursor(page, input_box)
            await input_box.click()
            await page.wait_for_timeout(100)
            await input_box.fill("")
            await human_type(page, 'input[name="user_phone"]', phone)
            value = await input_box.input_value()
            if value.strip() == phone.strip():
                return True
            await page.wait_for_timeout(200)
        except Exception as e:
            log(f"[WARN] fill_phone attempt {attempt+1} failed: {e}", LOG_FILE)
    log("[ERROR] Не удалось заполнить поле Телефон", LOG_FILE)
    return False


async def fill_gender(page, gender, retries=3):
    for attempt in range(retries):
        try:
            input_box = page.get_by_placeholder("Выберите пол")
            await human_move_cursor(page, input_box)
            await input_box.click()
            await page.wait_for_timeout(100)
            try:
                await page.get_by_text(gender, exact=True).click(timeout=CFG["SELECT_ITEM_TIMEOUT"])
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
            log(f"[WARN] fill_gender attempt {attempt+1} failed: {e}", LOG_FILE)
    log("[ERROR] Не удалось выбрать пол", LOG_FILE)
    return False


async def fill_age(page, age, retries=3):
    for attempt in range(retries):
        try:
            input_box = page.get_by_placeholder("0")
            await human_move_cursor(page, input_box)
            await input_box.click()
            await page.wait_for_timeout(100)
            await input_box.fill("")
            await human_type(page, 'input[name="user_age"]', age)
            value = await input_box.input_value()
            if value.strip() == age.strip():
                return True
            await page.wait_for_timeout(200)
        except Exception as e:
            log(f"[WARN] fill_age attempt {attempt+1} failed: {e}", LOG_FILE)
    log("[ERROR] Не удалось заполнить поле Возраст", LOG_FILE)
    return False


async def fill_courier_type(page, courier_type, retries=3):
    for attempt in range(retries):
        try:
            input_box = page.get_by_placeholder("Выберите тип")
            await human_move_cursor(page, input_box)
            await input_box.click()
            await page.wait_for_timeout(100)
            try:
                await page.get_by_text(courier_type, exact=True).click(timeout=CFG["SELECT_ITEM_TIMEOUT"])
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
            log(f"[WARN] fill_courier_type attempt {attempt+1} failed: {e}", LOG_FILE)
    log("[ERROR] Не удалось выбрать тип курьера", LOG_FILE)
    return False


async def fill_policy_checkbox(page, retries=3):
    for attempt in range(retries):
        try:
            checkbox = await page.query_selector('.form-policy-checkbox')
            if checkbox:
                await checkbox.click()
                # Проверка: SVG-галочка появляется в DOM после клика
                checked = await page.query_selector('.form-policy-checkbox svg')
                if checked:
                    return True
            await page.wait_for_timeout(200)
        except Exception as e:
            log(f"[WARN] fill_policy_checkbox attempt {attempt+1} failed: {e}", LOG_FILE)
    log("[ERROR] Не удалось поставить галочку политики", LOG_FILE)
    return False



def get_partial_city(city_name):
    min_percent = 70
    max_percent = 85
    percent = _rnd.randint(min_percent, max_percent)
    cut = max(1, int(len(city_name) * percent / 100))
    return city_name[:cut]

async def human_type_city_autocomplete(page, selector: str, text: str):
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

    log(f'[DEBUG] typing "{text}" len={n} total_time={total:.2f}', LOG_FILE)


async def human_move_cursor(page, el):
    """Плавно ведёт курсор к случайной точке внутри элемента ``el``."""
    b = await el.bounding_box()
    if not b:
        return
    target_x = b["x"] + _rnd.uniform(8, b["width"]  - 8)
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
    log(f"[INFO] Курсор к {sel} ({int(target_x)},{int(target_y)})", LOG_FILE)




async def emulate_user_reading(page, total_time, LOG_FILE):
    start_time = asyncio.get_event_loop().time()
    blocks = ['.hero', '.about', '.benefits', '.form-wrapper']
    height = await page.evaluate("document.body.scrollHeight")
    current_y = 0

    while asyncio.get_event_loop().time() - start_time < total_time:
        action = _rnd.choices(
            ["scroll_down", "scroll_up", "pause", "mouse_wiggle", "to_block"],
            weights=[0.48, 0.14, 0.20, 0.06, 0.12]
        )[0]

        if action == "scroll_down":
            step = _rnd.choice([
                _rnd.randint(*CFG["SCROLL_STEP"]["down1"]),
                _rnd.randint(*CFG["SCROLL_STEP"]["down2"])
            ])
            current_y = min(current_y + step, height-1)
            if step >= 400:
                parts = _rnd.randint(3, 6)
                for _ in range(parts):
                    await page.mouse.wheel(0, step / parts)
                    await asyncio.sleep(_rnd.uniform(0.06, 0.12))
            else:
                await page.mouse.wheel(0, step)
            log(f"[INFO] wheel вниз на {step}", LOG_FILE)
            await asyncio.sleep(_rnd.uniform(0.7, 1.7))
        elif action == "scroll_up":
            step = _rnd.randint(*CFG["SCROLL_STEP"]["up"])
            current_y = max(current_y - step, 0)
            await page.mouse.wheel(0, -step)
            log(f"[INFO] wheel вверх на {step}", LOG_FILE)
            await asyncio.sleep(_rnd.uniform(0.5, 1.1))
        elif action == "pause":
            t = _rnd.uniform(1.2, 3.8)
            log(f"[INFO] Пауза {t:.1f}", LOG_FILE)
            await asyncio.sleep(t)
        elif action == "mouse_wiggle":
            x = _rnd.randint(100, 1200)
            y = _rnd.randint(100, 680)
            dx = _rnd.randint(-10, 10)
            dy = _rnd.randint(-8, 8)
            await page.mouse.move(x, y, steps=_rnd.randint(4, 10))
            await asyncio.sleep(_rnd.uniform(0.08, 0.18))
            await page.mouse.move(x+dx, y+dy, steps=2)
            log(f"[INFO] Мышь дрожит ({x},{y})", LOG_FILE)
            await asyncio.sleep(_rnd.uniform(0.10, 0.22))
        else:
            sel = _rnd.choice(blocks)
            el = await page.query_selector(sel)
            if el:
                box = await el.bounding_box()
                if box:
                    x = box["x"] + _rnd.uniform(12, box["width"]-12)
                    y = box["y"] + _rnd.uniform(12, box["height"]-12)
                    await page.mouse.move(x, y, steps=_rnd.randint(10, 22))
                    log(f"[INFO] Мышь на {sel} ({int(x)},{int(y)})", LOG_FILE)
                    await asyncio.sleep(_rnd.uniform(0.4, 1.3))




# --- 000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000 ---
# --- 000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000 ---
# --- 000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000 ---




# ====================================================================================
# Плавный, крупный и потом мелкий скролл к форме, без телепортов
# ====================================================================================

async def smooth_scroll_to_form(page):
    form = await page.query_selector("div.form-wrapper")
    if not form:
        log("[WARN] div.form-wrapper не найден", LOG_FILE)
        return

    while True:
        b = await form.bounding_box()
        if not b:
            log("[WARN] Не удалось получить bounding_box формы", LOG_FILE)
            return

        form_top = b["y"]
        viewport_height = await page.evaluate("window.innerHeight")
        scroll_y = await page.evaluate("window.scrollY")

        if 0 <= form_top < viewport_height // 4:
            log(f"[SCROLL] Форма видна: form_top={form_top}, viewport_height={viewport_height}", LOG_FILE)
            break

        # --- HUMAN-LIKE SCROLL STEP + PAUSE ---
        direction = 1 if form_top > 0 else -1
        if direction > 0:
            step = _rnd.choice([
                _rnd.randint(*CFG["SCROLL_STEP"]["down1"]),
                _rnd.randint(*CFG["SCROLL_STEP"]["down2"])
            ])
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
        log(f"[SCROLL] wheel {'down' if direction>0 else 'up'} {step}", LOG_FILE)

        # Остановки возле блоков с текстом
        text_blocks = [".hero", ".about", ".benefits", ".form-wrapper"]
        for sel in text_blocks:
            el = await page.query_selector(sel)
            if el:
                b2 = await el.bounding_box()
                if b2 and abs(b2["y"] - new_y) < 60:
                    pause_t = _rnd.uniform(1.2, 3.2)
                    log(f"[INFO] Пауза у блока {sel} {pause_t:.1f} сек", LOG_FILE)
                    await asyncio.sleep(pause_t)
                    break

        await asyncio.sleep(_rnd.uniform(0.8, 2.1))


        # Дальше стандартные шаги
        diff = (form_top - viewport_height // 4)
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
        log(f"[SCROLL] wheel small: scroll_y={scroll_y} → {new_y}, form_top={form_top}, step={step}", LOG_FILE)
        await asyncio.sleep(0.06)
















async def run_browser():
    global screenshot_path
    async with async_playwright() as p:

        launch_kwargs = {
            "headless": json_headless if json_headless is not None else CFG["HEADLESS"],
            "channel": "chrome",
            "args": [
                "--incognito",
                "--disable-gpu",
                "--no-sandbox",
                "--disable-dev-shm-usage",
            ],
        }
        if proxy_url:
            launch_kwargs["proxy"] = {"server": proxy_url}

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




        await context.add_init_script('Object.defineProperty(navigator,"webdriver",{get:()=>undefined})')
        await context.add_init_script(f'Object.defineProperty(navigator, "platform", {{get: () => "{FP_PLATFORM}"}})')
        await context.add_init_script(f'Object.defineProperty(navigator, "deviceMemory", {{get: () => {FP_DEVICE_MEMORY}}})')
        await context.add_init_script(f'Object.defineProperty(navigator, "hardwareConcurrency", {{get: () => {FP_HARDWARE_CONCURRENCY}}})')
        await context.add_init_script(f'Object.defineProperty(navigator, "languages", {{get: () => {json.dumps(FP_LANGUAGES)}}})')
        await context.add_init_script(f"""
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
""")
        log("[INFO] Patched WebGL2 getParameter", LOG_FILE)
        page = await context.new_page()
        cursor = create_cursor(page)

        await context.route("**/*", should_abort)





    
        error_msg = ""

        try:
            # --- загрузка страницы с таймаут-отловом ---
            try:
                await page.goto(
                    "https://go.cpatrafficpoint.ru/click?o=3&a=103",
                    wait_until="domcontentloaded",
                    timeout=CFG["PAGE_GOTO_TIMEOUT"]
                )
                log("[INFO] Страница загружена", LOG_FILE)
            except PWTimeoutError:
                error_msg = f"Timeout {CFG['PAGE_GOTO_TIMEOUT'] // 1000} сек. при загрузке лендинга"
                log(f"[ERROR] {error_msg}", LOG_FILE)
                raise
            except PlaywrightError as e:
                if "has been closed" in str(e) or "TargetClosedError" in str(e):
                    global BROWSER_CLOSED_MANUALLY
                    BROWSER_CLOSED_MANUALLY = True
                    sys.exit(0)
                raise








            # Проверка на 503 ошибку
            status = await page.evaluate("document.documentElement.innerText")
            if "503" in status or "Сервис временно недоступен" in status or "Service Unavailable" in status:
                error_msg = "Ошибка 503: сайт временно недоступен"
                log(f"[ERROR] {error_msg}", LOG_FILE)
                raise Exception(error_msg)

            try:
                await page.wait_for_selector("div.form-wrapper", timeout=CFG["FORM_WRAPPER_TIMEOUT"])
                log("[INFO] Контент формы загружен (div.form-wrapper найден)", LOG_FILE)
            except Exception as e:
                log(f"[WARN] div.form-wrapper не найден: {e}", LOG_FILE)

            # измеряем фактическое время полной загрузки
            load_ms = await page.evaluate(
                "() => { const t = performance.timing; return (t.loadEventEnd || t.domContentLoadedEventEnd) - t.navigationStart; }"
            )
            load_sec = max(load_ms / 1000, 0)

    
            min_read   = 1.5                 # минимум «чтения», сек
            base_read  = _rnd.uniform(10, 25)
            total_time = max(min_read, base_read - max(0, 7 - load_sec))




            
            start_time = asyncio.get_event_loop().time()
            log(f"[INFO] Имитация “чтения” лендинга: {total_time:.1f} сек", LOG_FILE)

            await emulate_user_reading(page, total_time, LOG_FILE)



            # ====================================================================================
            # Быстрые крупные скроллы к форме, без телепортов
            # ====================================================================================
            await smooth_scroll_to_form(page)

            # ====================================================================================
            # Этап 4. Клик мышью по каждому полю, заполнение всех полей, установка галочки
            # ====================================================================================
            try:
                
                
                log("[INFO] Вводим ФИО через fill_full_name", LOG_FILE)
                await fill_full_name(page, user_name)

                log("[INFO] Вводим город через fill_city", LOG_FILE)
                await fill_city(page, user_city)
                await asyncio.sleep(0.3)

                log("[INFO] Вводим телефон через fill_phone", LOG_FILE)
                await fill_phone(page, phone_for_form)
                await asyncio.sleep(0.1)

                log("[INFO] Вводим пол через fill_gender", LOG_FILE)
                await fill_gender(page, user_gender)
                await asyncio.sleep(0.1)

                log("[INFO] Вводим возраст через fill_age", LOG_FILE)
                await fill_age(page, user_age)
                await asyncio.sleep(0.1)

                log("[INFO] Вводим тип курьера через fill_courier_type", LOG_FILE)
                await fill_courier_type(page, user_courier_type)
                await asyncio.sleep(0.1)

                log("[INFO] Ставим галочку политики через fill_policy_checkbox", LOG_FILE)
                await fill_policy_checkbox(page)
                await asyncio.sleep(0.1)

                log("[INFO] Все поля формы заполнены и чекбокс отмечен. Ожидание завершено.", LOG_FILE)


            except Exception as e:
                log(f"[ERROR] Ошибка на этапе заполнения формы: {e}", LOG_FILE)

            # ====================================================================================
            # Этап 5. Скриншот формы после заполнения и проверка заполненности
            # ====================================================================================
            screenshot_dir = os.path.join(WORK_DIR, "Media", "Screenshots")
            os.makedirs(screenshot_dir, exist_ok=True)
            date_str = datetime.now().strftime("%d.%m.%Y")
            screenshot_name = f"{date_str}-{user_phone}.png"
            screenshot_path = os.path.join(screenshot_dir, screenshot_name)
            await page.screenshot(path=screenshot_path, full_page=False)
            log(f"[INFO] Скриншот формы сохранён: {screenshot_path}", LOG_FILE)

            await asyncio.sleep(1.5)

            try:
                values = await page.evaluate("""
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
                """)
                required_fields = {
                    "Имя":         values["name"],
                    "Город":       values["city"],
                    "Телефон":     values["phone"],
                    "Пол":         values["gender"],
                    "Возраст":     values["age"],
                    "Тип курьера": values["courier"]
                }

                empty_fields = [k for k, v in required_fields.items() if not v.strip()]
                if empty_fields:
                    log(f"[ERROR] Не заполнены поля: {', '.join(empty_fields)}", LOG_FILE)
                    error_msg = f"Не заполнены поля: {', '.join(empty_fields)}"
                else:
                    error_msg = ""
            except Exception as e:
                log(f"[ERROR] Ошибка при проверке/скриншоте: {e}", LOG_FILE)
                error_msg = str(e)

            if error_msg:
                # Пропускаем клик по кнопке, сразу идём в finally
                pass
            else:
                # ====================================================================================
                # Этап 6. Ghost-cursor доводит курсор до кнопки + нативный клик + извлечение utm_term
                # ====================================================================================
                scroll_step = _rnd.choice([
                    _rnd.randint(*CFG["SCROLL_STEP"]["down1"]),
                    _rnd.randint(*CFG["SCROLL_STEP"]["down2"])
                ])
                current_y = await page.evaluate("window.scrollY")
                new_y = current_y + scroll_step
                await page.evaluate(f"window.scrollTo(0, {new_y})")
                await asyncio.sleep(_rnd.uniform(0.7, 1.7))
                try:
                    button_selector = 'button.btn_submit'
                    old_url = page.url
                    try:
                        await cursor.click(button_selector)
                        log("[INFO] Кнопка 'Оставить заявку' успешно нажата", LOG_FILE)
                    except Exception as e:
                        log(f"[ERROR] Не удалось кликнуть по кнопке 'Оставить заявку': {e}", LOG_FILE)
                        raise

                    try:
                        # Ждём редирект (10 сек)
                        await page.wait_for_function(
                            f'document.location.href !== "{old_url}"',
                            timeout=CFG["REDIRECT_TIMEOUT"]
                        )
                        log("[INFO] URL изменился — заявка успешно отправлена", LOG_FILE)

                        modal_selector = 'div.modal-message-content'
                        modal = await page.query_selector(modal_selector)

                        if modal is None:
                            try:
                                await page.wait_for_selector(modal_selector, timeout=CFG["MODAL_SELECTOR_TIMEOUT"])
                                log("[INFO] Всплывающее окно подтверждения появилось после ожидания", LOG_FILE)
                            except Exception as e:
                                html_content = await page.content()
                                log(f"[ERROR] Всплывающее окно подтверждения НЕ появилось после ожидания: {e}", LOG_FILE)
                                log(f"[DEBUG HTML CONTENT]: {html_content[:2000]}", LOG_FILE)
                                raise
                        else:
                            log("[INFO] Всплывающее окно подтверждения уже было на экране", LOG_FILE)


                        log("[INFO] Всплывающее окно подтверждения появилось", LOG_FILE)
                        await asyncio.sleep(_rnd.uniform(1, 4))
                        # ====== Этап 7: извлечение utm_term из финального URL ======
                        try:
                            final_url = page.url
                            log(f"[DEBUG] Финальный URL для поиска utm_term: {final_url}", LOG_FILE)
                            global POSTBACK
                            POSTBACK = None
                            if "utm_term=" in final_url:
                                start = final_url.find("utm_term=") + len("utm_term=")
                                end = final_url.find("&", start)
                                if end == -1:
                                    POSTBACK = final_url[start:]
                                else:
                                    POSTBACK = final_url[start:end]
                                log(f"[INFO] POSTBACK: {POSTBACK}", LOG_FILE)
                            else:
                                log("[WARN] utm_term не найден в ссылке", LOG_FILE)
                        except Exception as e:
                            log(f"[ERROR] Ошибка на этапе извлечения utm_term: {e}", LOG_FILE)
                    except Exception as e:
                        if page.url == old_url:
                            log("[ERROR] Редирект после клика не произошел", LOG_FILE)
                            raise Exception("Редирект после клика не произошел")
                        else:
                            log(f"[ERROR] Всплывающее окно подтверждения НЕ появилось: {e}", LOG_FILE)
                except Exception as e:
                    log(f"[ERROR] Ошибка при клике по кнопке 'Оставить заявку': {e}", LOG_FILE)





        finally:
            await context.close()
            await browser.close()
async def main():
    await asyncio.wait_for(run_browser(), timeout=CFG["RUN_TIMEOUT"])


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as e:            # любая непойманная ошибка
        log(f"[FATAL] {e}", LOG_FILE)
        fatal = {"error": f"UNCAUGHT {e.__class__.__name__}: {e}"}
        print(json.dumps(fatal, ensure_ascii=False))
        sys.exit(1)


# ====================================================================================
# Этап 8. Возврат данных во Flask (результаты выполнения)
# ====================================================================================
error_lines = []
with open(LOG_FILE, encoding="utf-8") as f:
    f.seek(LOG_START_POS)
    for line in f:
        if "[ERROR]" in line:
            error_lines.append(line.strip())
all_errors = error_lines
error_msg = "\n".join(all_errors).strip()

result = {
    "phone": user_phone
}


if error_msg:
    result["error"] = error_msg
    result["screenshot"] = screenshot_path
else:
    result["POSTBACK"] = POSTBACK




if not BROWSER_CLOSED_MANUALLY:
    send_webhook(result, webhook_url)
print(json.dumps(result, ensure_ascii=False))
