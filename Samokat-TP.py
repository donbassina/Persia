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

import logging
import requests

from python_ghost_cursor.playwright_async import create_cursor
from python_ghost_cursor.playwright_async._spoof import GhostCursor
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
from utils import RunContext, make_log_file, version_check, load_selectors
from logger_setup import get_logger
from proxy_utils import parse_proxy, probe_proxy, ProxyError


_REQUIRED = {
    "playwright": ("1.43", "2.0"),
    "playwright-stealth": ("2.0.0", "3.0"),
}

version_check(_REQUIRED)

logger = get_logger("samokat.main")


_rnd = SystemRandom()  # единый генератор на весь скрипт

# global ghost-cursor instance, created in run_browser
GCURSOR: GhostCursor | None = None

# selectors loaded from YAML profile in ``main``
# default profile path: selectors/default.yml
selectors: dict | None = None
proxy_cfg: dict | None = None


async def gc_move(x: float, y: float):
    if GCURSOR is None:
        raise RuntimeError("GCURSOR not initialized")
    await GCURSOR.move_to({"x": x, "y": y})


async def gc_click(target):
    if GCURSOR is None:
        raise RuntimeError("GCURSOR not initialized")
    page = GCURSOR.page
    el = None
    if isinstance(target, str):
        el = await page.query_selector(target)
    else:
        el = target
    box = await el.bounding_box() if el else None
    if box:
        x = box["x"] + box["width"] / 2
        y = box["y"] + box["height"] / 2
        await gc_move(x, y)
        if hasattr(GCURSOR, "click_absolute"):
            await GCURSOR.click_absolute(x, y)
        else:
            await GCURSOR.click(None)
        return
    await GCURSOR.click(target if isinstance(target, str) else None)


async def gc_wheel(delta_y: float):
    if GCURSOR is None:
        raise RuntimeError("GCURSOR not initialized")
    if not hasattr(GCURSOR, "wheel"):
        raise RuntimeError("GCURSOR lacks wheel method")
    await GCURSOR.wheel(0, delta_y)


async def gc_press():
    if GCURSOR is None:
        return
    await GCURSOR.page.mouse.down()


async def gc_release():
    if GCURSOR is None:
        return
    await GCURSOR.page.mouse.up()


async def human_scroll(total_px: int):
    """Scroll page in a human-like manner using the mouse wheel."""
    direction = 1 if total_px > 0 else -1
    remain = abs(total_px)
    while remain > 0:
        step = min(int(_rnd.lognormvariate(3.9, 0.45)), remain, 200)
        await gc_wheel(direction * step + _rnd.randint(-0, 0))
        v = step
        while v > 3:
            await gc_wheel(direction * int(v))
            v *= 0.75
            await asyncio.sleep(0.016)
        remain -= step
        if _rnd.random() < 0.14:
            await gc_wheel(-direction * _rnd.randint(0, 0))
        if _rnd.random() < 0.35:
            await asyncio.sleep(_rnd.uniform(1.25, 4.9))


async def drag_scroll(total_px: int):
    """Simulate scrollbar drag for scrolling."""
    if GCURSOR is None:
        raise RuntimeError("GCURSOR not initialized")
    page = GCURSOR.page
    scroll_height = await page.evaluate("document.body.scrollHeight")
    viewport_height = await page.evaluate("window.innerHeight")
    if scroll_height <= viewport_height:
        return
    scroll_y = await page.evaluate("window.scrollY")
    max_scroll = scroll_height - viewport_height
    new_scroll = max(0, min(scroll_y + total_px, max_scroll))
    slider_h = viewport_height * viewport_height / scroll_height
    track_h = viewport_height - slider_h
    start_y = scroll_y / max_scroll * track_h + slider_h / 2
    end_y = new_scroll / max_scroll * track_h + slider_h / 2
    x = await page.evaluate("window.innerWidth - 4")
    await gc_move(x, start_y)
    await gc_press()
    await gc_move(x, end_y)
    await gc_release()
    await asyncio.sleep(_rnd.uniform(0.1, 0.3))


def _to_bool(val: str | bool) -> bool:
    if isinstance(val, bool):
        return val
    return str(val).lower() in {"1", "true", "yes", "on"}


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
                logger.warning(
                    "webhook fail, retry %s in 60s: %s",
                    attempt + 1,
                    e,
                )
                time.sleep(60)
        logger.error("webhook 3rd fail: %s", e)


def send_result(
    ctx: RunContext,
    phone: str,
    webhook_url: str,
    headless_error: bool,
    proxy_used: bool,
) -> None:
    """Send final result via webhook and print JSON."""
    result: dict[str, str | bool | None] = {"phone": phone, "proxy_used": proxy_used}

    if ctx.errors:
        result["error"] = ", ".join(ctx.errors)
        if ctx.screenshot_path:
            result["screenshot"] = os.path.abspath(ctx.screenshot_path)
    else:
        if ctx.postback:
            result["POSTBACK"] = ctx.postback
        else:
            result["error"] = "POSTBACK missing"
            if ctx.screenshot_path:
                result["screenshot"] = os.path.abspath(ctx.screenshot_path)

    if ctx.log_file:
        result["log"] = os.path.abspath(ctx.log_file)

    if headless_error and "error" not in result:
        result["error"] = "bad headless value"

    result_state = "SUCCESS" if "error" not in result else "ERROR"
    logger.info("RESULT: %s", result_state)

    if not ctx.browser_closed_manually:
        send_webhook(result, webhook_url, ctx)
    print(json.dumps(result, ensure_ascii=False))


try:
    params = json.load(sys.stdin)
    json_headless_raw = params.get("headless")
    headless_error = False
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
            headless_error = True
            ctx_json_headless = None
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
    proxy_url = json_proxy or cli_proxy or None
    ctx = RunContext(
        cli_overrides=overrides,
        cli_proxy=cli_proxy,
        proxy_url=proxy_url,
        log_file=log_file,
        log_start_pos=log_start,
        json_headless=ctx_json_headless,
    )
    file_handler = logging.FileHandler(ctx.log_file, encoding="utf-8")
    file_handler.setFormatter(
        logging.Formatter(
            "[%(asctime)s] %(levelname)s %(name)s – %(message)s",
            datefmt="%H:%M:%S",
        )
    )
    logger.addHandler(file_handler)
    logger.info("Получены параметры: %s", params)
    load_cfg(base_dir=Path(__file__).parent, cli_overrides=overrides, ctx=ctx)
    if proxy_url:
        logger.info("Proxy enabled: %s", proxy_url)
    if proxy_url:
        try:
            parsed = parse_proxy(proxy_url)
            proxy_cfg = {
                "server": f"{parsed['scheme']}://{parsed['host']}:{parsed['port']}"
            }
            if parsed.get("user"):
                proxy_cfg["username"] = parsed["user"]
            if parsed.get("password"):
                proxy_cfg["password"] = parsed["password"]
        except ProxyError:
            logger.error("bad_proxy_format")
            fatal = {"phone": user_phone, "error": "bad_proxy_format"}
            send_webhook(fatal, webhook_url, ctx)
            print(json.dumps(fatal, ensure_ascii=False))
            sys.exit(1)
        if not probe_proxy(parsed):
            logger.warning("proxy_unavailable – continue without proxy")
            proxy_cfg = None
    if ctx.json_headless is not None:
        logger.info("headless overridden by JSON → %s", ctx.json_headless)
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
        logger.info(
            "ABORT %s %s (%s, len≈%s)",
            req.method,
            req.url,
            r_type,
            headers.get("content-length", "?"),
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
logger.info("Platform: %s", FP_PLATFORM)
logger.info("Device Memory: %s", FP_DEVICE_MEMORY)
logger.info("Hardware Concurrency: %s", FP_HARDWARE_CONCURRENCY)
logger.info("Languages: %s", FP_ACCEPT_LANGUAGE)
logger.info("Timezone: %s", tz)
logger.info("WebGL Vendor: %s", FP_WEBGL_VENDOR)
logger.info("WebGL Renderer: %s", FP_WEBGL_RENDERER)


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

    logger.info(f'[DEBUG] typing "{text}" len={n} total_time={total:.2f}')


async def fill_full_name(page, name, ctx: RunContext, retries=3):
    for attempt in range(retries):
        try:
            input_box = page.locator(selectors["form"]["name"])
            await human_move_cursor(page, input_box, ctx)
            await ghost_click(input_box)
            await page.wait_for_timeout(50)
            await page.wait_for_timeout(100)
            await input_box.fill("")
            await human_type(page, selectors["form"]["name"], name, ctx)
            value = await input_box.input_value()
            if value.strip() == name.strip():
                return True
            await page.wait_for_timeout(200)
        except Exception as e:
            logger.warning("fill_full_name attempt %s failed: %s", attempt + 1, e)
    logger.error("Не удалось заполнить поле ФИО")
    return False


async def fill_city(page, city, ctx: RunContext, retries=3):
    for attempt in range(retries):
        try:
            input_box = page.locator(selectors["form"]["city"])
            await human_move_cursor(page, input_box, ctx)
            await ghost_click(input_box)
            item_sel = selectors["form"]["city_item"]
            await page.locator(item_sel).get_by_text(city, exact=True).click()
            return True
        except Exception as e:
            logger.warning("fill_city attempt %s failed: %s", attempt + 1, e)
    logger.error("Не удалось выбрать город")
    return False


async def fill_phone(page, phone, ctx: RunContext, retries=3):
    for attempt in range(retries):
        try:
            input_box = page.locator(selectors["form"]["phone"])
            await human_move_cursor(page, input_box, ctx)
            await ghost_click(input_box)
            await page.wait_for_timeout(50)
            await page.wait_for_timeout(100)
            await input_box.fill("")
            await human_type(page, selectors["form"]["phone"], phone, ctx)
            value = await input_box.input_value()
            if value.strip() == phone.strip():
                return True
            await page.wait_for_timeout(200)
        except Exception as e:
            logger.warning("fill_phone attempt %s failed: %s", attempt + 1, e)
    logger.error("Не удалось заполнить поле Телефон")
    return False


async def fill_gender(page, gender, ctx: RunContext, retries=3):
    for attempt in range(retries):
        try:
            input_box = page.locator(selectors["form"]["gender"])
            await human_move_cursor(page, input_box, ctx)
            await ghost_click(input_box)
            item_sel = selectors["form"]["gender_item"]
            await page.locator(item_sel).get_by_text(gender, exact=True).click()
            return True
        except Exception as e:
            logger.warning("fill_gender attempt %s failed: %s", attempt + 1, e)
    logger.error("Не удалось выбрать пол")
    return False


async def fill_age(page, age, ctx: RunContext, retries=3):
    for attempt in range(retries):
        try:
            input_box = page.locator(selectors["form"]["age"])
            await human_move_cursor(page, input_box, ctx)
            await ghost_click(input_box)
            await page.wait_for_timeout(50)
            await page.wait_for_timeout(100)
            await input_box.fill("")
            await human_type(page, selectors["form"]["age"], age, ctx)
            value = await input_box.input_value()
            if value.strip() == age.strip():
                return True
            await page.wait_for_timeout(200)
        except Exception as e:
            logger.warning("fill_age attempt %s failed: %s", attempt + 1, e)
    logger.error("Не удалось заполнить поле Возраст")
    return False


async def fill_courier_type(page, courier_type, ctx: RunContext, retries=3):
    for attempt in range(retries):
        try:
            input_box = page.locator(selectors["form"]["courier"])
            await human_move_cursor(page, input_box, ctx)
            await ghost_click(input_box)
            item_sel = selectors["form"]["courier_item"]
            await page.locator(item_sel).get_by_text(courier_type, exact=True).click()
            return True
        except Exception as e:
            logger.warning(
                "fill_courier_type attempt %s failed: %s",
                attempt + 1,
                e,
            )
    logger.error("Не удалось выбрать тип курьера")
    return False


async def fill_policy_checkbox(page, ctx: RunContext, retries=3):
    for attempt in range(retries):
        try:
            await ghost_click(page.locator(selectors["form"]["policy"]))
            return True
        except Exception as e:
            logger.warning(
                "fill_policy_checkbox attempt %s failed: %s",
                attempt + 1,
                e,
            )
    logger.error("Не удалось поставить галочку политики")
    return False


async def submit_form(page, ctx: RunContext):
    """Click submit button or submit form if the button is hidden."""
    btn = page.locator(selectors["form"]["submit"])
    try:
        display = await btn.evaluate("el => getComputedStyle(el).display")
        if display == "none":
            await page.locator("form").evaluate("f => f.submit()")
        else:
            await ghost_click(btn)
    except Exception as e:
        logger.error("submit_form failed: %s", e)
        raise


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

    logger.info(f'[DEBUG] typing "{text}" len={n} total_time={total:.2f}')


async def ghost_click(selector_or_element):
    """Click element using ghost cursor with logging."""
    if GCURSOR is None:
        raise RuntimeError("GCURSOR not initialized")
    page = GCURSOR.page
    el = None
    if isinstance(selector_or_element, str):
        el = await page.query_selector(selector_or_element)
    else:
        el = selector_or_element
    box = await el.bounding_box() if el else None
    if box:
        name = (
            await el.get_attribute("name")
            or await el.get_attribute("placeholder")
            or await el.evaluate("el => el.className")
            or (
                selector_or_element
                if isinstance(selector_or_element, str)
                else "element"
            )
        )
        logger.info(
            f"[INFO] Курсор к {name} ({int(box['x']+box['width']/2)},{int(box['y']+box['height']/2)})"
        )
    await gc_click(selector_or_element)


async def human_move_cursor(page, el, ctx: RunContext):
    """Плавно ведёт курсор к случайной точке внутри элемента ``el``."""
    b = await el.bounding_box()
    if not b:
        return
    target_x = b["x"] + _rnd.uniform(8, b["width"] - 8)
    target_y = b["y"] + _rnd.uniform(8, b["height"] - 8)

    if GCURSOR is None:
        raise RuntimeError("GCURSOR not initialized")
    cur = getattr(ctx, "mouse_pos", (0, 0))
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
        await gc_move(x, y)
        ctx.mouse_pos = (x, y)
        cur_x, cur_y = x, y
    final_x = cur_x + _rnd.uniform(-4, 4)
    final_y = cur_y + _rnd.uniform(-3, 3)
    await gc_move(final_x, final_y)
    cur_x, cur_y = final_x, final_y
    ctx.mouse_pos = (cur_x, cur_y)
    sel = (
        await el.get_attribute("name")
        or await el.get_attribute("placeholder")
        or await el.evaluate("el => el.className")
        or "element"
    )
    logger.info(f"[INFO] Курсор к {sel} ({int(target_x)},{int(target_y)})")


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
            if step > 200:
                step = 200
            current_y = min(current_y + step, height - 1)
            if _rnd.random() < 0.005:
                await drag_scroll(step)
            else:
                await human_scroll(step)
            logger.info(f"[SCROLL] wheel down {step}")
            await asyncio.sleep(_rnd.uniform(0.7, 1.7))
        elif action == "scroll_up":
            step = _rnd.randint(*CFG["SCROLL_STEP"]["up"])
            if step > 320:
                step = 320
            current_y = max(current_y - step, 0)
            if _rnd.random() < 0.005:
                await drag_scroll(-step)
            else:
                await human_scroll(-step)
            logger.info(f"[SCROLL] wheel up {step}")
            await asyncio.sleep(_rnd.uniform(0.5, 1.1))
        elif action == "pause":
            t = _rnd.uniform(1.2, 3.8)
            logger.info(f"[INFO] Пауза {t:.1f}")
            await asyncio.sleep(t)
        elif action == "mouse_wiggle":
            x = _rnd.randint(100, 1200)
            y = _rnd.randint(100, 680)
            dx = _rnd.randint(-10, 10)
            dy = _rnd.randint(-8, 8)
            await gc_move(x, y)
            await asyncio.sleep(_rnd.uniform(0.08, 0.18))
            await gc_move(x + dx, y + dy)
            logger.info(f"[INFO] Мышь дрожит ({x},{y})")
            await asyncio.sleep(_rnd.uniform(0.10, 0.22))
        else:
            sel = _rnd.choice(blocks)
            el = await page.query_selector(sel)
            if el:
                box = await el.bounding_box()
                if box:
                    x = box["x"] + _rnd.uniform(12, box["width"] - 12)
                    y = box["y"] + _rnd.uniform(12, box["height"] - 12)
                    await gc_move(x, y)
                    logger.info(f"[INFO] Мышь на {sel} ({int(x)},{int(y)})")
                    await asyncio.sleep(_rnd.uniform(0.4, 1.3))


# --- 000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000 ---
# --- 000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000 ---
# --- 000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000 ---

async def scroll_to_form_like_reading(page, ctx: RunContext, timeout: float = 15.0):
    """
    Плавный «человеческий» скролл к div.form-wrapper.
    Использует ровно те же wheel-scroll, шаги, задержки, что и в emulate_user_reading.
    После выхода из функции должна быть видна хотя бы часть формы.
    """
    import asyncio

    start = asyncio.get_event_loop().time()
    viewport_h = await page.evaluate("window.innerHeight")

    while True:
        form = await page.query_selector("div.form-wrapper")
        if form:
            bb = await form.bounding_box()
            if bb:
                # если форма уже видна — случайная точка «приземления» ±25 px
                rand_pad = _rnd.randint(-25, 25)
                if 0 <= bb["y"] + rand_pad < viewport_h // 3:
                    return
                # форма выше экрана — прокрутка вверх мелкими шагами
                if bb["y"] < 0:
                    await human_scroll(-60)
                    await asyncio.sleep(_rnd.uniform(0.5, 1.0))
                    continue

        # расстояние до цели → динамический размер шага
        step = _rnd.choice([
            _rnd.randint(160, 200) if bb and bb["y"] > 1000 else
            _rnd.randint(100, 140) if bb and bb["y"] > 600 else
            _rnd.randint(60,  90)  if bb and bb["y"] > 250 else
            _rnd.randint(25,  45)
        ])
        await human_scroll(step)
        logger.info(f"[SCROLL] to-form wheel {step}")
        await asyncio.sleep(_rnd.uniform(0.6, 1.4))

        # защита от бесконечного цикла
        if asyncio.get_event_loop().time() - start > timeout:
            logger.warning("scroll_to_form_like_reading: timeout")
            return


# ====================================================================================
# Плавный, крупный и потом мелкий скролл к форме, без телепортов
# ====================================================================================


async def smooth_scroll_to_form(page, ctx: RunContext):
    form = await page.query_selector("div.form-wrapper")
    if not form:
        logger.warning("div.form-wrapper не найден")
        return
    timeout = CFG["SCROLL_TO_FORM"]["TIMEOUT"]
    max_iters = CFG["SCROLL_TO_FORM"]["MAX_ITERS"]
    block_pause = CFG["SCROLL_TO_FORM"]["BLOCK_PAUSE"]
    fine_steps = CFG["SCROLL_TO_FORM"]["FINE_STEPS"]
    start_ts = asyncio.get_event_loop().time()

    for i in range(max_iters):
        b = await form.bounding_box()
        if not b:
            logger.warning("Не удалось получить bounding_box формы")
            return

        form_top = b["y"]
        viewport_height = await page.evaluate("window.innerHeight")
        scroll_y = await page.evaluate("window.scrollY")

        if 0 <= form_top < viewport_height // 4:
            logger.info(
                "[SCROLL] Форма видна: form_top=%s, viewport_height=%s",
                form_top,
                viewport_height,
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
        if step > 320:
            step = 320
        new_y = scroll_y + direction * step
        if _rnd.random() < 0.02:
            await drag_scroll(direction * step)
        else:
            await human_scroll(direction * step)
        logger.info(f"[SCROLL] wheel {'down' if direction>0 else 'up'} {step}")

        # Остановки возле блоков с текстом
        text_blocks = [".hero", ".about", ".benefits", ".form-wrapper"]
        for sel in text_blocks:
            el = await page.query_selector(sel)
            if el:
                b2 = await el.bounding_box()
                if b2 and abs(b2["y"] - new_y) < 60:
                    time_left = (
                        timeout - (asyncio.get_event_loop().time() - start_ts) - 1
                    )
                    pause_t = min(_rnd.uniform(*block_pause), max(time_left, 0))
                    logger.info(f"[INFO] Пауза у блока {sel} {pause_t:.1f} сек")
                    await asyncio.sleep(pause_t)
                    break

        time_left = timeout - (asyncio.get_event_loop().time() - start_ts) - 1
        await asyncio.sleep(min(_rnd.uniform(0.2, 0.5), max(time_left, 0)))

        diff = form_top - viewport_height // 4
        abs_diff = abs(diff)
        if abs_diff > fine_steps[0]:
            step = fine_steps[0]
        elif abs_diff > fine_steps[1]:
            step = fine_steps[1]
        elif abs_diff > fine_steps[2]:
            step = fine_steps[2]
        else:
            step = fine_steps[3]
        if step > 150:
            step = 150

        delta = step if diff > 0 else -step
        new_y = scroll_y + delta
        if _rnd.random() < 0.02:
            await drag_scroll(delta)
        else:
            await human_scroll(delta)
        logger.info(
            "[SCROLL] wheel small: scroll_y=%s → %s, form_top=%s, step=%s",
            scroll_y,
            new_y,
            form_top,
            step,
        )
        await asyncio.sleep(0.06)

        elapsed = asyncio.get_event_loop().time() - start_ts
        logger.info("[SCROLL] iter %s/%s elapsed=%.2fs", i + 1, max_iters, elapsed)
        if elapsed > timeout:
            break

    b = await form.bounding_box()
    if b:
        form_top = b["y"]
        viewport_height = await page.evaluate("window.innerHeight")
        if not (0 <= form_top < viewport_height // 4):
            await drag_scroll(form_top - viewport_height // 4)


async def run_browser(ctx: RunContext):
    async with async_playwright() as p:
        headless = (
            ctx.json_headless if ctx.json_headless is not None else CFG["HEADLESS"]
        )

        browser = await p.chromium.launch(
            proxy=proxy_cfg if proxy_cfg else None,
            headless=headless,
            channel="chrome",
            args=[
                "--incognito",
                "--disable-gpu",
                "--no-sandbox",
                "--disable-dev-shm-usage",
            ],
        )

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
        logger.info("[INFO] Patched WebGL2 getParameter")
        page = await context.new_page()
        global GCURSOR
        GCURSOR = create_cursor(page)
        if not hasattr(GCURSOR, "wheel"):

            async def _wheel(dx: float, dy: float):
                m = getattr(page, "mouse")
                await m.wheel(dx, dy)

            GCURSOR.wheel = _wheel  # type: ignore[attr-defined]

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
                logger.info("[INFO] Страница загружена")
            except PWTimeoutError:
                error_msg = f"Timeout {CFG['PAGE_GOTO_TIMEOUT'] // 1000} сек. при загрузке лендинга"
                logger.error(f" {error_msg}")
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
                logger.error(f" {error_msg}")
                raise Exception(error_msg)

            try:
                await page.wait_for_selector(
                    "div.form-wrapper", timeout=CFG["FORM_WRAPPER_TIMEOUT"]
                )
                logger.info("[INFO] Контент формы загружен (div.form-wrapper найден)")
            except Exception as e:
                logger.warning(f" div.form-wrapper не найден: {e}")

            # измеряем фактическое время полной загрузки
            load_ms = await page.evaluate(
                "() => { const t = performance.timing; return (t.loadEventEnd || t.domContentLoadedEventEnd) - t.navigationStart; }"
            )
            load_sec = max(load_ms / 1000, 0)

            min_read = 1.5  # минимум «чтения», сек
            base_read = _rnd.uniform(2, 6)
            total_time = max(min_read, base_read - max(0, 7 - load_sec))

            logger.info(f"[INFO] Имитация “чтения” лендинга: {total_time:.1f} сек")

            await emulate_user_reading(page, total_time, ctx)

            # ====================================================================================
            # Быстрые крупные скроллы к форме, без телепортов
            # ====================================================================================
            # await smooth_scroll_to_form(page, ctx)

            # ====================================================================================
            # Скроллим к форме теми же мелкими шагами, что и при «гулянии»
            # ====================================================================================
            await scroll_to_form_like_reading(page, ctx)

            # ====================================================================================
            # Этап 4. Клик мышью по каждому полю, заполнение всех полей, установка галочки
            # ====================================================================================
            try:
                logger.info("[INFO] Вводим ФИО через fill_full_name")
                await fill_full_name(page, user_name, ctx)

                logger.info("[INFO] Вводим город через fill_city")
                await fill_city(page, user_city, ctx)
                await asyncio.sleep(0.3)

                logger.info("[INFO] Вводим телефон через fill_phone")
                await fill_phone(page, phone_for_form, ctx)
                await asyncio.sleep(0.1)

                logger.info("[INFO] Вводим пол через fill_gender")
                await fill_gender(page, user_gender, ctx)
                await asyncio.sleep(0.1)

                logger.info("[INFO] Вводим возраст через fill_age")
                await fill_age(page, user_age, ctx)
                await asyncio.sleep(0.1)

                logger.info("[INFO] Вводим тип курьера через fill_courier_type")
                await fill_courier_type(page, user_courier_type, ctx)
                await asyncio.sleep(0.1)

                logger.info("[INFO] Ставим галочку политики через fill_policy_checkbox")
                await fill_policy_checkbox(page, ctx)
                await asyncio.sleep(0.1)

                logger.info(
                    "[INFO] Все поля формы заполнены и чекбокс отмечен. Ожидание завершено."
                )

            except Exception as e:
                logger.error(f" Ошибка на этапе заполнения формы: {e}")

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
            logger.info(f"[INFO] Скриншот формы сохранён: {ctx.screenshot_path}")

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
                        name:    document.querySelector(%s)?.value || "",
                        city:    document.querySelector('input[name="user_city"]')?.value || "",
                        phone:   document.querySelector(%s)?.value || "",
                        gender:  getSelectText("user_gender"),
                        age:     document.querySelector('input[name="user_age"]')?.value || "",
                        courier: getSelectText("user_courier_type")
                    }
                }
                """
                    % (
                        json.dumps(selectors["form"]["name"]),
                        json.dumps(selectors["form"]["phone"]),
                    )
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
                    logger.error(
                        "Не заполнены поля: %s",
                        ", ".join(empty_fields),
                    )
                    ctx.errors = empty_fields
                    error_msg = f"Не заполнены поля: {', '.join(empty_fields)}"
                else:
                    ctx.errors = []
                    error_msg = ""
            except Exception as e:
                logger.error(f" Ошибка при проверке/скриншоте: {e}")
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
                    old_url = page.url
                    try:
                        await submit_form(page, ctx)
                        logger.info("[INFO] Кнопка 'Оставить заявку' успешно нажата")
                    except Exception as e:
                        logger.error(
                            "Не удалось кликнуть по кнопке 'Оставить заявку': %s",
                            e,
                        )
                        raise

                    try:
                        # Ждём редирект (10 сек)
                        await page.wait_for_function(
                            f'document.location.href !== "{old_url}"',
                            timeout=CFG["REDIRECT_TIMEOUT"],
                        )
                        logger.info("[INFO] URL изменился — заявка успешно отправлена")

                        modal_selector = selectors["form"]["thank_you"]
                        modal = await page.query_selector(modal_selector)

                        if modal is None:
                            try:
                                await page.wait_for_selector(
                                    modal_selector,
                                    timeout=CFG["MODAL_SELECTOR_TIMEOUT"],
                                )
                                logger.info(
                                    "[INFO] Всплывающее окно подтверждения появилось после ожидания"
                                )
                            except PWTimeoutError:
                                logger.info("selector_not_found:thank_you")
                                html_content = await page.content()
                                logger.error(
                                    "Всплывающее окно подтверждения НЕ появилось после ожидания: Timeout"
                                )
                                logger.info(
                                    f"[DEBUG HTML CONTENT]: {html_content[:2000]}"
                                )
                                raise
                            except Exception as e:
                                html_content = await page.content()
                                logger.error(
                                    "Всплывающее окно подтверждения НЕ появилось после ожидания: %s",
                                    e,
                                )
                                logger.info(
                                    f"[DEBUG HTML CONTENT]: {html_content[:2000]}"
                                )
                                raise
                        else:
                            logger.info(
                                "[INFO] Всплывающее окно подтверждения уже было на экране"
                            )

                        logger.info("[INFO] Всплывающее окно подтверждения появилось")
                        await asyncio.sleep(_rnd.uniform(1, 4))
                        # ====== Этап 7: извлечение utm_term из финального URL ======
                        try:
                            final_url = page.url
                            logger.info(
                                "[DEBUG] Финальный URL для поиска utm_term: %s",
                                final_url,
                            )
                            ctx.postback = None
                            if "utm_term=" in final_url:
                                start = final_url.find("utm_term=") + len("utm_term=")
                                end = final_url.find("&", start)
                                if end == -1:
                                    ctx.postback = final_url[start:]
                                else:
                                    ctx.postback = final_url[start:end]
                                logger.info("POSTBACK: %s", ctx.postback)
                            else:
                                logger.error("utm_term не найден в ссылке")
                                raise Exception("utm_term not found in final URL")
                        except Exception as e:
                            logger.error("Ошибка на этапе извлечения utm_term: %s", e)
                    except Exception as e:
                        if page.url == old_url:
                            logger.error("Редирект после клика не произошел")
                            raise Exception("Редирект после клика не произошел")
                        else:
                            logger.error(
                                "Всплывающее окно подтверждения НЕ появилось: %s",
                                e,
                            )
                except Exception as e:
                    logger.error(
                        "Ошибка при клике по кнопке 'Оставить заявку': %s",
                        e,
                    )

        finally:
            await context.close()
            await browser.close()


async def main(ctx: RunContext):
    """Entry point for execution: load selectors then run browser."""
    global selectors
    profile = params.get("selectors_profile", "default")
    try:
        selectors = load_selectors(profile)
    except FileNotFoundError:
        raise RuntimeError(f"selectors profile '{profile}' not found")

    path = Path("selectors") / f"{profile}.yml"
    logger.info("selectors profile: %s file: %s", profile, path.resolve())

    await asyncio.wait_for(run_browser(ctx), timeout=CFG["RUN_TIMEOUT"])


if __name__ == "__main__":
    try:
        asyncio.run(main(ctx))
    except RuntimeError as e:
        if str(e).startswith("selectors profile"):
            logger.info(f"[FATAL] {e}")
            fatal = {"phone": user_phone, "error": "selectors_profile_not_found"}
            if not ctx.browser_closed_manually:
                send_webhook(fatal, webhook_url, ctx)
            print(json.dumps(fatal, ensure_ascii=False))
            sys.exit(1)
        else:
            logger.info(f"[FATAL] {e}")
            fatal = {"error": f"UNCAUGHT {e.__class__.__name__}: {e}"}
            print(json.dumps(fatal, ensure_ascii=False))
            sys.exit(1)
    except Exception as e:  # любая непойманная ошибка
        logger.info(f"[FATAL] {e}")
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
if error_lines and not ctx.errors:
    ctx.errors = error_lines

proxy_used = proxy_cfg is not None
logger.info("proxy_used: %s", proxy_used)

send_result(ctx, user_phone, webhook_url, headless_error, proxy_used)
