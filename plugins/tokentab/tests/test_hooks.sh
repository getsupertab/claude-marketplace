#!/bin/sh
# Feeds sample hook payloads to the shell hooks (version gate and guard) the way Claude
# Code does, in a throwaway config folder. No dependencies beyond sh and a Python 3; run
# from anywhere, exits non-zero on a failure:
#
#   sh plugins/tokentab/tests/test_hooks.sh
set -u
SCRIPTS="$(cd "$(dirname "$0")/../scripts" && pwd)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
export CLAUDE_CONFIG_DIR="$TMP/claude"
DATA="$CLAUDE_CONFIG_DIR/plugins/data/tokentab-supertab"
FAILED=0

check() {  # check <name> <haystack> <needle>; an empty needle means the output must be empty
  if [ -z "$3" ]; then
    [ -z "$2" ] && { echo "pass  $1"; return; }
  else
    case "$2" in *"$3"*) echo "pass  $1"; return ;; esac
  fi
  echo "FAIL  $1"; echo "      wanted: ${3:-<nothing>}"; echo "      got:    ${2:-<nothing>}"
  FAILED=1
}

# Version gate
prompt='{"session_id": "s-gate", "hook_event_name": "UserPromptSubmit", "prompt": "hi"}'
check "gate warns once when the module left no marker" \
  "$(printf '%s' "$prompt" | "$SCRIPTS/loaded-check.sh")" "hooks module did not load"
check "gate stays quiet on the next prompt" \
  "$(printf '%s' "$prompt" | "$SCRIPTS/loaded-check.sh")" ""
mkdir -p "$DATA/state/loaded" && : > "$DATA/state/loaded/s-loaded"
check "gate stays quiet when the module wrote its marker" \
  "$(printf '%s' '{"session_id": "s-loaded"}' | "$SCRIPTS/loaded-check.sh")" ""
check "gate ignores a payload without a session id" \
  "$(printf '%s' '{}' | "$SCRIPTS/loaded-check.sh")" ""

# Without CLAUDE_CONFIG_DIR the gate looks where the module writes: .claude in USERPROFILE,
# else HOME. On Windows USERPROFILE is a C:\ path, as Node and the module see it.
native() { if command -v cygpath >/dev/null 2>&1; then cygpath -w "$1"; else printf '%s' "$1"; fi; }
for who in profile home; do
  mkdir -p "$TMP/$who/.claude/plugins/data/tokentab-supertab/state/loaded"
  : > "$TMP/$who/.claude/plugins/data/tokentab-supertab/state/loaded/s-$who"
done
check "gate finds the marker under USERPROFILE before HOME, trailing separator and all" \
  "$(printf '%s' '{"session_id": "s-profile"}' | env -u CLAUDE_CONFIG_DIR \
     USERPROFILE="$(native "$TMP/profile")$(command -v cygpath >/dev/null 2>&1 && echo '\' || echo /)" \
     HOME="$TMP/home" "$SCRIPTS/loaded-check.sh")" ""
check "gate finds the marker under HOME when USERPROFILE is unset" \
  "$(printf '%s' '{"session_id": "s-home"}' | env -u CLAUDE_CONFIG_DIR -u USERPROFILE \
     HOME="$TMP/home/" "$SCRIPTS/loaded-check.sh")" ""

# Guard
check "guard passes when no budget file exists" \
  "$(printf '%s' '{"session_id": "s1", "hook_event_name": "PreToolUse", "tool_name": "Read"}' | "$SCRIPTS/guard.sh")" ""

cat > "$DATA/budget.json" <<'EOF'
{"budgets_on": true, "session_limit_usd": 0.000001, "daily_limit_usd": null, "hard_stop": true}
EOF
T="$TMP/s-guard.jsonl"
cat > "$T" <<'EOF'
{"type": "user", "sessionId": "s-guard", "timestamp": "2026-10-13T10:00:00.000Z", "message": {"role": "user", "content": "hi"}}
{"type": "assistant", "sessionId": "s-guard", "requestId": "req_1", "timestamp": "2026-10-13T10:00:01.000Z", "message": {"model": "claude-opus-5-5", "usage": {"input_tokens": 1000, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0, "output_tokens": 500}, "content": [{"type": "text", "text": "ok"}]}}
EOF
# MSYS converts paths in arguments, not in stdin: the payload carries the path as Python
# on Windows opens it (C:/..., forward slashes so the JSON needs no escapes)
TRANSCRIPT="$(if command -v cygpath >/dev/null 2>&1; then cygpath -m "$T"; else printf '%s' "$T"; fi)"
tool() {  # tool <tool name> <command>
  printf '{"session_id": "s-guard", "transcript_path": "%s", "hook_event_name": "PreToolUse", "tool_name": "%s", "tool_input": {"command": "%s"}}' "$TRANSCRIPT" "$1" "$2"
}
check "guard denies a tool call past a hard session limit" \
  "$(tool Read "" | "$SCRIPTS/guard.sh")" '"permissionDecision": "deny"'
check "guard lets TokenTab's own report command through" \
  "$(tool Bash "python3 /plugins/tokentab/tokentab/tokentab.py --status" | "$SCRIPTS/guard.sh")" ""
# Each launcher the hooks may find Python with, and Windows paths (JSON-escaped below:
# \\ is one backslash, \" a quote)
for cmd in \
  "python /plugins/tokentab/tokentab/tokentab.py --once" \
  "py -3 /plugins/tokentab/tokentab/sankey.py" \
  'python C:\\Users\\a\\.claude\\plugins\\cache\\supertab\\tokentab\\1\\tokentab\\tokentab.py --once' \
  'py -3 \"C:\\Users\\A B\\.claude\\plugins\\tokentab\\tokentab\\sankey.py\"'; do
  check "guard lets the report command through as: $cmd" "$(tool Bash "$cmd" | "$SCRIPTS/guard.sh")" ""
done
for cmd in \
  "py /plugins/tokentab/tokentab/tokentab.py --once" \
  "python /plugins/tokentab/tokentab/tokentab.py --once && rm -rf x" \
  'py -3 \"$(rm x)tokentab/tokentab.py\"'; do
  check "guard denies a look-alike: $cmd" "$(tool Bash "$cmd" | "$SCRIPTS/guard.sh")" '"permissionDecision": "deny"'
done

# Python launchers, in the module's order: a python3 that is not Python 3 (the Store
# stub) and a python that is Python 2 are passed over for py -3
REAL="$(for py in python3 python "py -3"; do $py -c 'import sys; sys.exit(sys.version_info[0] != 3)' 2>/dev/null && { $py -c 'import sys; print(sys.executable)'; break; }; done)"
BIN="$TMP/bin"
mkdir -p "$BIN"
printf '#!/bin/sh\nexit 9009\n' > "$BIN/python3"
printf '#!/bin/sh\n[ "$1" = -c ] && exit 1\necho python >> "%s/launched"\n' "$TMP" > "$BIN/python"
printf '#!/bin/sh\n[ "$1" = -3 ] && shift\n[ "$1" = -c ] || echo "py -3" >> "%s/launched"\nexec "%s" "$@"\n' "$TMP" "$REAL" > "$BIN/py"
chmod +x "$BIN/python3" "$BIN/python" "$BIN/py"
PATH="$BIN:$PATH" "$SCRIPTS/guard.sh" < /dev/null
check "guard passes over a python3 stub and a Python 2, and runs py -3" "$(cat "$TMP/launched" 2>/dev/null)" 'py -3'
check "guard ran one launcher" "$(wc -l < "$TMP/launched" | tr -d ' ')" '1'

exit $FAILED
