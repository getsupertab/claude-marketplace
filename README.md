# TokenTab: see what Claude Code is spending

**Ever wondered why one prompt cost so much more than the last?** So did we. TokenTab is
a free Claude Code plugin that prices every API call as it happens and shows you where
the money went, tool by tool. Nothing leaves your machine.

```
/plugin install tokentab --marketplace getsupertab/claude-marketplace
```

Start a new session (or run `/reload-plugins`) and it's counting from your first prompt.

## Why we built it

It started over Slack. One of us noticed a Claude Code session had burned through far
more than usual, and nobody could say why. Context? Big files? Too many tool calls?
Something in the background? We all had guesses and no answers.

So we built a meter. Once we could see the numbers, a few things surprised us:

- **Going to get coffee can cost you.** The prompt cache expires. Come back after it has
  and your next message re-sends the whole conversation at the expensive rate. TokenTab
  catches that before it's sent and tells you what it'll cost, so you can decide.
- **The "suggested next prompt" isn't free.** After each turn Claude Code makes a
  background call to guess what you'll type next, and that call re-reads your context
  too. You never see it. TokenTab does.
- **Cheaper models aren't always cheaper.** We watched a Haiku helper make four times the
  calls Opus did on the same job, and still not finish.

And the one we didn't expect: **once Claude could see the numbers, it worked
differently.** TokenTab never tells Claude how to save money. It just shows it the bill.
With that in front of it, Claude started handing us commands to run ourselves instead of
churning through the output, and stopped writing essays when a short answer would do.

## "Doesn't `/usage` already do this?"

Sort of! We use both. `/usage` tells you how much is left in the tank. TokenTab tells you
what's using it.

| | `/usage` | TokenTab |
|---|:-:|:-:|
| How much of my plan is left | ✅ | ✅ (as percent budgets) |
| Cost in dollars, per session, day and month | ❌ | ✅ |
| Which tool or call cost what | ❌ | ✅ |
| Always on screen | ❌ you run it | ✅ |
| Warns or stops you *before* you spend | ❌ | ✅ |
| Claude can see the numbers | ❌ | ✅ |
| Limits you pick yourself | ❌ | ✅ |

If you're on Pro or Max and just don't want to hit the wall, `/usage` is honestly fine.
If you want to know *why* a session got expensive, or you're paying per token, that's
where TokenTab comes in.

## What you get

- **Every API call, priced.** Including helper agents, web search, prompt suggestions and
  Claude Code's own background calls: the ones you'd never think to count.
- **A live pane beside the chat** with this session's cost, or a status line under the
  prompt in narrow terminals.
- **The breakdown.** Cost by tool, and by type (input, cache writes, cache reads, output,
  thinking), plus today and this month across every session and project.
- **A picture of where the money went.** A flow diagram and cost over time, in your
  browser.
- **Plain answers.** Ask Claude "how much have I spent?" or "where did the money go?" and
  it answers from the same numbers.
- **Budgets, if you want them.** Session and daily limits that warn you, and can stop
  tool calls if you ask them to. On Pro or Max they count in percent of your plan's
  5-hour and weekly windows; on the API they count in dollars. Your account decides,
  even when you switch between them. Off until you turn them on.
- **Heads-ups that save money.** A warning before an expensive resend after the cache
  goes cold, and a nudge to start fresh once a new session would be cheaper than carrying
  on.
- **A card to share** what a session cost, if you're proud of it (or horrified).

## Your data stays yours

We think you should be able to see what you're paying for, and that starts with us
being clear about what TokenTab does:

- **No account, no sign-up, no telemetry.** TokenTab makes no network calls. Nothing is
  sent to us or anyone else.
- **It records numbers, not content.** For each API call: the time, model, token counts
  and cost. Never your prompts, Claude's replies or your files.
- **It writes to one folder** inside your Claude config folder, and nowhere else.
- **It's open source** (Apache 2.0), so you don't have to take our word for any of this.
  The details are in the [TokenTab README](plugins/tokentab/README.md#privacy).
- **One command to remove it:** `claude plugin uninstall tokentab@supertab`.

## Try it

```
/plugin install tokentab --marketplace getsupertab/claude-marketplace
```

Confirm the source, pick a scope ("Install for you" puts it in every project), then start
a new session or run `/reload-plugins`.

You'll need **Claude Code 2.1.304 or later** and **Python 3**, on macOS, Linux or
Windows. TokenTab works in Claude Code (terminal and the Desktop app's Code tab), not in
Claude chat.

The [TokenTab README](plugins/tokentab/README.md) covers the pane, budgets and every
setting.

## Tell us what you think

TokenTab is early, and we'd love to hear from you:

- **Budgets:** do you care? Would you actually set one? What number? Or is just seeing
  the spend enough?
- **Your tricks for keeping sessions cheap.** We'll collect the good ones and share them.
- **Anything else,** good or bad.

Email us at [product@supertab.co](mailto:product@supertab.co) or
[open an issue](https://github.com/getsupertab/claude-marketplace/issues). And if you're
curious what Supertab does when it isn't building token meters, it's at
[supertab.co](https://supertab.co/).

---

## About this marketplace

This repo is Supertab's plugin marketplace for Claude Code (marketplace name:
`supertab`).

| Plugin | Install id | What it does |
|---|---|---|
| [TokenTab](plugins/tokentab/README.md) | `tokentab@supertab` | See what Claude Code is spending while you work, with a live cost pane and budget limits that warn or stop. Needs Claude Code 2.1.304 or later. |

The repo is public, so no git credentials are needed. From a shell:

```bash
claude plugin marketplace add getsupertab/claude-marketplace
```

```bash
claude plugin install <plugin>@supertab
```

### Updates

```bash
claude plugin marketplace update supertab
```

```bash
claude plugin update <plugin>@supertab
```

Then start a new session (or run `/reload-plugins`). Sessions already open keep the old
version until they end.

To update automatically: `/plugin` → Marketplaces → supertab → Enable auto-update.
Claude Code then checks at startup, and an update applies from the next session.

**Not from the Claude Desktop app.** Desktop starts its bundled `claude` with
`DISABLE_AUTOUPDATER=1`, and Claude Code skips plugin and marketplace auto-update behind
the same flag. From Desktop, pull updates by hand with the two commands above.

### Remove

```bash
claude plugin uninstall <plugin>@supertab
```

```bash
claude plugin marketplace remove supertab
```

Some plugins change things outside Claude Code's plugin folders; their READMEs say how
to undo that. Do it before uninstalling.

### Adding a plugin

1. Put it in `plugins/<name>/`, with a `.claude-plugin/plugin.json` manifest and its
   components (`skills/`, `hooks/hooks.json`, `commands/`, `agents/`, scripts referenced
   via `${CLAUDE_PLUGIN_ROOT}`). Keep everything plugin-specific, including its README,
   inside that folder.
2. Add an entry to `plugins` in `.claude-plugin/marketplace.json`:
   `{"name": "<name>", "source": "./plugins/<name>", "description": "..."}`.
3. Add a row to the table above.
4. Check it: `claude plugin validate .`
5. Give it a `version` in `plugin.json` and raise it with every change, or installed
   copies never update.

Data a plugin must keep across updates belongs in `${CLAUDE_PLUGIN_DATA}`, or in its own
folder under the user's home. Never inside the plugin folder, which is replaced on every
update.

See [AGENTS.md](AGENTS.md) for the edit-and-reload loop and the checks.
