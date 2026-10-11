# Contributing

Thanks for helping. Bug reports, fixes and small features are all welcome. For
anything large, open an issue first so we can agree on the shape before you
write code.

## Setup

```bash
python -m venv .venv
. .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env   # only needed to run the bot, not the tests
```

Read [AGENTS.md](AGENTS.md) for the architecture and conventions, and
[docs/known-gaps.md](docs/known-gaps.md) for what's planned next.

## Tests

Every PR must pass the same checks CI runs:

```bash
python -m pytest tests/ -q --ignore=tests/live_intents.py
python tests/simulate.py
python -m compileall -q -x '/(\.venv|venv|data|\.git)/' .
```

Add a test for each behaviour change. `tests/live_intents.py` calls a real LLM
and costs money; it is opt-in and only needed when you change the intent prompt.

Test fixtures that look like secrets need a `# gitleaks:allow` comment, or the
CI secret scan fails.

## Pull requests

- Keep each PR to one change. Match the style of the code around it.
- Update `README.md` or `docs/` when behaviour changes.
- For a change members will notice, add a `CHANGELOG.md` entry. Don't add
  `announce: true`; the maintainer decides what goes on the `What's new` board.
- CI on a first-time contributor's PR waits for maintainer approval.

By contributing you agree that your work is released under the
[MIT License](LICENSE) and that you'll follow the
[Code of Conduct](CODE_OF_CONDUCT.md).
