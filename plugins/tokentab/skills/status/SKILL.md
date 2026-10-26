---
name: status
description: "Open the TokenTab pane beside the chat: what this session has spent, live, against its budgets. User-invoked."
disable-model-invocation: true
---

# TokenTab status

TokenTab's hooks module answers this command itself: it opens the pane (the live view,
redrawn after every API call) and prints the session's cost line. You are reading this
text only because the module did not load, so nothing is tracked in this session. Tell
the user that TokenTab needs Claude Code 2.1.287 or later, and that the first prompt of
the session says so when the module is missing. For the figures of earlier sessions, run
on its own, exactly as written:

    python3 ${CLAUDE_PLUGIN_ROOT}/tokentab/tokentab.py --once

and relay them without the colour codes. Where `python3` is missing or is not Python 3 (Windows), run the same with `python`, else `py -3`, in its place.
