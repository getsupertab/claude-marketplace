# TokenTab

See what Claude Code is spending while you work, and set limits so it doesn't run away
with your budget.

TokenTab records the cost of every API call Claude Code makes, including helper agents
and Claude Code's own background calls. It shows the running total beside the chat,
warns you when you near a limit, and can stop tool calls once you pass it. It also gives
Claude the numbers, so you can ask "what have I spent?" and get a straight answer.

There's nothing to set up: no telemetry, no settings, no restart.

**Requirements:** Claude Code 2.1.304 or later, and Python 3 (standard library only) on
macOS, Linux or Windows. Older versions of Claude Code can't load TokenTab; it tells you
so on your first prompt.

More detail on how costs are worked out, how budgets behave and the threat model is in
[`tokentab/README.md`](tokentab/README.md).

## Install

In a Claude Code session:

```
/plugin install tokentab --marketplace getsupertab/claude-marketplace
```

Confirm the source, then pick a scope ("Install for you" puts it in every project).
Start a new session, or run `/reload-plugins`, and TokenTab is tracking from your first
prompt.

From a shell instead:

```bash
claude plugin marketplace add getsupertab/claude-marketplace
claude plugin install tokentab@supertab
```

## See what you've spent

**The pane.** A live view opens beside the chat when a session starts. In a terminal
narrower than 144 columns it waits for your first prompt instead, and the status line
under the prompt shows the session's and today's cost until then. `/tokentab:status`
opens it at any time. It refreshes after every API call and every 30 seconds.

The pane has two tabs:

- **Spend** (`s`): this session's cost, with a bar against its limit. Press `d` for
  details: cost by type (input, cache writes, cache reads, output, thinking), cost per
  tool, and every session today and this month.
- **Budget** (`b`): your limits, editable in place, and the switch that opens the pane
  at session start.

**In the browser.** "Open in browser" (`o`) opens a status page with a flow diagram of
where the money went, cost over time, and every figure TokenTab tracks. It keeps
updating for 30 minutes; the dot in its header says whether it's `live`, `stale` or a
`snapshot`.

**Ask Claude.** "How much have I spent?" or "Where did the money go?" gets an answer
from the same numbers.

**In your own terminal:**

```bash
python3 ~/.claude/plugins/cache/supertab/tokentab/*/tokentab/tokentab.py
```

Add `--once` for a single snapshot or `--json` for the raw numbers.

To stop the pane opening on its own, switch it off on the Budget tab, or in a terminal
session set `tokentab.autoOpen` in `/config`.

## Budgets

Budgets are off until you turn them on, with `/tokentab:budget` or on the Budget tab.
You can also just ask: "raise my daily limit to 30", "hard stop on".

Once on, the defaults are $25 per session and $40 per day. TokenTab warns at 70% and at
100% of a limit. It only warns unless you switch on `hard_stop`, which blocks tool calls
past the limit.

With budgets off, the one thing TokenTab still stops is a prompt sent after the prompt
cache has expired, since that prompt pays to rebuild the whole context.

## Update

```bash
claude plugin marketplace update supertab
claude plugin update tokentab@supertab
```

Then start a new session; sessions already open keep the old version until they end.
The Claude Desktop app never updates plugins on its own, so run these by hand there.

## Uninstall

```bash
claude plugin uninstall tokentab@supertab
```

TokenTab changes nothing outside Claude Code's plugin folders. Your data (the cost
ledger and your limits) stays in `plugins/data/tokentab-supertab/` inside your Claude
config folder (`$CLAUDE_CONFIG_DIR`, or `~/.claude`). Delete that folder to remove it
too.

## How it works

| Part | What it does |
|---|---|
| `hooks/register.tsx` | Runs inside Claude Code. Sees every API request, prices it and writes one line to the ledger. Draws the pane and the status line. |
| `scripts/guard.sh`, `tokentab/guard.py` | The budget guard. Runs before every prompt and tool call, warns or stops, and tells Claude how much budget is left. |
| `scripts/loaded-check.sh` | Tells you once, on your first prompt, if your Claude Code is too old to load TokenTab. |
| `tokentab/tokentab.py`, `tokentab/sankey.py` | The accounting, the terminal view, and the browser status page. |
| `tokentab/budget.py` | Checks and saves a changed limit. |
| `skills/` | `/tokentab:status`, `/tokentab:budget`, and the spend answers Claude gives. |

Everything runs from the plugin folder and writes only to its data folder:

| In `plugins/data/tokentab-supertab/` | |
|---|---|
| `budget.json` | Your limits. |
| `ledger/<day>/<session>.jsonl` | One line per API request. |
| `state/` | The guard's working state. |
| `status/<session>.html` | The browser status page. |

A ledger line:

```json
{"ts": "2026-10-15T15:39:50.069Z", "source": "mod", "session_id": "…", "model": "claude-fable-5-1",
 "tokens": {"input": 10, "output": 40, "cache_read": 21099, "cache_write": 10364},
 "cost_usd": 0.023048, "cost_usd_cc": 0.0230479, "fees_usd": 0, "query_source": "sdk",
 "request_id": "req_…", "prices_verified": true, "duration_ms": 1599}
```

`cost_usd` is the token cost at TokenTab's prices. `cost_usd_cc` is Claude Code's own
figure; anything it charges beyond tokens, such as web searches, is in `fees_usd`.

## Development

See [AGENTS.md](../../AGENTS.md) for the edit-and-reload loop and the checks.
