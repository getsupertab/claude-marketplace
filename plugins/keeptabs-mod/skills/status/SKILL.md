---
name: status
description: Write the keeptabs status page and summarise what this Claude Code session has spent against its budgets. Use when the user asks about keeptabs, their spend, cost so far, budgets, a visual cost overview or spending flow, or to "see where the money goes".
---

# keeptabs status

Run these as two separate Bash calls, each exactly as written. keeptabs never blocks
these commands, even during a budget stop, but only when each is run on its own:
do not chain them with `&&`, pipes or redirects, and do not quote the path.

    python3 ${CLAUDE_PLUGIN_ROOT}/keeptabs/sankey.py

    python3 ${CLAUDE_PLUGIN_ROOT}/keeptabs/keeptabs.py --once

The first writes the status page for the most recently active session as a snapshot
file and prints its path (`~/.claude/plugins/data/keeptabs-mod-supertab/state/status.html`): where the money
went as a flow diagram, cost over time, then every figure below. It is a file, so it
shows the moment it was written; run the command again for a fresh one.

The second prints the same figures as text, for your summary: cost by rate, requests,
call sources, budget bars and cost per tool. It contains terminal colour codes; leave
those out when you relay it.

Give the user the page's path (in Claude Desktop you can open it in the app's browser
pane if they ask). Then report the session cost and the budget lines in a few lines.

Every API call is in the ledger from the moment the plugin loaded, helpers and
background calls included. If the figures look empty for the current session, the
hooks module did not load; Claude Code 2.1.287 or later is needed, and the first
prompt of the session says so.

For budgets, point the user to `/keeptabs-mod:budget`. Do not edit anything in
`~/.claude/plugins/data/keeptabs-mod-supertab` unless the user explicitly asks you to.
