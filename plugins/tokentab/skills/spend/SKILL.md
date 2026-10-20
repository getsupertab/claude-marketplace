---
name: spend
description: Summarise what this Claude Code session has spent against its budgets, from TokenTab's figures. Use when the user asks about TokenTab, their spend, cost so far, budgets, a cost overview or spending flow, or to "see where the money goes".
---

# TokenTab spend

The live figures are in the TokenTab pane beside the chat: `/tokentab:status` opens
it (in a narrow terminal the numbers are on TokenTab's status line instead), and its
"open in browser" button writes the status page (where the money went as a flow
diagram, cost over time, every figure) and opens it. This skill is for answering in
words.

Run this as one Bash call, exactly as written. TokenTab never blocks it, even during a
budget stop, but only when it is run on its own: do not chain it with `&&`, pipes or
redirects, and do not quote the path.

    python3 ${CLAUDE_PLUGIN_ROOT}/tokentab/tokentab.py --once

It prints the figures as text: cost by rate, requests, call sources, budget bars and
cost per tool. It contains terminal colour codes; leave those out when you relay it.
Report the session cost and the budget lines in a few lines.

If the user wants the status page and has no pane, run on its own, the same way:

    python3 ${CLAUDE_PLUGIN_ROOT}/tokentab/sankey.py

It writes the page for the most recently active session as a snapshot file and prints
its path (`~/.claude/plugins/data/tokentab-supertab/state/status.html`). It is a
file, so it shows the moment it was written; run the command again for a fresh one. In
Claude Desktop you can open it in the app's browser pane if they ask.

Every API call is in the ledger from the moment the plugin loaded, helpers and
background calls included. If the figures look empty for the current session, the
hooks module did not load; Claude Code 2.1.287 or later is needed, and the first
prompt of the session says so.

For budgets, point the user to `/tokentab:budget`. Do not edit anything in
`~/.claude/plugins/data/tokentab-supertab` unless the user explicitly asks you to.
