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
import atexit
import logging
import requests
from urllib.parse import urlparse
from contextlib import suppress  # для мягкой отмены фоновых тасков

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
    """Безопасный клик через GhostCursor: селектор всегда резолвим в элемент.
    Строка внутрь GCURSOR.click не передаётся (во избежание no-op)."""
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
            await GCURSOR.click(None)  # клик по текущей позиции
        return
    # fallback: если элемента нет/нет bbox — делаем нативный клик по элементу
    if el:
        await el.click()
    else:
        await GCURSOR.click(None)


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
            await asyncio.sleep(_rnd.uniform(1.25, 3.2))  # умеренные паузы


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
    await asyncio.sleep(_rnd.uniform(0.1, 0.25))


def _to_bool(val: str | bool) -> bool:
    if isinstance(val, bool):
        return val
    return str(val).lower() in {"1", "true", "yes", "on"}


def normalize_phone(num: str) -> str:
    digits = "".join(ch for ch in str(num) if ch.isdigit())
    return digits[-10:] if len(digits) >= 10 else digits


# --- POSTBACK extraction (редактируй только эти 2 строки) ---
POSTBACK_START = "utm_term="
POSTBACK_END = "&utm"  # None → до конца строки
POSTBACK_FALLBACK = "Samokat"  # что вернуть, если start не найден


def extract_postback_from_url(
    url: str,
    start: str = POSTBACK_START,
    end: str | None = POSTBACK_END,
    fallback: str = POSTBACK_FALLBACK,
) -> str:
    """Возвращает подстроку между start и end из URL.
       Если start не найден — возвращает fallback (без декодирования)."""
    try:
        if start in url:
            tail = url.split(start, 1)[1]
            return tail.split(end, 1)[0] if end and (end in tail) else tail
        return fallback
    except Exception:
        return fallback


def send_webhook(result, webhook_url, ctx: RunContext):
    if webhook_url:
        e = None
        # быстрее и мягче: максимум ~65 секунд вместо ~3 минут
        backoff = [5, 15, 30]
        for attempt, pause in enumerate(backoff, start=1):
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
                    "webhook fail, retry %s in %ss: %s",
                    attempt,
                    pause,
                    e,
                )
                time.sleep(pause)
        logger.error("webhook 3rd fail: %s", e)


def _safe_proxy_str(parsed: dict | None, raw: str | None) -> str:
    """Возвращает строку вида 'scheme://host:port' без логина/пароля.
    Если есть parsed (из parse_proxy), используем его поля, иначе парсим raw по минимуму.
    """
    if parsed:
        scheme = parsed.get("scheme")
        host = parsed.get("host")
        port = parsed.get("port")
        if scheme and host and port:
            return f"{scheme}://{host}:{port}"
    if raw:
        try:
            up = urlparse(raw)
            if up.scheme and up.hostname and up.port:
                return f"{up.scheme}://{up.hostname}:{up.port}"
        except Exception:
            pass
        return raw
    return ""


# --- Единый перевод кодов ошибок на русский ---
ERROR_RU = {
    "bad_proxy_format": "Некорректный формат прокси",
    "bad_proxy_unreachable": "Прокси недоступен",
    "selectors_profile_not_found": "Профиль селекторов не найден",
    "selectors_not_found": "Профиль селекторов не найден",
    "no_redirect": "Не произошёл редирект после отправки формы",
    "thankyou_timeout": "Сообщение «Спасибо» не появилось за 120 секунд",
    "duplicate_request": "Повторный запуск для того же сайта и телефона",
    "duplicate_phone": "Одновременный запуск для этого телефона",
    "unexpected_error": "Непредвиденная ошибка выполнения",
    "POSTBACK missing": "POSTBACK отсутствует",
    "bad headless value": "Некорректное значение параметра headless",
}

_REQUIRED_FIELD_NAMES = {"Имя", "Город", "Телефон", "Пол", "Возраст", "Тип курьера"}


def _to_ru(err: str) -> str:
    return ERROR_RU.get(err, err)


def _errors_to_ru(err_list: list[str]) -> str:
    if err_list and all(e in _REQUIRED_FIELD_NAMES for e in err_list):
        return "Не заполнены поля: " + ", ".join(err_list)
    return ", ".join(_to_ru(e) for e in err_list)


def send_result(
    ctx: RunContext,
    phone: str,
    webhook_url: str,
    headless_error: bool,
    proxy_used: bool,
) -> None:
    """Финальный JSON/вебхук. Успех = есть ctx.postback (реально извлечённый POSTBACK)."""
    result: dict[str, str] = {"phone": phone}
    success = bool(ctx.postback)

    if success:
        result["POSTBACK"] = ctx.postback
    elif ctx.errors:
        result["error"] = _errors_to_ru(ctx.errors)
    else:
        result["error"] = _to_ru("POSTBACK missing")

    if ("error" not in result) and (not success) and headless_error:
        result["error"] = _to_ru("bad headless value")

    if "error" in result and ctx.screenshot_path:
        result["screenshot"] = ctx.screenshot_path

    result_state = "SUCCESS" if "error" not in result else "ERROR"
    logger.info("RESULT: %s", result_state)
    if "error" in result:
        logger.info("ИТОГ: %s", result["error"])
    else:
        logger.info("POSTBACK получен")

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

    # --- de-dup handlers & stop propagation ---
    logger.propagate = False
    abs_log = os.path.abspath(ctx.log_file)

    for h in list(logger.handlers):
        if isinstance(h, logging.FileHandler) and getattr(h, "baseFilename", None) == abs_log:
            logger.removeHandler(h)
            with suppress(Exception):
                h.close()

    if not any(
        isinstance(h, logging.FileHandler) and getattr(h, "baseFilename", None) == abs_log
        for h in logger.handlers
    ):
        logger.addHandler(file_handler)

    # --- console highlight: green background for SUCCESS line ---
    try:
        from colorama import init as _cinit, Fore, Back, Style

        _cinit()

        class _GreenBGFormatter(logging.Formatter):
            PATS = ("RESULT: SUCCESS", "---")

            def format(self, record: logging.LogRecord) -> str:
                s = super().format(record)
                try:
                    if any(p in record.getMessage() for p in self.PATS):
                        return f"{Back.GREEN}{Fore.BLACK}{s}{Style.RESET_ALL}"
                except Exception:
                    pass
                return s

        fmt = "[%(asctime)s] %(levelname)s %(name)s – %(message)s"
        datefmt = "%H:%M:%S"

        has_stream = False
        for h in logger.handlers:
            if isinstance(h, logging.StreamHandler) and not isinstance(
                h, logging.FileHandler
            ):
                h.setFormatter(_GreenBGFormatter(fmt, datefmt=datefmt))
                has_stream = True

        if not has_stream:
            sh = logging.StreamHandler()
            sh.setLevel(logger.level)
            sh.setFormatter(_GreenBGFormatter(fmt, datefmt=datefmt))
            logger.addHandler(sh)
    except Exception:
        pass

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
            logger.error("bad_proxy_unreachable")
            fatal = {"phone": user_phone, "error": "bad_proxy_unreachable"}
            send_webhook(fatal, webhook_url, ctx)
            print(json.dumps(fatal, ensure_ascii=False))
            sys.exit(1)
        else:
            logger.info("Прокси проверен: OK — %s", _safe_proxy_str(parsed, proxy_url))
    if ctx.json_headless is not None:
        logger.info("headless overridden by JSON → %s", ctx.json_headless)
except Exception as e:
    print(f"[ERROR] Не удалось получить JSON из stdin: {e}")
    sys.exit(1)

# === Доп. параметры из JSON ===
def _as_str(v) -> str:
    return "" if v is None else str(v)

phone_from_Avito = _as_str(params.get("phone_from_Avito", ""))
phone_for_form = phone_from_Avito if phone_from_Avito else _as_str(user_phone)

user_age = _as_str(params.get("user_age", ""))
user_city = _as_str(params.get("user_city", ""))
user_name = _as_str(params.get("user_name", ""))
birth_date = _as_str(params.get("birth_date", ""))
user_gender = _as_str(params.get("user_gender", ""))
user_courier_type = _as_str(params.get("user_courier_type", ""))

_enc = sys.stdout.encoding
if not _enc or _enc.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")
_enc = sys.stderr.encoding
if not _enc or _enc.lower() != "utf-8":
    sys.stderr.reconfigure(encoding="utf-8")


# === P-6. Единый фильтр для блокировки «лишних» запросов ===
async def should_abort(route, ctx: RunContext, allow_host: str | None = None):
    """
    Минимальный безопасный фильтр:
      • блокируем ресурсы с resource_type in {"image","media"}, КРОМЕ основного хоста лендинга (п.9)
      • блокируем URL по CFG["BLOCK_PATTERNS"] (подстраховано исключением allow_host) (п.15)
      • пропускаем картинки с хостов из CFG["ALLOW_IMAGE_HOSTS"]
    """
    req = route.request
    r_type = req.resource_type
    url = req.url
    host = urlparse(url).hostname or ""

    # allowlist для изображений/CDN
    allow_image_hosts = set(CFG.get("ALLOW_IMAGE_HOSTS", []) or [])

    # не рубим медиа/картинки для основного хоста и для allowlist
    block_by_type = (r_type in ("image", "media")) and (
        (allow_host is None or host != allow_host) and (host not in allow_image_hosts)
    )

    def _match_pattern(p: str) -> bool:
        p = p.strip()
        if not p:
            return False
        if "://" not in p and "/" not in p and "." in p:
            return p in host
        return p in url

    block_by_pattern = any(_match_pattern(p) for p in CFG["BLOCK_PATTERNS"])

    must_abort = block_by_type or (
        block_by_pattern and (allow_host is None or host != allow_host)
    )

    if not must_abort:
        await route.continue_()
        return

    if not ctx.first_abort_logged:
        logger.info("ABORT %s %s (%s)", req.method, req.url, r_type)
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


# ==== тайм-зона по крупным городам РФ (без случайного сдвига) ====
TZ_BY_CITY = {
    "Москва": "Europe/Moscow",
    "Санкт-Петербург": "Europe/Moscow",
    "Нижний Новгород": "Europe/Moscow",
    "Казань": "Europe/Moscow",
    "Воронеж": "Europe/Moscow",
    "Ростов-на-Дону": "Europe/Moscow",
    "Волгоград": "Europe/Volgograd",
    "Самара": "Europe/Samara",  # UTC+4
    "Екатеринбург": "Asia/Yekaterinburg",  # корректная таймзона
    "Челябинск": "Asia/Yekaterinburg",
    "Уфа": "Asia/Yekaterinburg",
    "Пермь": "Asia/Yekaterinburg",
    "Омск": "Asia/Omsk",  # UTC+6
    "Новосибирск": "Asia/Novosibirsk",  # UTC+7
    "Красноярск": "Asia/Krasnoyarsk",
}
tz = TZ_BY_CITY.get(user_city, "Europe/Moscow")

# === fingerprint values (согласовано под Windows/Chrome) ===
FP_PLATFORM = "Win32"
FP_DEVICE_MEMORY = 8
FP_HARDWARE_CONCURRENCY = 8
FP_LANGUAGES = ["ru-RU", "ru"]
FP_ACCEPT_LANGUAGE = "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7"
FP_WEBGL_VENDOR = "Google Inc. (Intel)"
FP_WEBGL_RENDERER = "ANGLE (Intel(R) UHD Graphics 620 Direct3D11 vs_5_0 ps_5_0)"
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
    base = _rnd.lognormvariate(CFG["HUMAN_DELAY_μ"], CFG["HUMAN_DELAY_σ"])
    return max(0.08, min(base, 0.18))


async def human_type(page, selector: str, text: str, ctx: RunContext):
    await page.focus(selector)
    n = len(text)
    coef = 0.8 if n <= 3 else 1.1 if n > 20 else 1.0
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


# -------------------- ФИО --------------------
async def fill_full_name(page, name: str, ctx: RunContext, retries: int = 3) -> bool:
    for attempt in range(retries):
        try:
            input_box = page.locator(selectors["form"]["name"])
            await _scroll_if_needed(input_box, dropdown_room=150, step_range=(300, 420))
            await human_move_cursor(page, input_box, ctx)
            await ghost_click(input_box)
            await page.wait_for_timeout(_rnd.randint(20, 40))
            await input_box.fill("")
            await human_type(page, selectors["form"]["name"], name, ctx)
            if (await input_box.input_value()).strip() == name.strip():
                return True
            await page.wait_for_timeout(_rnd.randint(40, 70))
        except Exception as e:
            logger.warning("fill_full_name attempt %s failed: %s", attempt + 1, e)
    logger.error("Не удалось заполнить поле ФИО")
    return False


# -------------------- ГОРОД --------------------
async def fill_city(page, city: str, ctx: RunContext, retries: int = 3) -> bool:
    item_sel = selectors["form"]["city_item"]
    list_sel = selectors["form"].get("city_list", item_sel)
    list_sel_visible = f"{list_sel}:visible"
    for attempt in range(retries):
        try:
            inp = page.locator(selectors["form"]["city"])
            await _scroll_if_needed(inp, dropdown_room=260, step_range=(280, 300))
            await human_move_cursor(page, inp, ctx)
            await ghost_click(inp)
            await inp.fill("")
            target_len = max(2, int(len(city) * 0.75))
            typed = ""
            picked = False
            for ch in city:
                await human_type_city_autocomplete(page, selectors["form"]["city"], ch, ctx)
                typed += ch
                await page.evaluate(
                    """sel => {const el = document.querySelector(sel);
                               if (!el) return;
                               ['input','keyup'].forEach(t=>el.dispatchEvent(new Event(t,{bubbles:true})));
                    }""",
                    selectors["form"]["city"],
                )
                await asyncio.sleep(_rnd.uniform(0.06, 0.12))
                lst = page.locator(list_sel_visible)
                prev_cnt = -1
                cnt = 0
                for _ in range(15):
                    cnt = await lst.count()
                    if cnt > 0 and cnt != prev_cnt:
                        break
                    prev_cnt = cnt
                    await asyncio.sleep(0.06)

                if len(typed) >= target_len and cnt > 0:
                    # 1) Пытаемся выбрать точное совпадение
                    options = [(await lst.nth(i).inner_text()).strip() for i in range(min(cnt, 20))]
                    if city in options:
                        idx = options.index(city)
                        item = lst.nth(idx)
                        await human_move_cursor(page, item, ctx)
                        await ghost_click(item)
                        await asyncio.sleep(_rnd.uniform(0.03, 0.06))
                        picked = True
                        break
                    # 2) Если точного нет — выбираем первый элемент как fallback
                    item = lst.first
                    await human_move_cursor(page, item, ctx)
                    await ghost_click(item)
                    await asyncio.sleep(_rnd.uniform(0.03, 0.06))
                    picked = True
                    break

            if not picked:
                # 3) финальный fallback — Enter в инпут (если автокомплит поддерживает)
                await page.keyboard.press("Enter")
                await asyncio.sleep(_rnd.uniform(0.05, 0.12))

            # проверка значения
            if (await inp.input_value()).strip():
                return True
            raise ValueError(f"{city} not selected")
        except Exception as e:
            logger.warning("fill_city attempt %s failed: %s", attempt + 1, e)
    logger.error("Не удалось выбрать город")
    raise ValueError("fill_city failed")


async def fill_phone(page, phone: str, ctx: RunContext, retries: int = 3) -> bool:
    def _norm(num: str) -> str:
        return "".join(ch for ch in num if ch.isdigit())[-10:]
    target = _norm(phone)
    for attempt in range(retries):
        try:
            input_box = page.locator(selectors["form"]["phone"])
            await human_move_cursor(page, input_box, ctx)
            await ghost_click(input_box)
            await page.wait_for_timeout(_rnd.randint(25, 45))
            await input_box.fill("")
            await human_type(page, selectors["form"]["phone"], phone, ctx)
            await page.wait_for_timeout(_rnd.randint(60, 80))
            typed = await input_box.input_value()
            if _norm(typed) == target:
                logger.info("[INFO] Телефон введён корректно: %s → %s", phone, typed)
                return True
            logger.warning(
                "fill_phone mismatch (attempt %s): typed=%s exp=%s",
                attempt + 1, typed, phone,
            )
            await page.wait_for_timeout(_rnd.randint(40, 70))
        except Exception as e:
            logger.warning("fill_phone attempt %s failed: %s", attempt + 1, e)
    logger.error("Не удалось заполнить поле Телефон")
    return False


# ─── helpers ──────────────────────────────────────────────────────────
async def _tiny_scroll_once(px: int) -> None:
    if GCURSOR is None:
        raise RuntimeError("GCURSOR not initialized")
    direction = 1 if px > 0 else -1
    remain = abs(px)
    while remain > 0:
        step = min(_rnd.randint(40, 70), remain)
        await GCURSOR.wheel(0, direction * step)
        remain -= step
        await asyncio.sleep(_rnd.uniform(0.04, 0.08))


async def _scroll_if_needed(
    locator,
    *,
    dropdown_room: int = 220,
    step_range: tuple[int, int] = (170, 190),
) -> None:
    page = locator.page
    box = await locator.bounding_box()
    if not box:
        return
    vh = await page.evaluate("window.innerHeight")
    space_below = vh - (box["y"] + box["height"])
    if space_below < dropdown_room:
        step = _rnd.randint(*step_range)
        await _tiny_scroll_once(step)
        await asyncio.sleep(_rnd.uniform(0.18, 0.33))


# -------------------- ПОЛ --------------------
async def fill_gender(page, gender: str, ctx: RunContext, retries: int = 3) -> bool:
    input_sel = selectors["form"]["gender"]
    item_sel = selectors["form"]["gender_item"]
    input_box = page.locator(input_sel)
    for attempt in range(retries):
        try:
            await _scroll_if_needed(input_box, dropdown_room=220, step_range=(190, 200))
            await human_move_cursor(page, input_box, ctx)
            await ghost_click(input_box)
            option = page.locator(item_sel).get_by_text(gender, exact=True)
            await option.wait_for(state="visible", timeout=4_000)
            await human_move_cursor(page, option, ctx)
            await ghost_click(option)
            val = await page.evaluate(
                """(selInput) => {
                    const inp = document.querySelector(selInput);
                    if (!inp) return "";
                    const wrap = inp.closest('.form-select, .select-wrapper, .select');
                    if (wrap){
                        const sel = wrap.querySelector('.form-select__selected, .selected, [data-selected], .form-list-item.selected');
                        if (sel && sel.textContent) return sel.textContent.trim();
                    }
                    return inp.value || "";
                }""",
                input_sel,
            )
            if val.lower().startswith(gender.lower()):
                return True
            raise ValueError(f"gender value mismatch (“{val}”)")
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
            await page.wait_for_timeout(_rnd.randint(25, 45))
            await input_box.fill("")
            await human_type(page, selectors["form"]["age"], _as_str(age), ctx)
            value = await input_box.input_value()
            if value.strip() == _as_str(age).strip():
                return True
            await page.wait_for_timeout(_rnd.randint(40, 70))
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
            logger.warning("fill_courier_type attempt %s failed: %s", attempt + 1, e)
    logger.error("Не удалось выбрать тип курьера")
    return False


async def fill_policy_checkbox(page, ctx: RunContext, retries=3):
    for attempt in range(retries):
        try:
            input_box = page.locator(selectors["form"]["policy"])
            await _scroll_if_needed(input_box)
            await ghost_click(input_box)
            return True
        except Exception as e:
            logger.warning("fill_policy_checkbox attempt %s failed: %s", attempt + 1, e)
    logger.error("Не удалось поставить галочку политики")
    return False


async def submit_form(page, ctx: RunContext):
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
    await page.focus(selector)
    n = len(text)
    coef = 0.6 if n <= 3 else 1.1 if n > 20 else 1.0
    total = 0.0
    for char in text:
        delay = _human_delay() * coef
        await page.keyboard.type(char, delay=0)
        await asyncio.sleep(delay)
        total += delay
    logger.info(f'[DEBUG] typing "{text}" len={n} total_time={total:.2f}')


async def ghost_click(selector_or_element):
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
            or (selector_or_element if isinstance(selector_or_element, str) else "element")
        )
        logger.info(
            f"[INFO] Курсор к {name} ({int(box['x']+box['width']/2)},{int(box['y']+box['height']/2)})"
        )
    await gc_click(selector_or_element)


async def click_no_move_if_close(page, el, ctx: RunContext, threshold_px: int = 7):
    if GCURSOR is None:
        raise RuntimeError("GCURSOR not initialized")
    box = await el.bounding_box()
    if not box:
        await ghost_click(el)
        return
    cx = box["x"] + box["width"] / 2
    cy = box["y"] + box["height"] / 2
    mx, my = getattr(ctx, "mouse_pos", (None, None))
    close_enough = False
    if mx is None or my is None:
        close_enough = True
    else:
        dx = (mx - cx)
        dy = (my - cy)
        dist = (dx * dx + dy * dy) ** 0.5
        close_enough = dist <= threshold_px
    if close_enough:
        if hasattr(GCURSOR, "click_absolute"):
            await GCURSOR.click_absolute(cx, cy)
        else:
            await GCURSOR.click(None)
        ctx.mouse_pos = (cx, cy)
    else:
        await ghost_click(el)
        ctx.mouse_pos = (cx, cy)


async def human_move_cursor(page, el, ctx: RunContext):
    b = await el.bounding_box()
    if not b:
        return
    target_x = b["x"] + _rnd.uniform(8, b["width"] - 8)
    target_y = b["y"] + _rnd.uniform(8, b["height"] - 8)
    if GCURSOR is None:
        raise RuntimeError("GCURSOR not initialized")
    cur = getattr(ctx, "mouse_pos", (0, 0))
    pivots = []
    if _rnd.random() < 0.3:
        pivots.append(
            (
                (cur[0] + target_x) / 2 + _rnd.uniform(-10, 10),
                (cur[1] + target_y) / 2 + _rnd.uniform(-10, 10),
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
            weights=[0.55, 0.12, 0.18, 0.05, 0.10],
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
            await asyncio.sleep(_rnd.uniform(0.6, 1.3))
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
            await asyncio.sleep(_rnd.uniform(0.4, 0.9))
        elif action == "pause":
            t = _rnd.uniform(0.9, 2.2)
            logger.info(f"[INFO] Пауза {t:.1f}")
            await asyncio.sleep(t)
        elif action == "mouse_wiggle":
            x = _rnd.randint(100, 1200)
            y = _rnd.randint(100, 680)
            dx = _rnd.randint(-10, 10)
            dy = _rnd.randint(-8, 8)
            await gc_move(x, y)
            await asyncio.sleep(_rnd.uniform(0.08, 0.16))
            await gc_move(x + dx, y + dy)
            logger.info(f"[INFO] Мышь дрожит ({x},{y})")
            await asyncio.sleep(_rnd.uniform(0.10, 0.18))
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
                    await asyncio.sleep(_rnd.uniform(0.3, 0.9))


async def scroll_to_form_like_reading(page, ctx: RunContext, timeout: float = 15.0):
    sel = (
        (selectors or {}).get("form", {}).get("wrapper")
        or CFG.get("SELECTORS", {}).get("FORM_WRAPPER")
        or "div.form-wrapper"
    )
    form = await page.query_selector(sel)
    if not form:
        logger.warning("[WARN] scroll_to_form_like_reading: форма %s не найдена", sel)
        return

    vh = await page.evaluate("window.innerHeight")
    box = await form.bounding_box()
    if not box:
        logger.warning("[WARN] scroll_to_form_like_reading: bounding_box пуст")
        return

    form_center = box["y"] + box["height"] / 2
    current_scroll = await page.evaluate(
        "document.scrollingElement.scrollTop || window.scrollY"
    )
    viewport_center = current_scroll + vh / 2
    distance = abs(form_center - viewport_center)
    logger.info(
        f"[SCROLL] scrollTop={current_scroll:.0f}, form_center={form_center:.0f}, "
        f"distance={distance:.0f}px"
    )

    logger.info("[SCROLL] ► final-jump started")

    rect = await form.evaluate("el => el.getBoundingClientRect()")
    form_view_center = rect["top"] + rect["height"] / 2
    header_h = await page.evaluate(
        """
        (() => {
            const el = [...document.querySelectorAll('*')].find(e=>{
                const s = getComputedStyle(e);
                return s.position==='fixed'
                       && parseInt(s.top||0)===0
                       && e.offsetHeight>40 && e.offsetHeight<200;
            });
            return el ? el.offsetHeight : 0;
        })()
    """
    )

    target_center = vh / 2 - header_h + _rnd.randint(0, 60)
    remaining = form_view_center - target_center
    distance = abs(remaining)
    prev_sign = 1 if remaining > 0 else -1

    logger.info(
        f"[DEBUG-JUMP] start  form_center={form_view_center:.0f} "
        f"target_center={target_center:.0f}  diff={remaining:+.0f}"
    )

    while distance > 5:
        direction = 1 if remaining > 0 else -1
        if distance < 200:
            min_step = 40
        elif distance < 600:
            min_step = 80
        else:
            min_step = 120
        pct = _rnd.uniform(0.12, 0.22)
        step = max(min_step, min(350, int(distance * pct)))
        step = int(min(step, distance))
        await human_scroll(direction * step)
        logger.info(f"[SCROLL] wheel {'down' if direction>0 else 'up'} {step}")
        await asyncio.sleep(_rnd.uniform(0.20, 0.45))
        rect = await form.evaluate("el => el.getBoundingClientRect()")
        form_view_center = rect["top"] + rect["height"] / 2
        remaining = form_view_center - target_center
        distance = abs(remaining)
        curr_sign = 1 if remaining > 0 else -1
        if curr_sign != prev_sign:
            break
        prev_sign = curr_sign

    if abs(remaining) > 0:
        jitter = _rnd.randint(-10, 10)
        await human_scroll(int(remaining + jitter))

    await asyncio.sleep(0.25)


# ====================================================================================
# Плавный, крупный и потом мелкий скролл к форме, без телепортов
# ====================================================================================
async def smooth_scroll_to_form(page, ctx: RunContext):
    form = await page.query_selector("div.form-wrapper")
    if not form:
        logger.warning("div.form-wrapper не найден")
        return

    while True:
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

        text_blocks = [".hero", ".about", ".benefits", ".form-wrapper"]
        for sel in text_blocks:
            el = await page.query_selector(sel)
            if el:
                b2 = await el.bounding_box()
                if b2 and abs(b2["y"] - new_y) < 60:
                    pause_t = _rnd.uniform(1.0, 2.8)
                    logger.info(f"[INFO] Пауза у блока {sel} {pause_t:.1f} сек")
                    await asyncio.sleep(pause_t)
                    break

        await asyncio.sleep(_rnd.uniform(2.2, 3.6))

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
        if step > 320:
            step = 320

        if diff > 0:
            new_y = scroll_y + step
            delta = step
        else:
            new_y = scroll_y - step
            delta = -step
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


async def _apply_stealth(context, page):
    """Совместимость разных версий playwright-stealth."""
    try:
        await stealth.Stealth().apply_stealth_async(context)  # 2.x API
        return
    except Exception as e:
        logger.info(f"[INFO] stealth via Stealth() failed: {e}")
    try:
        from playwright_stealth import stealth_async  # type: ignore
        try:
            await stealth_async(page)  # some versions expect page
        except TypeError:
            await stealth_async(context)  # others expect context
    except Exception as e:
        logger.warning(f"[WARN] playwright-stealth not applied: {e}")


def _set_cursor_speed(cur, vmin=1750, vmax=2200):
    """Совместимость разных версий python-ghost-cursor."""
    current = getattr(cur, "min_speed", None)
    if callable(current):
        try:
            cur.min_speed = (lambda v=vmin: v)  # type: ignore[assignment]
        except Exception:
            pass
    else:
        try:
            cur.min_speed = vmin  # type: ignore[assignment]
        except Exception:
            pass
    current = getattr(cur, "max_speed", None)
    if callable(current):
        try:
            cur.max_speed = (lambda v=vmax: v)  # type: ignore[assignment]
        except Exception:
            pass
    else:
        try:
            cur.max_speed = vmax  # type: ignore[assignment]
        except Exception:
            pass


# ========= Универсальный детектор успеха после submit =========
async def _wait_submit_result(context, page, submit_btn, modal_selector: str, ctx: RunContext,
                              first_wait: float = 30.0, second_wait: float = 120.0):
    """
    Кликает submit и ждёт один из сигналов успеха:
      • redirect (URL изменился на той же вкладке),
      • появление модалки "Спасибо" по селектору,
      • появление текста 'спасибо'/'thank you' на странице,
      • новая вкладка (popup) в контексте или через page.popup,
      • закрытие исходной вкладки и появление новой активной.
    Возвращает (success: bool, active_page: Page|None).
    """
    def ms(sec: float) -> int: return int(sec * 1000)
    try:
        old_url = page.url
    except PlaywrightError:
        old_url = ""

    # слушатели popup заранее
    task_ctx_popup = asyncio.create_task(context.wait_for_event("page"))
    task_page_popup = asyncio.create_task(page.wait_for_event("popup"))

    async def _cleanup_popup_waiters():
        for t in (task_ctx_popup, task_page_popup):
            if not t.done():
                t.cancel()
            try:
                await t
            except asyncio.CancelledError:
                pass
            except Exception:
                pass

    async def w_redirect(p, timeout_ms):
        try:
            await p.wait_for_function("url => location.href !== url", arg=old_url, timeout=timeout_ms)
            return ("redirect", p)
        except Exception:
            return None

    async def w_modal(p, timeout_ms):
        if not modal_selector:
            return None
        try:
            await p.wait_for_selector(modal_selector, state="visible", timeout=timeout_ms)
            return ("modal", p)
        except Exception:
            return None

    async def w_thanks_text(p, timeout_ms):
        js = """
        () => {
          const t = (document.body && document.body.innerText || "").toLowerCase();
          return t.includes("спасибо") || t.includes("thank you");
        }"""
        try:
            await p.wait_for_function(js, timeout=timeout_ms)
            return ("thanks-text", p)
        except Exception:
            return None

    async def w_popup(timeout_ms):
        done, _ = await asyncio.wait(
            [task_ctx_popup, task_page_popup],
            timeout=timeout_ms / 1000,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if not done:
            return None
        for t in done:
            try:
                newp = t.result()
                try:
                    await newp.wait_for_load_state("domcontentloaded", timeout=20_000)
                except Exception:
                    pass
                return ("popup", newp)
            except Exception:
                continue
        return None

    async def w_close_then_new(p, timeout_ms):
        try:
            await p.wait_for_event("close", timeout=timeout_ms)
        except Exception:
            return None
        t0 = time.time()
        while time.time() - t0 < 3.0:
            pages = [pg for pg in context.pages if not pg.is_closed()]
            if pages:
                newp = pages[-1]
                with suppress(Exception):
                    await newp.wait_for_load_state("domcontentloaded", timeout=20_000)
                logger.info("[INFO] Старая вкладка закрыта, переключились на новую")
                return ("closed->new", newp)
            await asyncio.sleep(0.1)
        return ("closed", None)

    async def phase(timeout_s):
        timeout_ms = ms(timeout_s)
        watchers = [
            w_redirect(page, timeout_ms),
            w_modal(page, timeout_ms),
            w_thanks_text(page, timeout_ms),
            w_popup(timeout_ms),
            w_close_then_new(page, timeout_ms),
        ]
        tasks = [asyncio.create_task(w) for w in watchers]
        done, pending = await asyncio.wait(
            tasks,
            timeout=timeout_s,
            return_when=asyncio.FIRST_COMPLETED,
        )
        for t in pending:
            t.cancel()
        if pending:
            try:
                await asyncio.gather(*pending, return_exceptions=True)
            except Exception:
                pass
        if not done:
            return False, page
        for t in done:
            try:
                res = t.result()
                if res and res[0] in {"redirect", "modal", "thanks-text", "popup", "closed->new"}:
                    return True, res[1]
            except Exception:
                continue
        return False, page

    # первый клик
    try:
        with suppress(Exception):
            await submit_btn.scroll_into_view_if_needed()
        with suppress(Exception):
            await human_move_cursor(page, submit_btn, ctx)
        await ghost_click(submit_btn)
        logger.info("[INFO] Первый клик по кнопке 'Оставить заявку'")
        _box = await submit_btn.bounding_box()
        if _box:
            _cx = _box["x"] + _box["width"] / 2
            _cy = _box["y"] + _box["height"] / 2
            ctx.mouse_pos = (_cx, _cy)
    except PlaywrightError as e:
        logger.error("Клик по submit не удался: %s", e)
        await _cleanup_popup_waiters()
        return False, page

    ok, active = await phase(first_wait)
    if ok:
        await _cleanup_popup_waiters()
        return True, active

    # повторный клик при необходимости (без движения, если близко)
    with suppress(Exception):
        if active and (not active.is_closed()) and active == page:
            await click_no_move_if_close(active, submit_btn, ctx, threshold_px=7)
            logger.info("[INFO] Повторный клик по кнопке 'Оставить заявку'")

    ok, active = await phase(second_wait)
    await _cleanup_popup_waiters()
    return ok, (active if ok else None)


async def run_browser(ctx: RunContext):
    global GCURSOR

    async with async_playwright() as p:
        headless = (
            ctx.json_headless if ctx.json_headless is not None else CFG["HEADLESS"]
        )

        # Fallback: если нет установленного Chrome
        try:
            browser = await p.chromium.launch(
                proxy=proxy_cfg if proxy_cfg else None,
                headless=headless,
                channel="chrome",
                args=[
                    "--incognito",
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                ],
            )
        except Exception as e:
            logger.warning("Chrome channel launch failed (%s). Fallback to default Chromium.", e)
            browser = await p.chromium.launch(
                proxy=proxy_cfg if proxy_cfg else None,
                headless=headless,
                args=[
                    "--incognito",
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

        page = await context.new_page()
        await _apply_stealth(context, page)

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

        GCURSOR = create_cursor(page)
        _set_cursor_speed(GCURSOR, 1750, 2200)

        if not hasattr(GCURSOR, "wheel"):
            async def _wheel(dx: float, dy: float):
                m = getattr(page, "mouse")
                await m.wheel(dx, dy)
            GCURSOR.wheel = _wheel  # type: ignore[attr-defined]

        async def _abort(route):
            try:
                allow_host = urlparse(page.url).hostname
            except Exception:
                allow_host = None
            await should_abort(route, ctx, allow_host=allow_host)

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
                error_msg = (
                    f"Timeout {CFG['PAGE_GOTO_TIMEOUT'] // 1000} сек. при загрузке лендинга"
                )
                logger.error(f"{error_msg}")
                raise
            except PlaywrightError as e:
                if "has been closed" in str(e) or "TargetClosedError" in str(e):
                    ctx.browser_closed_manually = True
                    sys.exit(0)
                raise

            # Проверка на 503 (легковесная)
            try:
                status_head = await page.evaluate("(d=> (d.innerText||'').slice(0,2048)) (document.documentElement)")
            except Exception:
                status_head = ""
            if (
                "503" in status_head
                or "Сервис временно недоступен" in status_head
                or "Service Unavailable" in status_head
            ):
                error_msg = "Ошибка 503: сайт временно недоступен"
                logger.error(f"{error_msg}")
                raise Exception(error_msg)

            try:
                await page.wait_for_selector(
                    "div.form-wrapper", timeout=CFG["FORM_WRAPPER_TIMEOUT"]
                )
                logger.info("[INFO] Контент формы загружен (div.form-wrapper найден)")
            except Exception as e:
                logger.warning(f"div.form-wrapper не найден: {e}")

            load_ms = await page.evaluate(
                "() => { const t = performance.timing; return (t.loadEventEnd || t.domContentLoadedEventEnd) - t.navigationStart; }"
            )
            load_sec = max(load_ms / 1000, 0)
            min_read = 1.5
            base_read = _rnd.uniform(2, 6)
            total_time = max(min_read, base_read - max(0, 7 - load_sec))
            logger.info(f"[INFO] Имитация “чтения” лендинга: {total_time:.1f} сек")

            await emulate_user_reading(page, total_time, ctx)
            await scroll_to_form_like_reading(page, ctx)

            # ====================================================================================
            # Этап 4. Заполнение полей и чекбокса
            # ====================================================================================
            try:
                logger.info("[INFO] Вводим ФИО через fill_full_name")
                await fill_full_name(page, user_name, ctx)

                logger.info("[INFO] Вводим город через fill_city")
                await fill_city(page, user_city, ctx)
                await asyncio.sleep(0)

                logger.info("[INFO] Вводим телефон через fill_phone")
                await fill_phone(page, phone_for_form, ctx)
                await asyncio.sleep(0)

                logger.info("[INFO] Вводим пол через fill_gender")
                await fill_gender(page, user_gender, ctx)
                await asyncio.sleep(0)

                logger.info("[INFO] Вводим возраст через fill_age")
                await fill_age(page, user_age, ctx)
                await asyncio.sleep(0)

                logger.info("[INFO] Вводим тип курьера через fill_courier_type")
                await fill_courier_type(page, user_courier_type, ctx)
                await asyncio.sleep(0)

                logger.info("[INFO] Ставим галочку политики через fill_policy_checkbox")
                await fill_policy_checkbox(page, ctx)
                await asyncio.sleep(_rnd.uniform(0.1, 0.3))

                logger.info("[INFO] Все поля формы заполнены и чекбокс отмечен.")
            except Exception as e:
                logger.error(f"Ошибка на этапе заполнения формы: {e}")

            # ====================================================================================
            # Этап 5. Скриншот формы после заполнения и проверка заполненности
            # ====================================================================================
            screenshot_dir = os.path.join(WORK_DIR, "Media", "Screenshots")
            os.makedirs(screenshot_dir, exist_ok=True)
            ts = datetime.now().strftime("%Y%m%d-%H%M%S")
            filename = f"{ts}_{user_phone}.png"
            path = os.path.join(screenshot_dir, filename)
            await page.screenshot(path=path, full_page=False)
            ctx.screenshot_path = path
            logger.info(f"[INFO] Скриншот формы сохранён: {ctx.screenshot_path}")
            await asyncio.sleep(0.3)

            try:
                values = await page.evaluate(
                    """
                () => {
                    const getSelectText = (inputName, fallback="") => {
                        const inp = document.querySelector('input[name="'+inputName+'"]');
                        if (inp && inp.value) return inp.value;
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
                    logger.error("Не заполнены поля: %s", ", ".join(empty_fields))
                    ctx.errors = empty_fields
                    error_msg = f"Не заполнены поля: {', '.join(empty_fields)}"
                else:
                    ctx.errors = []
                    error_msg = ""
            except Exception as e:
                logger.error(f"Ошибка при проверке/скриншоте: {e}")
                error_msg = str(e)

            if error_msg:
                pass
            else:
                # ====================================================================================
                # Этап 6. Отправка формы + универсальное ожидание успеха
                # ====================================================================================
                try:
                    btn_visible = page.locator("button.btn_submit:visible")
                    if await btn_visible.count():
                        submit_btn = btn_visible.last
                    else:
                        submit_btn = (
                            page.locator("form")
                            .locator(selectors["form"]["submit"] + ":visible")
                            .last
                        )

                    # разумный дефолт на случай пустого в профиле
                    modal_selector = (
                        (selectors.get("form", {}) or {}).get("thank_you")
                        or ".thank-you, .modal-thanks, [data-thanks], .ty, .thanks, .popup-thanks"
                    )

                    success, active_page = await _wait_submit_result(
                        context, page, submit_btn, modal_selector, ctx,
                        first_wait=30.0, second_wait=120.0
                    )

                    final_page = active_page if (active_page and not active_page.is_closed()) else page
                    final_url = ""
                    with suppress(Exception):
                        final_url = final_page.url

                    if success:
                        # Успех засчитываем ТОЛЬКО если реально извлекли postback (без использования fallback)
                        term = ""
                        if final_url:
                            term = extract_postback_from_url(final_url)
                            if term == POSTBACK_FALLBACK:
                                term = ""
                        if not term:
                            # альтернативные источники postback: скрытые поля/мета/атрибуты
                            try:
                                term = await final_page.evaluate("""
                                    () => {
                                      const byQS = (sel)=>document.querySelector(sel);
                                      const v = byQS('input[name="utm_term"]')?.value
                                            || byQS('[data-postback]')?.getAttribute('data-postback')
                                            || byQS('meta[name="utm_term"]')?.content
                                            || '';
                                      return v || '';
                                    }
                                """)
                            except Exception:
                                term = ""
                        if term:
                            ctx.postback = term
                            logger.info("postback: %s", term or "<empty>")
                            await asyncio.sleep(_rnd.uniform(1.0, 4.0))
                        else:
                            logger.error("Редирект/модалка были, но POSTBACK не извлечён")
                            ctx.errors.append("POSTBACK missing")
                    else:
                        logger.error("Редирект или всплывающее окно 'Спасибо' не появилось")
                        ctx.errors.append("thankyou_timeout")

                except Exception as e:
                    logger.error("Ошибка при клике по кнопке 'Оставить заявку': %s", e)

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
    # ---- Идемпотентность: блокировка дубликатов по телефону (кроссплатформенная) ----
    run_main = True
    lock_file_path: str | None = None
    lock_fd: int | None = None
    LOCK_TTL_SEC = 3600  # 1 час TTL

    try:
        import hashlib
        import ctypes
        from ctypes import wintypes
        import time as _time

        norm_phone = normalize_phone(user_phone)
        if not norm_phone:
            logger.warning("Empty phone → skip duplicate guard")
        else:
            key_src = f"phone|{norm_phone}"
            digest = hashlib.sha1(
                key_src.encode("utf-8"), usedforsecurity=False
            ).hexdigest()
            mutex_name = f"Global\\samokat_{digest}"

            is_windows = os.name == "nt"
            duplicate = False

            if is_windows:
                try:
                    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
                    CreateMutexW = kernel32.CreateMutexW
                    CreateMutexW.restype = wintypes.HANDLE
                    CreateMutexW.argtypes = [
                        wintypes.LPVOID,
                        wintypes.BOOL,
                        wintypes.LPCWSTR,
                    ]

                    win_mutex_handle = CreateMutexW(None, True, mutex_name)
                    last_error = ctypes.get_last_error()
                    ERROR_ALREADY_EXISTS = 183

                    if not win_mutex_handle:
                        logger.warning("CreateMutexW failed; will try file lock fallback")
                    elif last_error == ERROR_ALREADY_EXISTS:
                        logger.info("Duplicate run blocked for phone: %s", norm_phone)
                        duplicate = True
                except Exception as e:
                    logger.warning("Idempotency (mutex) init failed: %s", e)

            if not is_windows or "win_mutex_handle" not in locals() or not win_mutex_handle:
                try:
                    locks_dir = os.path.join(os.path.dirname(__file__), "Locks")
                    os.makedirs(locks_dir, exist_ok=True)
                    lock_file_path = os.path.join(locks_dir, f"samokat_{digest}.lock")
                    if os.path.exists(lock_file_path):
                        with suppress(Exception):
                            st = os.stat(lock_file_path)
                            if _time.time() - st.st_mtime > LOCK_TTL_SEC:
                                os.remove(lock_file_path)
                                logger.info("Stale lock removed for phone: %s", norm_phone)

                    flags = os.O_CREAT | os.O_EXCL | os.O_RDWR
                    lock_fd = os.open(lock_file_path, flags, 0o644)
                    os.write(lock_fd, f"{os.getpid()} {int(_time.time())}".encode("utf-8"))
                except FileExistsError:
                    logger.info("Duplicate run blocked (file lock) for phone: %s", norm_phone)
                    duplicate = True
                except Exception as e:
                    logger.warning("File lock guard failed: %s", e)

            if duplicate:
                with suppress(Exception):
                    ctx.errors.append("duplicate_phone")
                run_main = False

            if lock_file_path and lock_fd is not None:
                def _cleanup_lock():
                    with suppress(Exception):
                        os.close(lock_fd)  # type: ignore[arg-type]
                    with suppress(Exception):
                        os.remove(lock_file_path)
                atexit.register(_cleanup_lock)

    except Exception as e:
        logger.warning("Idempotency init failed: %s", e)

    # ---- Глобальный таймаут из CFG ----
    try:
        if run_main:
            asyncio.run(main(ctx))
        else:
            logger.info("Skip main() due to duplicate_phone")
    except asyncio.TimeoutError:
        logger.error("Global timeout hit (CFG.RUN_TIMEOUT)")
        ctx.errors.append("unexpected_error")
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
    except Exception as e:
        logger.info(f"[FATAL] {e}")
        fatal = {"error": f"UNCAUGHT {e.__class__.__name__}: {e}"}
        print(json.dumps(fatal, ensure_ascii=False))
        sys.exit(1)

    # ====================================================================================
    # Этап 8. Возврат данных во Flask (результаты выполнения)
    # ====================================================================================
    proxy_used = proxy_cfg is not None
    logger.info("proxy_used: %s", proxy_used)

    send_result(ctx, user_phone, webhook_url, headless_error, proxy_used)
