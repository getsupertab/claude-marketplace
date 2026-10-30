# claude-marketplace

Supertab's plugin marketplace for Claude Code. Marketplace name: `supertab`.

## Plugins

| Plugin | Install id | What it does |
|---|---|---|
| [keeptabs](plugins/keeptabs/README.md) | `keeptabs@supertab` | Shows what Claude Code is spending, stops it at limits you set, and gives the agent the numbers it needs to stay inside a budget. Proof of concept. |
| [TokenTab](plugins/tokentab/README.md) | `tokentab@supertab` | keeptabs rewritten around a Claude Code hooks module (Claude Code 2.1.287 or later): every API call in a ledger from the first prompt, no telemetry setup. Being tested beside keeptabs; install one or the other. |

Each plugin's README covers what it needs after install and how to remove it cleanly.

## Add the marketplace

The repo is public, so no git credentials are needed. In Claude Code:

```
/plugin marketplace add getsupertab/claude-marketplace
/plugin install <plugin>@supertab
```

or from a shell: `claude plugin marketplace add getsupertab/claude-marketplace`, then
`claude plugin install <plugin>@supertab`. Each plugin's README covers what
happens after install.

Claude Code v2.1.275 or later can do both in one step, inside a session:
`/plugin install <plugin> --marketplace getsupertab/claude-marketplace`.

## Updates

Plugins here set no `version`, so every commit to `main` is a new version, and the
version string is the commit sha (`~/.claude/plugins/cache/supertab/<plugin>/<sha>/`).
Reinstalling the same commit lands in the same folder; it is not a way to get a fresh
state (uninstalling deletes a plugin's data, see Remove). To pull a new version:

```bash
claude plugin marketplace update supertab
```

```bash
claude plugin update <plugin>@supertab
```

The marketplace update alone already reinstalls plugins at the new commit; the second
command confirms it. Then start a new session (or run `/reload-plugins`).

Automatic updates are off by default for custom marketplaces. To turn them on: `/plugin`
→ Marketplaces → supertab → Enable auto-update. Claude Code then checks
at startup, and an update applies from the next session. keeptabs' `/keeptabs:setup`
turns this on for you.

**Not from the Claude Desktop app.** Desktop starts its bundled `claude` with
`DISABLE_AUTOUPDATER=1` (the app updates that binary itself), and Claude Code skips
plugin and marketplace auto-update behind the same flag (`Plugin autoupdate: skipped
(auto-updater disabled)` in `~/.claude/debug/`). Auto-update only runs in sessions
started from the terminal CLI. From Desktop, pull updates by hand with the two commands
above.

## Remove

```bash
claude plugin uninstall <plugin>@supertab
```

```bash
claude plugin marketplace remove supertab
```

Some plugins change things outside Claude Code's plugin folders; their READMEs say how
to undo that. Do it before uninstalling. For keeptabs, run `/keeptabs:uninstall` first.

## Adding a plugin

1. Put it in `plugins/<name>/`, with a `.claude-plugin/plugin.json` manifest and its
   components (`skills/`, `hooks/hooks.json`, `commands/`, `agents/`, scripts referenced
   via `${CLAUDE_PLUGIN_ROOT}`). Keep everything plugin-specific, including its README,
   inside that folder.
2. Add an entry to `plugins` in `.claude-plugin/marketplace.json`:
   `{"name": "<name>", "source": "./plugins/<name>", "description": "..."}`.
3. Add a row to the table above.
4. Check it: `claude plugin validate .`

Data a plugin must keep across updates belongs in `${CLAUDE_PLUGIN_DATA}`, or in its own
folder under the user's home. Never inside the plugin folder, which is replaced on every
update.
