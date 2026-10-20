#!/bin/sh
# UserPromptSubmit ("prompt") and PreToolUse: run TokenTab's guard.py. It reads the
# ledger the hooks module writes, and the session's transcript for the request check.
exec python3 "$(dirname "$0")/../tokentab/guard.py"
