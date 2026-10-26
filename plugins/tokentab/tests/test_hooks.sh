#!/bin/sh
# Feeds sample hook payloads to the shell hooks (version gate and guard) the way Claude
# Code does, in a throwaway config folder. No dependencies beyond sh and python3; run
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
tool() {  # tool <tool name> <command>
  printf '{"session_id": "s-guard", "transcript_path": "%s", "hook_event_name": "PreToolUse", "tool_name": "%s", "tool_input": {"command": "%s"}}' "$T" "$1" "$2"
}
check "guard denies a tool call past a hard session limit" \
  "$(tool Read "" | "$SCRIPTS/guard.sh")" '"permissionDecision": "deny"'
check "guard lets TokenTab's own report command through" \
  "$(tool Bash "python3 /plugins/tokentab/tokentab/tokentab.py --status" | "$SCRIPTS/guard.sh")" ""

exit $FAILED
