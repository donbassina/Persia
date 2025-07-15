# -*- coding: utf-8 -*-
import random
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
import os
from datetime import datetime
from python_ghost_cursor.playwright_async import create_cursor

def make_log_file(phone):
    logs_dir = os.path.join(os.path.dirname(__file__), "Logs")
    os.makedirs(logs_dir, exist_ok=True)
    date_str = datetime.now().strftime("%d.%m.%Y")
    log_file = os.path.join(logs_dir, f"{date_str}-{phone}.txt")
    return log_file

def log(msg, LOG_FILE):
    ts = datetime.now()
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(f"{ts}  {msg}\n")

try:
    params = json.load(sys.stdin)
    user_phone = params.get("user_phone", "")
    LOG_FILE = make_log_file(user_phone)
    LOG_START_POS = os.path.getsize(LOG_FILE) if os.path.exists(LOG_FILE) else 0
    log(f"[INFO] Получены параметры: {params}", LOG_FILE)

    EXTRA_UA     = params.get("ua", "Mozilla/5.0")
    headless_flag = params.get("headless", False)
    webhook_url = params.get("Webhook", "")
except Exception as e:
    print(f"[ERROR] Не удалось получить JSON из stdin: {e}")
    sys.exit(1)

import requests
import time

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






















# Основная рабочая директория
WORK_DIR = os.path.dirname(__file__)














def send_webhook(result, webhook_url):
    if webhook_url:
        e = None
        for attempt in range(3):
            try:
                resp = requests.post(webhook_url, json=result, timeout=10)
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



POSTBACK = None



screenshot_path = ""

# ====================================================================================
# Этап 3. 
# ====================================================================================

async def human_type(page, selector, text, min_delay=0.10, max_delay=0.66):
    await page.focus(selector)
    for char in text:
        await page.keyboard.type(char)
        await asyncio.sleep(random.uniform(min_delay, max_delay))



async def fill_full_name(page, name, retries=3):
    for attempt in range(retries):
        try:
            input_box = page.get_by_placeholder("ФИО")
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
    for attempt in range(retries):
        try:
            input_box = page.get_by_placeholder("Выберите город")
            await input_box.click()
            await page.wait_for_timeout(100)
            await input_box.fill("")
            part_city = get_partial_city(city)
            await human_type_city_autocomplete(page, 'input[name="user_city"]', part_city)
            try:
                await page.wait_for_selector('.form-list-item', timeout=1500)
            except PWTimeoutError:
                pass
            try:
                await page.get_by_text(city, exact=True).click(timeout=1500)
            except Exception:
                value = await input_box.input_value()
                if value.strip().lower() == city.strip().lower():
                    return True
                continue
            value = await input_box.input_value()
            if value.strip().lower() == city.strip().lower():
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
            await input_box.click()
            await page.wait_for_timeout(100)
            try:
                await page.get_by_text(gender, exact=True).click(timeout=1500)
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
            await input_box.click()
            await page.wait_for_timeout(100)
            try:
                await page.get_by_text(courier_type, exact=True).click(timeout=1500)
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
    percent = random.randint(min_percent, max_percent)
    cut = max(1, int(len(city_name) * percent / 100))
    return city_name[:cut]

async def human_type_city_autocomplete(page, selector, text, min_delay=0.11, max_delay=0.18):
    await page.focus(selector)
    for char in text:
        await page.keyboard.type(char)
        await page.evaluate("""
            ({selector, char}) => {
                var input = document.querySelector(selector);
                if (input) {
                    ['keydown', 'keyup', 'input'].forEach(eventType => {
                        var event = new KeyboardEvent(eventType, {
                            bubbles: true,
                            cancelable: true,
                            key: char,
                            code: char,
                            charCode: char.charCodeAt(0),
                            keyCode: char.charCodeAt(0)
                        });
                        input.dispatchEvent(event);
                    });
                }
            }
        """, {"selector": selector, "char": char})
        await asyncio.sleep(random.uniform(min_delay, max_delay))




async def emulate_user_reading(page, total_time, LOG_FILE):
    start_time = asyncio.get_event_loop().time()
    blocks = ['.hero', '.about', '.benefits', '.form-wrapper']
    height = await page.evaluate("document.body.scrollHeight")
    current_y = 0

    while asyncio.get_event_loop().time() - start_time < total_time:
        action = random.choices(
            ["scroll_down", "scroll_up", "pause", "mouse_wiggle", "to_block"],
            weights=[0.48, 0.14, 0.20, 0.06, 0.12]
        )[0]

        if action == "scroll_down":
            step = random.choice([random.randint(120, 350), random.randint(400, 800)])
            current_y = min(current_y + step, height-1)
            await page.evaluate(f"window.scrollTo(0, {current_y})")
            log(f"[INFO] Скроллим вниз на {step}", LOG_FILE)
            await asyncio.sleep(random.uniform(0.7, 1.7))
        elif action == "scroll_up":
            step = random.randint(80, 290)
            current_y = max(current_y - step, 0)
            await page.evaluate(f"window.scrollTo(0, {current_y})")
            log(f"[INFO] Скроллим вверх на {step}", LOG_FILE)
            await asyncio.sleep(random.uniform(0.5, 1.1))
        elif action == "pause":
            t = random.uniform(1.2, 3.8)
            log(f"[INFO] Пауза {t:.1f}", LOG_FILE)
            await asyncio.sleep(t)
        elif action == "mouse_wiggle":
            x = random.randint(100, 1200)
            y = random.randint(100, 680)
            dx = random.randint(-10, 10)
            dy = random.randint(-8, 8)
            await page.mouse.move(x, y, steps=random.randint(4, 10))
            await asyncio.sleep(random.uniform(0.08, 0.18))
            await page.mouse.move(x+dx, y+dy, steps=2)
            log(f"[INFO] Мышь дрожит ({x},{y})", LOG_FILE)
            await asyncio.sleep(random.uniform(0.10, 0.22))
        else:
            sel = random.choice(blocks)
            el = await page.query_selector(sel)
            if el:
                box = await el.bounding_box()
                if box:
                    x = box["x"] + random.uniform(12, box["width"]-12)
                    y = box["y"] + random.uniform(12, box["height"]-12)
                    await page.mouse.move(x, y, steps=random.randint(10, 22))
                    log(f"[INFO] Мышь на {sel} ({int(x)},{int(y)})", LOG_FILE)
                    await asyncio.sleep(random.uniform(0.4, 1.3))


async def smooth_scroll_to_form(page):
    form = await page.query_selector("div.form-wrapper")
    if not form:
        return
    b = await form.bounding_box()
    if not b:
        return
    viewport_height = 768
    center_screen = viewport_height / 2
    while True:
        current_y = await page.evaluate("window.scrollY")
        b = await form.bounding_box()
        center_form = b["y"] + b["height"] / 2
        diff = center_form - center_screen
        abs_diff = abs(diff)
        if abs_diff <= 40:
            break
        if abs_diff > 400:
            step = 300
        elif abs_diff > 120:
            step = 100
        else:
            step = 40
        new_y = current_y + step if diff > 0 else current_y - step
        await page.evaluate(f"window.scrollTo(0, {new_y})")
        current_y = new_y
        await asyncio.sleep(random.uniform(0.04, 0.1))

async def run_browser():
    global screenshot_path
    async with async_playwright() as p:

        browser = await p.chromium.launch(
            headless=headless_flag,
            channel="chrome",
            args=[
                "--incognito",
                "--disable-gpu",
                "--no-sandbox",
                "--disable-dev-shm-usage"
            ]
        )


        context = await browser.new_context(
            user_agent=EXTRA_UA,
            locale="ru-RU",
            timezone_id=tz,
            viewport={"width": 1366, "height": 768}
        )

        # apply playwright-stealth anti-bot measures
        await stealth.Stealth().apply_stealth_async(context)

        page = await context.new_page()






        cursor = create_cursor(page)




        await context.add_init_script('Object.defineProperty(navigator,"webdriver",{get:()=>undefined})')
   

        await context.route("**/*.{mp4,webm}", lambda r: r.abort())
        await context.route("*/gtag/*",        lambda r: r.abort())





    
        error_msg = ""

        try:
            # --- загрузка страницы с таймаут-отловом ---
            try:
                await page.goto(
                    "https://go.cpatrafficpoint.ru/click?o=3&a=103",
                    wait_until="domcontentloaded",
                    timeout=60_000
                )
                log("[INFO] Страница загружена", LOG_FILE)
            except PWTimeoutError:
                error_msg = "Timeout 60 сек. при загрузке лендинга"
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
                await page.wait_for_selector("div.form-wrapper", timeout=30000)
                log("[INFO] Контент формы загружен (div.form-wrapper найден)", LOG_FILE)
            except Exception as e:
                log(f"[WARN] div.form-wrapper не найден: {e}", LOG_FILE)

            # измеряем фактическое время полной загрузки
            load_ms = await page.evaluate(
                "() => { const t = performance.timing; return (t.loadEventEnd || t.domContentLoadedEventEnd) - t.navigationStart; }"
            )
            load_sec = max(load_ms / 1000, 0)

    
            min_read   = 1.5                 # минимум «чтения», сек
            base_read  = random.uniform(7, 15)
            total_time = max(min_read, base_read - max(0, 7 - load_sec))




            
            start_time = asyncio.get_event_loop().time()
            log(f"[INFO] Имитация “чтения” лендинга: {total_time:.1f} сек", LOG_FILE)

            await emulate_user_reading(page, total_time, LOG_FILE)



            # ====================================================================================
            # Плавный, крупный и потом мелкий скролл к форме, без телепортов
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
                scroll_step = random.randint(0, 190)
                current_y = await page.evaluate("window.scrollY")
                new_y = current_y + scroll_step
                await page.evaluate(f"window.scrollTo(0, {new_y})")
                await asyncio.sleep(random.uniform(0.2, 0.45))
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
                        await page.wait_for_function(f'document.location.href !== "{old_url}"', timeout=20000)
                        log("[INFO] URL изменился — заявка успешно отправлена", LOG_FILE)

                        modal_selector = 'div.modal-message-content'
                        modal = await page.query_selector(modal_selector)

                        if modal is None:
                            try:
                                await page.wait_for_selector(modal_selector, timeout=10000)
                                log("[INFO] Всплывающее окно подтверждения появилось после ожидания", LOG_FILE)
                            except Exception as e:
                                html_content = await page.content()
                                log(f"[ERROR] Всплывающее окно подтверждения НЕ появилось после ожидания: {e}", LOG_FILE)
                                log(f"[DEBUG HTML CONTENT]: {html_content[:2000]}", LOG_FILE)
                                raise
                        else:
                            log("[INFO] Всплывающее окно подтверждения уже было на экране", LOG_FILE)


                        log("[INFO] Всплывающее окно подтверждения появилось", LOG_FILE)
                        await asyncio.sleep(random.uniform(1, 4))
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
            # ====================================================================================
            # Этап 9. Удаляем только свой профиль с ретраем
            # ====================================================================================
            
            







if __name__ == "__main__":
    try:
        # общий предел на весь скрипт (180 с). Меняйте по нужде
        asyncio.run(asyncio.wait_for(run_browser(), timeout=180))
    except Exception as e:            # любая непойманная ошибка
        # пишем в лог и всё-таки отдаём JSON, чтобы n8n не подвис
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

