# Contributing to RunLedger

Thanks for helping. This file covers setup, tests, and the rules the code follows.

By contributing, you agree that your contribution is licensed under the Apache License 2.0
(see [LICENSE](LICENSE)).

## Set up

```bash
git clone <repository URL> runledger
cd runledger
python -m venv .venv
. .venv/bin/activate              # Windows: .venv\Scripts\activate
python -m pip install -e . pytest
```

## Run the tests

```bash
python -m pytest -q
```

`tests/test_packaging.py` builds a wheel with pip. The first build downloads the build
backend from the package index. Offline, those tests are skipped with the reason.

## Rules for the code

- **Standard library only at runtime.** `dependencies` in `pyproject.toml` stays empty.
  `pytest` is a development dependency only.
- **Python 3.9 must keep working.** Avoid `match` statements, `X | Y` union types outside
  annotations (inside annotations they are fine when the module starts with
  `from __future__ import annotations`), parenthesized context managers, `zip(strict=...)`,
  `datetime.UTC`, and other names added after 3.9. CI byte-compiles the package with 3.9 and
  runs the tests on 3.9.
- **Windows, macOS and Linux all work.** Build paths with `pathlib`, and do not depend on a
  particular shell in tests.
- **No real data in fixtures.** Session files, API keys and customer data never go into
  `tests/`. Use small synthetic files, such as those in `tests/fixtures/`.
- **Match the style of the code around your change.** There is no formatter or linter in CI.

## Agent adapters

Each coding agent is read by an adapter in `runledger/adapters/`. The adapter contract (the
`NAME`, `LABEL`, `detect`, `parse` and `find_sessions` members, and the canonical tool names)
is described in the docstring at the top of `runledger/adapters/__init__.py`. Agents that
should not need an adapter write the native format described in `docs/format.md`.

To add an adapter, add the module, register its name in `ADAPTER_MODULES` in
`runledger/adapters/__init__.py`, and add tests with a synthetic session file.

## Pull requests

- Say what changed and why. Link the issue if there is one.
- Add or update tests for the change. Run `python -m pytest -q` before you open the pull
  request.
- Keep the change focused, and add a line to `CHANGELOG.md` under "Unreleased".

## Security issues

Do not open a public issue for a vulnerability. Follow [SECURITY.md](SECURITY.md).
