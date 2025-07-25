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
  "headless":   true
}
EOF
```

С прокси

```bash
cat params.json | python Samokat-TP.py --proxy=http://1.2.3.4:8080
```

params.json – тот же JSON, который n8n отправляет в stdin (хз че эт). При отсутствии поля
`headless` используется значение по умолчанию из `config_defaults.json`.

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
