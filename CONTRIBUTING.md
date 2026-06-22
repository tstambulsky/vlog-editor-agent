# Contributing

Thanks for your interest in improving **auto-vlog-editor**. Contributions of all
kinds are welcome — bug reports, docs, and code.

## Development setup

Everything runs in Docker; you don't need a local Python environment.

```bash
# Run the test suite
docker build --target dev -t auto-vlog-dev . && docker run --rm auto-vlog-dev

# Render smoke test with synthetic clips (HDR + freeze-frame cases)
docker run --rm -v "$PWD":/srv/app -e DATA_DIR=/tmp/vj -e PYTHONPATH=/srv/app \
  auto-vlog-dev python scripts/smoke_render.py
```

If you prefer a local environment: `python3.12 -m venv .venv && .venv/bin/pip install -r requirements.txt`.

## Ground rules

- Read [`AGENTS.md`](AGENTS.md) first — it documents the non-obvious invariants
  (the approval gate, the `app/claude_cli.py` boundary, the beat-sync contract, the
  "ASR gives timing, the script gives text" rule). Breaking these breaks the product.
- **Never commit secrets.** Real credentials live only in `.env` (gitignored). Use
  `.env.example` to document any new variable.
- Keep changes focused and match the surrounding code style.
- Add or update tests for behavior changes (`tests/`), and run them before opening a PR.

## Pull requests

1. Fork and create a feature branch.
2. Make your change with tests.
3. Run the test suite (above) and confirm it passes.
4. Open a PR describing the change and the motivation.

## Reporting issues

Open a GitHub issue with steps to reproduce, expected vs. actual behavior, and
relevant logs (with any secrets/personal data redacted).
