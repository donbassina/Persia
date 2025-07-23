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

С прокси

```bash
cat params.json | python Samokat-TP.py --proxy=http://1.2.3.4:8080
```

params.json – тот же JSON, который n8n отправляет в stdin.
