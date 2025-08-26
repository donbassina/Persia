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
from contextlib import suppress  # (п.2) нужно для мягкой отмены тасков в детекторе

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
proxy_cfg: dict | None = None  # unused global; per-run proxy is stored in ctx


async def gc_move(x: float, y: float):
    if GCURSOR is None:
        raise RuntimeError("GCURSOR not initialized")
    await GCURSOR.move_to({"x": x, "y": y})


async def gc_click(target):
    """Клик строго через Ghost-cursor: скроллим в видимую область, берём bbox, подводим, кликаем."""
    if GCURSOR is None:
        raise RuntimeError("GCURSOR not initialized")
    page = GCURSOR.page
    el = await page.query_selector(target) if isinstance(target, str) else target
    if not el:
        logger.warning("gc_click: element not found for %r", target)
        return

    # всегда стараемся сделать элемент видимым
    with suppress(Exception):
        await el.scroll_into_view_if_needed()
        await asyncio.sleep(_rnd.uniform(0.01, 0.03))

    box = await el.bounding_box()
    if not box:
        logger.warning("gc_click: no bbox for %r", target)
        return

    x = box["x"] + box["width"] / 2
    y = box["y"] + box["height"] / 2
    await gc_move(x, y)
    if hasattr(GCURSOR, "click_absolute"):
        await GCURSOR.click_absolute(x, y)
    else:
        # остаётся Ghost-cursor, но по уже подведённой позиции
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
        if _rnd.random() < 0.12:
            await asyncio.sleep(_rnd.uniform(0.18, 0.45))


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


def normalize_phone(num: str) -> str:
    digits = "".join(ch for ch in str(num) if ch.isdigit())
    return digits[-10:] if len(digits) >= 10 else digits




# --- POSTBACK extraction (редактируй только эти 2 строки) ---
POSTBACK_START = "utm_term="
POSTBACK_END   = "&utm"        # None → до конца строки
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
    # Если это известный код — вернём русский эквивалент; иначе — как есть
    return ERROR_RU.get(err, err)


def _errors_to_ru(err_list: list[str]) -> str:
    # Если это исключительно названия обязательных полей — свернём в одну фразу
    if err_list and all(e in _REQUIRED_FIELD_NAMES for e in err_list):
        return "Не заполнены поля: " + ", ".join(err_list)
    # Иначе переведём каждый код/строку по словарю и склеим
    return ", ".join(_to_ru(e) for e in err_list)











def _append_run_result_from_log(log_path: str, out_txt_path: str) -> None:
    """
    Читает «хвост» лог-файла и ищет последний RESULT: SUCCESS/ERROR.
    В зависимости от результата дописывает одну строку в out_txt_path.
    """
    try:
        # читаем хвост файла (≈8 КБ)
        tail = ""
        with open(log_path, "rb") as f:
            try:
                f.seek(0, os.SEEK_END)
                size = f.tell()
                f.seek(max(0, size - 8192))
                tail = f.read().decode("utf-8", errors="ignore")
            except Exception:
                f.seek(0)
                tail = f.read().decode("utf-8", errors="ignore")

        i_succ = tail.rfind("RESULT: SUCCESS")
        i_err  = tail.rfind("RESULT: ERROR")

        if i_succ == i_err == -1:
            # если по какой-то причине сигнатура не найдена — считаем ошибкой
            state = "ERROR"
        else:
            state = "SUCCESS" if i_succ > i_err else "ERROR"

        line = "Самокат: Лидок в коробочке" if state == "SUCCESS" else "Самокат: Лидок хуёк"

        os.makedirs(os.path.dirname(out_txt_path), exist_ok=True)
        with open(out_txt_path, "a", encoding="utf-8") as out:
            out.write(line + "\n")

        logger.info("Run outcome appended to %s (%s)", out_txt_path, state)
    except Exception as e:
        logger.warning("append_run_result_from_log failed: %s", e)



















def send_result(
    ctx: RunContext,
    phone: str,
    webhook_url: str,
    headless_error: bool,
    proxy_used: bool,
) -> None:
    """Send final result via webhook and print JSON.

    Требования п.2 «Нового плана»:
    — Если получен POSTBACK (то есть был редирект или модалка, и мы извлекли postback), ЭТО УСПЕХ.
    — На успехе НЕЛЬЗЯ добавлять никакой error (в т.ч. за некорректный headless).
    — Итог не зависит от наличия строк "[ERROR]" в логах (это уберём отдельным патчем ниже).
    """
    result: dict[str, str] = {"phone": phone}

    success = bool(ctx.postback)

    if success:
        # Есть POSTBACK → всегда успех, любые ошибки игнорируем
        result["POSTBACK"] = ctx.postback
    else:
        # Нет успеха → формируем причину
        if ctx.errors:
            result["error"] = _errors_to_ru(ctx.errors)
        elif headless_error:
            # headless_error НЕ ломает успех, но если успеха нет и других ошибок нет — отдадим его
            result["error"] = _to_ru("bad headless value")
        else:
            result["error"] = _to_ru("POSTBACK missing")

    # Скриншот — только при ошибке
    if "error" in result and ctx.screenshot_path:
        result["screenshot"] = ctx.screenshot_path

    # Логи итога
    result_state = "SUCCESS" if "error" not in result else "ERROR"
    logger.info("RESULT: %s", result_state)
    if "error" in result:
        logger.info("ИТОГ: %s", result["error"])
    else:
        logger.info("POSTBACK получен")

    # Отправка вебхука (как и было), затем печать JSON в stdout
    if not ctx.browser_closed_manually:
        send_webhook(result, webhook_url, ctx)
    print(json.dumps(result, ensure_ascii=False))

    # После вебхука: зафиксировать результат запуска в txt на основе логов
    try:
        _append_run_result_from_log(ctx.log_file, RUN_RESULTS_TXT)
    except Exception as e:
        logger.warning("append runs file failed: %s", e)


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
    file_handler.setFormatter(logging.Formatter(
        "[%(asctime)s] %(levelname)s %(name)s – %(message)s",
        datefmt="%H:%M:%S",
    ))

    # --- de-dup handlers & stop propagation ---
    logger.propagate = False
    abs_log = os.path.abspath(ctx.log_file)

    # убрать уже подвешенные FileHandler к тому же файлу
    for h in list(logger.handlers):
        if isinstance(h, logging.FileHandler) and getattr(h, "baseFilename", None) == abs_log:
            logger.removeHandler(h)
            try:
                h.close()
            except Exception:
                pass

    # повесить РОВНО один FileHandler
    if not any(isinstance(h, logging.FileHandler) and getattr(h, "baseFilename", None) == abs_log
               for h in logger.handlers):
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

        # если уже есть консольный хэндлер — заменим форматтер; иначе добавим новый
        has_stream = False
        for h in logger.handlers:
            if isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler):
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
            _proxy_cfg = {
                "server": f"{parsed['scheme']}://{parsed['host']}:{parsed['port']}"
            }
            if parsed.get("user"):
                _proxy_cfg["username"] = parsed["user"]
            if parsed.get("password"):
                _proxy_cfg["password"] = parsed["password"]
            # store proxy configuration in context to avoid global state
            ctx.proxy_cfg = _proxy_cfg
        except ProxyError:
            logger.error("bad_proxy_format")
            fatal = {"phone": user_phone, "error": "bad_proxy_format"}
            send_webhook(fatal, webhook_url, ctx)
            print(json.dumps(fatal, ensure_ascii=False))
            sys.exit(1)
        if not probe_proxy(parsed):
            logger.error("bad_proxy_unreachable")
            fatal = {"phone": user_phone, "error": "bad_proxy_unreachable"}
            # Отправляем webhook (если не закрыт браузер вручную; на этом этапе браузер ещё не запускался)
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

# (п.10) безопасная проверка кодировок stdout/stderr
_enc = sys.stdout.encoding
if not _enc or _enc.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")
_enc = sys.stderr.encoding
if not _enc or _enc.lower() != "utf-8":
    sys.stderr.reconfigure(encoding="utf-8")


# === P-6. Единый фильтр для блокировки «лишних» запросов ===
async def should_abort(route, ctx: RunContext):
    """
    Минимальный безопасный фильтр:
      • блокируем ресурсы с resource_type in {"image","media"}
      • блокируем URL по CFG["BLOCK_PATTERNS"]
    Никаких проверок «:status» и «content-length» на стадии запроса.
    """
    req = route.request
    r_type = req.resource_type
    url = req.url

    must_abort = (r_type in ("image", "media")) or any(
        p in url for p in CFG["BLOCK_PATTERNS"]
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
RUN_RESULTS_TXT = os.path.join(WORK_DIR, "Logs", "run_results.txt")


async def get_form_position(page, selector="div.form-wrapper"):
    el = await page.query_selector(selector)
    if not el:
        return None
    box = await el.bounding_box()
    if not box:
        return None
    return {"top": box["y"], "center": box["y"] + box["height"] / 2}


# ==== Тайм-зона по городу (мягкое сопоставление + дефолт Москва) ====

def _norm_city(name: str) -> str:
    """Нормализует строку города: нижний регистр, замена ё→е, убираем пунктуацию/служебные слова."""
    s = (name or "").strip().lower()
    repl = {
        "ё": "е", "—": " ", "–": " ", "-": " ", ".": " ", ",": " ", "’": " ", "'": " ",
        "«": " ", "»": " ", "(": " ", ")": " ",
    }
    s = s.translate(str.maketrans(repl))
    # убираем частые приставки/слова
    stop = ("город", "г", "республика", "респ", "край", "область", "обл")
    parts = [p for p in s.split() if p not in stop]
    return " ".join(parts)


# Карта «город → IANA таймзона»
CITY_TO_TZ: dict[str, str] = {
    # UTC+2
    "калининград": "Europe/Kaliningrad", "советск": "Europe/Kaliningrad",

    # UTC+3 (MSK)
    "москва": "Europe/Moscow", "санкт петербург": "Europe/Moscow", "спб": "Europe/Moscow", "питер": "Europe/Moscow",
    "калуга": "Europe/Moscow", "тверь": "Europe/Moscow", "тула": "Europe/Moscow", "орел": "Europe/Moscow",
    "брянск": "Europe/Moscow", "смоленск": "Europe/Moscow", "ярославль": "Europe/Moscow", "кострома": "Europe/Moscow",
    "иваново": "Europe/Moscow", "владимир": "Europe/Moscow", "рязань": "Europe/Moscow", "липецк": "Europe/Moscow",
    "курск": "Europe/Moscow", "тамбов": "Europe/Moscow", "вологда": "Europe/Moscow", "архангельск": "Europe/Moscow",
    "мурманск": "Europe/Moscow", "петрозаводск": "Europe/Moscow", "псков": "Europe/Moscow",
    "великий новгород": "Europe/Moscow", "нижний новгород": "Europe/Moscow", "казань": "Europe/Moscow",
    "ростов на дону": "Europe/Moscow", "краснодар": "Europe/Moscow", "сочи": "Europe/Moscow",
    "воронеж": "Europe/Moscow", "севастополь": "Europe/Moscow", "симферополь": "Europe/Moscow",
    "белгород": "Europe/Moscow",
    # спец-регионы MSK
    "волгоград": "Europe/Volgograd", "волжский": "Europe/Volgograd", "камышин": "Europe/Volgograd",

    # UTC+4
    "самара": "Europe/Samara", "тольятти": "Europe/Samara", "сызрань": "Europe/Samara", "syzran": "Europe/Samara",
    "ижевск": "Europe/Samara",
    "ульяновск": "Europe/Ulyanovsk", "димитровград": "Europe/Ulyanovsk",
    "астрахань": "Europe/Astrakhan",
    "саратов": "Europe/Saratov", "энгельс": "Europe/Saratov", "балаково": "Europe/Saratov",

    # UTC+5
    "екатеринбург": "Asia/Yekaterinburg", "челябинск": "Asia/Yekaterinburg", "пермь": "Asia/Yekaterinburg",
    "уфа": "Asia/Yekaterinburg", "оренбург": "Asia/Yekaterinburg", "тюмень": "Asia/Yekaterinburg",
    "курган": "Asia/Yekaterinburg", "ханты мансийск": "Asia/Yekaterinburg", "сургут": "Asia/Yekaterinburg",
    "нижневартовск": "Asia/Yekaterinburg", "салехард": "Asia/Yekaterinburg",

    # UTC+6
    "омск": "Asia/Omsk",

    # UTC+7
    "новосибирск": "Asia/Novosibirsk", "бердск": "Asia/Novosibirsk", "искитим": "Asia/Novosibirsk", "обь": "Asia/Novosibirsk",
    "барнаул": "Asia/Barnaul", "бийск": "Asia/Barnaul", "рубцовск": "Asia/Barnaul", "новоалтайск": "Asia/Barnaul",
    "томск": "Asia/Tomsk", "северск": "Asia/Tomsk",
    "кемерово": "Asia/Novokuznetsk", "новокузнецк": "Asia/Novokuznetsk", "прокопьевск": "Asia/Novokuznetsk",
    "киселевск": "Asia/Novokuznetsk",
    "красноярск": "Asia/Krasnoyarsk", "абакан": "Asia/Krasnoyarsk", "норильск": "Asia/Krasnoyarsk",
    "кызыл": "Asia/Krasnoyarsk", "канск": "Asia/Krasnoyarsk", "ачинск": "Asia/Krasnoyarsk",

    # UTC+8
    "иркутск": "Asia/Irkutsk", "братск": "Asia/Irkutsk", "ангарск": "Asia/Irkutsk",
    "улан уде": "Asia/Irkutsk", "улан удэ": "Asia/Irkutsk",

    # UTC+9
    "якутск": "Asia/Yakutsk", "нерюнгри": "Asia/Yakutsk", "благовещенск": "Asia/Yakutsk",
    "свободный": "Asia/Yakutsk", "тында": "Asia/Yakutsk", "зея": "Asia/Yakutsk",
    "чита": "Asia/Chita",

    # UTC+10
    "владивосток": "Asia/Vladivostok", "хабаровск": "Asia/Vladivostok",
    "комсомольск на амуре": "Asia/Vladivostok", "биробиджан": "Asia/Vladivostok",
    "находка": "Asia/Vladivostok", "уссурийск": "Asia/Vladivostok", "арсеньев": "Asia/Vladivostok",

    # UTC+11
    "южно сахалинск": "Asia/Sakhalin", "корсаков": "Asia/Sakhalin",
    "холмск": "Asia/Sakhalin", "оха": "Asia/Sakhalin",
    "магадан": "Asia/Magadan", "среднеколымск": "Asia/Srednekolymsk",

    # UTC+12
    "петропавловск камчатский": "Asia/Kamchatka", "анадырь": "Asia/Anadyr",
}


DEFAULT_TZ = "Europe/Moscow"

def guess_timezone(city: str) -> str:
    key = _norm_city(city)
    if not key:
        return DEFAULT_TZ

    # 1) точное совпадение
    if key in CITY_TO_TZ:
        return CITY_TO_TZ[key]

    # 2) сопоставление без пробелов (ростов-на-дону vs ростов на дону)
    key_nospace = key.replace(" ", "")
    for name, tzid in CITY_TO_TZ.items():
        if name.replace(" ", "") == key_nospace:
            return tzid

    # 3) попытка найти по вхождению токенов (например, “г. Великий Новгород”)
    tokens = key.split()
    for name, tzid in CITY_TO_TZ.items():
        nm_tokens = name.split()
        # если все токены короткой формы присутствуют в длинной — считаем совпадением
        if all(t in tokens for t in nm_tokens):
            return tzid

    # 4) дефолт
    return DEFAULT_TZ

user_city = (user_city or "").strip()
tz = guess_timezone(user_city)
logger.info("Timezone (by user_city or default): %s → %s", user_city, tz)



# === fingerprint values (согласовано под Windows/Chrome) ===
FP_PLATFORM = "Win32"
FP_DEVICE_MEMORY = 8
FP_HARDWARE_CONCURRENCY = 8
FP_LANGUAGES = ["ru-RU", "ru"]
FP_ACCEPT_LANGUAGE = "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7"
# Ближе к реальному Chrome/Windows (ANGLE)
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
    """Возвращает задержку перед следующим символом, s."""
    base = _rnd.lognormvariate(
        CFG["HUMAN_DELAY_μ"], CFG["HUMAN_DELAY_σ"]
    )  # медиана ≈0.14
    return max(0.09, min(base, 0.22))


async def human_type(page, selector: str, text: str, ctx: RunContext):
    """Печатает текст «по-человечески».
    Блокирует прокрутку ТОЛЬКО во время набора поля имени, не трогая видимость скроллбара:
    — активируется после первого реально напечатанного символа,
    — отключается сразу по завершении ввода имени,
    — overflow/паддинги/скроллбар НЕ меняются.
    """
    await page.focus(selector)
    n = len(text)
    coef = 0.8 if n <= 3 else 1.15 if n > 20 else 1.0
    total = 0.0

    name_sel = (getattr(ctx, "selectors", None) or {}).get("form", {}).get("name")
    is_name = (selector == name_sel)
    guard_installed = False

    INSTALL_JS = r"""
    (() => {
      if (window.__SG && window.__SG.active) return true;
      const d = document;
      const frozen = {
        x: window.pageXOffset || d.documentElement.scrollLeft || 0,
        y: window.pageYOffset || d.documentElement.scrollTop || 0
      };
      const prevent = e => { e.preventDefault(); e.stopImmediatePropagation(); };
      const keyPrevent = e => {
        const ae = d.activeElement;
        const isEd = ae && (ae.tagName==='INPUT'||ae.tagName==='TEXTAREA'||ae.isContentEditable);
        const keys = ['ArrowUp','ArrowDown','PageUp','PageDown','Home','End',' '];
        if (!isEd && keys.includes(e.key)) { e.preventDefault(); e.stopImmediatePropagation(); }
      };
      const onScroll = () => { window.scrollTo(frozen.x, frozen.y); };

      const old = {
        scrollTo: window.scrollTo,
        scrollBy: window.scrollBy,
        elSIV: Element.prototype.scrollIntoView,
      };

      // Запрещаем программный скролл (но не трогаем overflow/полосы прокрутки)
      window.scrollTo = function(){};
      window.scrollBy = function(){};
      Element.prototype.scrollIntoView = function(){};

      // Глушим пользовательские источники прокрутки
      window.addEventListener('wheel', prevent, {passive:false, capture:true});
      window.addEventListener('touchmove', prevent, {passive:false, capture:true});
      window.addEventListener('keydown', keyPrevent, {passive:false, capture:true});
      window.addEventListener('scroll', onScroll, {passive:false, capture:true});

      window.__SG = {active:true, prevent, keyPrevent, onScroll, old};
      return true;
    })();
    """

    RESTORE_JS = r"""
    (() => {
      const g = window.__SG;
      if (!g || !g.active) return false;

      window.removeEventListener('wheel', g.prevent, {capture:true});
      window.removeEventListener('touchmove', g.prevent, {capture:true});
      window.removeEventListener('keydown', g.keyPrevent, {capture:true});
      window.removeEventListener('scroll', g.onScroll, {capture:true});

      if (g.old){
        window.scrollTo = g.old.scrollTo;
        window.scrollBy = g.old.scrollBy;
        Element.prototype.scrollIntoView = g.old.elSIV;
      }
      window.__SG = {active:false};
      return true;
    })();
    """

    for char in text:
        delay = _human_delay() * coef

        # возможная «опечатка»
        if not char.isdigit() and _rnd.random() < CFG["TYPO_PROB"]:
            await page.keyboard.type(char, delay=0)
            if is_name and not guard_installed:
                try:
                    await page.evaluate(INSTALL_JS)
                    logger.info("[SCROLL-GUARD] installed")
                    guard_installed = True
                except Exception:
                    pass
            await asyncio.sleep(delay)
            await page.keyboard.press("Backspace")
            total += delay

        # основной ввод символа
        await page.keyboard.type(char, delay=0)
        if is_name and not guard_installed:
            try:
                await page.evaluate(INSTALL_JS)
                logger.info("[SCROLL-GUARD] installed")
                guard_installed = True
            except Exception:
                pass

        await asyncio.sleep(delay)
        total += delay

    # снимаем блок только по окончании печати имени
    if is_name and guard_installed:
        try:
            await page.evaluate(RESTORE_JS)
            logger.info("[SCROLL-GUARD] restored")
        except Exception:
            pass

    logger.info(f'[DEBUG] typing "{text}" len={n} total_time={total:.2f}')



# -------------------- ФИО --------------------
async def fill_full_name(page, name: str, ctx: RunContext, retries: int = 3) -> bool:
    for attempt in range(retries):
        try:
            input_box = page.locator((getattr(ctx, "selectors", None) or selectors or {})["form"]["name"])

            # ► СКРОЛЛ, если нужно, чтобы поле оказалось в видимой зоне
            if attempt == 0:
                await _scroll_if_needed(
                    input_box, dropdown_room=150, step_range=(170, 190)
                )  # room≈высота клавиатуры


            await human_move_cursor(page, input_box, ctx)
            await ghost_click(input_box)
            await page.wait_for_timeout(_rnd.randint(10, 12))

            await input_box.fill("")
            await human_type(page, (getattr(ctx, "selectors", None) or selectors or {})["form"]["name"], name, ctx)

            if (await input_box.input_value()).strip() == name.strip():
                return True
            await page.wait_for_timeout(_rnd.randint(10, 12))
        except Exception as e:
            logger.warning("fill_full_name attempt %s failed: %s", attempt + 1, e)

    logger.error("Не удалось заполнить поле ФИО")
    return False


# ========================= ГОРОД ========================================================================


# -------------------- ГОРОД --------------------
async def fill_city(page, city: str, ctx: RunContext, retries: int = 3) -> bool:
    """
    Печатает `city` по-человечески и выбирает его в выпадашке.
    Алгоритм:
      1. при необходимости — микро-скролл, чтобы поле было видно;
      2. курсор → клик по инпуту, очищаем;
      3. печатаем по одной букве с human-delay, после каждой буквы:
           • триггерим 'input' и 'keyup';
           • ждём, пока список вариантов обновится (≤ 0.9 с);
           • если набрано ≥ 75 % слова и вариантов ≤ 4 —
             выбираем точное совпадение.
    """
    item_sel = (getattr(ctx, "selectors", None) or selectors or {})["form"]["city_item"]
    list_sel = (getattr(ctx, "selectors", None) or selectors or {})["form"].get("city_list", item_sel)
    list_sel_visible = f"{list_sel}:visible"  # считаем только видимые

    for attempt in range(retries):
        try:
            inp = page.locator((getattr(ctx, "selectors", None) or selectors or {})["form"]["city"])

            # 1) скроллим, если под полем < 260 px — только на первом заходе
            if attempt == 0:
                await _scroll_if_needed(inp, dropdown_room=260, step_range=(110, 120))


            # 2) курсор, клик, очистка
            await human_move_cursor(page, inp, ctx)
            await ghost_click(inp)
            await inp.fill("")

            target_len = max(2, int(len(city) * 0.75))  # ≥ 75 %
            typed = ""

            for ch in city:
                # 3-а) печать одной буквы с human-delay
                await human_type_city_autocomplete(
                    page, (getattr(ctx, "selectors", None) or selectors or {})["form"]["city"], ch, ctx
                )
                typed += ch

                # 3-б) пинаем автокомплит
                await page.evaluate(
                    """sel => {
                        const el = document.querySelector(sel);
                        if (!el) return;
                        ['input', 'keyup'].forEach(t =>
                            el.dispatchEvent(new Event(t, { bubbles: true }))
                        );
                    }""",
                    (getattr(ctx, "selectors", None) or selectors or {})["form"]["city"],
                )

                # 3-в) короткая «человечная» пауза
                await asyncio.sleep(_rnd.uniform(0.01, 0.04))

                # 3-г) ждём изменения списка (≤ 15 × 60 мс)
                lst = page.locator(list_sel_visible)
                prev_cnt = -1
                for _ in range(3):
                    cnt = await lst.count()
                    if cnt > 0 and cnt != prev_cnt:
                        break
                    prev_cnt = cnt
                    await asyncio.sleep(0.02)
                else:
                    raise ValueError("dropdown no change")

                # 3-д) если вариантов мало — кликаем нужный
                if len(typed) >= target_len and cnt <= 4:
                    options = [
                        (await lst.nth(i).inner_text()).strip() for i in range(cnt)
                    ]
                    if city in options:
                        idx = options.index(city)
                        item = lst.nth(idx)
                        await human_move_cursor(page, item, ctx)
                        await ghost_click(item)
                        await asyncio.sleep(_rnd.uniform(0.01, 0.03))
                        if (await inp.input_value()).strip() == city.strip():
                            return True
                        raise ValueError("value mismatch after click")

            # если весь цикл прошёл без выбора — ошибка
            raise ValueError(f"{city} not found")
        except Exception as e:
            logger.warning("fill_city attempt %s failed: %s", attempt + 1, e)

    logger.error("Не удалось выбрать город")
    raise ValueError("fill_city failed")


# ==========================================================================================================


async def fill_phone(page, phone: str, ctx: RunContext, retries: int = 3) -> bool:
    """Заполняет поле телефона «по-человечески» с учётом маски (+7 (999) …).
    Сравниваем только последние 10 цифр, чтобы игнорировать скобки/пробелы/+7."""

    # функция-помощник: оставляем только 10 конечных цифр
    def _norm(num: str) -> str:
        return "".join(ch for ch in num if ch.isdigit())[-10:]

    target = _norm(phone)

    for attempt in range(retries):
        try:
            input_box = page.locator((getattr(ctx, "selectors", None) or selectors or {})["form"]["phone"])

            # курсор → клик по полю
            await human_move_cursor(page, input_box, ctx)
            await ghost_click(input_box)
            await asyncio.sleep(0.005)

            # очищаем и печатаем номер
            await input_box.fill("")
            await human_type(page, (getattr(ctx, "selectors", None) or selectors or {})["form"]["phone"], phone, ctx)

            # ждём, пока маска применится
            await page.wait_for_timeout(_rnd.randint(10, 12))

            # проверяем, что введён тот же набор цифр
            typed = await input_box.input_value()
            if _norm(typed) == target:
                logger.info("[INFO] Телефон введён корректно: %s → %s", phone, typed)
                return True

            # если не совпало — лёгкая пауза и повтор
            logger.warning(
                "fill_phone mismatch (attempt %s): typed=%s exp=%s",
                attempt + 1,
                typed,
                phone,
            )
            await page.wait_for_timeout(_rnd.randint(10, 12))

        except Exception as e:
            logger.warning("fill_phone attempt %s failed: %s", attempt + 1, e)

    logger.error("Не удалось заполнить поле Телефон")
    return False


# ==========================================================================================================


# ─── helpers ──────────────────────────────────────────────────────────
async def _tiny_scroll_once(px: int) -> None:
    """
    «Человечный» короткий скролл на ±px px, как в human_scroll:
    логнормальные шаги, инерционный «докат», микро-джиттер и короткие паузы.
    """
    if GCURSOR is None:
        raise RuntimeError("GCURSOR not initialized")

    direction = 1 if px > 0 else -1
    remain = abs(px)

    # ограничим максимальный «крупный» шаг, чтобы рядом с инпутами не уезжать
    MAX_STEP = 120  # можно 90–140 по вкусу

    while remain > 0:
        # базовый «человечный» шаг
        step = min(int(_rnd.lognormvariate(3.6, 0.35)), remain, MAX_STEP)
        await GCURSOR.wheel(0, direction * step)

        # инерционный докат (экспоненциально убывающие подпульсы)
        v = step
        while v > 4:
            v = int(v * 0.72)
            await GCURSOR.wheel(0, direction * v)
            await asyncio.sleep(_rnd.uniform(0.012, 0.028))

        remain -= step

        # микро-джиттер и короткая пауза, но без длинных «задумчивостей»
        if _rnd.random() < 0.12:
            await GCURSOR.wheel(0, -direction * _rnd.randint(0, 6))
        await asyncio.sleep(_rnd.uniform(0.02, 0.05))



# ──────────────────────────────────────────────────────────────────────


# ─── helpers ──────────────────────────────────────────────────────────
async def _scroll_if_needed(
    locator,
    *,
    dropdown_room: int = 220,
    step_range: tuple[int, int] = (170, 190),  # ← новое
) -> None:
    """
    Проверяет, хватает ли места под элементом для выпадашки/клика.
    Если нет – делает ОДИН «человечный» скролл вниз.

    locator        – Playwright-локатор (input, select и т.п.)
    dropdown_room  – минимальное свободное пространство под элементом, px
    step_range     – диапазон шага колёсиком, px
    """
    page = locator.page
    box = await locator.bounding_box()
    if not box:  # элемент не найден
        return

    vh = await page.evaluate("window.innerHeight")
    space_below = vh - (box["y"] + box["height"])

    if space_below < dropdown_room:  # нужен микро-скролл
        step = _rnd.randint(*step_range)  # ← используем новый диапазон
        await _tiny_scroll_once(step)
        await asyncio.sleep(_rnd.uniform(0.18, 0.33))


# ──────────────────────────────────────────────────────────────────────


# -------------------- ПОЛ --------------------
async def fill_gender(page, gender: str, ctx: RunContext, retries: int = 3) -> bool:
    input_sel = (getattr(ctx, "selectors", None) or selectors or {})["form"]["gender"]
    item_sel = (getattr(ctx, "selectors", None) or selectors or {})["form"]["gender_item"]
    input_box = page.locator(input_sel)

    for attempt in range(retries):
        try:
            # ► тот же универсальный скролл — только на первом заходе
            if attempt == 0:
                await _scroll_if_needed(input_box, dropdown_room=190, step_range=(90, 95))


            # открыть список
            await human_move_cursor(page, input_box, ctx)
            await ghost_click(input_box)

            # выбрать пункт
            option = page.locator(item_sel).get_by_text(gender, exact=True)
            await option.wait_for(state="visible", timeout=600)
            await human_move_cursor(page, option, ctx)
            await ghost_click(option)

            # (п.9) проверка через текст выбранного пункта возле инпута
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


# ==========================================================================================================


async def fill_age(page, age, ctx: RunContext, retries=3):
    for attempt in range(retries):
        try:
            input_box = page.locator((getattr(ctx, "selectors", None) or selectors or {})["form"]["age"])
            await human_move_cursor(page, input_box, ctx)
            await ghost_click(input_box)
            await asyncio.sleep(0.005)
            await input_box.fill("")
            await human_type(page, (getattr(ctx, "selectors", None) or selectors or {})["form"]["age"], age, ctx)
            value = await input_box.input_value()
            if value.strip() == age.strip():
                return True
            await page.wait_for_timeout(_rnd.randint(10, 12))
        except Exception as e:
            logger.warning("fill_age attempt %s failed: %s", attempt + 1, e)
    logger.error("Не удалось заполнить поле Возраст")
    return False











async def fill_courier_type(page, courier_type, ctx: RunContext, retries: int = 3) -> bool:
    input_sel = (getattr(ctx, "selectors", None) or selectors or {})["form"]["courier"]
    item_sel  = (getattr(ctx, "selectors", None) or selectors or {})["form"]["courier_item"]
    input_box = page.locator(input_sel)

    for attempt in range(retries):
        try:
            # Лёгкий авто-скролл только на первой попытке
            if attempt == 0:
                await _scroll_if_needed(input_box, dropdown_room=220, step_range=(110, 120))

            # Открыть выпадашку
            await human_move_cursor(page, input_box, ctx)
            await ghost_click(input_box)

            # Дождаться появления списка опций (быстро из-за default_timeout=2500)
            options = page.locator(f"{item_sel}:visible")
            await options.first.wait_for(state="visible", timeout=1200)

            # Выбрать нужный пункт (без дополнительного движения курсором)
            option = options.filter(has_text=str(courier_type)).first
            await ghost_click(option)

            # Проверить, что реально выбралось (как в fill_gender)
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
            if val.lower().startswith(str(courier_type).lower()):
                return True

            # Если не совпало — форсим новую попытку без лишних пауз
            raise ValueError(f"courier value mismatch ({val!r})")

        except Exception as e:
            logger.warning("fill_courier_type attempt %s failed: %s", attempt + 1, e)

    logger.error("Не удалось выбрать тип курьера")
    return False

















async def fill_policy_checkbox(page, ctx: RunContext, retries: int = 1) -> bool:
    sel = (getattr(ctx, "selectors", None) or selectors or {})["form"]["policy"]
    container = page.locator(sel)
    cb = container.locator('input[type="checkbox"]').first

    # если настоящего input нет — попробуем label[for] → input#id
    if await cb.count() == 0:
        try:
            if await container.evaluate("el => el.tagName==='INPUT' && el.type==='checkbox'"):
                cb = container
            else:
                for_id = await container.get_attribute("for")
                if for_id:
                    cb = page.locator(f"#{for_id}")
        except Exception:
            pass

    # целимся ТОЛЬКО в один таргет (чтобы не «переклёпывать»)
    click_target = container if await container.count() else cb

    async def _is_checked() -> bool:
        # 1) честный input
        try:
            if await cb.count():
                return await cb.is_checked()
        except Exception:
            pass
        # 2) aria/классы/вложенный input — для кастомных чекбоксов
        return await page.evaluate(
            """(sel) => {
                const el = document.querySelector(sel);
                if (!el) return false;
                const aria = el.getAttribute('aria-checked');
                if (aria != null) return aria === 'true';
                const cls = el.className || '';
                if (/\b(checked|is-checked|active|on)\b/i.test(cls)) return true;
                const inp = el.querySelector('input[type="checkbox"]');
                return !!(inp && inp.checked);
            }""",
            sel
        )

    async def _wait_stable_checked(duration=0.12, poll=0.03, max_wait=0.8) -> bool:
        need = int(duration / poll)
        streak = 0
        t0 = asyncio.get_event_loop().time()
        while True:
            if await _is_checked():
                streak += 1
                if streak >= need:
                    return True
            else:
                streak = 0
            if asyncio.get_event_loop().time() - t0 > max_wait:
                # не добились стабильности — вернём текущее состояние, чтобы не зависать
                return await _is_checked()
            await asyncio.sleep(poll)


    # уже отмечен — ничего не трогаем
    with suppress(Exception):
        if await _is_checked():
            return True

    # 0) если есть реальный input — пробуем Playwright.check (не мигает)
    try:
        if await cb.count():
            await cb.check(force=True)
            await asyncio.sleep(0.05)
            ok = await _wait_stable_checked()
            if ok:
                # уберём курсор с элемента, чтобы никакие hover-обработчики не дергали классы
                with suppress(Exception):
                    await GCURSOR.page.mouse.move(10, 10)
                return True
            # если не ок — пойдём по следующим стратегиям (клик/форс), без return

    except Exception:
        pass

    # 1) один человеческий клик по единственному таргету
    try:
        with suppress(Exception):
            await _scroll_if_needed(click_target, dropdown_room=100, step_range=(90, 110))
        await human_move_cursor(page, click_target, ctx)
        await ghost_click(click_target)
        await asyncio.sleep(_rnd.uniform(0.02, 0.05))
        if await _wait_stable_checked():
            with suppress(Exception):
                await GCURSOR.page.mouse.move(10, 10)
            return True
    except Exception as e:
        logger.warning("fill_policy_checkbox: click path failed: %s", e)

    # 2) форс-установка (без кликов) + события
    try:
        await page.evaluate(
            """(sel) => {
                const root = document.querySelector(sel);
                if (!root) return;
                const inp = root.matches('input[type="checkbox"]')
                    ? root
                    : (root.querySelector('input[type="checkbox"]') || null);

                if (inp) {
                    inp.checked = true;
                    inp.setAttribute('aria-checked','true');
                    inp.dispatchEvent(new Event('input', {bubbles:true}));
                    inp.dispatchEvent(new Event('change',{bubbles:true}));
                } else {
                    root.classList.add('checked','is-checked','active','on');
                    root.setAttribute('aria-checked','true');
                    root.dispatchEvent(new Event('input', {bubbles:true}));
                    root.dispatchEvent(new Event('change',{bubbles:true}));
                }
            }""",
            sel
        )
        await _wait_stable_checked()
        with suppress(Exception):
            await GCURSOR.page.mouse.move(10, 10)
        return True
    except Exception as e:
        logger.warning("fill_policy_checkbox: force path failed: %s", e)

    logger.error("Не удалось поставить галочку политики")
    await asyncio.sleep(1)
    return False
    
















async def submit_form(page, ctx: RunContext):
    """Click submit button or submit form if the button is hidden."""
    btn = page.locator((getattr(ctx, "selectors", None) or selectors or {})["form"]["submit"])
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













async def click_no_move_if_close(page, el, ctx: RunContext, threshold_px: int = 7):
    """Клик через Ghost-cursor без прокладки траектории,
    если курсор уже близко к центру кнопки."""
    if GCURSOR is None:
        raise RuntimeError("GCURSOR not initialized")

    box = await el.bounding_box()
    if not box:
        # fallback: обычный ghost_click (пусть сам разбирается)
        await ghost_click(el)
        return

    cx = box["x"] + box["width"] / 2
    cy = box["y"] + box["height"] / 2

    mx, my = getattr(ctx, "mouse_pos", (None, None))
    # Если положение мыши нам неизвестно — безопаснее не двигать её вовсе
    # и кликнуть по центру (через click_absolute, если он есть).
    close_enough = False
    if mx is None or my is None:
        close_enough = True
    else:
        dx = (mx - cx)
        dy = (my - cy)
        dist = (dx * dx + dy * dy) ** 0.5
        close_enough = dist <= threshold_px

    if close_enough:
        # Клик БЕЗ движения
        if hasattr(GCURSOR, "click_absolute"):
            await GCURSOR.click_absolute(cx, cy)
        else:
            await GCURSOR.click(None)  # клик в текущей позиции
        # логически считаем, что курсор на центре
        ctx.mouse_pos = (cx, cy)
    else:
        # Далеко — используем обычный путь с подводом
        await ghost_click(el)  # внутри будет move + click
        # после ghost_click курсор на центре; зафиксируем
        ctx.mouse_pos = (cx, cy)


















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
            await asyncio.sleep(_rnd.uniform(0.5, 1.1))
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
            t = _rnd.uniform(0.7, 1.6) 
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


async def scroll_to_form_like_reading(page, ctx: RunContext, timeout: float = 15.0):
    """
    Быстро прокручивает страницу так, чтобы центр формы оказался в центре экрана,
    с разбросом ±70 px (рандомно на каждый запуск).
    """
    sel = (
        (getattr(ctx, "selectors", None) or selectors or {}).get("form", {}).get("wrapper")
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

    # === ФИНАЛЬНЫЙ СКРОЛ К ФОРМЕ ================
    logger.info("[SCROLL] ► final-jump started")

    # положение формы и фикс-хедера
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
    prev_sign = 1 if remaining > 0 else -1  # знак «до/после»

    logger.info(
        f"[DEBUG-JUMP] start  form_center={form_view_center:.0f} "
        f"target_center={target_center:.0f}  diff={remaining:+.0f}"
    )

    # «колёсный» докат к форме
    while distance > 5:
        direction = 1 if remaining > 0 else -1  # пересчитываем каждый шаг

        # динамический минимум шага: чем ближе, тем меньше
        if distance < 200:
            min_step = 40
        elif distance < 600:
            min_step = 80
        else:
            min_step = 120

        pct = _rnd.uniform(0.12, 0.22)
        step = max(min_step, min(350, int(distance * pct)))
        step = min(step, distance)

        await human_scroll(direction * step)
        logger.info(f"[SCROLL] wheel {'down' if direction>0 else 'up'} {step}")
        await asyncio.sleep(_rnd.uniform(0.20, 0.45))

        # пересчёт после шага
        rect = await form.evaluate("el => el.getBoundingClientRect()")
        form_view_center = rect["top"] + rect["height"] / 2
        remaining = form_view_center - target_center
        distance = abs(remaining)
        curr_sign = 1 if remaining > 0 else -1

        # если знак поменялся — форму «перелетели», достаточно
        if curr_sign != prev_sign:
            break
        prev_sign = curr_sign

    # добиваем последние пиксели и лёгкий «джиттер»
    if abs(remaining) > 0:
        jitter = _rnd.randint(-10, 10)  # ±10 px для естественности
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
                    pause_t = _rnd.uniform(1.2, 4.2)
                    logger.info(f"[INFO] Пауза у блока {sel} {pause_t:.1f} сек")
                    await asyncio.sleep(pause_t)
                    break

        await asyncio.sleep(_rnd.uniform(3.8, 5.1))

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


# (п.6) устойчивый stealth-апплай с фолбэком на разные API
async def _apply_stealth(context, page=None):
    try:
        await stealth.Stealth().apply_stealth_async(context)  # 2.x API
        return
    except Exception as e:
        logger.info(f"[INFO] stealth via Stealth() failed: {e}")
    try:
        from playwright_stealth import stealth_async  # type: ignore
        try:
            # у некоторых версий параметр — context
            await stealth_async(context)
        except TypeError:
            # у других — page
            if page is not None:
                await stealth_async(page)
            else:
                raise
    except Exception as e:
        logger.warning(f"[WARN] playwright-stealth not applied: {e}")


# (п.5) совместимая установка скоростей курсора
def _set_cursor_speed(cur, vmin=1750, vmax=2200):
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


# (п.2) Усиленный детектор результата сабмита
async def _wait_submit_result(context, page, submit_btn, modal_selector: str, ctx: RunContext,
                              first_wait: float = 30.0, second_wait: float = 120.0):
    """
    После клика ждём СНАЧАЛА редирект в той же вкладке, затем «Спасибо» в этой же вкладке.
    Без обработки новых вкладок/попапов. Успех = появление модалки «Спасибо»
    или текста 'спасибо'/'thank you' на странице.
    Возвращает (success: bool, active_page: Page|None), где active_page всегда текущая page.
    """
    def ms(sec: float) -> int: return int(sec * 1000)

    async def wait_redirect_same_tab(p, old_url: str, timeout_s: float) -> bool:
        try:
            await p.wait_for_function("url => location.href !== url", arg=old_url, timeout=ms(timeout_s))
            return True
        except Exception:
            return False

    async def wait_thanks_same_tab(p, timeout_s: float) -> bool:
        short = min(timeout_s, 4.0)  # ждём модалку недолго
        if modal_selector:
            # моментальная проверка "есть и видно" без долгого ожидания
            try:
                exists_and_visible = await p.evaluate(
                    """sel => {
                        const el = document.querySelector(sel);
                        if (!el) return false;
                        const s = getComputedStyle(el);
                        const r = el.getBoundingClientRect();
                        return s.display !== 'none'
                            && s.visibility !== 'hidden'
                            && r.width > 0 && r.height > 0;
                    }""",
                    modal_selector,
                )
            except Exception:
                exists_and_visible = False
            if exists_and_visible:
                return True
            try:
                await p.wait_for_selector(modal_selector, state="visible", timeout=ms(short))
                return True
            except Exception:
                pass
        try:
            await p.wait_for_function(
                "() => { const t=(document.body&&document.body.innerText||'').toLowerCase();"
                "return t.includes('спасибо')||t.includes('thank you'); }",
                timeout=ms(short),
            )
            return True
        except Exception:
            return False


    # ——— Клик №1 ———
    # перед тем как кликать, удостоверимся, что страница ещё открыта
    if hasattr(page, "is_closed") and page.is_closed():
        logger.error("Page is closed before submit click")
        return False, page
    try:
        with suppress(Exception):
            await submit_btn.scroll_into_view_if_needed()
        with suppress(Exception):
            await human_move_cursor(page, submit_btn, ctx)
        await ghost_click(submit_btn)
        logger.info("[INFO] Первый клик по кнопке 'Оставить заявку'")
        with suppress(Exception):
            _box = await submit_btn.bounding_box()
            if _box:
                ctx.mouse_pos = (_box["x"] + _box["width"] / 2, _box["y"] + _box["height"] / 2)
    except PlaywrightError as e:
        logger.error("Клик по submit не удался: %s", e)
        return False, page

    try:
        old_url = page.url
    except PlaywrightError:
        old_url = ""

    # быстрый чекап: модалка уже появилась?
    if await wait_thanks_same_tab(page, 4.0):
        return True, page


    # 1) ждём редирект в той же вкладке
    redirected = await wait_redirect_same_tab(page, old_url, first_wait)
    # 2) после редиректа (или даже без него, на всякий случай) ждём «Спасибо»
    if redirected:
        # URL уже новый; дополнительное окно ожидания модалки
        if await wait_thanks_same_tab(page, first_wait):
            return True, page
    else:
        # редиректа нет, но вдруг сайт показал «Спасибо» без смены URL
        if await wait_thanks_same_tab(page, first_wait):
            return True, page

    # ——— Клик №2 (повтор) ———
    with suppress(Exception):
        await click_no_move_if_close(page, submit_btn, ctx, threshold_px=7)
        logger.info("[INFO] Повторный клик по кнопке 'Оставить заявку'")
    await asyncio.sleep(0)  # фикс-пауза перед повторным ожиданием

    try:
        old_url = page.url
    except PlaywrightError:
        old_url = ""

    redirected = await wait_redirect_same_tab(page, old_url, second_wait)
    if redirected:
        if await wait_thanks_same_tab(page, second_wait):
            return True, page
    else:
        if await wait_thanks_same_tab(page, second_wait):
            return True, page

    return False, page



async def run_browser(ctx: RunContext):
    async with async_playwright() as p:
        headless = (ctx.json_headless if ctx.json_headless is not None else False)

        browser = await p.chromium.launch(
            proxy=(ctx.proxy_cfg if getattr(ctx, "proxy_cfg", None) else None),
            headless=headless,
            channel="chrome",
            args=[
                "--incognito",
                # убрали --disable-gpu, чтобы не конфликтовать с WebGL-спуфингом
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

        # (п.6) apply playwright-stealth anti-bot measures с фолбэком
        await _apply_stealth(context)

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
        page.set_default_timeout(9900)
        global GCURSOR
        GCURSOR = create_cursor(page)
        # (п.5) совместимая установка скорости курсора
        _set_cursor_speed(GCURSOR, 4200, 5800)

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

            # (п.12) Лёгкая проверка на 503: берём небольшой срез текста
            try:
                status_head = await page.evaluate(
                    "(d=> (d.innerText||'').slice(0,2048)) (document.documentElement)"
                )
            except Exception:
                status_head = ""
            if (
                "503" in status_head
                or "Сервис временно недоступен" in status_head
                or "Service Unavailable" in status_head
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

            try:
                box = await page.locator("div.form-wrapper").bounding_box()
                vh = await page.evaluate("window.innerHeight")
                if box and box["y"] < vh * 1.2:
                    total_time = _rnd.uniform(0.6, 1.2)
            except Exception:
                pass

            logger.info(f"[INFO] Имитация “чтения” лендинга: {total_time:.1f} сек")

            await emulate_user_reading(page, total_time, ctx)
            await scroll_to_form_like_reading(page, ctx)
            # ====================================================================================
            # Этап 4. Клик мышью по каждому полю, заполнение всех полей, установка галочки
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
                await asyncio.sleep(_rnd.uniform(0.1, 0.2))

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

            ts = datetime.now().strftime("%Y%m%d-%H%M%S")
            filename = f"{ts}_{user_phone}.png"

            path = os.path.join(screenshot_dir, filename)

            await page.screenshot(path=path, full_page=False)
            ctx.screenshot_path = path
            logger.info(f"[INFO] Скриншот формы сохранён: {ctx.screenshot_path}")

            await asyncio.sleep(0.3)

            try:
                values = await page.evaluate(
                    """(sels) => {
                        const qs = (s) => (s ? document.querySelector(s) : null);

                        const getSelectText = (inputSel) => {
                            const inp = qs(inputSel);
                            if (!inp) return "";
                            if (typeof inp.value === "string" && inp.value.trim()) {
                                return inp.value.trim();
                            }
                            const wrap =
                                inp.closest('.form-select, .select-wrapper, .select, .custom-select, .react-select, [role="combobox"]') ||
                                inp.parentElement || inp;

                            const picked =
                                wrap.querySelector('.form-select__selected, .selected, [data-selected], .form-list-item.selected, .option.selected, [aria-selected="true"], .select__single-value, .select__value, [data-value][aria-selected="true"]');

                            if (picked && picked.textContent) return picked.textContent.trim();

                            const attr = inp.getAttribute('data-value') || inp.getAttribute('value') || "";
                            return (attr || "").trim();
                        };

                        const getInputValue = (sel) => {
                            const el = qs(sel);
                            return el && typeof el.value === "string" ? el.value.trim() : "";
                        };

                        return {
                            name:    getInputValue(sels.NAME),
                            city:    getSelectText(sels.CITY),
                            phone:   getInputValue(sels.PHONE),
                            gender:  getSelectText(sels.GENDER),
                            age:     getInputValue(sels.AGE),
                            courier: getSelectText(sels.COURIER),
                        };
                    }""",
                    {
                        "NAME":    (getattr(ctx, "selectors", None) or selectors or {})["form"]["name"],
                        "CITY":    (getattr(ctx, "selectors", None) or selectors or {})["form"]["city"],
                        "PHONE":   (getattr(ctx, "selectors", None) or selectors or {})["form"]["phone"],
                        "AGE":     (getattr(ctx, "selectors", None) or selectors or {})["form"]["age"],
                        "GENDER":  (getattr(ctx, "selectors", None) or selectors or {})["form"]["gender"],
                        "COURIER": (getattr(ctx, "selectors", None) or selectors or {})["form"]["courier"],
                    }
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
                # Этап 6. (п.2) Универсальное ожидание успеха сабмита
                # ====================================================================================
                await asyncio.sleep(_rnd.uniform(0.2, 0.4))
                try:
                    # видимая кнопка .btn_submit приоритетно; иначе из профиля (тоже :visible)
                    btn_visible = page.locator("button.btn_submit:visible")
                    if await btn_visible.count():
                        submit_btn = btn_visible.last
                    else:
                        submit_btn = page.locator("form").locator(
                            (getattr(ctx, "selectors", None) or selectors or {})["form"]["submit"] + ":visible"
                        ).last

                    modal_selector = (getattr(ctx, "selectors", None) or selectors or {}).get("form", {}).get("thank_you") \
                        or ".modal-message-content, .modal-message-text"

                    success, active_page = await _wait_submit_result(
                        context, page, submit_btn, modal_selector, ctx,
                        first_wait=30.0, second_wait=120.0
                    )

                    final_page = active_page if (active_page and not active_page.is_closed()) else page
                    final_url = ""
                    with suppress(Exception):
                        final_url = final_page.url

                    if success:
                        term = extract_postback_from_url(final_url)
                        ctx.postback = term
                        logger.info("postback: %s", term or "<empty>")
                        await asyncio.sleep(_rnd.uniform(0.25, 0.60))
                    else:
                        logger.error("Редирект/модалка/текст 'Спасибо' не появились")
                        ctx.errors.append("thankyou_timeout")

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

    # save loaded selectors onto context for later use by form-fillers
    try:
        setattr(ctx, "selectors", selectors)
    except Exception:
        # if context does not allow attribute assignment, silently ignore
        pass

    # единый глобальный таймаут берём из CFG
    await asyncio.wait_for(run_browser(ctx), timeout=CFG["RUN_TIMEOUT"])


if __name__ == "__main__":
    # ---- Идемпотентность: блокировка дубликатов по телефону (кроссплатформенная) ----
    run_main = True
    lock_file_path: str | None = None
    lock_fd: int | None = None
    LOCK_TTL_SEC = 3600  # (п.11) TTL для file-lock — 1 час
    try:
        import hashlib
        import ctypes
        from ctypes import wintypes
        import time as _time  # для проверки mtime lock-файла

        norm_phone = normalize_phone(user_phone)
        if not norm_phone:
            logger.warning("Empty phone → skip duplicate guard")
        else:
            key_src = f"phone|{norm_phone}"
            digest = hashlib.sha1(
                key_src.encode("utf-8"), usedforsecurity=False
            ).hexdigest()
            mutex_name = f"Global\\samokat_{digest}"

            # Windows: mutex
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

                    handle = CreateMutexW(None, True, mutex_name)
                    last_error = ctypes.get_last_error()
                    ERROR_ALREADY_EXISTS = 183

                    if not handle:
                        logger.warning(
                            "CreateMutexW failed; will try file lock fallback"
                        )
                    elif last_error == ERROR_ALREADY_EXISTS:
                        logger.info("Duplicate run blocked for phone: %s", norm_phone)
                        duplicate = True
                except Exception as e:
                    logger.warning("Idempotency (mutex) init failed: %s", e)

            # Fallback (и Unix): файловая блокировка с O_EXCL (+ TTL)
            if not is_windows or not "handle" in locals() or not handle:
                try:
                    locks_dir = os.path.join(os.path.dirname(__file__), "Locks")
                    os.makedirs(locks_dir, exist_ok=True)
                    lock_file_path = os.path.join(locks_dir, f"samokat_{digest}.lock")

                    # (п.11) Снимаем протухший лок по TTL
                    if os.path.exists(lock_file_path):
                        with suppress(Exception):
                            st = os.stat(lock_file_path)
                            if _time.time() - st.st_mtime > LOCK_TTL_SEC:
                                os.remove(lock_file_path)
                                logger.info("Stale lock removed for phone: %s", norm_phone)

                    flags = os.O_CREAT | os.O_EXCL | os.O_RDWR
                    lock_fd = os.open(lock_file_path, flags, 0o644)
                    # записываем PID (mtime используется для TTL)
                    os.write(lock_fd, str(os.getpid()).encode("utf-8"))
                except FileExistsError:
                    logger.info("Duplicate run blocked (file lock) for phone: %s", norm_phone)
                    duplicate = True
                except Exception as e:
                    logger.warning("File lock guard failed: %s", e)

            if duplicate:
                # регистрируем ошибку и пропускаем запуск
                try:
                    ctx.errors.append("duplicate_phone")
                except Exception:
                    pass
                run_main = False

            # Авто-очистка file-lock при выходе
            if lock_file_path and lock_fd is not None:

                def _cleanup_lock():
                    with suppress(Exception):
                        os.close(lock_fd)  # type: ignore[arg-type]
                    with suppress(Exception):
                        os.remove(lock_file_path)

                atexit.register(_cleanup_lock)

    except Exception as e:
        logger.warning("Idempotency init failed: %s", e)

    # ---- Дальше: единый глобальный таймаут из main(); убираем внешний 300s wait_for ----
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
    except Exception as e:  # любая непойманная ошибка
        logger.info(f"[FATAL] {e}")
        fatal = {"error": f"UNCAUGHT {e.__class__.__name__}: {e}"}
        print(json.dumps(fatal, ensure_ascii=False))
        sys.exit(1)

    # ====================================================================================
    # Этап 8. Возврат данных во Flask (результаты выполнения)
    # ====================================================================================
    proxy_used = getattr(ctx, "proxy_cfg", None) is not None
    logger.info("proxy_used: %s", proxy_used)

    send_result(ctx, user_phone, webhook_url, headless_error, proxy_used)
