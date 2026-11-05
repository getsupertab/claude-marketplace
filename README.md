# Supertab's Claude Code plugins

Plugins from [Supertab](https://supertab.co) for Claude Code. Marketplace name:
`supertab`.

## TokenTab: see what Claude Code is spending

Ever wondered why one prompt cost so much more than the last? TokenTab prices every API
call as it happens and shows you where the money went, tool by tool, with optional
budgets that warn you before you overspend. Free, open source, and nothing leaves your
machine.

```
/plugin install tokentab --marketplace getsupertab/claude-marketplace
```

**[Read more about TokenTab →](plugins/tokentab/README.md)**

## All plugins

| Plugin | Install id | What it does |
|---|---|---|
| [TokenTab](plugins/tokentab/README.md) | `tokentab@supertab` | See what Claude Code is spending while you work, with a live cost pane and budget limits that warn or stop. Needs Claude Code 2.1.304 or later. |

Each plugin's README covers what it needs after install and how to remove it cleanly.

## Add the marketplace

The repo is public, so no git credentials are needed. In Claude Code:

```
/plugin marketplace add getsupertab/claude-marketplace
/plugin install <plugin>@supertab
```

or from a shell:

```bash
claude plugin marketplace add getsupertab/claude-marketplace
```

```bash
claude plugin install <plugin>@supertab
```

Claude Code v2.1.275 or later can do both in one step, inside a session:
`/plugin install <plugin> --marketplace getsupertab/claude-marketplace`.

## Updates

```bash
claude plugin marketplace update supertab
```

```bash
claude plugin update <plugin>@supertab
```

Then start a new session (or run `/reload-plugins`). Sessions already open keep the old
version until they end.

To update automatically: `/plugin` → Marketplaces → supertab → Enable auto-update.
Claude Code then checks at startup, and an update applies from the next session.

**Not from the Claude Desktop app.** Desktop starts its bundled `claude` with
`DISABLE_AUTOUPDATER=1` (the app updates that binary itself), and Claude Code skips
plugin and marketplace auto-update behind the same flag (`Plugin autoupdate: skipped
(auto-updater disabled)` in `~/.claude/debug/`). From Desktop, pull updates by hand with
the two commands above.

## Remove

```bash
claude plugin uninstall <plugin>@supertab
```

```bash
claude plugin marketplace remove supertab
```

Some plugins change things outside Claude Code's plugin folders; their READMEs say how
to undo that. Do it before uninstalling.

## Adding a plugin

1. Put it in `plugins/<name>/`, with a `.claude-plugin/plugin.json` manifest and its
   components (`skills/`, `hooks/hooks.json`, `commands/`, `agents/`, scripts referenced
   via `${CLAUDE_PLUGIN_ROOT}`). Keep everything plugin-specific, including its README,
   inside that folder.
2. Add an entry to `plugins` in `.claude-plugin/marketplace.json`:
   `{"name": "<name>", "source": "./plugins/<name>", "description": "..."}`.
3. Add a row to the table above.
4. Check it: `claude plugin validate .`
5. Give it a `version` in `plugin.json` and raise it with every change, or installed
   copies never update.

Data a plugin must keep across updates belongs in `${CLAUDE_PLUGIN_DATA}`, or in its own
folder under the user's home. Never inside the plugin folder, which is replaced on every
update.

See [AGENTS.md](AGENTS.md) for the edit-and-reload loop and the checks.
