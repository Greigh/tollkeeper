# Contributing to Tollkeeper

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
cd frontend && npm install && cd ..
```

## Dev loop

```bash
.venv/bin/python scripts/dev.py   # backend (uvicorn --reload) + frontend (vite HMR)
```

## Checks before opening a PR

```bash
.venv/bin/python -m pytest tests/ -x -q
cd frontend && npm run build
```

## Adapter rules

- Credentials are **read-only**: read what the provider's own tooling stores;
  never refresh, rewrite, or store vendor tokens.
- Never synthesize quota numbers — missing fields stay unknown, not zero.
- Detail percentages are whole numbers representing **remaining** quota.
- Add a mocked test in `tests/test_adapters.py` for any new endpoint shape;
  if the path can't be verified live, say so in the PR.
