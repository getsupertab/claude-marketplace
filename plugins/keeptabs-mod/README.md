# keeptabs-mod

keeptabs rewritten around a Claude Code hooks module, being tested beside the original
`keeptabs` plugin. Supertab's proof of concept that shows what Claude Code is spending, stops it at limits
you set, and gives the agent the numbers it needs to stay inside a budget. Packaged as
a plugin in [claude-marketplace](../../README.md). The POC's own documentation (how the
numbers are worked out, budgets, the threat model) is in
[`keeptabs/README.md`](keeptabs/README.md).

Needs **Claude Code 2.1.287 or later**: keeptabs' core is a hooks module, which older
versions do not load (the first prompt then says so, and nothing is tracked). macOS,
Python 3 (standard library only).

## Install

The repo is private: you need access and working git credentials, see the
[marketplace README](../../README.md). Inside a session:

```
/plugin install keeptabs-mod --marketplace getsupertab/claude-marketplace
```

Claude Code asks you to confirm the source, then to pick a scope (choose "Install for
you" to have it in every project). If you have the original `keeptabs` installed, switch
it off first, or both guards run on every prompt:

```bash
claude plugin disable keeptabs@supertab
```

That is the whole install: no setup, no settings
change, no restart. **Start a new session** (or run `/reload-plugins`) and keeptabs is
working from its first prompt:

- every API call is in the ledger, helpers and Claude Code's own background calls
  included, whether or not telemetry is switched on;
- the budget guard runs on every prompt and tool call.

Set your own limits with `/keeptabs-mod:budget`. The defaults are the POC author's limits
(warn-only, $25 per session, $40 per day, $2 per request, and a job budget check-in at
$1). See Budgets in the POC README.

In two steps instead (`/plugin marketplace add getsupertab/claude-marketplace`, then
`/plugin install keeptabs-mod@supertab`), or from a shell: `claude plugin marketplace add
getsupertab/claude-marketplace` and `claude plugin install keeptabs-mod@supertab`.

## See the numbers

The live view opens in a **pane beside the chat** when a session starts: in Claude
Desktop and in a terminal of 144 columns or more (110 once you have opened it yourself).
In a narrower terminal the pane waits and keeptabs' **status line** under the prompt
shows session cost, today's cost and what is left of the job budget; `/keeptabs-mod:status`
opens the pane at any width. The pane is the terminal live view, redrawn after every
API call and every 30 seconds. Its "open in browser" button writes the status page
(where the money went as a flow diagram, cost over time, and every figure keeptabs
tracks) as a snapshot file and opens it. Ask Claude what you have spent and it answers
from the same figures. In a terminal of your own:

```bash
python3 ~/.claude/plugins/cache/supertab/keeptabs-mod/*/keeptabs/keeptabs.py
```

is the live view (`--once` for one snapshot, `--json` for the raw numbers), and
`sankey.py` next to it writes the status page and prints its path.

## Updates

Update as described in the [marketplace README](../../README.md) (from the Claude
Desktop app always by hand: `claude plugin marketplace update supertab`, since Desktop
disables auto-update), then start a new session. Sessions already running keep the old
version until they end.

## Uninstall

```bash
claude plugin uninstall keeptabs-mod@supertab
```

That is all of it: keeptabs changes nothing outside Claude Code's plugin folders. Its
data (ledger, guard state and your `budget.json`) stays in
`~/.claude/plugins/data/keeptabs-mod-supertab/`; delete that folder to remove it too.

## What runs where

| Part | Where it runs | What it does |
|---|---|---|
| `hooks/register.tsx`, the hooks module | Inside Claude Code, loaded from `hooks/hooks.json` (`modules`) | Sees every API request in-process (`telemetry.log`), prices it from `keeptabs/prices.json` and appends one line to the session's ledger file. At session start it writes the loaded marker, on first run copies `budget.json` into the data folder and opens the pane; answers `/keeptabs-mod:status` by opening it. Draws the pane (the live view, run as `keeptabs.py --once`) or sets the status line (`keeptabs.py --status`); the "open in browser" button runs `sankey.py` and opens the file |
| `scripts/guard.sh` → `keeptabs/guard.py` | Shell hooks on `UserPromptSubmit` and `PreToolUse` | The budget guard: reads the ledger and the session's transcript, warns or stops, gives Claude the budget hints |
| `scripts/loaded-check.sh` | Shell hook on `UserPromptSubmit` | The version gate: says once, on the first prompt, when the module did not load |
| `keeptabs/keeptabs.py`, `keeptabs/sankey.py` | Python, run by the hooks module, the spend skill or by hand | The accounting, the terminal live view (`--once`) and the status line (`--status`); the status page as a snapshot file |
| `skills/status` | `/keeptabs-mod:status`, answered by the hooks module | Opens the pane and hands Claude the figures; the skill's own text runs only where the module did not load |
| `skills/spend`, `skills/budget` | Claude, when asked what was spent; `/keeptabs-mod:budget` | Answer in words from `keeptabs.py --once`; show or change the limits |
| Data: `budget.json`, `ledger/<day>/<session>.jsonl`, `state/`, `status/<session>.html` | `~/.claude/plugins/data/keeptabs-mod-supertab/`, one fixed path, separate from keeptabs' folder | Kept across updates; the status page is rewritten on each "open in browser" |

Code runs from the plugin folder, `${CLAUDE_PLUGIN_ROOT}`
(`~/.claude/plugins/cache/supertab/keeptabs-mod/<version>/`, the version being the git
commit sha). Nothing is written anywhere else.

The ledger line, one per API request:

```json
{"ts": "2026-10-15T15:39:50.069Z", "source": "mod", "session_id": "…", "model": "claude-fable-5-1",
 "tokens": {"input": 10, "output": 40, "cache_read": 21099, "cache_write": 10364},
 "cost_usd": 0.023048, "cost_usd_cc": 0.0230479, "fees_usd": 0, "query_source": "sdk",
 "request_id": "req_…", "prices_verified": true, "duration_ms": 1599}
```

`cost_usd` is the token price at keeptabs' rates; `cost_usd_cc` is Claude Code's own
figure, and what it has on top of the token price is `fees_usd` (web search is billed
per search).

## Development

See [AGENTS.md](../../AGENTS.md) for the edit-and-reload loop and the checks.

## How the POC is packaged

`keeptabs/` holds what is left of the POC (`guard.py`, `keeptabs.py`, `sankey.py`,
`budget.json`, `prices.json`, `README.md`) and runs in place, from the plugin folder.
The POC's `collector.py`, `health.py` and `setup.py` are replaced by the hooks module;
`gate/` is left out. Each change to the POC files is marked `# plugin: ...`: data goes
to the plugin's data folder (`~/.claude/plugins/data/keeptabs-mod-supertab`, the path the
module builds from `HOME`), `prices.json` comes from the script's own folder, and messages point to
`/keeptabs-mod:budget`. The plugin copies only `budget.json` into the data folder, once, as
your starting limits.

To see the whole patch (from the parent project):

```bash
diff -ru eric-poc marketplace/plugins/keeptabs-mod-mod/keeptabs -x gate -x setup.py -x collector.py -x health.py
```
