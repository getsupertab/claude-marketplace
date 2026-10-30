---
name: budget
description: Show or change TokenTab's spending limits (session, daily, warnings, hard stop). Use when the user asks, in the same prompt, to see or change a TokenTab limit, e.g. "raise my daily limit to 30" or "hard stop on". Never change a limit unasked.
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

Read the file and show, in a short list: whether `budgets_on` is on (off, or missing,
means none of the limits below are checked), `limit_unit`, the two limits of the unit
in effect (`null` means off), `warn_at_percent` and `hard_stop` (true blocks at 100%,
false only warns). "Turn budgets on/off" means `budgets_on`.

`limit_unit` says what the limits count: `usd` (`session_limit_usd`, `daily_limit_usd`),
`percent` of the plan's usage windows (`five_hour_limit_percent`, `weekly_limit_percent`,
Pro and Max), or `auto`, which takes percent where Claude Code reports usage windows and
USD otherwise. Under `auto`, `state/rate_limits.json` beside budget.json holds the last
reading: windows listed means percent. "Limits in dollars/percent" sets `limit_unit`; a
limit the user names without a unit ("daily limit 30") is the one of the unit in effect
(today in USD, this week in percent). Give the file path, and mention that
the user can ask for a change with `/tokentab:budget <what to change>`.

## With arguments: change the limits

1. Work out exactly which keys change and to what, and show the change (old → new)
   before writing. If the request is ambiguous, ask.
2. Change each key through the validator, one call per key, never by editing the file:

       python3 "${CLAUDE_PLUGIN_ROOT}/tokentab/budget.py" set KEY VALUE

   Where `python3` is missing or is not Python 3 (Windows), run the same with `python`, else `py -3`, in its place.
   KEY is dotted for nested settings (`cold_resume.confirm_seconds`); VALUE is a number, `off` for a
   limit switched off, `on`/`off` for a switch, `auto`/`usd`/`percent` for `limit_unit`. It refuses a value that does not fit or a
   key that does not exist, and keeps the file's order and comments. `budget.py get` prints
   the file.
3. Show the new values. They apply from the next prompt; no restart is needed. The same
   limits are editable in the TokenTab pane's Options tab.

TokenTab tells Claude never to raise a limit on its own. That rule is about Claude
acting unasked; this skill acts only on what the user asked for in the same prompt. If TokenTab blocks the edit anyway (for example
during a hard stop), give the user the path so they can edit the file themselves.
