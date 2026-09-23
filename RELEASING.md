# Releasing

Releases are automatic. Nobody bumps versions, builds bundles, or drafts releases
by hand.

## Day to day

Every PR that changes shipped behaviour adds a line under `## [Unreleased]` at the
top of `CHANGES.md` (CI warns on a code change without one). Don't touch version
numbers.

```markdown
## [Unreleased]
<!-- release: minor -->
<!-- title: sturdier reviews, fairer rarity -->
- **Fixed: …**
```

Both markers are optional HTML comments (invisible on GitHub):

- `release:` sets the version bump: `patch` (default), `minor` for new tools,
  parameters or JSON fields, and `major` for anything that breaks the stability
  contract in README. When several PRs set it, keep the highest.
- `title:` is the release's tagline, giving `v1.17.0 — sturdier reviews, fairer rarity`.

## What happens

`.github/workflows/publish.yml` runs every day at 03:00 UTC (10pm US Central during daylight time, 9pm in winter). If `[Unreleased]` is
empty it does nothing. Otherwise it:

1. Runs `release.py check`, ruff, the tests and the token audit.
2. Runs `scripts/release.py bump`, which sets the new version in all five places
   (pyproject, manifest, both server.json fields, `__version__`) and renames the
   section.
3. Commits `X.Y.Z: release` to `main` and tags `vX.Y.Z`.
4. Builds the sdist, the wheel and `steam-mcp.mcpb`.
5. Publishes to PyPI.
6. Creates the GitHub Release, with the notes taken from CHANGES.md and the bundle
   attached.
7. Registers the PyPI package and the bundle (with its hash) with the MCP
   Registry.

## Shipping now

For a broken install, a security fix or a regression, go to Actions → Release →
Run workflow. It runs the same pipeline immediately. The `level` input can force
patch, minor or major.

## If a run fails

Re-run the failed jobs. The PyPI step skips files that are already uploaded, and
the release step updates an existing release instead of failing. If `prepare`
failed because `main` moved while it ran, just run it again.

## Checking locally

```bash
python scripts/release.py check     # the five version fields agree; CHANGES.md is well formed
python scripts/release.py pending   # anything waiting to ship?
```
