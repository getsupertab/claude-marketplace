import type { EngineInterface, Register, TelemetryRecordEntry, Timer, UiOpenResult } from 'claude-code'

// keeptabs' hooks module (docs/plan-CORE-537-status-page-tabs.md), two parts:
//
// The ledger (phase 0). Every API request the engine makes reaches `telemetry.log` as
// an `api_request` record for the operator's collector, with or without telemetry
// switched on, main loop and helpers alike. This module prices it and appends one
// line to the session's ledger file; the Python accounting reads those.
//
// The pane (phase 1). `session.start` opens a pane beside the chat; `/keeptabs-mod:status`
// (the status skill, answered by a hook) opens it asked; `ui.render` draws the terminal live view in it, which is
// `keeptabs.py --once` with its ANSI codes turned into Text spans, so the pane and the
// standalone terminal view cannot drift apart. While the pane is not placed (a narrow
// terminal) one status line carries the numbers instead. Redrawn after every ledger
// write and on a 30 s clock; nothing polls the data folder. Under `claude -p`, or with
// no surface, nothing visual happens and the ledger is written as before.

type Prices = {
  _verified?: boolean
  models?: Record<string, Record<string, number>>
  default?: Record<string, number>
}

type Tokens = { input: number; output: number; cache_read: number; cache_write: number }

export type LedgerLine = {
  ts: string
  source: 'mod'
  session_id: string
  model: string
  tokens: Tokens
  cost_usd: number
  cost_usd_cc: number | null
  fees_usd: number
  duration_ms?: number
  query_source: string | null
  request_id: string | null
  prices_verified: boolean
}

// A file nears the 4 MiB read-and-write limit: the next line starts <session>-2.jsonl.
const ROLL_BYTES = 3.5 * 1024 * 1024

/** The plugin's data folder (`${CLAUDE_PLUGIN_DATA}` for shell hooks and skills). `$.plugin`
 *  has no data path, so it is built from HOME here, the same path the Python falls back to. */
export function dataDir(home: string): string {
  return `${home.replace(/\/$/, '')}/.claude/plugins/data/keeptabs-mod-supertab`
}

/** `YYYY-MM-DD` of an instant, the module environment's local date. */
export function dayOf(ms: number): string {
  const d = new Date(ms)
  const p = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`
}

export function rate(prices: Prices, model: string, key: string): number {
  const m = prices.models?.[model] ?? prices.default ?? {}
  return Number(m[key] ?? 0)
}

/** USD for the tokens at the plugin's prices. Telemetry does not say whether a cache
 *  write was 5m or 1h; Claude Code uses 1h by default, so that rate is applied. */
export function price(prices: Prices, model: string, t: Tokens): number {
  return (t.input * rate(prices, model, 'input') + t.output * rate(prices, model, 'output')
    + t.cache_read * rate(prices, model, 'cache_read') + t.cache_write * rate(prices, model, 'cache_write_1h')) / 1_000_000
}

const num = (v: unknown): number => (typeof v === 'number' && Number.isFinite(v) ? v : Number(v) || 0)
const str = (v: unknown): string | null => (typeof v === 'string' && v !== '' ? v : null)
const round6 = (x: number) => Math.round(x * 1e6) / 1e6

/** One `api_request` record as a ledger line: the collector's shape, `source` "mod". */
export function toLine(e: TelemetryRecordEntry, prices: Prices, sessionId: string, ts: string): LedgerLine {
  const a = e.attributes
  const model = str(a.model) ?? 'unknown'
  const tokens: Tokens = {
    input: Math.trunc(num(a.input_tokens)),
    output: Math.trunc(num(a.output_tokens)),
    cache_read: Math.trunc(num(a.cache_read_tokens ?? a.cache_read_input_tokens)),
    cache_write: Math.trunc(num(a.cache_creation_tokens ?? a.cache_creation_input_tokens)),
  }
  const ours = price(prices, model, tokens)
  // Claude Code's own figure for comparison; what it has on top of the token price is a
  // non-token charge (web search is billed per search). cost_usd stays tokens only.
  const cc = a.cost_usd === undefined || a.cost_usd === null ? null : num(a.cost_usd)
  const fees = cc !== null && cc - ours > 0.0005 ? round6(cc - ours) : 0
  const line: LedgerLine = {
    ts, source: 'mod', session_id: sessionId, model, tokens,
    cost_usd: round6(ours), cost_usd_cc: cc, fees_usd: fees,
    query_source: str(a.query_source), request_id: str(a.request_id),
    prices_verified: Boolean(prices._verified),
  }
  if (a.duration_ms !== undefined) line.duration_ms = Math.trunc(num(a.duration_ms))
  return line
}

let prices: Prices | undefined        // read once per load from the plugin's own file
let queue: Promise<void> = Promise.resolve()   // $.fs.write replaces the file: appends are serialised
const part = new Map<string, number>()         // session id -> which -N file is current

async function loadPrices($: EngineInterface): Promise<Prices> {
  if (prices === undefined) prices = JSON.parse(await $.fs.read(`${$.plugin.root}/keeptabs/prices.json`)) as Prices
  return prices
}

async function home($: EngineInterface): Promise<string> {
  const h = await $.env.get('HOME')
  if (!h) throw new Error('HOME is unset')
  return dataDir(h)
}

/** Appends one line: read the file, write it back with the line added. `$.fs.write` replaces
 *  the file in place and the engine has no append, rename or delete, so a crash in the middle
 *  of a write can leave a cut last line; the next append drops that tail, never the file. */
function append($: EngineInterface, data: string, line: LedgerLine): Promise<void> {
  queue = queue.then(async () => {
    const dir = `${data}/ledger/${dayOf(Date.parse(line.ts))}`
    const text = JSON.stringify(line) + '\n'
    for (let n = part.get(line.session_id) ?? 1; ; n++) {
      const path = `${dir}/${line.session_id}${n > 1 ? `-${n}` : ''}.jsonl`
      let prev = ''
      try { prev = await $.fs.read(path) } catch { /* first line of the day */ }
      if (prev !== '' && !prev.endsWith('\n')) prev = prev.slice(0, prev.lastIndexOf('\n') + 1)
      if (prev.length + text.length > ROLL_BYTES) continue
      part.set(line.session_id, n)
      await $.fs.write(path, prev + text)
      return
    }
  }).catch(() => { /* a failed append must not stop the next one */ })
  return queue
}

// ---------- the pane ----------

const PANE = 'keeptabs'
const TITLE = 'keeptabs'
// The slash command is the `status` skill's name: a registered command (`$.command.register`)
// runs as a flat `/<name>` with no plugin prefix and `status` is the built-in's, so the
// `command.run` hook below answers the skill's command in its place instead. The skill's own
// text runs only where the module did not load.
const COMMAND = 'keeptabs-mod:status'
// Open the pane unasked at session start. A plain constant until the Options view
// (phase 3) puts it behind the manifest's `userConfig`.
const AUTO_OPEN = true
const REDRAW_MS = 30_000
const HELPER_TIMEOUT_MS = 20_000

/** The four colours keeptabs.py uses, as hex from the xterm palette rather than theme keys
 *  or colour names, so every surface (Desktop's light mode included) draws the terminal look. */
export const PALETTE: Record<string, string> = { '31': '#cd0000', '32': '#00cd00', '33': '#cdcd00', '36': '#00cdcd' }

export type Span = { text: string; bold?: true; dim?: true; color?: string }

const ANSI = /\x1b\[([0-9;]*)m/g
export const stripAnsi = (s: string): string => s.replace(ANSI, '')

/** One rendered line as styled spans: SGR 1 → bold, 2 → dim, 31/32/33/36 → the palette
 *  colour, 0 (and a bare `ESC[m`) → everything off, 22 → bold and dim off, 39 → colour
 *  off. Anything else is dropped; keeptabs.py writes nothing else. */
export function spansOf(line: string): Span[] {
  const out: Span[] = []
  let bold = false, dim = false, color: string | undefined
  const push = (text: string) => {
    if (text === '') return
    const span: Span = { text }
    if (bold) span.bold = true
    if (dim) span.dim = true
    if (color !== undefined) span.color = color
    out.push(span)
  }
  let last = 0
  for (const m of line.matchAll(ANSI)) {
    push(line.slice(last, m.index))
    last = m.index + m[0].length
    for (const code of (m[1] || '0').split(';')) {
      if (code === '0' || code === '') { bold = dim = false; color = undefined }
      else if (code === '1') bold = true
      else if (code === '2') dim = true
      else if (code === '22') bold = dim = false
      else if (code === '39') color = undefined
      else if (PALETTE[code] !== undefined) color = PALETTE[code]
    }
  }
  push(line.slice(last))
  return out
}

/** `keeptabs.py --once` as the pane's lines: the `ctrl-c to quit` line goes (there is no
 *  ctrl-c in a pane) and so do blank lines at the end. */
export function liveLines(text: string): string[] {
  const lines = text.replace(/\r/g, '').split('\n').filter(l => stripAnsi(l).trim() !== 'ctrl-c to quit')
  while (lines.length > 0 && stripAnsi(lines[lines.length - 1]!).trim() === '') lines.pop()
  return lines
}

type Live = { lines: string[]; status: string; error?: string }

let live: Live | undefined              // the last helper output; a reload starts empty
let sessionId: string | undefined
let tick: Timer | undefined             // set while this load draws anything (the 30 s clock)
let running: Promise<void> | undefined  // one refresh at a time; a request during one runs after it
let again = false

async function sid($: EngineInterface): Promise<string> {
  return (sessionId ??= await $.session.id())
}

/** Runs one of the plugin's Python helpers; its stdout, or the last line of stderr as the error. */
async function python($: EngineInterface, script: string, args: readonly string[]): Promise<string> {
  const r = await $.process.run(['python3', `${$.plugin.root}/keeptabs/${script}`, ...args], { timeoutMs: HELPER_TIMEOUT_MS })
  if (r.exitCode !== 0) throw new Error(r.stderr.trim().split('\n').pop() || `${script} exited ${r.exitCode}`)
  return r.stdout
}

async function isPlaced($: EngineInterface): Promise<boolean> {
  try { return (await $.ui.panes()).some(p => p.id === PANE && p.isPlaced) } catch { return false }
}

/** One redraw: the live view when the pane is placed (and the status line cleared), else
 *  the status line alone. One helper process either way. */
async function refreshNow($: EngineInterface): Promise<void> {
  const id = await sid($)
  try {
    if (await isPlaced($)) {
      const lines = liveLines(await python($, 'keeptabs.py', ['--once', '--session', id]))
      live = { lines, status: live?.status ?? '' }
      $.ui.status(undefined)
    } else {
      const status = (await python($, 'keeptabs.py', ['--status', '--session', id])).trim()
      live = { lines: live?.lines ?? [], status }
      $.ui.status(status)
    }
  } catch (err) {
    live = { lines: live?.lines ?? [], status: live?.status ?? '', error: String(err instanceof Error ? err.message : err) }
  }
  $.ui.invalidate('ui.render')
}

/** Asks for a redraw; redraws in flight are not stacked, one more runs after the current. */
function refresh($: EngineInterface): Promise<void> {
  if (running) { again = true; return running }
  running = refreshNow($).catch(() => { /* drawn as the last good state */ }).then(async () => {
    running = undefined
    if (again) { again = false; await refresh($) }
  })
  return running
}

function startClock($: EngineInterface): void {
  tick?.cancel()
  tick = $.clock.every(REDRAW_MS, () => { void refresh($) })
}

async function openPane($: EngineInterface): Promise<UiOpenResult> {
  try { return await $.ui.open({ id: PANE, title: TITLE }) }
  catch (err) { return { isPlaced: false, reason: String(err instanceof Error ? err.message : err) } }
}

/** The status page (the Sankey, the chart, every figure) written now by sankey.py as
 *  `status/<session>.html` in the data folder, then opened in the default browser. A
 *  Button rather than a Link: a remote surface draws a `file:` Link as plain text. */
async function openSnapshot($: EngineInterface): Promise<void> {
  try {
    const id = await sid($)
    const path = `${await home($)}/status/${id}.html`
    await python($, 'sankey.py', ['--session', id, '--out', path])
    const opened = await $.process.run(['open', path], { timeoutMs: HELPER_TIMEOUT_MS })
    if (opened.exitCode !== 0) throw new Error(opened.stderr.trim() || `open exited ${opened.exitCode}`)
    $.ui.toast(`keeptabs: status page opened (${path})`)
  } catch (err) {
    $.ui.toast(`keeptabs: could not open the status page: ${err instanceof Error ? err.message : String(err)}`)
  }
}

export const register: Register = on => {
  on('session.start', async ($, e, next) => {
    const r = await next(e)
    try {
      const data = await home($)
      // The loaded marker: scripts/loaded-check.sh looks for it on the first prompt.
      await $.fs.write(`${data}/state/loaded/${await $.session.id()}`, new Date(await $.clock.now()).toISOString() + '\n')
      // First run: the shipped limits become the user's budget.json, never touched again.
      if (!(await $.fs.exists(`${data}/budget.json`))) {
        await $.fs.write(`${data}/budget.json`, await $.fs.read(`${$.plugin.root}/keeptabs/budget.json`))
      }
    } catch { /* nothing to track yet; the shell gate reports a module that did not load */ }

    // A reload runs this again: every step below is idempotent (an open id retitles).
    live = undefined
    sessionId = undefined
    if (e.isInteractive && e.surface !== null) {   // `claude -p` and a bare host: nothing visual
      if (AUTO_OPEN) await openPane($)
      startClock($)
      void refresh($)
    }
    return r
  })

  // The terminal draws each line as styled Text spans. A remote surface (Desktop, VS Code,
  // mobile) draws Text in a proportional font and collapses an empty one, which breaks the
  // renderer's columns; there the view is one Code block of the plain text, in the
  // surface's code font, so the layout holds and blank lines stay, at the price of bold,
  // dim and the four colours. (Seen on Desktop 2.1.295; the types offer no monospace Text.)
  on('ui.render', { component: 'Pane', requestId: PANE }, ($, e) => {
    const { Box, Text, Button, Code } = $.ui.resolve(e)
    const l = live
    // Placed after a status-only refresh (the terminal widened): fetch the lines once.
    if (l === undefined || (l.lines.length === 0 && l.error === undefined)) void refresh($)
    const lines = l?.lines ?? []
    return (
      <Box flexDirection="column">
        <Box>
          <Button key="open-browser" label="open in browser" hotkey="o" onPress={() => { void openSnapshot($) }} />
          <Text dimColor>  the status page: where the money went, cost over time</Text>
        </Box>
        {l === undefined && <Text dimColor>loading...</Text>}
        {l?.error !== undefined && <Text color="error">keeptabs: {l.error}</Text>}
        {e.surface === 'terminal'
          ? lines.map(line => (
            <Text wrap="end">
              {spansOf(line === '' ? ' ' : line).map(s => <Text bold={s.bold} dimColor={s.dim} color={s.color}>{s.text}</Text>)}
            </Text>
          ))
          : lines.length > 0 && <Code source={lines.map(stripAnsi).join('\n')} />}
      </Box>
    )
  })

  // `/keeptabs-mod:status`: asked, so the pane is placed at any width. Answered here without
  // `next`, so the skill's text never runs while the module is loaded. The figures go back
  // as `context` too, so Claude can answer "what have I spent" from them.
  on('command.run', { command: COMMAND }, async $ => {
    const opened = await openPane($)
    if (tick === undefined) startClock($)
    const id = await sid($)
    let status = ''
    let figures = ''
    try {
      status = (await python($, 'keeptabs.py', ['--status', '--session', id])).trim()
      figures = liveLines(await python($, 'keeptabs.py', ['--once', '--session', id])).map(stripAnsi).join('\n')
    } catch (err) {
      status = `keeptabs: ${err instanceof Error ? err.message : String(err)}`
    }
    void refresh($)
    const where = opened.isPlaced
      ? 'The keeptabs pane is open beside the chat; "open in browser" there writes the status page.'
      : `The keeptabs pane is not placed here (${opened.reason}).`
    return { text: `${status}\n${where}`, context: figures === '' ? undefined : [`keeptabs, this session's figures as of now:\n${figures}`] }
  }).catch(() => ({ text: 'keeptabs: the status command failed' }))

  // Every API request's usage, in-process. The record goes on first (`next`), then our
  // line is written: the hook never denies, and a failure here never touches the record.
  on('telemetry.log', { to: 'collector' }, async ($, e, next) => {
    const r = await next(e)
    if (e.event === 'api_request') {
      try {
        const id = str(e.attributes['session.id']) ?? await $.session.id()
        const ts = e.loggedAt || new Date(await $.clock.now()).toISOString()
        await append($, await home($), toLine(e, await loadPrices($), id, ts))
        if (tick !== undefined) void refresh($)
      } catch { /* see above */ }
    }
    return r
  }).catch(($, e, next) => next(e))
}
