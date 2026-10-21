---
name: budget
description: Show or change TokenTab's spending limits (session, daily, per request, job budgets, warnings, hard stop). Use when the user asks, in the same prompt, to see or change a TokenTab limit, e.g. "raise my daily limit to 30" or "hard stop on". Never change a limit unasked.
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

Read the file and show, in a short list: session, daily and per-request limits (USD and
tokens, `null` means off), `warn_at_percent`, `hard_stop` (true blocks at 100%, false only
warns), and the job budget settings (`job.ask`, `job.ask_mid_request`, `job.ask_over_usd`,
`job.hard_stop`, the word scale). A fresh install has every limit and both job asks off:
say so when that is what the file holds.
Say that a job budget is set by starting a prompt with, for example, `$2:` or `beer:`,
and cleared with `nobudget:`. Give the file path, and mention that the user can ask for a
change with `/tokentab:budget <what to change>`.

## With arguments: change the limits

1. Work out exactly which keys change and to what, and show the change (old → new)
   before writing. If the request is ambiguous, ask.
2. Change each key through the validator, one call per key, never by editing the file:

       python3 "${CLAUDE_PLUGIN_ROOT}/tokentab/budget.py" set KEY VALUE

   KEY is dotted for the job settings (`job.ask_over_usd`); VALUE is a number, `off` for a
   limit switched off, `on`/`off` for a switch. It refuses a value that does not fit or a
   key that does not exist, and keeps the file's order and comments. `budget.py get` prints
   the file.
3. Show the new values. They apply from the next prompt; no restart is needed. The same
   limits are editable in the TokenTab pane's Options tab.

TokenTab tells Claude never to raise a limit on its own. That rule is about Claude
acting unasked; this skill acts only on what the user asked for in the same prompt. If TokenTab blocks the edit anyway (for example
during a hard stop), give the user the path so they can edit the file themselves.
