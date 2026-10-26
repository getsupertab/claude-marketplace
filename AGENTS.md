# Notes for agents working on this marketplace

## Testing a change to a plugin

An installed plugin runs from a snapshot in `~/.claude/plugins/cache/supertab/<plugin>/<sha>/`,
not from this working tree, so a change here is not live until a session reads it.
Two loops, both without a commit:

- **Folder marketplace** (terminal and Claude Desktop alike). Add this working tree as a
  second marketplace once, `claude plugin marketplace add <path to this folder>`, and
  install from it (`claude plugin install tokentab@<its name>`). A plugin installed from
  a folder marketplace with a relative `source` is read from the folder itself: edit,
  run `/reload-plugins` in the session, done. Shell hooks and skills pick the change up
  at once; the hooks module reloads with them.
- **`--plugin-dir`** (terminal). `claude --plugin-dir plugins/tokentab` loads the plugin
  from disk for that session and hot-reloads the hooks module on every save. For a
  session Claude Desktop starts, name the folder in `CLAUDE_CODE_PLUGIN_DIRS` in the
  `env` block of `~/.claude/settings.json` instead (back the file up first, remove the
  entry when done; each change needs a Desktop restart).

**Two copies at once.** With `tokentab@supertab` installed, either loop loads the
module twice, the installed copy and the working tree: two ledger lines per request and
two panes. Before the first dev run, `claude plugin disable tokentab@supertab`, and
re-enable it at the end of the job. A session already open keeps the copy it started
with.

A behavioural check of the pane needs a terminal: 144 columns or more places the pane
unasked at session start; narrower, the status line under the prompt shows the numbers
and `/tokentab:status` places the pane. `tmux new-session -d -x 160 -y 50` and
`tmux capture-pane -p` give a terminal of a known width and its contents as text.

The GitHub marketplace (`supertab`) stays the way others install; after a commit lands
on `main`, `claude plugin marketplace update supertab` and `claude plugin update
tokentab@supertab`, then a new session.

When a job that changed a plugin is done, offer the user the reload, and remind them
that a session they already had open runs the old copy's shell hooks until it ends.

## Checks for TokenTab

- `claude plugin validate plugins/tokentab`: reads the manifest and the hooks module the
  way the engine will and lists what the module hooks and calls.
- `claude plugin test plugins/tokentab`: runs `hooks/*.test.tsx`. The harness has no
  `$.fs`, pane, command, process or status line beneath the plugin and nothing answers
  `telemetry.log`, `session.id` or `session.start`: the test stands in for each with
  `on(...)` hooks and settles the module's unawaited redraws with `mock.clock`'s `settle()`.
  A slash command of the module's is a skill the `command.run` hook answers in its place
  (`{ command: 'tokentab:status' }`); `$.command.register` makes a flat `/<name>` that
  a built-in of the same name shadows, see the plan doc's phase 1 notes.
- `npx -y -p typescript tsc -p plugins/tokentab` (no global `tsc` here): type-checks
  the module and its test against the types Claude Code lays in
  `plugins/tokentab/.claude-plugin/types/` when it loads the plugin from a
  `--plugin-dir` or watched folder (git-ignored; the first line names the version that
  wrote them). Until a session has done that, point a `tsconfig.json` outside the plugin
  at the `types/claude-code.d.ts` the plugin-authoring skill writes.
- `python3 plugins/tokentab/tests/test_accounting.py` after changing TokenTab's cost or
  Sankey code.
- `sh plugins/tokentab/tests/test_hooks.sh`: feeds sample payloads to the shell hooks
  (version gate and guard).
- CI (`.github/workflows/tokentab.yml`) runs these offline checks on Ubuntu, macOS and
  Windows; its type check reads the declarations in `dev/ci-types/` (see `dev/README.md`).
- A headless end-to-end check that the module sees API calls on the installed Claude
  Code (also the regression check after a Claude Code update):
  `claude -p "Reply with the single word ok" --model haiku --settings dev/usage-probe/notelemetry.json --plugin-dir plugins/tokentab`,
  then read the newest line under `plugins/data/tokentab-supertab/ledger/` in `$CLAUDE_CONFIG_DIR`,
  else in `~/.claude`.
