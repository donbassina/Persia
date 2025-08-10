## Quick start

```bash
# install deps
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
playwright install chromium  # once, to download browser binaries
python tools/init_selectors.py  # optional: create selectors template
```

Run script

Без прокси

```bash
cat params.json | python Samokat-TP.py
```

Пример JSON с принудительным `headless`:

```bash
cat <<EOF | python Samokat-TP.py
{
  "user_phone": "79991234567",
  "user_name":  "Вася",
  "headless":   true,
  "selectors_profile": "default"
}
EOF
```

Поле *selectors_profile* задаёт файл `selectors/<name>.yml`.
Если не указано — используется `default`.

С прокси

```bash
cat params.json | python Samokat-TP.py --proxy=http://1.2.3.4:8080
```

Через JSON:

```bash
cat <<EOF | python Samokat-TP.py
{
  "user_phone": "79991234567",
  "proxy": "http://1.2.3.4:8080"
}
EOF
```

При неверном формате прокси задача завершается с ошибкой `bad_proxy_format`.

- Если `proxy` **передан** в JSON и прокси **доступен** — скрипт работает через прокси.
- Если `proxy` **передан**, но прокси **недоступен** — задача немедленно завершается с ошибкой `bad_proxy_unreachable`, отправляется webhook (если указан), выполнение без прокси **не допускается**.
- Если `proxy` **не передан** — скрипт работает с локального IP.

params.json – тот же JSON, который n8n отправляет в stdin (хз че эт). При отсутствии поля
`headless` используется значение по умолчанию из `config_defaults.json`.

Смена профиля селекторов

```bash
cat <<EOF | python Samokat-TP.py
{
  "user_phone": "79991234567",
  "user_name":  "Тест",
  "selectors_profile": "foodexpress"
}
EOF
```

## Формат входного JSON

Скрипт получает входные данные через **stdin** (обычно от n8n).

Пример полного JSON:
```json
{
  "user_phone": "79991234567",
  "user_name": "Иван Иванов",
  "user_city": "Москва",
  "user_age": "25",
  "user_gender": "Мужской",
  "user_courier_type": "Пеший",
  "selectors_profile": "default",
  "proxy": "socks5://217.199.252.33:1080",
  "headless": true,
  "Webhook": "https://example.com/webhook"
}
```

### Прокси

- Если `proxy` передан и формат валиден, но прокси недоступен — немедленный фэйл `bad_proxy_unreachable`, webhook отправляется; выполнение без прокси **не продолжается**.
- Если `proxy` не передан — работаем с локального IP.

Поддерживаемые схемы: http://, https://, socks5://.
Авторизация (логин/пароль) опциональна:

    без авторизации: socks5://217.199.252.33:1080

    с авторизацией: socks5://user:pass@217.199.252.33:1080

    HTTP-прокси без/с авторизацией: http://1.2.3.4:8080, http://user:pass@1.2.3.4:8080

### Скриншоты

- Основной скриншот делается **после чекбокса и до отправки формы** — этот файл сохраняется всегда.
- Возможен финальный скриншот после клика (имя с суффиксом `-final`), если блок дошёл до этой части.

Путь: Media/Screenshots/<ДД.ММ.ГГГГ>-<номер_телефона>[ (N)].png

### Webhook

Отправляется **всегда**.

Успех:
```json
{ "phone": "...", "POSTBACK": "..." }
```

Ошибка:
```json
{ "phone": "...", "error": "...", "screenshot": "..." }
```
(поле `screenshot` добавляется, если файл существует)

Ретрай до 3 попыток с паузой 30 секунд.

### POSTBACK

В текущей версии POSTBACK формируется как `path+query` финального URL, пример: `/thanks?lead=123`. `utm_term` используется только в логах и на успех не влияет.

### Структура каталогов

- Logs/ — лог-файлы всех запусков (имя включает дату и номер телефона), пример: Logs/2025-08-10-79991234567.txt
- Media/Screenshots/ — скриншоты формы (основной и, при наличии, *-final.png), пример: Media/Screenshots/10.08.2025-79991234567.png

### Dependencies

Install locked versions:

```bash
pip install -r requirements.lock
playwright install chromium
```

To update:

```bash
tools/update_deps.sh && pytest
```

### Environment variables

Configuration values can also be provided via a `.env` file in the project
root. All loaded values are logged to `Logs/...txt`.

Example `.env`:

```
UA="Mozilla/5.0 ..."
HEADLESS=false
```

В конце каждого лога есть строка RESULT: SUCCESS или RESULT: ERROR.
Путь к лог-файлу и скриншоту возвращается в JSON-ответе.

### Автоперезапуск (Windows)

`run_loop.bat` бесконечно перезапускает скрипт и выдерживает паузы после ошибок.

| Переменная | По умолчанию | Описание |
|------------|--------------|----------|
| `JSON_FILE` | `params.json` | Файл, подаваемый на stdin |
| `HEADLESS`  | *(пусто)* | `1` → `--headless=true`, `0` → `--headless=false` |
| `MAX_RETRY` | `3` | Сколько подряд неуспешных запусков допускается |
| `COOLDOWN`  | `300` | Пауза (сек) после исчерпания `MAX_RETRY` |

Пример — отключить headless и уменьшить паузу:

```bat
set HEADLESS=0
set COOLDOWN=120
run_loop.bat
```
