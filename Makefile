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

.PHONY: install lint typecheck test check

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
