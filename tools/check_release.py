#!/usr/bin/env python3
"""Check a release tag against the version files, and print its changelog notes.

The release workflow runs this before it tests, builds or publishes anything:

    python tools/check_release.py --tag "$GITHUB_REF_NAME"

The tag must match `version` in pyproject.toml and `__version__` in
runledger/__init__.py, and CHANGELOG.md must have a `## [version]` heading.
With `--notes` the CHANGELOG section of that version is printed on stdout,
ready for `gh release create --notes-file -`.

Exit codes: 0 everything matches; 1 a mismatch, named on stderr; 2 no tag given.
"""
import argparse
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def project_version(text):
    """The `version` of the [project] table in a pyproject.toml."""
    start = text.index("[project]")
    end = text.find("\n[", start + 1)
    section = text[start:] if end == -1 else text[start:end]
    match = re.search(r'^version\s*=\s*"([^"]+)"', section, re.M)
    return match.group(1) if match else None


def init_version(text):
    """`__version__` of runledger/__init__.py."""
    match = re.search(r'^__version__\s*=\s*"([^"]+)"', text, re.M)
    return match.group(1) if match else None


def changelog_section(text, version):
    """The `## [version]` section of a changelog, heading included, or None."""
    heading = re.compile(r"^## \[%s\]" % re.escape(version))
    lines = text.splitlines()
    start = next((i for i, line in enumerate(lines) if heading.match(line)), None)
    if start is None:
        return None
    end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("## [")), len(lines))
    return "\n".join(lines[start:end]).strip("\n")


def read(root, relative):
    return (root / relative).read_text(encoding="utf-8")


def check(tag, root=ROOT):
    """Mismatches between the tag and the version files, each naming its source."""
    version = tag[1:] if tag.startswith("v") else tag
    problems = []
    try:
        pyproject = project_version(read(root, "pyproject.toml"))
    except (OSError, ValueError):
        pyproject = None
    if pyproject != version:
        problems.append("pyproject.toml says %s" % (pyproject or "nothing"))
    try:
        init = init_version(read(root, str(Path("runledger") / "__init__.py")))
    except OSError:
        init = None
    if init != version:
        problems.append("runledger/__init__.py says %s" % (init or "nothing"))
    try:
        changelog = read(root, "CHANGELOG.md")
    except OSError:
        changelog = None
    if changelog is None or changelog_section(changelog, version) is None:
        problems.append("CHANGELOG.md has no '## [%s]' heading" % version)
    return problems


def use_utf8_output():
    """Write UTF-8 even on a Windows console whose code page cannot encode the changelog."""
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")


def main(argv=None):
    use_utf8_output()
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--tag", default=os.environ.get("GITHUB_REF_NAME", ""),
                        help="release tag, e.g. v0.6.1 (default: $GITHUB_REF_NAME)")
    parser.add_argument("--root", default=None, help="repository root (default: the parent of tools/)")
    parser.add_argument("--notes", action="store_true",
                        help="print the CHANGELOG section of the tag instead of only checking")
    args = parser.parse_args(argv)

    tag = args.tag.strip()
    if not tag:
        parser.error("--tag (or the GITHUB_REF_NAME environment variable) is required")
    root = Path(args.root).resolve() if args.root else ROOT

    problems = check(tag, root)
    if problems:
        sys.stderr.write("tag %s does not match: %s\n" % (tag, "; ".join(problems)))
        return 1

    if args.notes:
        version = tag[1:] if tag.startswith("v") else tag
        sys.stdout.write(changelog_section(read(root, "CHANGELOG.md"), version) + "\n")
    else:
        sys.stdout.write("tag %s matches pyproject.toml, runledger/__init__.py and CHANGELOG.md\n" % tag)
    return 0


if __name__ == "__main__":
    sys.exit(main())
