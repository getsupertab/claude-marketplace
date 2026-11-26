---
name: budget
description: Show or change TokenTab's budget (its type, limit, stop at limit). Use when the user asks, in the same prompt, to see or change a TokenTab budget or limit, e.g. "raise my daily limit to 30", "make my budget the session" or "stop at the limit". Never change a limit unasked.
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

Read the file and show, in a short list: whether `budgets_on` is on (the BUDGET switch; off, or
missing, means nothing is checked), the unit in effect, the budget type for that unit and its
limit (`null` means off), and `hard_stop` ("Stop at limit": true blocks at the limit, false only
warns; warnings come at 10%, 5% and 2% of the budget left, fixed, not a setting) and `spend_less`
(beta; true also asks Claude to spend less at each warning, and to wrap up when `hard_stop` is
on; false only warns the user; null, the default, follows `hard_stop`, and `auto` sets it back
to null). "Turn budgets on/off" means `budgets_on`. Also show `headline` (`auto`, `session`,
`today`, `five_hour` or `week`: which figure leads in the pane, status line and reopen button,
as what is left; `auto` follows the budget type while the budget is on, else the week on Pro
and Max (the daily budget if an older file's `daily_budget.enabled` is on) and today in USD; `five_hour` and `week` are Pro and Max only).

Only one budget is checked: `budget_type.usd` or `budget_type.percent`, whichever unit is in
effect. Each type has one limit key:

| Unit | Type | Limit key |
|---|---|---|
| usd | `today` | `daily_limit_usd` |
| usd | `session` | `session_limit_usd` |
| percent | `today` (the daily budget) | `weekly_limit_percent`, the week's cap it is spread from (null: 100) |
| percent | `week` | `weekly_limit_percent` |
| percent | `five_hour` | `five_hour_limit_percent` |
| percent | `session` | `session_limit_usd`, dollars at API rates |

A `null` type takes the limit the file had set: `today` in USD unless only the session limit is
set; in percent `today` if an older file's `daily_budget.enabled` is true, else `week` unless only
the 5-hour limit is set. The daily budget (type `today` in percent) is what is left of the week
spread over the days left, fixed at the start of each local day; its beta
`daily_budget.carry_over` (on: each full day gets a seventh of the week, the days the week
starts and ends on their part of one, plus what earlier days left unused, minus what they
overspent, so a day unused doubles the next).

The account decides the unit; it is not a setting. Percent where Claude Code reports the plan's
usage windows (Pro, Max), else USD (Enterprise, API key). `state/rate_limits.json` beside
budget.json holds the last reading: windows listed means percent. If the user asks to switch
units, say the account sets it. A limit the user names without a type ("my limit 30") is the
budget type's. "The daily budget" in percent means type `today`. Give the file path, and mention
that the user can ask for a change with `/tokentab:budget <what to change>`.

## With arguments: change the limits

1. Work out exactly which keys change and to what, and show the change (old → new)
   before writing. If the request is ambiguous, ask.
2. Change each key through the validator, one call per key, never by editing the file:

       python3 "${CLAUDE_PLUGIN_ROOT}/tokentab/budget.py" set KEY VALUE

   Where `python3` is missing or is not Python 3 (Windows), run the same with `python`, else `py -3`, in its place.
   KEY is dotted for nested settings (`cold_resume.confirm_seconds`); VALUE is a number, `off` for a
   limit switched off, `on`/`off` for a switch (also `auto` for `spend_less`, back to following `hard_stop`), `auto`, `session`, `today`, `five_hour` or `week` for `headline`; for `budget_type.usd` `today` or `session`, for `budget_type.percent` `today`, `week`, `five_hour` or `session`, and `auto` for either. It refuses a value that does not fit or a
   key that does not exist, and keeps the file's order and comments. `budget.py get` prints
   the file.
3. Show the new values. They apply from the next prompt; no restart is needed. The same
   limits are editable in the TokenTab pane's Options tab, all but `daily_budget.carry_over`.

TokenTab tells Claude never to raise a limit on its own. That rule is about Claude
acting unasked; this skill acts only on what the user asked for in the same prompt. If TokenTab blocks the edit anyway (for example
while stopped at the limit), give the user the path so they can edit the file themselves.
