---
name: status
description: Open the keeptabs pane beside the chat: what this session has spent, live, against its budgets. User-invoked.
disable-model-invocation: true
---

# keeptabs status

keeptabs' hooks module answers this command itself: it opens the pane (the live view,
redrawn after every API call) and prints the session's cost line. You are reading this
text only because the module did not load, so nothing is tracked in this session. Tell
the user that keeptabs needs Claude Code 2.1.287 or later, and that the first prompt of
the session says so when the module is missing. For the figures of earlier sessions, run
on its own, exactly as written:

    python3 ${CLAUDE_PLUGIN_ROOT}/keeptabs/keeptabs.py --once

and relay them without the colour codes.
