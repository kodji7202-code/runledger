# Release checklist

RunLedger uses semantic versioning after 1.0. Release candidates use PEP 440 pre-release versions
such as `1.0.0rc1`; the tag, `pyproject.toml`, `runledger/__init__.py` and the `CHANGELOG.md`
heading must spell the version identically. `tools/check_release.py` is the first release-job gate.

## Before tagging 1.0 or a release candidate

1. Merge the intended changes into `main` and confirm the full CI matrix is green there.
2. Update the version in both version files and move the relevant `CHANGELOG.md` entries under the
   exact release heading. For a release candidate, use a heading such as `## [1.0.0rc1]`; do not
   reuse or rewrite a published pre-release.
3. Confirm the receipt schema/API compatibility statements still match the release. Additive format
   or API changes need no major version; incompatible changes do not belong in a patch or minor 1.x
   release.
4. Check the operations docs: backup verification, restore/rollback wording, the upgrade notes and
   the API deprecation contract must describe the behavior being tagged.

## What the tag workflow proves

Pushing `v<version>` runs the tests, builds the sdist/wheel and then runs
`tools/check_clean_install.py`. That gate installs the built wheel into clean virtual environments,
rather than importing the checkout:

- the base install proves `runledger --version`, receipt generation and `runledger validate` work
  with the zero-dependency CLI and that FastAPI is not installed accidentally;
- the `[server]` install proves the optional FastAPI/uvicorn extra resolves and a fresh team server
  answers `/health`;
- exactly one wheel and one sdist must be present.

PyPI publication and the GitHub Release happen only after those checks pass.

## Manual release-candidate validation

Before replacing a release candidate with final 1.0, validate the published artifact on machines or
containers that did not build it:

```bash
python -m pip install 'runledger-ai==1.0.0rc1'
runledger --version
python -m pip install 'runledger-ai[server]==1.0.0rc1'
runledger serve --help
```

Build and start the Docker image from the same commit, then check `/health`, one receipt push, one
browser sign-in and a verified backup/restore drill using the self-hosting runbook. Record any
release-blocking defect in the changelog; fix forward in a new release candidate (`1.0.0rc2`, and so
on). Publishing PyPI artifacts and pushing tags are maintainer actions, not something automation
should do before explicit release approval.
