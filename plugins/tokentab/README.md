# TokenTab: see what Claude Code is spending

**Ever wondered why one prompt cost so much more than the last?** So did we. TokenTab is
a free Claude Code plugin that prices every API call as it happens and shows you where
the money went, tool by tool. Nothing leaves your machine.

```
/plugin install tokentab --marketplace getsupertab/claude-marketplace
```

Start a new session (or run `/reload-plugins`) and it's counting from your first prompt.
Full steps are under [Install](#install).

## Why we built it

It started over Slack. One of us noticed a Claude Code session had burned through far
more than usual, and nobody could say why. Context? Big files? Too many tool calls?
Something in the background? We all had guesses and no answers.

So we built a meter. Once we could see the numbers, a few things surprised us:

- **Going to get coffee can cost you.** The prompt cache expires. Come back after it has
  and your next message re-sends the whole conversation at the expensive rate. TokenTab
  catches that before it's sent and tells you what it'll cost, so you can decide.
- **The "suggested next prompt" isn't free.** After each turn Claude Code makes a
  background call to guess what you'll type next, and that call re-reads your context
  too. You never see it. TokenTab does.
- **Cheaper models aren't always cheaper.** We watched a Haiku helper make four times the
  calls Opus did on the same job, and still not finish.

And the one we didn't expect: **once Claude could see the numbers, it worked
differently.** TokenTab never tells Claude how to save money. It just shows it the bill.
With that in front of it, Claude started handing us commands to run ourselves instead of
churning through the output, and stopped writing essays when a short answer would do.

## "Doesn't `/usage` already do this?"

Sort of! We use both. `/usage` tells you how much is left in the tank. TokenTab tells you
what's using it.

| | `/usage` | TokenTab |
|---|:-:|:-:|
| How much of my plan is left | ✅ | ✅ (as percent budgets) |
| Cost in dollars, per session, day and month | ❌ | ✅ |
| Which tool or call cost what | ❌ | ✅ |
| Always on screen | ❌ you run it | ✅ |
| Warns or stops you *before* you spend | ❌ | ✅ |
| Claude can see the numbers | ❌ | ✅ |
| Limits you pick yourself | ❌ | ✅ |

If you're on Pro or Max and just don't want to hit the wall, `/usage` is honestly fine.
If you want to know *why* a session got expensive, or you're paying per token, that's
where TokenTab comes in.

## What you get

- **Every API call, priced.** Including helper agents, web search, prompt suggestions and
  Claude Code's own background calls: the ones you'd never think to count.
- **A live pane beside the chat** with this session's cost, or a status line under the
  prompt in narrow terminals.
- **The breakdown.** Cost by tool, and by type (input, cache writes, cache reads, output,
  thinking), plus today and this month across every session and project.
- **A picture of where the money went.** A flow diagram and cost over time, in your
  browser.
- **Plain answers.** Ask Claude "how much have I spent?" or "where did the money go?" and
  it answers from the same numbers.
- **Budgets, if you want them.** Session and daily limits that warn you, and can stop
  tool calls if you ask them to. On Pro or Max they count in percent of your plan's
  5-hour and weekly windows; on the API they count in dollars. Your account decides,
  even when you switch between them. Off until you turn them on.
- **Heads-ups that save money.** A warning before an expensive resend after the cache
  goes cold, and a nudge to start fresh once a new session would be cheaper than carrying
  on.
- **A card to share** what a session cost, if you're proud of it (or horrified).


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


**Requirements:** Claude Code 2.1.304 or later, and Python 3 (standard library only) on
macOS, Linux or Windows. It works in Claude Code (the terminal and the Desktop app's Code
tab), not in Claude chat. Older versions of Claude Code can't load TokenTab; it tells you
so on your first prompt.

There's nothing to set up: no settings, no restart. More detail on how costs are worked
out, how budgets behave and the threat model is in
[`tokentab/README.md`](tokentab/README.md).

## See what you've spent

**The pane.** A live view opens beside the chat when a session starts. In a terminal
narrower than 144 columns it waits for your first prompt instead, and the status line
under the prompt shows the session's and today's cost until then. `/tokentab:status`
opens it at any time. It refreshes after every API call and every 30 seconds.

The pane has two tabs:

- **Spend** (`s`): this session's cost, with a bar against its limit; on a percent
  budget it leads with the plan's usage windows. On Pro or Max, dollars are what the
  session would cost at API rates. Press `d` for details: cost by type (input, cache
  writes, cache reads, output, thinking), cost per tool, and every session today and
  this month. "Share" (`h`) shows the session as a card to copy, or as a page in the
  browser with an image to download and links to post it on X or LinkedIn.
- **Budget** (`b`): your limits, editable in place, and the switch that opens the pane
  at session start.

**In the browser.** "Open in browser" (`o`) opens a status page with everything the pane
shows, from the same report and in your plan's unit, with room for more. It leads with a
summary: the session against its limit, tokens, msgs, a request's average cost and the
context, plus a banner when you're over a limit or the cache has gone cold. Below that come
a flow diagram of where the money went, cost over time, the top tools, the cost by rate,
by call source and by model, this month's sessions, and every tool with its breakdown
(Bash → git → status). It keeps updating for 30 minutes; once it stops, the summary dims
and says so.

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

On a Pro or Max plan the limits count in percent of the plan's 5-hour and weekly usage
windows instead, 80% and 90% by default. TokenTab picks the unit from your account: percent
where Claude Code reports usage windows, dollars otherwise. It's not a setting: your
account decides.

### What it does with budgets off

The limits are off, but these checks run from the start. Each one is set in
`budget.json` (`/tokentab:budget` shows it):

- **Expired cache.** When a long session (100k tokens of context or more) has sat idle
  long enough for its prompt cache to expire, TokenTab holds back your next prompt once
  and tells you what re-sending it will cost. Send it again within two minutes and it
  goes through. Nothing has been sent at that point, so holding it back costs nothing.
  Turn it off by removing `cold_resume`.
- **Helpers can't start helpers.** A helper agent that tries to start or message another
  agent is stopped. Set `helpers_may_delegate` to `true` to allow it.
- **Notes to Claude.** When a session's context passes 200k and again 350k tokens, Claude
  is told how much each call now costs and mentions it in one line. From 150k tokens on,
  each prompt also carries a note of about 150 words asking Claude to suggest a new
  session if the prompt doesn't need the conversation so far. Turn them off with
  `context_warn_tokens: []` and `new_session.enabled: false`.
- **Tamper check.** If the usage record for a session shrinks, which doesn't happen in
  normal use, TokenTab stops the session and says why.

## Your data stays yours

We think you should be able to see what you're paying for, and that starts with us being
clear about what TokenTab does:

- **No account, no sign-up, no telemetry.** TokenTab sends nothing over the network and
  calls no outside service. The share page's X and LinkedIn links open those sites with
  the card's text only when you click them.
- **It records numbers, not content.** For each API call: the time, model, token counts,
  cost, session id and request id. Never your prompts, Claude's replies or your files.
- **What it reads:** the cost records Claude Code produces for every API call (it reads
  them and passes them on unchanged), and the session's transcript, to count tokens for
  the budget guard. The transcript is only read, never copied. From `.claude.json` it
  reads only the account and organization ids, so a usage-window reading is never applied
  to another login.
- **What it runs:** Python 3, to work out costs and draw the views; `open`, `xdg-open` or
  `explorer.exe` when you choose "Open in browser" or the share page; and `uname`, to tell
  macOS from Linux. Nothing else.
- **Where it writes:** only `plugins/data/tokentab-supertab/` in your Claude config
  folder. Uninstalling leaves that folder; delete it to remove the data.
- **It's open source** (Apache 2.0), so you don't have to take our word for any of this.

## Tell us what you think

TokenTab is early, and we'd love to hear from you:

- **Budgets:** do you care? Would you actually set one? What number? Or is just seeing
  the spend enough?
- **Your tricks for keeping sessions cheap.** We'll collect the good ones and share them.
- **Anything else,** good or bad.

Email us at [product@supertab.co](mailto:product@supertab.co) or
[open an issue](https://github.com/getsupertab/claude-marketplace/issues). And if you're
curious what Supertab does when it isn't building token meters, it's at
[supertab.co](https://supertab.co/).

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
| `hooks/tokens.ts` | The design tokens (colours and fonts) the pane and the browser status page share. |
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
