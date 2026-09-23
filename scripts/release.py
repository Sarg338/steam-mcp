#!/usr/bin/env python3
"""Release bookkeeping for steam-mcp: one version, six places, one changelog.

The version lives in pyproject.toml, manifest.json, server.json (twice) and
the __version__ of both steam_mcp/__init__.py and steam_mcp/server.py. Changes accumulate under `## [Unreleased]` in
CHANGES.md as they merge; the publish workflow turns that section into a release.

Commands (standard library only, so CI can run them before installing anything):

    check             every version field agrees and CHANGES.md is well formed;
                      exits non-zero naming each problem.
    pending           exit 0 if [Unreleased] has entries to ship, 1 if not.
    bump [LEVEL]      bump every version field and turn [Unreleased] into the
                      new version's heading. LEVEL is patch, minor, major, or
                      auto (default): the section's `<!-- release: minor -->`
                      marker, else patch. Prints the new version.
    notes VERSION     print that version's changelog entries (release body).
    title VERSION     print the release title, "vX.Y.Z — tagline" when the
                      [Unreleased] section carried `<!-- title: tagline -->`.

Markers are HTML comments, so they don't render on GitHub:

    ## [Unreleased]
    <!-- release: minor -->
    <!-- title: sturdier reviews, fairer rarity -->
    - **Fixed: …**
"""

from __future__ import annotations

import contextlib
import io
import json
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
PYPROJECT = ROOT / "pyproject.toml"
MANIFEST = ROOT / "manifest.json"
SERVER_JSON = ROOT / "server.json"
SERVER_PY = ROOT / "steam_mcp" / "server.py"
INIT_PY = ROOT / "steam_mcp" / "__init__.py"
CHANGES = ROOT / "CHANGES.md"

LEVELS = ("patch", "minor", "major")
SEMVER_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")
PYPROJECT_RE = re.compile(r'^version = "([^"]+)"$', re.M)
SERVER_PY_RE = re.compile(r'^__version__ = "([^"]+)"$', re.M)
# "## [1.16.1]" or "## [1.16.1] — tagline" (the tagline becomes the release title)
HEADING_RE = re.compile(r"^## \[([^\]]+)\](?:[ \t]+[—-][ \t]+(.+?))?[ \t]*$", re.M)
MARKER_RE = re.compile(r"^<!--\s*(release|title):\s*(.*?)\s*-->[ \t]*\n?", re.M)


# --- reading ------------------------------------------------------------------

def versions() -> dict[str, str | None]:
    """Every place the version is written, by a human-readable location."""
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    server = json.loads(SERVER_JSON.read_text(encoding="utf-8"))
    pypi = next((p for p in server.get("packages", [])
                 if p.get("registryType") == "pypi"), {})
    py = PYPROJECT_RE.search(PYPROJECT.read_text(encoding="utf-8"))
    sv = SERVER_PY_RE.search(SERVER_PY.read_text(encoding="utf-8"))
    iv = SERVER_PY_RE.search(INIT_PY.read_text(encoding="utf-8"))
    return {
        "pyproject.toml [project].version": py.group(1) if py else None,
        "manifest.json version": manifest.get("version"),
        "server.json version": server.get("version"),
        "server.json packages[pypi].version": pypi.get("version"),
        "steam_mcp/server.py __version__": sv.group(1) if sv else None,
        "steam_mcp/__init__.py __version__": iv.group(1) if iv else None,
    }


def sections(text: str) -> list[tuple[str, str | None, int, int, int]]:
    """(label, tagline, heading_start, body_start, end) per `## [...]` section.

    The body runs from the end of the heading line to the next heading (or the
    end of the file).
    """
    heads = list(HEADING_RE.finditer(text))
    out = []
    for i, m in enumerate(heads):
        end = heads[i + 1].start() if i + 1 < len(heads) else len(text)
        out.append((m.group(1), m.group(2), m.start(), m.end(), end))
    return out


def unreleased(text: str) -> tuple[str, dict[str, str]] | None:
    """(entries, markers) of the [Unreleased] section, or None if there is none."""
    for label, _tag, _head, start, end in sections(text):
        if label == "Unreleased":
            body = text[start:end]
            markers = {k: v for k, v in MARKER_RE.findall(body)}
            entries = MARKER_RE.sub("", body).strip()
            return entries, markers
    return None


def current_version() -> str:
    found = {v for v in versions().values()}
    if len(found) != 1 or None in found:
        raise SystemExit("version fields disagree — run `release.py check`")
    return found.pop()


# --- commands -----------------------------------------------------------------

def cmd_check() -> int:
    problems = []
    vs = versions()
    for where, v in vs.items():
        if v is None:
            problems.append(f"{where}: not found")
        elif not SEMVER_RE.match(v):
            problems.append(f"{where}: {v!r} is not X.Y.Z")
    distinct = {v for v in vs.values() if v}
    if len(distinct) > 1:
        detail = "; ".join(f"{w} = {v}" for w, v in vs.items())
        problems.append(f"version fields disagree: {detail}")

    text = CHANGES.read_text(encoding="utf-8")
    secs = sections(text)
    labels = [s[0] for s in secs]
    if labels.count("Unreleased") > 1:
        problems.append("CHANGES.md has more than one [Unreleased] section")
    if "Unreleased" in labels and labels[0] != "Unreleased":
        problems.append("CHANGES.md: [Unreleased] must be the first section")
    released = [lb for lb in labels if lb != "Unreleased"]
    for lb in released:
        if not SEMVER_RE.match(lb):
            problems.append(f"CHANGES.md: heading [{lb}] is not a version")
    if len(set(released)) != len(released):
        problems.append("CHANGES.md: a version heading appears twice")
    if len(distinct) == 1:
        (v,) = distinct
        if released and released[0] != v:
            problems.append(
                f"CHANGES.md: newest released section is [{released[0]}] but the "
                f"version is {v} — bump through `release.py bump`, not by hand")
    ur = unreleased(text)
    if ur is not None:
        level = ur[1].get("release")
        if level is not None and level not in LEVELS:
            problems.append(f"CHANGES.md: `release: {level}` must be one of {LEVELS}")

    for p in problems:
        print(f"release check: {p}", file=sys.stderr)
    if not problems:
        print(f"release check: ok ({next(iter(distinct))})")
    return 1 if problems else 0


def next_version(version: str, level: str) -> str:
    major, minor, patch = (int(x) for x in SEMVER_RE.match(version).groups())
    if level == "major":
        return f"{major + 1}.0.0"
    if level == "minor":
        return f"{major}.{minor + 1}.0"
    return f"{major}.{minor}.{patch + 1}"


def cmd_bump(level: str = "auto") -> int:
    with contextlib.redirect_stdout(io.StringIO()):
        if cmd_check() != 0:
            return 1
    text = CHANGES.read_text(encoding="utf-8")
    ur = unreleased(text)
    if ur is None or not ur[0]:
        print("nothing to release: CHANGES.md has no [Unreleased] entries",
              file=sys.stderr)
        return 1
    entries, markers = ur
    if level == "auto":
        level = markers.get("release") or "patch"
    if level not in LEVELS:
        print(f"unknown level {level!r}; use one of {LEVELS} or auto",
              file=sys.stderr)
        return 2
    old = current_version()
    new = next_version(old, level)

    # CHANGES.md: the [Unreleased] section becomes the new version's, markers
    # folded into its heading (tagline) or dropped (level).
    heading = f"## [{new}]"
    if markers.get("title"):
        heading += f" — {markers['title']}"
    for label, _tag, head, _start, end in sections(text):
        if label == "Unreleased":
            text = text[:head] + heading + "\n" + entries + "\n\n" + text[end:]
            break
    CHANGES.write_text(text, encoding="utf-8")

    PYPROJECT.write_text(PYPROJECT_RE.sub(f'version = "{new}"',
                                          PYPROJECT.read_text(encoding="utf-8"),
                                          count=1), encoding="utf-8")
    for path in (SERVER_PY, INIT_PY):
        path.write_text(SERVER_PY_RE.sub(f'__version__ = "{new}"',
                                         path.read_text(encoding="utf-8"),
                                         count=1), encoding="utf-8")
    # The JSON files are edited in place rather than re-serialized, so their
    # formatting (and anything a re-dump would normalize) stays untouched;
    # cmd_check below confirms every field landed.
    json_version = re.compile(r'("version":\s*")' + re.escape(old) + '"')
    for path in (MANIFEST, SERVER_JSON):
        path.write_text(json_version.sub(rf'\g<1>{new}"',
                                         path.read_text(encoding="utf-8")),
                        encoding="utf-8")

    with contextlib.redirect_stdout(io.StringIO()):
        if cmd_check() != 0:  # the bump itself must leave everything consistent
            return 1
    print(new)
    return 0


def _section(version: str) -> tuple[str | None, str]:
    text = CHANGES.read_text(encoding="utf-8")
    for label, tagline, _head, start, end in sections(text):
        if label == version:
            return tagline, MARKER_RE.sub("", text[start:end]).strip()
    raise SystemExit(f"CHANGES.md has no [{version}] section")


def cmd_notes(version: str) -> int:
    _tag, body = _section(version)
    print(body)
    print("\n**Install:** `uvx steam-mcp` · `pip install -U steam-mcp` · or the "
          "`.mcpb` desktop extension below.")
    return 0


def cmd_title(version: str) -> int:
    tagline, _body = _section(version)
    print(f"v{version} — {tagline}" if tagline else f"v{version}")
    return 0


def cmd_pending() -> int:
    ur = unreleased(CHANGES.read_text(encoding="utf-8"))
    has = ur is not None and bool(ur[0])
    print("pending" if has else "nothing to release")
    return 0 if has else 1


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__, file=sys.stderr)
        return 2
    cmd, args = argv[1], argv[2:]
    if cmd == "check" and not args:
        return cmd_check()
    if cmd == "pending" and not args:
        return cmd_pending()
    if cmd == "bump" and len(args) <= 1:
        return cmd_bump(*args)
    if cmd in ("notes", "title") and len(args) == 1:
        return (cmd_notes if cmd == "notes" else cmd_title)(args[0])
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
