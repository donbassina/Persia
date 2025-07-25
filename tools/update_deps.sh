#!/usr/bin/env bash
pip install --upgrade pip pip-tools &&
pip-compile --upgrade requirements.txt -o requirements.lock
