.PHONY: install test build frontend dev app status spend capsule handoff clean

VENV := .venv
PYTHON := $(VENV)/bin/python
PIP := $(VENV)/bin/pip

install:
	python3 -m venv $(VENV)
	$(PIP) install -e '.[dev]'
	cd frontend && npm install

test:
	$(PYTHON) -m unittest discover -s tests -v

build: frontend-build

frontend-build:
	cd frontend && npm run build

dev:
	$(PYTHON) scripts/dev.py

app:
	$(PYTHON) -m router app

status:
	$(PYTHON) -m router status

spend:
	$(PYTHON) -m router spend --days 30

capsule:
	$(PYTHON) -m router capsule --list

handoff:
	$(PYTHON) scripts/handoff.py

dist: build
	$(PYTHON) -m pip install --upgrade build
	$(PYTHON) -m build
	$(PYTHON) -m pip install --upgrade twine
	$(PYTHON) -m twine check dist/*

bundle:
	$(PYTHON) scripts/bundle.py

install-wheel: dist
	$(PYTHON) -m pip install dist/coding_router-*.whl

clean:
	rm -rf $(VENV) frontend/node_modules frontend/dist router/app/static/assets dist *.egg-info
	find . -type d -name __pycache__ -exec rm -rf {} +
