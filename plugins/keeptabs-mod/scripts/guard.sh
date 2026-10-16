#!/bin/sh
# UserPromptSubmit ("prompt") and PreToolUse: run keeptabs' guard.py. It reads the
# ledger the hooks module writes, and the session's transcript for the request check.
exec python3 "$(dirname "$0")/../keeptabs/guard.py"
