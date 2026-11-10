# TokenTab: see what Claude Code is spending

**Ever wondered why one prompt cost so much more than the last?** So did we. TokenTab is
a free Claude Code plugin that prices every API call as it happens and shows you where
the money went, tool by tool. Nothing leaves your machine.

```
/plugin install tokentab --marketplace getsupertab/claude-marketplace
```

Start a new session (or run `/reload-plugins`) and it's counting from your first prompt.
Full steps are under [Install](#install).

[Privacy](https://github.com/getsupertab/claude-marketplace/tree/main/plugins/tokentab#your-data-stays-yours):
TokenTab collects nothing and sends nothing; what it reads and writes on your machine is
under [Your data stays yours](#your-data-stays-yours).

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
- **Heads-ups that save money.** A warm cache while you step away, a handoff note
  written before it can expire, a warning before an expensive resend after it goes cold, and a nudge to start fresh once a new session would be cheaper than carrying
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
a flow diagram of where the money went, cost over time, the cost by rate,
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
  and asks above the prompt box before sending, with what it will cost: **Continue**
  sends it for you (shown as sent through TokenTab). Where nothing can be shown (`claude -p`), it blocks once; sending again within two minutes goes through. Nothing has been sent at that point, so holding it back costs nothing.
  Turn it off by removing `cold_resume`.
- **Keeping the cache warm.** When a long session sits idle between turns, TokenTab
  sends one cheap request shortly before its cache would expire, which re-reads the
  cached context and restarts its clock. The first of these asks Claude for a handoff
  note, saved as `handoff/<session>.md` in the data folder; you get a notification. The
  pings stop once they have cost half of one re-write, or after 8 hours. The session must
  stay open and the computer awake. Turn it off, or the note, on the pane's Budget tab
  (KEEP CACHE WARM), or tune it under `keep_warm`.
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
  the budget guard. The transcript is only read, never copied. It reads no credential,
  login or account file.
- **What it runs:** Python 3, to work out costs and draw the views; `open`, `xdg-open` or
  `explorer.exe` when you choose "Open in browser" or the share page; and `uname`, to tell
  macOS from Linux. Nothing else.
- **Where it writes:** only `plugins/data/tokentab-supertab/` in your Claude config
  folder. Uninstalling leaves that folder; delete it to remove the data.
- **It's open source** (Apache 2.0), so you don't have to take our word for any of this.

### What the hooks module does, call by call

For a reviewer reading `hooks/register.tsx`, each kind of call it makes and why:

- **Programs it starts (`$.process.run`).** Only these, with these arguments:
  - `python3`, `python` or `py -3` with `-c "import sys; print(sys.version_info[0])"`,
    once, to find a Python 3; the launcher found is kept in the plugin's store.
  - That Python with one of the plugin's own files, `tokentab/tokentab.py` (costs, the
    pane's figures, the status page) or `tokentab/budget.py` (a limit you change in the
    Budget tab), and local paths and values as arguments.
  - `uname -s`, to tell macOS from Linux.
  - `open` (macOS), `xdg-open` (Linux) or `explorer.exe` (Windows) with the path of a
    local HTML file, only when you choose "Open in browser" or the share page.

  None of these programs is sent anything but those arguments, and none of TokenTab's
  Python opens a network connection.
- **Files it writes (`$.fs.write`).** Only under `plugins/data/tokentab-supertab/` in your
  Claude config folder:
  - `ledger/<day>/<session>.jsonl`: one line per API call (the numbers listed above).
    The budget guard (`tokentab/guard.py`, run by the plugin's `PreToolUse` and
    `UserPromptSubmit` hooks) reads these files and warns or stops on what they add up to;
    nothing else runs or obeys them.
  - `budget.json`: your limits, copied from the plugin's default on first run.
  - `state/loaded/<session>`, `state/windows/<session>.json` and
    `state/rate_limits.json`: that the module is loaded, and your plan's usage-window
    percentages, for the guard.
  - `status/<session>.html` and `.js`, and the share page: the pages "Open in browser"
    shows.
- **What it reads.** The files above, `prices.json` and `budget.json`, the API usage
  records (see the telemetry hook below). No credential, login or account file.
  Environment variables: `CLAUDE_CONFIG_DIR`, `HOME`, `USERPROFILE`, `OS`,
  to find the config folder and the platform.
- **What it sends out.** Nothing. The values it reads go into the local files above, the
  pane, the status line and, for `/tokentab:status`, the command's answer in your session.
  Two web addresses appear in the code, and neither is a way out:
  - `http://www.w3.org/2000/svg` is the SVG namespace in the pane's bars and the share
    card (`barSvg`, `shareCardSvg`). It is an identifier, never fetched; an SVG drawn as an
    image does not render without it.
  - `https://x.com/intent/post` and `https://www.linkedin.com/feed/` are the share page's
    "Share on X" and "Share on LinkedIn" links (`shareLinks`, asserted in
    `register.test.tsx`). Your browser opens one only when you click it, with the card's
    text and nothing else.

  The environment variables it reads are folder paths and the platform name, never a key
  or token.
- **Configuration it sets (`$.config.set`).** Only `tokentab.autoOpen`, its own option,
  when you flip "Open at session start" in the Budget tab. It sets no environment variable
  and no other setting, and it does not hook `config.set`.
- **Events it hooks.** `session.start` and `session.attach` (open the pane),
  `ui.render` and `ui.press` (its own pane), `command.run` for `/tokentab:status` only
  (answers it in place of the skill's text), `prompt.submit` (opens a pane the start left
  undrawn; always passes the prompt on), `session.measure` (the usage windows) and
  `telemetry.log` (below). Every hook passes its event on, and none takes a permission
  decision.

### The telemetry hook

To see every API call, TokenTab hooks Claude Code's `telemetry.log` event with the
matcher `{ to: 'collector' }` (`hooks/register.tsx`). That hook sees every record
addressed to the telemetry collector, with or without telemetry switched on. This is
what it does with them:

- **It passes every record on first, unchanged.** It never drops, delays, edits or
  denies one, and a failure in TokenTab never touches the record.
- **It reads only `api_request` records** and ignores every other kind.
- **From an `api_request` it keeps** the model, the four token counts, Claude Code's
  `cost_usd`, `query_source`, `request_id`, `duration_ms` and the session id, prices
  them, and appends one line to the local ledger (see below). Every other attribute of
  the record is ignored and not stored.
- **It sends nothing anywhere.** The ledger stays in TokenTab's data folder.

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
