#!/bin/sh
# UserPromptSubmit ("prompt") and PreToolUse: run TokenTab's guard.py. It reads the
# ledger the hooks module writes, and the session's transcript for the request check.
# The first of python3, python, py -3 that is Python 3, the hooks module's order; a
# python3 that is not (the Microsoft Store stub) is passed over.
for py in python3 python "py -3"; do
  # $py unquoted on purpose: "py -3" is a command and its argument
  if $py -c 'import sys; sys.exit(sys.version_info[0] != 3)' </dev/null >/dev/null 2>&1; then
    exec $py "$(dirname "$0")/../tokentab/guard.py"
  fi
done
echo "TokenTab: no Python 3 found (tried python3, python, py -3); the budget guard is off" >&2
exit 1
