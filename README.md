## Quick start

```bash
# install deps
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
playwright install  # once, to download browser binaries
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
playwright install
```

To update:

```bash
tools/update_deps.sh && pytest
```
