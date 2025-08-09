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
