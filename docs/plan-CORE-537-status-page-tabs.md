# Plan: keeptabs as a Claude Code mod, with the live view in a pane next to the chat

Ticket: [CORE-537](https://laterpay.atlassian.net/browse/CORE-537), "Add a UI component to
show usage in real time" (story, sprint Keeptabs, estimate S). First agreed between Jannes
and the project manager on 2026-10-14 as a tabbed web page; rewritten 2026-10-15 around a
Claude Code mod after the spike at the end of this document proved the mod route. Status:
**draft for review; phase 0 done 2026-10-15, phase 1 done 2026-10-16**. The earlier plan is summarised in the last section.

## In one paragraph

keeptabs becomes a plugin whose core is a **hooks module** (a "mod"): TypeScript that
Claude Code loads into the session and that sees every API call's usage in-process,
with or without telemetry. The module writes the ledger, draws the terminal live view in a
**pane docked next to the chat** in Claude Desktop and in the terminal, keeps the budget
guard in-process, and offers an options view where auto-open and the budget limits are
edited. The accounting, the terminal renderer and the graphics stay in Python and are run
by the module as helpers. The collector, the telemetry settings, `/keeptabs:setup`, the
restart, `health.py` and the INCOMPLETE state are removed: tracking is complete from the
first prompt of any session the plugin loads in. A mod cannot serve HTTP, so the status
page becomes a **snapshot file** opened in a browser for the Sankey, while the
cost-over-time chart moves into the Desktop pane as SVG. keeptabs has no users yet, so
there is no backwards compatibility to keep.

## Why

- The ticket asks for the live numbers next to the chat, on by default, on Desktop and if
  possible in the terminal. A pane does exactly that on both surfaces, per session, with
  no browser, no "is a page open" check and no HTTP server.
- The spike showed that `on('telemetry.log', { to: 'collector' })` delivers every
  `api_request` record in-process, helper calls included, even with telemetry switched off.
  That removes the whole setup story (settings edit, restart, health check, INCOMPLETE).
- The CEO likes the terminal live view as it is. The pane is measured in character cells
  of a monospace font on every surface, Desktop included, so the same text renders there
  unchanged.

## Verified API (Claude Code 2.1.295 types, read 2026-10-15)

From `types/claude-code.d.ts` as the plugin-authoring skill lays it out. The types are
written per build and are the reference; the blog post is not.

| Need | API | Notes |
|---|---|---|
| Every API call's usage | `on('telemetry.log', { to: 'collector' })`, `e.event === 'api_request'`, `e.attributes` | Attributes: `model`, `input_tokens`, `output_tokens`, `cache_read_tokens`, `cache_creation_tokens`, `query_source`, `request_id`, `cost_usd`, `session.id`. `next(e)` resolves `{ value }` or `{ deny }`; we never deny. Pinned `to`: a hook may rewrite a record, never redirect it. |
| Per step, with agent id | `on('turn.step', async function* ...)`, result `usage: TurnUsage \| null` (`ModelUsage & { model }`: `input_tokens`, `output_tokens`, `cache_read_input_tokens`, `cache_creation_input_tokens`), `toolUses`, `serverToolUses`, `agentId` | Misses the engine's helper calls (title, suggestions, search summariser); used only to tie tool uses to a step, not for totals. |
| Session totals from the engine | `$.session.usage()` → `{ startedAt, context: { tokens?, window, percent? }, rateLimits: [{ kind, percentUsed, resetsAt? }], cost?: { usd } }` | Free without `breakdown`. Called on `turn.complete`. The included-volume view for subscription users. |
| End of a turn | `on('turn.complete')`, `e.reason` (`answer`, `aborted`, `refusal`, `error`), result `{ text, usage? }` | Redraw point and the moment to refresh the Sankey snapshot. |
| Tool guard | `on('tool.call', hook)`, return `{ deny: string }` or `next(e)`; `e` has `tool`, `agentId` | Written with `.catch(($, e, next) => next.called ? next(e) : { deny })` so a failing guard still refuses. |
| Prompt guard | `on('prompt.submit')`, `e.text`, `e.origin`; result `{ text, context?: string[] }` or `{ drop }` | `context` is where the budget hints go, in place of today's `additionalContext`. |
| Pane | `$.ui.open({ id, title, focus?, rows?, columns? })` → `{ isPlaced }`; drawn by `on('ui.render', { component: 'Pane', requestId: id })`; props `title`, `isFocused`, `bodyColumns`, `placement: 'dock' \| 'inline'` | Unasked (session start) it is placed only from 144 terminal columns (110 once the person opened it); below that it waits undrawn and `isPlaced` is false. Asked (a command, a press) it is placed at any width. |
| Status line, toast | `$.ui.status(text \| undefined)`, `$.ui.toast(text, { timeoutMs })` | One status line per plugin: the fallback when the pane is not placed. |
| Text | `Text` props `color` (theme key such as `warning`, `error`, `success`, `subtle`, or a raw name or hex), `bold`, `dimColor`, `italic`, `underline`, `inverse`, `backgroundColor`, `wrap` | Everything the terminal view uses: DIM, BOLD, CYAN, YELLOW, RED, GREEN. |
| Elements per surface | terminal: `Box Text Button Input Select Link Code Markdown Raster Image`; desktop and vscode: the same but `Svg` instead of `Raster`/`Image`; mobile: no `Input`/`Select` | `const { Box, Text } = $.ui.resolve(e)`. |
| SVG on Desktop | `Svg { source, alt, width?, height?, isInteractive? }`, `source` at most 131,072 characters | Drawn as an image, or in a sandboxed frame when `isInteractive` (hover, `<title>` tooltips). Chart fits (about 6 KB); Sankey does not (about 430 KB). |
| Files | `$.fs.read(path)`, `$.fs.write(path, text)` (whole content, creates directories), `list`, `exists`, `stat` | **4 MiB limit per read or write; no append.** Absolute paths allowed. |
| Session values | `$.state.get/set(ref)` with a `types/index.d.ts` contract (`interface PluginState`), `atom`/`read`/`update` helpers; a write redraws readers | Survives hot reloads; the module's own variables do not. |
| Across sessions | `$.store.get/set/delete/keys`, JSON values | A JSON file of the plugin's own under `~/.claude`. |
| Time | `$.clock.now()`, `after`, `every(ms, fn)` → `{ cancel }`, `sleep` | |
| Helpers | `$.process.run(argv, { cwd, env, stdin, timeoutMs })` → `{ exitCode, stdout, stderr }` | 30 s default timeout, 4 MiB stdout cap. How the module runs the Python. |
| Commands | `$.command.register({ name, description })` in `session.start`; `on('command.run', { command })` → `{ text?, context? }` | `/keeptabs:status` becomes a command that opens the pane. |
| Settings | `PluginOptions` from the manifest's `userConfig` (second argument of `register`); `$.config.set` | Candidate for the auto-open switch; shows in the config menu and reloads the module on change. |
| Session | `$.session.id()`, `cwd()`, `model()`, `version()`; `session.start` has `surface` (`terminal`, `desktop`, `vscode`, `mobile`, or null) and `isInteractive` | |

Not in the API: HTTP serving, appending to a file, a DOM. `$.http.fetch` exists but is
not needed once the collector is gone.

## Architecture

### The module (`plugins/keeptabs/hooks/register.tsx`)

Three parts in one module, each a few hooks:

**Ledger.** `telemetry.log` for `api_request` records: price the tokens with the plugin's
`prices.json` (read once from `$.plugin.root`), keep the engine's own `cost_usd` beside
ours (today's `cost_usd_cc`) and the difference above token price as `fees_usd` (web
search), and append the record to the session's ledger file (format below). `turn.step`
adds the `agentId` and the step's tool uses to the record with the same request id when
one arrives, so the Sankey keeps its per-agent and per-tool attribution without the
transcript. `turn.complete` calls `$.session.usage()` and stores cost, context and rate
limits in `$.state` for the pane. Running totals (session cost, today's cost, last request,
requests count) live in `$.state`; the ledger file is the durable record.

**Guard.** `prompt.submit`: the job-budget prefix (`pizza:`, `$2:`, `nobudget:`), the
first-prompt estimate request, the budget hints as `context`, the new-session check.
`tool.call`: refusal at a hard stop or during a mid-request check-in, main loop and
subagents alike, written with the `.catch` guard pattern so a failing hook still refuses.
The guard's numbers come from `$.state` (this session) and the ledger (today, all
sessions), read through the Python accounting. Budget config stays in `budget.json`.

**Pane.** `session.start` opens the pane when auto-open is on (`$.ui.open`), registers
`/keeptabs:status`, and sets the status line. `ui.render` for the pane draws one of three
views chosen by a row of `Button`s at the top: **Live** (default), **Cost** (Desktop: the
chart as `Svg`; terminal: the figures as text), **Options**. Redraw on every ledger write
and on a 30 s clock for the time line; nothing polls the data folder. When the pane is not
placed (narrow terminal, no surface), `$.ui.status` shows one line: session cost, today's
cost, job budget left.

### What stays in Python (`plugins/keeptabs/keeptabs/`)

- `keeptabs.py`: all accounting (`report()`, dedup rules, prices, estimates per tool),
  the terminal renderer `render()`, and the standalone live loop for people who want it in
  a plain terminal. Called by the module as `python3 keeptabs.py --once --session <id>`
  (ANSI text for the Live view) and `--json` (numbers for the guard and the Cost view).
  The accounting test (`tests/test_accounting.py`) keeps passing unchanged: no number
  changes in this plan.
- `sankey.py`: the Sankey and the cost-over-time chart, as renderers only: `--chart-svg`
  prints the chart's SVG for the pane; `--snapshot <file>` writes a self-contained status
  page (header, KPI row, Sankey with its column toggles, chart) for a browser. All server
  code (SSE, `/status`, `/health`, body pushing, viewer counting) goes.
- `prices.json`, `budget.json` (the default copied once into the data folder).

Python is kept for the numbers because the correctness rules (dedup by request id,
resumed sessions, tail by byte offset, thinking re-filed from transcripts, per-tool
estimates) are tested there, and because the terminal look is `render()`. Rewriting them
in TypeScript buys nothing for this ticket.

The transcript reader in `keeptabs.py` stays for one job only: per-tool attribution
(sizes of tool results, thinking tokens), which the ledger does not have. It is no longer
a fallback source of totals, so the INCOMPLETE marker and "the larger of the two" rule
go.

### Live view: the terminal look, exactly

One renderer, two outputs. The module runs `render()` and converts the ANSI codes into
`Text` spans: DIM → `dimColor`, BOLD → `bold`, the four colours → `color` with the raw
names (`cyan`, `yellow`, `red`, `green`), each line a `Text` with `wrap: 'end'` inside a
`Box` column. Nothing is re-laid out, so the pane and the standalone terminal view cannot
drift apart. The last line, `ctrl-c to quit`, is left out. The renderer is about 62
columns; the pane's `bodyColumns` is passed as `--columns` so a narrow inline pane gets
the renderer's compact layout once that exists (later).

Colours: the four terminal colours as hex values (a standard terminal palette), not
theme keys and not colour names, so the pane looks like a terminal in Desktop's light mode
too and every surface draws the same thing. Budget warnings use the `warning` and `error`
theme keys so they follow the person's theme.

### Graphics now that nothing serves HTTP

- **Cost over time**: `sankey.py --chart-svg` (about 6 KB) drawn with `Svg` in the pane's
  Cost view on Desktop and VS Code, `isInteractive` for hover. In the terminal the Cost
  view shows the same series as a text table.
- **Sankey**: too big for `Svg` (430 KB against a 128 KB limit) and not drawable in the
  terminal. It lives in the **status snapshot**: `sankey.py --snapshot` writes
  `status/<session>.html` in the data folder, a self-contained page with a
  `<meta http-equiv="refresh" content="5">` so a browser keeps it current from a file
  URL. The module rewrites the snapshot after each `turn.complete` while it has been
  opened in the last 30 minutes (`$.store` remembers when). An "open in browser" `Link`
  (or `Button` running `open` through `$.process.run`) sits at the top of the pane on
  every view; `/keeptabs:status` opens the pane and mentions the link. In Claude Desktop
  the file can also be opened in the app's browser pane by Claude, on request, as today.
- Later: a Sankey diet (fewer nodes, no per-link paths for the long tail) to fit 128 KB so
  it can join the Desktop pane as a fourth view.

### Options view

- **Auto-open**: a `Button` toggle over the plugin's `userConfig` field `autoOpen`
  (default on). The field is set on an install screen, shows as a row in Claude Code's
  config menu, and the module writes it with `$.config.set({ key: 'keeptabs.autoOpen',
  value })`, which the types define as "as if the person did in the menu". A change
  reloads the module with the new `options`, so `session.start` runs again: it must be
  idempotent (`$.ui.open` on an open id only retitles; the command registration and the
  status line are re-done harmlessly). `$.store` is the fallback only if that reload
  turns out to disturb the pane in practice.
- **Budget**: `Input` fields over `budget.json`: session, daily and per-request limits in
  USD and tokens (empty means off), warn-at percent, hard stop, and the job settings (ask,
  ask-over amount, hard stop, the word scale). Shows the file path. Writes the whole file
  with `$.fs.write`, keeping the `_comment` keys and the key order, after validating types
  and ranges; an invalid value is shown in `error` colour under the field and not written.
  Changes apply from the next prompt. No `POST` endpoints and no origin checks: the pane
  is the session's own UI.
- Mobile has no `Input`: the Options view there shows the values and says to edit from
  a desktop or the terminal.

### Budget changes from the chat

`/keeptabs:budget <change>` stays a skill and gains model invocation, so "raise my daily
limit to 30" works in plain words, with the same show-the-diff-before-writing step. The
guard's rule that Claude never raises a limit unasked stays: the skill acts only on an
explicit request in the same prompt. Both the skill and the Options view go through the
same validation (the skill shells out to a small `budget.py set key value` so there is one
validator). An alternative, a `$.tool.register`ed `set_budget` tool the model calls, is
noted but not planned: the skill exists and is enough.

### Auto-open and surfaces

- **Claude Desktop**: `session.start` with `e.surface === 'desktop'` and auto-open on
  calls `$.ui.open({ id: 'keeptabs', title: 'keeptabs' })`. The pane docks beside the
  transcript. Per session by construction: two sessions side by side each show their own
  cost. No "is a page open" check, no viewer counting, no Claude involvement.
- **Terminal**: the same call. Unasked, the pane is placed from 144 columns; below that it
  waits undrawn, the status line carries one line of numbers, and `/keeptabs:status`
  (asked) places it at any width. The old plan's "no auto-open in the terminal because a
  browser tab is invasive" is moot: a pane in the same window is not.
- **VS Code**: as Desktop. **Mobile**: Live view only, read-only Options.
- `claude -p` runs: no pane, but the ledger and the guard work the same (the spike
  verified the hook fires there).

### Data folder and formats

`~/.claude/plugins/data/keeptabs-supertab/` (`${CLAUDE_PLUGIN_DATA}` for shell hooks and
skills). `$.plugin` offers only `name` and `root`, so the module builds the same path
from `$.env.get('HOME')`, in one function, matching the Python fallback. Kept across
updates, deleted on uninstall.

| Path | Written by | Format |
|---|---|---|
| `budget.json` | Options view, `/keeptabs:budget`; copied once from the plugin | As today, with `_comment` keys |
| `ledger/YYYY-MM-DD/<session-id>.jsonl` | the module | One JSON line per API request (below) |
| `state/jobs.json` | the module | Job budgets and request-cost history per session, replacing the guard's state files |
| `status/<session-id>.html` | `sankey.py --snapshot`, run by the module | Self-contained page, overwritten |
| `$.store` (engine-owned JSON) | the module | `autoOpen` fallback, `snapshotOpenedAt` per session |

Ledger line, the collector's record shape kept so `keeptabs.py` reads it with only a glob
change (`ledger/*.jsonl` → `ledger/*/*.jsonl`):

```json
{"ts": "2026-10-15T14:03:11.412Z", "source": "mod", "session_id": "…", "agent_id": null,
 "turn_id": "…", "model": "claude-fable-5-1",
 "tokens": {"input": 12, "output": 480, "cache_read": 71000, "cache_write": 2100},
 "cost_usd": 0.0412, "cost_usd_cc": 0.0512, "fees_usd": 0.01,
 "query_source": "main_loop", "request_id": "req_…", "tools": ["Bash", "Read"]}
```

One line per request: the `api_request` record arrives once per request, after the
response (the OTel stream had several lines per request; the reader's dedup by request id
stays as a safety). Per-session daily files keep every file far under the 4 MiB
read-and-write limit (a line is about 300 bytes; a session would need 14,000 requests in
a day). Appends are read-then-write, serialised through one promise chain in the module
as the probe does. If a file ever nears 4 MiB the module rolls to `<session>-2.jsonl`.

Gone: `raw/`, `state/collector.json`, `state/collector.pid`, the collector log, the
heartbeat.

**Why files and not SQLite, and when SQLite comes in (decided 2026-10-15).** The module
cannot use SQLite: it runs without Node or native bindings, and `$.fs` reads and writes
text only. Inserting a row would mean a `$.process.run` of Python per API request, where
the JSONL write is in-process and free; per-session files also avoid database locking
between sessions writing at once. JSONL stays the **source of truth**: one line per
request, append-only, human-readable, greppable, easy to check against the engine's own
figures. SQLite earns its place for queries across history (cost over time for a month,
per-project totals, spend per model over a quarter), where scanning hundreds of daily
files gets slow. When those views arrive, Python (whose standard library has `sqlite3`)
builds a **derived index**, `state/ledger.sqlite`, from the JSONL files on read, updates
it incrementally and can rebuild it from scratch at any time. The module never touches
it. To keep that switch painless, the ledger line keeps flat, stable keys and
`request_id` unique per line, the index's primary key. Not a phase: a note for the first
cross-history view.

### Migration: the delete list

The project has no users, so this is a cut, not a deprecation.

| Remove | Replaced by |
|---|---|
| `keeptabs/collector.py` | the module's `telemetry.log` hook |
| `keeptabs/health.py`, the INCOMPLETE state, "larger of the two" | nothing: tracking is complete when the plugin loads |
| `scripts/telemetry.py`, `skills/setup/`, the settings edit, the backup, the restart | nothing |
| `scripts/home.py` (collector lifecycle, lock, pid, hostname workaround) | a 20-line `scripts/first-run.py` that copies `budget.json` once, or the module's `session.start` |
| `scripts/session-start.sh` | the module's `session.start`; one shell hook stays on `UserPromptSubmit`, see "Version gate" |
| `skills/uninstall/` | the README: `claude plugin uninstall keeptabs@supertab` is the whole uninstall, nothing is left outside the plugin folders |
| `scripts/guard.sh`, `keeptabs/guard.py` (phase 2) | the module's `prompt.submit` and `tool.call` hooks, `state/jobs.json` |
| `sankey.py`: `body()` pushing, SSE, `/status`, `/health`, viewer counting, `--open`, `--port` | `--snapshot`, `--chart-svg` |
| `keeptabs.py`: transcript totals, INCOMPLETE marker, `health` import | transcripts for tool attribution only |
| `skills/status/SKILL.md` as a page opener | `/keeptabs:status` command registered by the module (the skill folder goes, or stays as a one-line pointer for discoverability) |
| README: Install step 2, "Full tracking", telemetry settings table row, collector sections, troubleshooting, the POC author's notes on `http.server` | one install step; a "What runs where" table: module, Python helpers, data folder |
| AGENTS.md: collector restart, side-by-side test collector | the dev loop below |
| `.claude-plugin/plugin.json` description "telemetry collector" | "budget guard and live cost view" |

Kept unchanged: `prices.json`, `budget.json` defaults, `tests/test_accounting.py`,
`skills/budget/`, the POC README's accounting sections.

### Developer loop

Two facts from the plugin-authoring reference that change AGENTS.md:

- A plugin installed from a **folder marketplace** with a relative `source` is read from
  the folder itself, not a cached copy: edit, `/reload-plugins`, done, in the terminal and
  in Desktop alike. Adding this working tree as a second marketplace
  (`claude plugin marketplace add <path to marketplace/>`) replaces the commit, update,
  update, new-session dance for day-to-day work. The GitHub marketplace stays the way
  others install.
- `claude --plugin-dir plugins/keeptabs` (or `CLAUDE_CODE_PLUGIN_DIRS` in the `env` block
  of `~/.claude/settings.json` for Desktop) hot-reloads the module on every save.

Checks: `claude plugin validate plugins/keeptabs` (reads the module as the engine will and
lists what it hooks and what would be refused), `tsc -p plugins/keeptabs` against the
types the engine lays in `.claude-plugin/types/`, `claude plugin test plugins/keeptabs`
for `hooks/*.test.ts`, and `python3 plugins/keeptabs/tests/test_accounting.py`.

## Phases

**Phase 0, done 2026-10-15 (branch `feature/mod-rewrite`): the ledger moved in-process.**
`hooks/hooks.json` has `"modules": ["./register.ts"]` beside the shell hooks; the module
writes `ledger/YYYY-MM-DD/<session-id>.jsonl` and the loaded marker and copies
`budget.json` on first run, nothing else. `keeptabs.py` reads the new glob and lost the
transcript totals, "larger of the two", INCOMPLETE and the health import. Deleted:
`collector.py`, `health.py`, `home.py`, `telemetry.py`, the setup and uninstall skills,
`session-start.sh`, sankey's server code. `register.test.ts` (three tests) and the
accounting test pass; `claude plugin validate` lists the module's two hooks and one env
read; a headless `claude -p` run wrote one priced line matching Claude Code's own cost to
the micro-dollar. What turned out differently from the text above:

- Checked on **2.1.292** (the Homebrew CLI), not 2.1.295: Desktop's 2.1.295 binary is its
  VM build and does not run on the host. 2.1.292 has `plugin validate` and `plugin test`.
- The ledger line has no `agent_id`, `turn_id` or `tools` yet (they come from `turn.step`
  in phase 1) and keeps the collector's `duration_ms` (the guard's cold-cache rule reads
  it) and `prices_verified`. The day folder is the module environment's local date.
- The module file is `register.ts`, not `.tsx`: no JSX before the pane.
- The version gate reads the data folder at the path the module builds from `HOME`, not
  `${CLAUDE_PLUGIN_DATA}`, so the two agree under `--plugin-dir` too (where the shell
  hooks get `keeptabs-inline`). `keeptabs.py`'s `data_dir()` keeps `CLAUDE_PLUGIN_DATA`
  first, then the same fixed path; the heartbeat-based folder search is gone.
- `claude plugin test` has no `$.fs` beneath the plugin and nothing answers
  `telemetry.log`, `session.id` or `session.start`: the test stands in for all of them
  with `on(...)` hooks (`{ value }` answers), and matches the plugin's files by path tail
  since the test's `$` has no `plugin.root`.
- `guard.py` still takes `max(transcript, ledger)` for the session total (it was "as it
  is" until phase 2) but no longer imports `health`, probes port 4318 or writes the
  INCOMPLETE message. `test_accounting.py` changed in two non-number places: the ledger
  fixture moved to `ledger/2026-10-13/fx.jsonl` and the collector launch case went.
- The TOTALS block lost "this project, all time": the ledger knows sessions, not
  projects. `skills/status` stays, writing the snapshot, until the pane command exists.
- `tsc -p plugins/keeptabs` needs the engine-laid types, which a `-p` run never writes;
  the check ran with a tsconfig outside the plugin pointing at the skill's
  `claude-code.d.ts`. `plugins/*/.claude-plugin/types/` is git-ignored for later.
- `validate` lists the `telemetry.log` hook as a gating hook with `.catch`: its handler
  is `next(e)`, so a failing module still passes the record on.

**Phase 1, done 2026-10-16 (branch `feature/mod-pane`): the pane.** ANSI-to-`Text`
conversion of `render()`; pane opened at `session.start` with auto-open on; status line
fallback; `/keeptabs-mod:status` as a registered command that opens the pane; redraw on
ledger writes and the 30 s clock; the "open in browser" button to a snapshot written on
demand. Verified under `claude -p` (no pane, one ledger line), in a wide and a narrow
terminal, and in Desktop with two sessions side by side. This is CORE-537's deliverable.
What turned out differently from the text above:

- Checked against the types **2.1.292** laid in `plugins/keeptabs-mod/.claude-plugin/types/`
  (the Desktop binary, 2.1.295, does not run on the host). Everything the plan relies
  on is there with the shapes in the API table: `$.ui.open`, `ui.render` for `Pane` with
  `title`, `isFocused`, `bodyColumns`, `placement`, `$.command.register` and
  `command.run`'s `{ text, context }`, `$.ui.status`, `$.ui.panes`, `Text`, `Box`,
  `Button`, `Link`. Two things the table does not say: a `-p` run places every pane
  (so the module gates the visuals on `session.start`'s `isInteractive` and `surface`
  rather than on the engine), and a remote surface draws a `Link` whose `href` is not
  `https:` as plain text, so the snapshot opens from a `Button` (hotkey `o`) that runs
  `sankey.py` and then `open`, on every surface.
- **`/keeptabs-mod:status` is not a registered command.** `$.command.register({ name })`
  makes a flat `/<name>`: no plugin prefix, no colon allowed in the name, and the same
  namespace as the built-ins, so `status` is shadowed by Claude Code's own `/status` and
  never shows in the typeahead (tried; `keeptabs` as the name worked). The
  `<plugin>:<name>` convention is the engine's for what a plugin ships as files (skills,
  markdown commands, agents). So the command stays the `status` skill, and the module's
  `command.run` hook matches `{ command: 'keeptabs-mod:status' }` and answers without
  `next`: the pane opens, the status line prints, the figures go to Claude as `context`,
  and the skill's prompt never runs. The skill's own text is the fallback where the module
  did not load (it says Claude Code 2.1.287 or later is needed). `validate` lists the
  matcher with the colon.
- **Desktop does not draw `Text` in character cells.** Its pane draws `Text` in a
  proportional font and collapses an empty `Text` to nothing, so the renderer's columns
  fell apart and the blank lines between blocks vanished (seen 2026-10-16 in two
  sessions side by side; the viewport is still reported in cells of the code font). The
  types offer no monospace `Text`; `Code` is monospace but plain; a `Client` surface
  module draws with the same elements. What the remote surfaces have is `Svg`, so on
  `desktop`, `vscode` and `mobile` the Live view is one `Svg` (`svgOf`): one `<text>` per
  line in a monospace face, a `<tspan>` per span, bold as the weight, dim and the four
  colours as classes, lines cut at 100 columns, about 10 KB. It has no ground of its
  own: the API carries no theme signal, so a `<style>` with `prefers-color-scheme`
  picks a light or a dark palette and the surface hands its scheme to the image, which
  is how the pane follows the app's theme (the plan's "hex values so it looks like a
  terminal in light mode too" was dropped for this: Jannes wanted the app's theme). The
  terminal keeps the spans, with a blank line drawn as one space so it keeps its row.
- The status line's numbers come from Python, `keeptabs.py --status`, one plain line
  from the same `report()` as the live view, not from `--json` and TypeScript: the
  snapshot JSON has no today's total or job budget. `report()` carries the job budget
  as numbers now (`R["job"]`); no number changed.
- One helper process per redraw, not two: the module runs `--once` while the pane is
  placed (and clears the status line) and `--status` while it is not. The render hook
  draws from the last helper output held in the module's own variables and
  `$.ui.invalidate` redraws it; a render that finds no lines (the pane placed after a
  status-only refresh, the terminal widened) asks for one refresh. Redraws in flight are
  coalesced, one more after the current. The module's variables start over on a hot
  reload, so a reload shows `loading...` until `session.start`'s refresh lands.
- Auto-open is a constant in the module, not the manifest's `userConfig` nor `$.store`:
  the Options view that would flip it is phase 3.
- `bodyColumns` is not passed: `keeptabs.py` has no `--columns` yet, and the narrow layout
  stays "later".
- The ledger line is as phase 0 left it (no `agent_id`, `turn_id`, `tools` from
  `turn.step`): the ledger code and its tests were kept as they were, by decision.
- Claude's own road to the numbers is a second skill, `skills/spend`, model-invocable
  (its description answers "what have I spent"); `status` is user-invoked only. The
  command hands Claude the same figures as `context` when the person runs it.
- The snapshot is `status/<session-id>.html` in the data folder, written on each press
  of the button; the refresh meta tag and the 30-minute regeneration rule are phase 4.
  `sankey.py`'s default output (`state/status.html`, the spend skill's) is unchanged.
- Tests: the harness has no pane, command, process or status line beneath the plugin
  either, so the test stands in for `ui.open`, `ui.panes`, `ui.status`, `ui.invalidate`,
  `ui.toast` and `process.run` with `on(...)` hooks, settles the
  module's unawaited redraws with `mock.clock`'s `settle()`, draws the pane through
  `$.ui.render` on `terminal` and `desktop` (a Text with no props comes back without a
  `props` key), and presses the button through `$.ui.press`. A test file may import the
  module's pure functions. `tsc` runs as `npx -y -p typescript tsc -p plugins/keeptabs-mod`
  against the laid types; there is no global `tsc`.
- A terminal of a known width for the behavioural checks came from `tmux` (installed for
  it); the Desktop check needed `CLAUDE_CODE_PLUGIN_DIRS` in `~/.claude/settings.json`
  and a restart, with the installed plugin disabled first so the module loads once.

**Phase 2 (one to two days): the guard in the module.** `prompt.submit` and `tool.call`
replace `guard.sh`/`guard.py`; job budgets and request history in `state/jobs.json`;
`$.session.usage()` on `turn.complete` feeds the pane's context, rate-limit windows and
the engine's own cost figure (shown beside ours; a difference is a bug to look at). The
guard's existing behaviour is the spec: same messages, same thresholds, hints as `context`.

**Phase 3 (one day): options and budget editing.** Options view with the auto-open toggle
and the budget form; `budget.py set` as the one validator; `/keeptabs:budget` becomes
model-invocable.

**Phase 4 (one day): graphics.** Chart as `Svg` in the Cost view on Desktop and VS Code,
text table in the terminal; `sankey.py --snapshot` with refresh and the 30-minute
regeneration rule. Later: Sankey diet to fit 128 KB; narrow layout for `render()`.

## Risks and how the plan handles them

- **Early-access API.** The types say the surface may change between releases without
  notice. Mitigation: the module touches a small set (listed above), each use sits in one
  function; `claude plugin validate` and `tsc` against the freshly written types are the
  first step after every Claude Code update; `dev/usage-probe/` is the regression check
  that `telemetry.log` still carries usage. Pin what we verified in this document, by
  version.
- **Version gate.** Mods load by default from Claude Code 2.1.287; before that only with
  `CLAUDE_CODE_ENABLE_FUNCTION_HOOKS=1` (seen on 2.1.280). `plugin.json` has no
  minimum-version field (validate on 2.1.295 reports one as unknown and ignored), and
  the classic hook input carries no version. So the gate is a loaded-marker check: the
  module's `session.start` writes `state/loaded/<session-id>` in the data folder; one
  classic `UserPromptSubmit` shell hook (a dozen lines, the last shell hook once the guard
  moves in) finds no marker for its session on the first prompt and returns a system
  message: keeptabs' module did not load, Claude Code 2.1.287 or later is needed, nothing
  is tracked. It catches any other load failure the same way. `UserPromptSubmit`, not
  `SessionStart`, because the order of a shell hook and the module's `session.start` at
  session start is not defined.
- **No append, 4 MiB.** Handled by per-session daily files and a serialised
  read-then-write chain. `$.fs.write` is not atomic: a crash mid-write could truncate a
  day's file for one session. Acceptable for a cost view; the module writes the new
  content to `<file>.tmp` first and then the real path, so at worst the `.tmp` holds the
  latest line.
- **Helper process per redraw.** Each Live redraw runs Python (about 100 to 200 ms). Redraws
  are event-driven (one per API request) plus one every 30 s, not every two seconds, and
  are coalesced while one is running. If it shows, the module caches the last `--json`
  and the renderer moves to a long-running `--serve-stdin` mode through `$.process.spawn`.
- **Unasked pane width floor.** In a terminal under 144 columns the pane waits undrawn.
  The status line covers it, and the plan says so in the README.
- **Surface differences.** `Svg` only on Desktop, VS Code and mobile; `Input` not on
  mobile. Each view checks `e.surface` and degrades to text.
- **Subagents spawned by a hook step past that hook.** Not our case (we spawn none), but
  worth a test when the guard moves in.
- **Desktop disables plugin auto-update.** Unchanged from today; the README keeps the
  manual update line. The folder-marketplace dev loop softens it for the team.
- **Data folder path from the module.** `$.plugin` has no data path; `$.env.get('HOME')`
  plus the known path, in one function to change when the API offers it.

## Decisions on the open questions (2026-10-15)

Checked against the 2.1.295 types and `claude plugin validate`:

- **Minimum version.** No manifest field exists; the shell hook stays, as a loaded-marker
  check on the first prompt (see Risks).
- **Auto-open storage.** `userConfig` field `autoOpen`, written by the module through
  `$.config.set` (install screen and config menu for free); the reload it causes is
  handled by an idempotent `session.start`. `$.store` only as fallback.
- **`hooks` and `modules` together.** Yes, one `hooks.json` holds both. Phase 0 can start
  without moving the shell hooks.
- **Data folder.** `$.plugin` gives `name` and `root` only; the module builds the data path
  from `HOME`.
- **Colours.** The types admit any string and say what a surface draws for an unknown one
  is its own, so colour names are not guaranteed to match across surfaces. Hex values for
  the four terminal colours; theme keys for warnings.
- **`status_html()` panels.** Dropped. They were the terminal figures as HTML panels; the
  Live view is those figures as the terminal draws them, and the snapshot is for the
  Sankey. The standalone `--json` keeps every number for anyone who wants them raw.

## What to tell Justin for CORE-537

- The ticket's own leaning, "automatically opening to the side on desktop, maybe
  something similar in the terminal, or a status bar", is exactly what a Claude Code mod
  pane does, and his pointer to the mods post was the right call. The spike on 2026-10-15
  verified it on 2.1.295.
- Scope change: the tabbed web page with Desktop browser auto-open (agreed 2026-10-14) is
  dropped in favour of the pane. The Sankey stays in a browser as a snapshot file; the
  chart moves into the pane on Desktop.
- The same spike removes the whole setup story (telemetry settings, restart, health
  check, INCOMPLETE). That is a second, bigger win for the sprint goal "submitted to the
  Claude Plugin Marketplace": install, new session, done.
- Estimate: S (under 8 hours) covered "open the existing page to the side". The work is
  now about five to seven days across phases 0 to 4. Suggest splitting: CORE-537 keeps
  phase 1 (the pane) with phase 0 as its prerequisite, estimate M to L; new stories for
  the guard in the module (phase 2), options and budget editing (phase 3), graphics
  (phase 4).
- New constraint to put in the ticket and the README: Claude Code 2.1.287 or later.
- Risk to state: the mod API is early access and may change; we have a one-command
  regression check and a small API footprint.

## Earlier plan and spike (history)

**2026-10-14 to 2026-10-15, the web-page plan (commit 9415b35).** One web page with tabs,
served by the collector: Live (an ANSI-to-HTML clone of `render()`), Flow (Sankey), Cost
over time, Options (auto-open switch, budget form via new `POST /options` and
`POST /budget` with loopback and `Origin` checks). Auto-open in Claude Desktop by having
the session-start hook tell Claude to open `/status?tab=live&session=<id>` in the app's
browser pane at its first reply; per-session viewer counting on the SSE stream so a page
in another session's pane would not suppress it; no auto-open in the terminal. Three
phases of about one day each plus one to two for options. Open questions then: whether
`target=_blank` leaves the Desktop pane, dark-always for the Live tab, who re-estimates the
ticket. All of it depended on the collector serving HTTP and on telemetry being set up.

**2026-10-15, the spike.** A probe mod (`dev/usage-probe/`, three files, validated with
`claude plugin validate`) ran in a Desktop session (2.1.295) and in headless `claude -p`
runs (2.1.292), logging every event that reports usage, and was compared with the OTel
ledger the collector wrote for the same period:

| Call type | OTel ledger | mod `telemetry.log` | mod `turn.step` |
|---|---|---|---|
| main-loop requests | yes | yes | yes |
| subagent requests | yes | yes | yes, with the agent id |
| prompt-suggestion helper call | yes | yes | no |
| web-search summariser (Haiku, ~14k input) | yes | yes | no |
| session-title generator (Haiku, headless) | n/a (telemetry off) | yes | no |

Every ledger record since the mod loaded also arrived in-process, matched by request id,
with model, the four token counts, query source, request id and the engine's `cost_usd`.
With telemetry switched off (`CLAUDE_CODE_ENABLE_TELEMETRY=0`, `OTEL_LOGS_EXPORTER=none`
via `--settings dev/usage-probe/notelemetry.json`) the hook still fired and the collector
received nothing. `turn.complete` plus `$.session.usage()` gave session cost, context size
and rate-limit windows. Homebrew 2.1.280 refused the module with the early-access message.
Regression check after a Claude Code update: set the log path at the top of
`dev/usage-probe/hooks/register.ts`, then

```bash
claude -p "Reply with the single word ok" --model haiku --settings dev/usage-probe/notelemetry.json --plugin-dir dev/usage-probe
```

and read the log.
