# Publishing Steam MCP

This guide walks through publishing the server to all four channels:

1. **GitHub** — source of truth
2. **PyPI** — so people can `pip install` / `uvx`
3. **MCP Registry** — cross-client discovery (`registry.modelcontextprotocol.io`)
4. **`.mcpb` Desktop Extension** — one-click install in Claude Desktop

Do them roughly in this order, because the registry depends on PyPI, and the
`.mcpb` release depends on GitHub.

---

## Step 0 — Pre-flight: replace the placeholder

Every `your-username` must become your real **GitHub username**. It appears in:

`manifest.json`, `pyproject.toml`, `server.json`, `README.md`, `PRIVACY.md`.

Find-and-replace `your-username` across the repo, then double-check `README.md`'s
top line is exactly:

```
<!-- mcp-name: io.github.<your-username>/steam-mcp -->
```

This marker is how the registry proves you own the PyPI package, so it **must**
match the `name` in `server.json`.

> If you change `manifest.json`, you must re-pack the `.mcpb` (Step 4) so the
> bundle reflects the new URLs.

---

## Step 1 — GitHub

```bash
cd steam-mcp
git init
git add .
git commit -m "Steam MCP v0.2.0"
git branch -M main
git remote add origin https://github.com/<your-username>/steam-mcp.git
git push -u origin main
```

`.gitignore` already keeps `.env`, `dist/`, and `*.mcpb` out of the repo — so your
API key is never committed. Confirm `.env` is **not** in `git status` before you push.

---

## Step 2 — PyPI

The MCP Registry hosts only metadata, so the package must live on PyPI first.

1. Make a [PyPI account](https://pypi.org/account/register/) and create an API
   token (Account Settings → API tokens).
2. Rebuild the artifacts (you edited the README in Step 0, so rebuild):

   ```bash
   python -m pip install --upgrade build twine
   python -m build            # writes dist/steam_mcp-0.2.0.tar.gz and .whl
   ```

3. (Optional but recommended) check, then upload:

   ```bash
   twine check dist/*
   twine upload dist/*
   ```

   Username: `__token__`  •  Password: your `pypi-...` token.

4. Verify: <https://pypi.org/project/steam-mcp/>. Confirm the rendered README
   still contains the `mcp-name:` comment (it's in the page source).

> **I can't run `twine upload` for you** — it requires entering your PyPI
> credentials, which I don't do. Run it yourself.

---

## Step 3 — MCP Registry

1. Install the publisher CLI:

   ```bash
   # macOS/Linux
   brew install mcp-publisher
   # or download the binary from:
   # https://github.com/modelcontextprotocol/registry/releases/latest
   ```

   Windows: download `mcp-publisher_windows_amd64.tar.gz` from the same releases
   page, extract `mcp-publisher.exe`, and put it on your `PATH`.

2. `server.json` is already written. Sanity-check the `name`, `version`, and
   `identifier` (`steam-mcp`) match what you published to PyPI.

3. Authenticate and publish:

   ```bash
   mcp-publisher login github
   mcp-publisher publish
   ```

   GitHub auth requires the name to start with `io.github.<your-username>/`,
   which it does.

4. Verify:

   ```bash
   curl "https://registry.modelcontextprotocol.io/v0.1/servers?search=steam-mcp"
   ```

> **Gotcha:** if publishing fails with *"Registry validation failed for
> package,"* the `mcp-name:` marker in your PyPI README is missing or doesn't
> match `server.json`'s `name`. Fix the README, rebuild, re-upload to PyPI, then
> re-run `mcp-publisher publish`.

---

## Step 4 — `.mcpb` Desktop Extension (one-click install)

1. If you edited `manifest.json` in Step 0, re-pack:

   ```bash
   npm install -g @anthropic-ai/mcpb     # once
   mcpb validate manifest.json
   mcpb pack . steam-mcp.mcpb
   ```

2. (Optional) self-sign for distribution:

   ```bash
   mcpb sign steam-mcp.mcpb --self-signed
   ```

3. Create a **GitHub Release** (tag `v0.2.0`) and attach `steam-mcp.mcpb`.
   Users install by downloading it and double-clicking, or via Claude Desktop →
   Settings → Extensions → *Install Extension…*. Claude Desktop prompts them for
   their Steam API key automatically (the `user_config` field).

4. (Optional) advertise the bundle in the registry too, by adding a second
   package to `server.json` and re-publishing:

   ```jsonc
   {
     "registryType": "mcpb",
     "identifier": "https://github.com/<your-username>/steam-mcp/releases/download/v0.2.0/steam-mcp.mcpb",
     "fileSha256": "<sha256 of the EXACT uploaded .mcpb>",
     "transport": { "type": "stdio" }
   }
   ```

   Compute the hash after your final pack:
   - macOS/Linux: `shasum -a 256 steam-mcp.mcpb`
   - Windows: `certutil -hashfile steam-mcp.mcpb SHA256`

---

## Step 5 — (Optional) Anthropic Connectors Directory

The in-product directory is for **remote, hosted** connectors and requires a
public privacy policy and review. This server is local + BYOK, so it doesn't fit
unless you host a remote version. If you ever do, submit at
<https://claude.com/docs/connectors/building/submission> (the `PRIVACY.md` here
covers the privacy-policy requirement).

---

## Heads-up before you ship

- You currently have a **manual `steam` entry** in `claude_desktop_config.json`
  from earlier setup. Once you install the `.mcpb`, remove that manual entry so
  you don't run two copies.
- Bump `version` in **all four** of `pyproject.toml`, `steam_mcp/__init__.py`,
  `manifest.json`, and `server.json` together on each release.
