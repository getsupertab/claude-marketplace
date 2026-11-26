---
name: budget
description: Show or change TokenTab's budget (its type, limit, stop at limit, carry over). Use when the user asks, in the same prompt, to see or change a TokenTab budget or limit, e.g. "raise my daily limit to 30", "make my budget the session" or "stop at the limit". Never change a limit unasked.
argument-hint: "[what to change]"
---

# TokenTab budget

The limits live in one file, under Claude Code's config folder: `$CLAUDE_CONFIG_DIR` when
that is set, else `~/.claude`:

    <config folder>/plugins/data/tokentab-supertab/budget.json

It belongs to the user. Plugin updates never change it; uninstalling the plugin deletes
it together with the rest of TokenTab's data.

Arguments: $ARGUMENTS

## No arguments: show the limits

Read the file and show, in a short list: whether `budgets_on` is on (Enabled under BUDGET; off,
or missing, means nothing is checked), the unit in effect, the budget type for that unit, and
`spend_less` (Economy mode, beta, on by default: Claude is also asked to spend less at each
warning; off, only the user is warned). In USD also show the type's limit (`null` means off) and
`hard_stop` ("Stop at limit": true blocks at the limit, false only warns). Warnings come at 10%,
5% and 2% of the budget left, fixed, not a setting. "Turn budgets on/off" means `budgets_on`.
The figure that leads the pane and the status line follows the budget type; it is not a setting.

Only one budget is checked: `budget_type.usd` or `budget_type.percent`, whichever unit is in
effect.

| Unit | Type | Limit |
|---|---|---|
| usd | `today` (every session, resets at midnight) | `daily_limit_usd` |
| usd | `session` | `session_limit_usd` |
| percent | `today` | none: what is left of the week (of 100%) spread over the days left |
| percent | `week` | none: Anthropic's own 100% of the weekly window |
| percent | `five_hour` | none: Anthropic's own 100% of the 5-hour window |

Percent has no limit to set and no stop, because Anthropic stops at 100% itself.
`daily_budget.carry_over` (beta, percent type `today` only): unused budget carries over to
tomorrow, and an overspent day takes it out of the next.

The account decides the unit; it is not a setting. Percent where Claude Code reports the plan's
usage windows (Pro, Max, Team), else USD (Enterprise, API key). `state/rate_limits.json` beside
budget.json holds the last reading: windows listed means percent. If the user asks to switch
units, say the account sets it. A limit the user names without a type ("my limit 30") is the
USD budget type's. "The daily budget" in percent means type `today`. Give the file path, and mention
that the user can ask for a change with `/tokentab:budget <what to change>`.

## With arguments: change the limits

1. Work out exactly which keys change and to what, and show the change (old → new)
   before writing. If the request is ambiguous, ask.
2. Change each key through the validator, one call per key, never by editing the file:

       python3 "${CLAUDE_PLUGIN_ROOT}/tokentab/budget.py" set KEY VALUE

   Where `python3` is missing or is not Python 3 (Windows), run the same with `python`, else `py -3`, in its place.
   KEY is dotted for nested settings (`cold_resume.confirm_seconds`); VALUE is a number, `off` for a
   limit switched off, `on`/`off` for a switch; for `budget_type.usd` `today` or `session`, for `budget_type.percent` `today`, `week` or `five_hour`. It refuses a value that does not fit or a
   key that does not exist, and keeps the file's order and comments. `budget.py get` prints
   the file.
3. Show the new values. They apply from the next prompt; no restart is needed. The same
   limits are editable in the TokenTab pane's Settings tab.

TokenTab tells Claude never to raise a limit on its own. That rule is about Claude
acting unasked; this skill acts only on what the user asked for in the same prompt. If TokenTab blocks the edit anyway (for example
while stopped at the limit), give the user the path so they can edit the file themselves.
