# Funciona en Windows (GNU make + cmd/sh) y en Linux/macOS.
VENV ?= .venv

ifeq ($(OS),Windows_NT)
PYTHON ?= python
VPY := $(VENV)/Scripts/python.exe
else
PYTHON ?= python3
VPY := $(VENV)/bin/python
endif

SRC := src tests

.PHONY: install lint typecheck test check security

install:
	$(PYTHON) -m venv $(VENV)
	$(VPY) -m pip install --upgrade pip
	$(VPY) -m pip install -e ".[dev]"

lint:
	$(VPY) -m ruff check $(SRC)
	$(VPY) -m ruff format --check $(SRC)

typecheck:
	$(VPY) -m mypy --strict src

test:
	$(VPY) -m pytest --cov=odds_scanner --cov-report=term-missing --cov-fail-under=85

check: lint typecheck test

# pip-audit: vulnerabilidades conocidas en dependencias instaladas (excluye el propio paquete).
# bandit: análisis estático; falla solo con severidad media o alta (-ll).
security:
	$(VPY) -m pip_audit --skip-editable
	$(VPY) -m bandit -r src -ll
