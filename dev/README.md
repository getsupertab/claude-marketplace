# dev/

Tools for working on the plugins; nothing here ships with them.

- `usage-probe/`: the spike mod from 2026-10-15 that showed a hooks module sees every API
  call's usage in-process (see `docs/plan-CORE-537-status-page-tabs.md`, "Spike result").
  Rerun it after a Claude Code update to check the API still delivers the same events.
- `ci-types/`: the hooks-module type declarations CI type-checks against
  (`tsc -p dev/ci-types`), copied from a plugin's `.claude-plugin/types/` because Claude Code
  writes them only when a live session loads the plugin. Their first line names the version
  that wrote them; keep `CLAUDE_CODE_VERSION` in `.github/workflows/tokentab.yml` the same.
