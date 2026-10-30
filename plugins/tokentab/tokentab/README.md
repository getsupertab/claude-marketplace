# How TokenTab works

The details behind the [TokenTab README](../README.md): where the numbers come from,
what is exact and what is estimated, every budget setting, and what TokenTab can and
can't enforce.

The files in this folder are plain Python 3, standard library only:

| File | What it does |
|---|---|
| `tokentab.py` | The accounting, and the terminal view of spend |
| `sankey.py` | The browser status page and its flow diagram |
| `guard.py` | The budget guard, run before every prompt and tool call |
| `budget.py` | Checks and saves a changed limit |
| `budget.json` | The starting limits, copied to your data folder on first run |
| `prices.json` | Per-token prices for each model |

## Where the numbers come from

- **The ledger comes first.** Claude Code produces a cost record for every API call,
  including ones the transcript never shows: prompt suggestions, web search, web fetch
  and helper agents. TokenTab writes one ledger line for each.
- **Transcripts fill in the rest.** Claude Code's session transcripts (including each
  helper's) supply what the ledger lacks: which tool a call was for, and how much of the
  output was thinking.
- **The guard uses whichever total is larger,** so a total never drops when one source
  is a line behind the other.
- **Prices** come from `prices.json`, the published per-token rates. Five rates matter:
  input, output, cache write (5 minutes), cache write (1 hour) and cache read. Thinking
  is billed as output; TokenTab shows it separately at the same rate.
- **Fees.** Web search is billed per search, not per token. Whatever Claude Code's own
  figure has on top of the token price goes in `fees_usd`; `cost_usd` stays tokens only.

Rules that keep the totals right:

- **Count each request once.** One request is logged several times while its output
  streams in. TokenTab counts it once and lets later lines add only what grew.
- **A resumed session repeats its parent's history** with the same request ids. Those
  are counted once, by the session they came from.
- **Files being written are read safely:** TokenTab reads up to the last complete line
  and picks up the rest next time.

## Cost per tool: what is exact, what is estimated

The cost of every call, by model and by rate, is exact. So is the split between
thinking and output text.

The cost per tool is partly estimated. Claude reports usage per API call, never per
part of a call, so splitting one call's tokens between its tools has to go by the size
of each part in the transcript (counted in characters, since TokenTab has no
tokenizer).

| Figure | Exact or estimated |
|---|---|
| Calls per tool | Exact |
| Asking (the tool call Claude writes) | The call's output tokens are exact; their split between tools is by size |
| Result (the tool's answer written into context) | Estimated by size, capped at a realistic tokens-per-character ratio |
| Re-reads (the result's share of every later call's context) | Estimated, reset when the context is compacted |
| Thinking | Never assigned to a tool: nothing ties it to one |

Known biases:

- When a call writes more tokens than its visible content explains (a cache miss
  rewriting the whole context, or the system prompt), the excess stays unassigned.
- Re-reads assume a tool result stays in context until a compaction. If Claude Code
  clears old tool results mid-session, the transcript doesn't show it, so those
  results keep collecting re-read cost.
- TokenTab's own notes to Claude are shown as "TokenTab overhead". Whatever no tool or
  note accounts for is shown as "prompts & replies" (or "context re-reads" for cache
  reads).

`tests/test_accounting.py` checks that the parts always add up: costs by rate sum to
the total, no tool is assigned more than there was, and every column of the flow
diagram sums to the session total.

## Budget settings

Everything is in `budget.json` in your data folder, amounts in US dollars. `null`
turns a limit off. `/tokentab:budget` or the pane's Budget tab changes them for you.

| Setting | Default | What it does |
|---|---|---|
| `budgets_on` | `false` | Switches the two limits below on or off. Their values are kept while it's off. |
| `session_limit_usd` | `25` | Limit for one session. Resets with a new session. |
| `daily_limit_usd` | `40` | Limit across all sessions and projects. Resets at midnight. |
| `warn_at_percent` | `70` | Warnings start here, then once per 10%. |
| `hard_stop` | `false` | `true` stops Claude at 100%; `false` only warns. |
| `context_warn_tokens` | `[200000, 350000]` | Tells you and Claude when the context passes each size. Re-arms after a compaction. |
| `cold_resume` | on | Holds back the first prompt after the prompt cache has expired. See below. |
| `new_session` | on | Asks Claude to suggest a new session when that would be cheaper. See below. |
| `helpers_may_delegate` | `false` | Whether a helper agent may start or message other agents. |

The last four work whether or not `budgets_on` is set.

### Expired cache

The prompt cache lasts an hour (or 5 minutes) from the start of the last request that
used it. Once it expires, the next call rewrites the whole context at the cache-write
rate. TokenTab checks your prompt before anything is sent, so in a session with 100k
tokens of context or more it holds that prompt back once, even with budgets off, and
shows what it will cost against what a new session would. Send it again within two
minutes to go ahead. `/compact` doesn't avoid the cost: compacting reads the whole
context once too.

### New session check

Every call re-reads the whole context, so the context size is the running cost of each
call. Once the context is at least twice what a new session starts with (about 150k
tokens), each prompt tells Claude the context size, the cost per call and how many calls
it takes for a new session to pay off. Claude decides whether your prompt needs the
conversation so far; if it doesn't, Claude suggests a new session in one line and offers
a handoff note.

### Helper agents

- Hard stops apply to helpers; warnings meant for you don't.
- Helpers may not start or message other agents unless `helpers_may_delegate` is on.
  Each handoff loses instructions and adds its own start-up cost.
- Helper spend counts toward every limit.

TokenTab gives Claude numbers, not tactics: there isn't yet the data to say which
approach is cheapest for a given job, so it doesn't recommend one.

## Known limits

- **A limit is checked at each tool call.** Nothing stops a response mid-generation,
  so you can go over a limit by the cost of the call already running.
- **Claude Code only.** Claude on the web and in the apps runs elsewhere and isn't
  tracked.
- **Claude can switch TokenTab off.** It has shell access, and your plugin settings
  and TokenTab's files are yours to write, so they're Claude's to write too. See the
  threat model below.
- **The guard's state is a cache.** It lives in `state/<session id>.json` in the data
  folder; delete it and the guard works out the same totals again from the transcript.
- **Debugging:** create `state/debug` in the data folder (or set `TOKENTAB_DEBUG`) and
  the guard logs each hook's input, without prompts or tool input, to
  `state/payloads.log`. Delete the file to stop.

## Threat model

### What TokenTab is

Installed as a plugin, TokenTab is a **cooperative control**. Every file it relies on
is writable by you, and so by an agent running as you. It keeps an agent that follows
its instructions inside a budget and makes spending visible. It does not stop an agent
that sets out to get around it.

A limit that binds would need the guard, its settings and the folders that hold them
out of the agent's reach (owned by root, registered through managed settings that the
user can't override), an agent with no way to gain more privilege, and a usage record
the agent can't rewrite. A plugin install provides none of these.

### Design rules that hold either way

**The total is worked out from an append-only record, never kept as a counter.** Delete
every state file and the guard arrives at the same total from the transcript. A counter
would be a one-line bypass.

**The total only goes up.** Claude Code has to write its transcripts, so an agent can
write them too, and truncating one would lower the total. So a recomputed total below
the highest one recorded is treated as tampering: the guard keeps the higher number,
refuses to continue, writes to `state/tamper.log`, and stays that way until a person
deletes `state/<session id>.json`.

That detects tampering rather than preventing it: an agent that truncates the
transcript *and* deletes the state file starts clean. Closing that gap needs the
running total held off the machine, out of reach of the thing it measures.

### Out of scope

An agent that persuades its user to run something with more privilege. That is social
engineering, and no technical control here addresses it.

### What has been demonstrated

- 2026-09-23: the guard stopped a tool call mid-turn at $170.24 against a $170.00
  limit, and the agent stopped without cooperating. It went over by one tool call, the
  known limit above.
- The tamper check has been tested against simulated truncation, not yet in real use.

## Findings so far

- **Prompt suggestions cost money you didn't ask for.** After each turn Claude Code
  makes a call to suggest your next prompt, and it re-reads the whole context: about
  2¢ in a short chat, about 5¢ in a long one.
- **Expensive jobs ($1 or more) usually start mid-session**, not on the first prompt.
- **A cheaper model isn't automatically cheaper.** On one browser-form job a Haiku
  helper made four times the calls Opus did, took longer and didn't finish. One job
  isn't a rule, which is why TokenTab's notes to Claude carry numbers only.
