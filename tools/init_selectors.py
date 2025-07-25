#!/usr/bin/env python3
"""Initialize default CSS selectors config."""
from __future__ import annotations

import os
from pathlib import Path

TEMPLATE = """# Селекторы формы по-умолчанию.
# При необходимости создавайте дополнительные YAML-файлы
# (например, `foodexpress.yml`) с той же схемой ключей.
form:
  name:      'input[name="name"]'          # поле «Имя»
  phone:     'input[name="phone"]'         # поле «Телефон»
  checkbox:  'input[type="checkbox"]'      # чекбокс согласия
  submit:    'button[type="submit"]'       # кнопка «Отправить»
  thank_you: '.modal-thanks'               # поп-ап «Спасибо»
"""


def main() -> None:
    os.makedirs("selectors", exist_ok=True)
    target = Path("selectors/default.yml")
    if target.exists():
        print("[init_selectors] selectors/default.yml already exists — skipped")
        return
    target.write_text(TEMPLATE, encoding="utf-8")
    print("[init_selectors] selectors/default.yml created")


if __name__ == "__main__":
    main()
