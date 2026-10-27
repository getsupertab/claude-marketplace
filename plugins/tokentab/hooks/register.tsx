import type { EngineInterface, PluginOptions, Register, RenderChildren, TelemetryRecordEntry, Timer, UiOpenResult } from 'claude-code'

// TokenTab's hooks module (docs/plan-CORE-537-status-page-tabs.md), three parts:
//
// The ledger (phase 0). Every API request the engine makes reaches `telemetry.log` as
// an `api_request` record for the operator's collector, with or without telemetry
// switched on, main loop and helpers alike. This module prices it and appends one
// line to the session's ledger file; the Python accounting reads those.
//
// The pane (phase 1, tabs and the flow 2026-10-16; the Figma redesign 2026-10-21).
// `session.start` opens a pane beside the chat (the `autoOpen` option); `/tokentab:status`
// (the status skill, answered by a hook) opens it asked. `ui.render` draws one of two
// tabs: Spend, the session against its limit, its cost by rate and what each tool cost,
// today and this month; Budget, the auto-open switch and the budget limits. Spend draws
// `tokentab.py --report`, the same report() the terminal live view and the status line
// print, so the figures cannot drift apart. While the pane is not placed
// (a narrow terminal) one status line carries the numbers instead. Redrawn a second after
// a ledger write and on a 30 s clock, both by the clock; nothing polls the data folder.
// Under `claude -p`, or with no surface, nothing visual happens and the ledger is
// written as before.
//
// The status page. "open in browser" writes `status/<session>.html` and opens it; for
// 30 minutes after, every redraw also writes its data file beside it (`sankey.py --push`),
// which the open page loads as a script and swaps in place: live without a server.

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

/** The plugin's data folder, under Claude Code's config folder: `CLAUDE_CONFIG_DIR` when set,
 *  else `.claude` in `USERPROFILE` (Windows), else in `HOME`. `$.plugin` has no data path, so
 *  it is built here, the same path the Python and the shell gate build. */
export function dataDir(config: string | undefined, profile: string | undefined, home: string | undefined): string {
  const trim = (path: string | undefined) => path?.replace(/[\\/]+$/, '')
  const base = trim(profile) || trim(home)
  const root = trim(config) || (base && `${base}/.claude`)
  if (!root) throw new Error('USERPROFILE and HOME are unset')
  return `${root}/plugins/data/tokentab-supertab`
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
  if (prices === undefined) prices = JSON.parse(await $.fs.read(`${$.plugin.root}/tokentab/prices.json`)) as Prices
  return prices
}

async function home($: EngineInterface): Promise<string> {
  return dataDir(await $.env.get('CLAUDE_CONFIG_DIR'), await $.env.get('USERPROFILE'), await $.env.get('HOME'))
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

const PANE = 'tokentab'
const TITLE = 'TokenTab'
// The slash command is the `status` skill's name: a registered command (`$.command.register`)
// runs as a flat `/<name>` with no plugin prefix and `status` is the built-in's, so the
// `command.run` hook below answers the skill's command in its place instead. The skill's own
// text runs only where the module did not load.
const COMMAND = 'tokentab:status'
const REDRAW_MS = 30_000
const BEAT_MS = 1_000                   // how soon after a ledger write the pane redraws
const HELPER_TIMEOUT_MS = 20_000
// The status page is kept current (its data file written on every redraw) this long
// after it was last opened from the pane; the page says "stale" once the pushes stop.
const PAGE_MS = 30 * 60_000

const ANSI = /\x1b\[([0-9;]*)m/g
export const stripAnsi = (s: string): string => s.replace(ANSI, '')

/** `tokentab.py --once` as plain lines for Claude's context: the `ctrl-c to quit` line
 *  goes and so do blank lines at the end. */
export function liveLines(text: string): string[] {
  const lines = text.replace(/\r/g, '').split('\n').filter(l => stripAnsi(l).trim() !== 'ctrl-c to quit')
  while (lines.length > 0 && stripAnsi(lines[lines.length - 1]!).trim() === '') lines.pop()
  return lines
}

// ---------- the report the tabs draw ----------

/** `tokentab.py --report`: Spend as data, from the same report() as the live
 *  view and the status line. `session` is null until the session's first ledger line. */
export type Report = {
  sid: string
  prices_verified: boolean
  today: number
  month: number
  overhead: number
  session: null | {
    title: string; cost: number; tokens: number; msgs: number
    limit_usd: number | null; frac: number | null
    warn_at: number; hard_stop: boolean
  }
  rates?: { label: string; key: string; tokens: number; cost: number; share: number }[]
  total?: { tokens: number; cost: number }
  unmatched?: number
  tools: { name: string; calls: number; cost: number; ask: number; ctx: number; read: number; ctx_tokens: number }[]
}

const grouped = (n: number, digits: number) =>
  n.toLocaleString('en-US', { minimumFractionDigits: digits, maximumFractionDigits: digits })
/** Dollars as the design writes them: 2 decimals for the hero and the totals, 4 in the tables. */
export const usd = (x: number, digits = 2): string => `$${grouped(x, digits)}`
export const int = (n: number): string => grouped(Math.round(n), 0)
export const pct = (share: number): string => `${Math.round(share * 100)}%`

/** How close a scope is to its limit: error at 100% and over, warning from `warn_at`. */
export type Tone = 'success' | 'warning' | 'error'
export function toneOf(frac: number, warnAt: number): Tone {
  return frac >= 1 ? 'error' : frac * 100 >= warnAt ? 'warning' : 'success'
}

// The series colours from the Figma frames, a light and a dark value each: the Svg bars
// pick one through prefers-color-scheme (the API carries no theme), the dots in the
// tables take the light one, a mid tone that reads on both grounds.
export const SERIES: Record<string, [string, string]> = {
  input: ['#9a99a6', '#a9a8b4'],
  cache_write_1h: ['#6b6fc5', '#8d90e0'],
  cache_write_5m: ['#9a9ee0', '#b3b6ee'],
  cache_read: ['#5ba899', '#6cc2b1'],
  output: ['#e0973a', '#f0ab55'],
  thinking: ['#a874c9', '#c08fe0'],
  fees: ['#c9655b', '#e07e74'],
  ask: ['#7f80cf', '#9a9be6'],
  ctx: ['#5ba899', '#6cc2b1'],
  read: ['#d9a23e', '#ebb757'],
  success: ['#2e7d32', '#5fd75f'],
  warning: ['#c18f00', '#e5c07b'],
  error: ['#c0392b', '#ff6b6b'],
  track: ['#ecebe6', '#3a3a3a'],
}
const BAR_STYLE = Object.entries(SERIES).map(([k, [light]]) => `.${k}{fill:${light}}`).join('')
  + '@media(prefers-color-scheme:dark){' + Object.entries(SERIES).map(([k, [, dark]]) => `.${k}{fill:${dark}}`).join('') + '}'

/** One bar as an Svg: a track the full width, the parts on it left to right, each a
 *  fraction of the width (cut at the end of the track), a hairline between them. Drawn at
 *  the slot's width, `height` pixels high. */
export function barSvg(parts: { series: string; frac: number }[], height = 6): string {
  const W = 1000, GAP = 3
  let x = 0
  const rects: string[] = []
  for (const p of parts) {
    const w = Math.min(Math.max(0, p.frac) * W, W - x)
    if (w <= 0) continue
    rects.push(`<rect class="${p.series}" x="${x.toFixed(1)}" y="0" width="${Math.max(1, w - (x + w < W ? GAP : 0)).toFixed(1)}" height="${height}"/>`)
    x += w
  }
  return `<svg xmlns="http://www.w3.org/2000/svg" width="${W}" height="${height}" viewBox="0 0 ${W} ${height}" preserveAspectRatio="none">`
    + `<style>${BAR_STYLE}</style><rect class="track" x="0" y="0" width="${W}" height="${height}"/>${rects.join('')}</svg>`
}

/** The same bar in block characters for the terminal: `cells` wide, each part its share
 *  of the cells in its colour, the rest of the track dim dots. */
export function barCells(parts: { series: string; frac: number }[], cells: number): { text: string; series?: string }[] {
  const out: { text: string; series?: string }[] = []
  let used = 0
  for (const p of parts) {
    const n = Math.min(cells - used, Math.round(Math.max(0, p.frac) * cells))
    if (n <= 0) continue
    out.push({ text: '█'.repeat(n), series: p.series })
    used += n
  }
  if (used < cells) out.push({ text: '·'.repeat(cells - used) })
  return out
}

// ---------- the tabs ----------

export type View = 'spend' | 'budget'
export const TABS: { view: View; label: string; hotkey: string }[] = [
  { view: 'spend', label: 'Spend', hotkey: 's' },
  { view: 'budget', label: 'Budget', hotkey: 'b' },
]
const TOOLS_SHOWN = 12

// The budget limits the Budget tab edits, in budget.json's keys (dotted below the
// top level), as sections of rows: a limit is an Input, a switch an on/off Button.
// Every write goes through tokentab/budget.py, the one validator.
export type Setting = { key: string; label: string; kind: 'limit' | 'switch' }
export const SECTIONS: { title: string; rows: Setting[] }[] = [
  { title: 'budgets', rows: [
    { key: 'budgets_on', label: 'check the limits below', kind: 'switch' },
  ] },
  { title: 'this session', rows: [
    { key: 'session_limit_usd', label: 'USD', kind: 'limit' },
  ] },
  { title: 'today', rows: [
    { key: 'daily_limit_usd', label: 'USD', kind: 'limit' },
  ] },
  { title: 'at the limit', rows: [
    { key: 'warn_at_percent', label: 'warn at, %', kind: 'limit' },
    { key: 'hard_stop', label: 'hard stop at 100%', kind: 'switch' },
  ] },
]

type Json = Record<string, unknown>

/** The value under a dotted key of a parsed budget.json. */
export function at(cfg: Json | undefined, key: string): unknown {
  let node: unknown = cfg
  for (const k of key.split('.')) {
    if (typeof node !== 'object' || node === null) return undefined
    node = (node as Json)[k]
  }
  return node
}

/** A limit as the field shows it: `off` for null, thousands grouped for tokens. */
export function shown(v: unknown): string {
  if (v === null || v === undefined) return 'off'
  if (typeof v === 'number') return Number.isInteger(v) ? v.toLocaleString('en-US') : String(v)
  return String(v)
}

type Options = { budget?: Json; path: string; errors: Record<string, string>; error?: string }

let opts: PluginOptions = {}           // the manifest's userConfig values, from register()
let view: View = 'spend'
let detailsOpen = false                 // Spend's rate breakdown and tools, folded until asked for
let report: Report | undefined          // the last `--report`; a reload starts empty
let error: string | undefined           // the last refresh's failure, drawn above the tab
let options: Options | undefined        // the last budget.json read
let pageOpenedAt: number | undefined    // ms; the page's data file is written while within PAGE_MS
let sessionId: string | undefined
let tick: Timer | undefined             // set while this load draws anything (the clock's beat)
let beats = 0                           // beats since the clock started; every REDRAW_MS worth redraws
let stale = false                       // a ledger line written since the last redraw: the next beat redraws
let running: Promise<void> | undefined  // one refresh at a time; a request during one runs after it
let again = false
let columns = 60                        // the pane body's width in cells, from the last render
let waiting = false                     // session.start's open left the pane undrawn: the first prompt opens it
let showing = false                     // this session's auto-open and clock have started (startShowing)
let autoOpenHere: boolean | undefined   // the switch set where /config had no row for it, `$.store`'s AUTO_OPEN

// An SDK host (the Desktop app, `claude -p`) lists no plugin's /config row, so there the
// Budget tab's switch is kept in `$.store` and stands over the manifest's option, until a
// change through /config clears it.
const AUTO_OPEN = 'autoOpen'
const autoOpen = () => autoOpenHere ?? opts.autoOpen !== false

async function sid($: EngineInterface): Promise<string> {
  return (sessionId ??= await $.session.id())
}

// The launchers tried in order, the same as scripts/guard.sh. Each must say it is Python 3,
// so a `python3` that is not (the Microsoft Store stub, a Python 2) is passed over.
export const LAUNCHERS: readonly (readonly string[])[] = [['python3'], ['python'], ['py', '-3']]
const PYTHON = 'python'                  // `$.store` key of the launcher found
let launcher: Promise<readonly string[]> | undefined   // the choice for this load, probed at most once
let chosen: readonly string[] | undefined              // what `launcher` resolved to

async function probe($: EngineInterface): Promise<readonly string[]> {
  for (const candidate of LAUNCHERS) {
    try {
      const r = await $.process.run([...candidate, '-c', 'import sys; print(sys.version_info[0])'], { timeoutMs: HELPER_TIMEOUT_MS })
      if (r.exitCode === 0 && r.stdout.trim() === '3') {
        await $.store.set(PYTHON, [...candidate]).catch(() => { /* found for this load anyway */ })
        return candidate
      }
    } catch { /* not on this machine */ }
  }
  throw new Error(`no Python 3 found (tried ${LAUNCHERS.map(l => l.join(' ')).join(', ')})`)
}

/** The launcher: the one the store kept, else the first of LAUNCHERS that is Python 3.
 *  `failed`, a launcher that would not start, probes again unless another call already did. */
function findPython($: EngineInterface, failed?: readonly string[]): Promise<readonly string[]> {
  const reprobe = failed !== undefined && failed === chosen
  if (launcher === undefined || reprobe) {
    chosen = undefined
    const pick = async (): Promise<readonly string[]> => {
      const kept = reprobe ? undefined : await $.store.get(PYTHON).catch(() => undefined)
      const py = Array.isArray(kept) && kept.length > 0 && kept.every(a => typeof a === 'string') ? kept as string[] : await probe($)
      if (launcher === next) chosen = py
      return py
    }
    const next = pick()
    launcher = next
    next.catch(() => { if (launcher === next) launcher = undefined })   // nothing found: the next call probes again
  }
  return launcher
}

/** Runs one of the plugin's Python helpers; its stdout, or the last line of stderr as the error.
 *  A launcher that no longer starts is probed for again, once. */
async function python($: EngineInterface, script: string, args: readonly string[]): Promise<string> {
  const argv = (py: readonly string[]) => [...py, `${$.plugin.root}/tokentab/${script}`, ...args]
  const py = await findPython($)
  let r
  try {
    r = await $.process.run(argv(py), { timeoutMs: HELPER_TIMEOUT_MS })
  } catch {
    r = await $.process.run(argv(await findPython($, py)), { timeoutMs: HELPER_TIMEOUT_MS })
  }
  if (r.exitCode !== 0) throw new Error(r.stderr.trim().split('\n').pop() || `${script} exited ${r.exitCode}`)
  return r.stdout
}

async function isPlaced($: EngineInterface): Promise<boolean> {
  try { return (await $.ui.panes()).some(p => p.id === PANE && p.isPlaced) } catch { return false }
}

const pagePath = (data: string, id: string) => `${data}/status/${id}.html`
const pushPath = (data: string, id: string) => `${data}/status/${id}.js`

async function pageIsOpen($: EngineInterface): Promise<boolean> {
  return pageOpenedAt !== undefined && (await $.clock.now()) - pageOpenedAt < PAGE_MS
}

async function readBudget($: EngineInterface): Promise<void> {
  const path = `${await home($)}/budget.json`
  const errors = options?.errors ?? {}
  try {
    options = { budget: JSON.parse(await $.fs.read(path)) as Json, path, errors }
  } catch (err) {
    options = { path, errors, error: `budget.json: ${err instanceof Error ? err.message : String(err)}` }
  }
}

/** One redraw: the current tab's data when the pane is placed (and the status line
 *  cleared), else the status line alone. Spend is one `--report` run; the status page's
 *  data file, while that page is open, is written by one more. */
async function refreshNow($: EngineInterface): Promise<void> {
  const id = await sid($)
  try {
    if (await isPlaced($)) {
      if (view === 'budget') await readBudget($)
      else report = JSON.parse(await python($, 'tokentab.py', ['--report', '--session', id])) as Report
      $.ui.status(undefined)
    } else {
      $.ui.status((await python($, 'tokentab.py', ['--status', '--session', id])).trim())
    }
    if (await pageIsOpen($)) await python($, 'sankey.py', ['--session', id, '--push', pushPath(await home($), id)])
    error = undefined
  } catch (err) {
    error = String(err instanceof Error ? err.message : err)
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

/** The clock that keeps the pane current: on the beat after a ledger write, else every
 *  REDRAW_MS. A ledger write only marks the pane stale, so the redraw runs here, on the
 *  `$` of the hook that started the clock, never on the `telemetry.log` dispatch's: a
 *  subagent's API request raises that in the subagent's loop, and while subagents work
 *  their requests would start nearly every redraw, a tab press's and the clock's folded
 *  in. One beat also takes a burst of parallel subagents' requests as one redraw. */
function startClock($: EngineInterface): void {
  tick?.cancel()
  beats = 0
  tick = $.clock.every(BEAT_MS, () => {
    if (!stale && ++beats % (REDRAW_MS / BEAT_MS) !== 0) return
    stale = false
    beats = 0
    void refresh($)
  })
}

function show($: EngineInterface, v: View): void {
  view = v
  $.ui.invalidate('ui.render')
  void refresh($)
}

async function openPane($: EngineInterface): Promise<UiOpenResult> {
  try { return await $.ui.open({ id: PANE, title: TITLE }) }
  catch (err) { return { isPlaced: false, reason: String(err instanceof Error ? err.message : err) } }
}

let isMac: Promise<boolean> | undefined   // `uname -s` asked at most once a load

/** What opens a file in the default browser here: `explorer.exe` on Windows (its exit code
 *  says nothing: 1 on success too), `open` on macOS, `xdg-open` elsewhere. */
async function opener($: EngineInterface, path: string): Promise<{ argv: string[]; checkExit: boolean }> {
  if ((await $.env.get('OS')) === 'Windows_NT') return { argv: ['explorer.exe', path.replace(/\//g, '\\')], checkExit: false }
  isMac ??= $.process.run(['uname', '-s'], { timeoutMs: HELPER_TIMEOUT_MS }).then(r => r.stdout.trim() === 'Darwin', () => false)
  return { argv: [(await isMac) ? 'open' : 'xdg-open', path], checkExit: true }
}

/** The status page (the Sankey, the chart, every figure) written now by sankey.py as
 *  `status/<session>.html` in the data folder, then opened in the default browser. A
 *  Button rather than a Link: a remote surface draws a `file:` Link as plain text. From
 *  now on, for PAGE_MS, every redraw writes the page's data file too (`$.store` keeps
 *  the moment across a reload of the module). */
async function openSnapshot($: EngineInterface): Promise<void> {
  let written: string | undefined   // the page, once sankey.py wrote it: a failed open names it
  try {
    const id = await sid($)
    const path = pagePath(await home($), id)
    await python($, 'sankey.py', ['--session', id, '--out', path])
    written = path
    const { argv, checkExit } = await opener($, path)
    const opened = await $.process.run(argv, { timeoutMs: HELPER_TIMEOUT_MS })
    if (checkExit && opened.exitCode !== 0) throw new Error(opened.stderr.trim() || `${argv[0]} exited ${opened.exitCode}`)
    pageOpenedAt = await $.clock.now()
    await $.store.set(`pageOpenedAt:${id}`, pageOpenedAt)
    $.ui.toast(`TokenTab: status page opened (${path}); it stays live for 30 minutes`)
    if (tick === undefined) startClock($)
  } catch (err) {
    $.ui.toast(`TokenTab: could not open the status page${written ? ` (${written})` : ''}: ${err instanceof Error ? err.message : String(err)}`)
  }
}

/** The auto-open switch is the manifest's `userConfig` field: written as the config menu
 *  would, so the menu, the install screen and the pane show one value. The write reloads
 *  the module with the new options; `session.start` then runs again and is idempotent.
 *  Where /config has no row for it (an SDK host), it is kept in `$.store` instead. A stored
 *  value is cleared before the /config write, so the reload cannot read it back. */
async function setAutoOpen($: EngineInterface, value: boolean): Promise<void> {
  const key = `${$.plugin.name}.autoOpen`
  try {
    if (!(await $.config.list()).some(row => row.key === key)) {
      autoOpenHere = value
      await $.store.set(AUTO_OPEN, value)
      $.ui.invalidate('ui.render')
      return
    }
    const stored = autoOpenHere
    await $.store.delete(AUTO_OPEN)
    const r = await $.config.set({ key, value })
    if ('deny' in r && r.deny !== undefined) {
      if (stored !== undefined) await $.store.set(AUTO_OPEN, stored)
      $.ui.toast(`TokenTab: auto-open not changed: ${r.deny}`)
    }
  } catch (err) {
    $.ui.toast(`TokenTab: auto-open not changed: ${err instanceof Error ? err.message : String(err)}`)
  }
}

/** One limit through the validator; a refused value is shown under its field, unwritten. */
async function setBudget($: EngineInterface, key: string, value: string): Promise<void> {
  if (options === undefined) await readBudget($)
  const errors = options?.errors ?? {}
  try {
    await python($, 'budget.py', ['set', key, value])
    delete errors[key]
  } catch (err) {
    errors[key] = err instanceof Error ? err.message : String(err)
  }
  await readBudget($)
  $.ui.invalidate('ui.render')
}

/** Once a session someone looks at, from whichever comes first, `session.start` with a
 *  surface or the first `session.attach`: the auto-open, the 30 s clock and a first
 *  redraw. Once, so a second client joining does not reopen a pane closed by hand. */
async function startShowing($: EngineInterface): Promise<void> {
  if (showing) return
  showing = true
  try { pageOpenedAt = num(await $.store.get(`pageOpenedAt:${await sid($)}`)) || undefined } catch { /* none */ }
  if (autoOpen()) {
    const opened = await openPane($)
    // Unasked, the engine places it only from 144 columns (110 once opened before).
    // Not placed: the first prompt opens it asked (`prompt.submit`), and the toast says so.
    if (!opened.isPlaced) {
      waiting = true
      $.ui.toast(`TokenTab: the pane opens with your first prompt (${opened.reason})`)
    }
  }
  startClock($)
  void refresh($)
}

export const register: Register = (on, pluginOptions) => {
  opts = pluginOptions ?? {}

  on('session.start', async ($, e, next) => {
    const r = await next(e)
    try {
      const data = await home($)
      // The loaded marker: scripts/loaded-check.sh looks for it on the first prompt.
      await $.fs.write(`${data}/state/loaded/${await $.session.id()}`, new Date(await $.clock.now()).toISOString() + '\n')
      // First run: the shipped limits become the user's budget.json, never touched again.
      if (!(await $.fs.exists(`${data}/budget.json`))) {
        await $.fs.write(`${data}/budget.json`, await $.fs.read(`${$.plugin.root}/tokentab/budget.json`))
      }
    } catch { /* nothing to track yet; the shell gate reports a module that did not load */ }

    // A reload runs this again: every step below is idempotent (an open id retitles).
    report = undefined
    error = undefined
    options = undefined
    sessionId = undefined
    waiting = false
    try {
      const v = await $.store.get(AUTO_OPEN)
      autoOpenHere = typeof v === 'boolean' ? v : undefined
    } catch { autoOpenHere = undefined }
    showing = false
    // The REPL starts with its terminal; an SDK host (the Desktop app) with none, so its
    // first `session.attach` starts the pane, or, on a reload, the surfaces already there.
    // A `-p` run never has one: nothing visual.
    if ((e.isInteractive && e.surface !== null) || (await $.session.surfaces()).length > 0) await startShowing($)
    return r
  })

  on('session.attach', async ($, e, next) => {
    const r = await next(e)
    try { await startShowing($) } catch { /* the pane waits for /tokentab:status */ }
    return r
  })

  // Two tabs over one report (Spend) and budget.json (Budget), the same tree on
  // every surface: the terminal ignores `size`, `weight` and `mono` and is monospaced
  // anyway; the bars are an Svg where the surface draws one and block characters in the
  // terminal. Only a Button takes a press, so the tabs and "open in browser" are Buttons.
  on('ui.render', { component: 'Pane', requestId: PANE }, ($, e) => {
    const { Box, Text, Button } = $.ui.resolve(e)
    columns = e.props.bodyColumns
    // A tab shown for the first time, or the pane placed after a status-only refresh:
    // fetch its data once. Only while nothing is in flight, so a render never starts a
    // refresh that invalidates into another render.
    const empty = view === 'budget' ? options === undefined : report === undefined && error === undefined
    if (empty && running === undefined) void refresh($)
    const tabs = (
      <Box gap={1}>
        {TABS.map(t => (
          <Button key={`tab-${t.view}`} label={t.label} hotkey={t.hotkey} variant={view === t.view ? 'primary' : 'secondary'}
            onPress={() => show($, t.view)} />
        ))}
        <Button key="open-browser" label="open in browser ↗" hotkey="o" onPress={() => { void openSnapshot($) }} />
      </Box>
    )
    const body = view === 'budget' ? budgetTab($, e)
      : report === undefined ? <Text dimColor>loading...</Text>
      : spendTab($, e, report)
    return (
      <Box flexDirection="column" gap={1}>
        {tabs}
        {error !== undefined && <Text color="error">TokenTab: {error}</Text>}
        {body}
      </Box>
    )
  })

  // `/tokentab:status`: asked, so the pane is placed at any width. Answered here without
  // `next`, so the skill's text never runs while the module is loaded. The figures go back
  // as `context` too, so Claude can answer "what have I spent" from them.
  on('command.run', { command: COMMAND }, async $ => {
    const opened = await openPane($)
    if (tick === undefined) startClock($)
    const id = await sid($)
    let status = ''
    let figures = ''
    try {
      status = (await python($, 'tokentab.py', ['--status', '--session', id])).trim()
      figures = liveLines(await python($, 'tokentab.py', ['--once', '--session', id])).map(stripAnsi).join('\n')
    } catch (err) {
      status = `TokenTab: ${err instanceof Error ? err.message : String(err)}`
    }
    void refresh($)
    const where = opened.isPlaced
      ? 'The TokenTab pane is open beside the chat; "open in browser" there writes the status page.'
      : `The TokenTab pane is not placed here (${opened.reason}).`
    return { text: `${status}\n${where}`, context: figures === '' ? undefined : [`TokenTab, this session's figures as of now:\n${figures}`] }
  }).catch(() => ({ text: 'TokenTab: the status command failed' }))

  // The person's change to the switch in /config: it stands over a value the Budget tab
  // kept in `$.store` where /config had no row. Cleared before the write, which reloads
  // the module (this module's own `$.config.set` does not come through here).
  on('config.set', async ($, e, next) => {
    if (e.key === `${$.plugin.name}.autoOpen`) {
      autoOpenHere = undefined
      await $.store.delete(AUTO_OPEN)
    }
    return next(e)
  }).catch(($, e, next) => next(e))

  // The person's first prompt after an auto-open the engine left undrawn: an open from
  // their own Enter is asked, so placed at any width. Once a session; the pane is opened
  // before the prompt enters, and a failure never holds the prompt back.
  on('prompt.submit', async ($, e, next) => {
    // `sdk`: a prompt typed in the Desktop app, an SDK host; `waiting` is only ever set
    // once a surface is there, so a `-p` run's prompt never opens anything.
    if (waiting && (e.origin.kind === 'composer' || e.origin.kind === 'bridge' || e.origin.kind === 'sdk')) {
      waiting = false
      if (!(await isPlaced($)) && (await openPane($)).isPlaced) void refresh($)
    }
    return next(e)
  }).catch(($, e, next) => next(e))

  // Every API request's usage, in-process. The record goes on first (`next`), then our
  // line is written: the hook never denies, and a failure here never touches the record.
  on('telemetry.log', { to: 'collector' }, async ($, e, next) => {
    const r = await next(e)
    if (e.event === 'api_request') {
      try {
        const id = str(e.attributes['session.id']) ?? await $.session.id()
        const ts = e.loggedAt || new Date(await $.clock.now()).toISOString()
        await append($, await home($), toLine(e, await loadPrices($), id, ts))
        if (tick !== undefined) stale = true   // the clock's next beat redraws, see startClock
      } catch { /* see above */ }
    }
    return r
  }).catch(($, e, next) => next(e))
}

// ---------- the kit the tabs are drawn with ----------

type RenderEvent = Parameters<EngineInterface['ui']['resolve']>[0]
type Part = { series: string; frac: number }

/** A bar: an Svg on the surfaces that draw one, block characters in the terminal (as
 *  wide as the pane's body, within reason). */
function bar($: EngineInterface, e: RenderEvent, parts: Part[], alt: string, height = 6): RenderChildren {
  if (e.surface !== 'terminal') {
    const { Svg } = $.ui.resolve(e)
    return <Svg source={barSvg(parts, height)} alt={alt} height={height} />
  }
  const { Text } = $.ui.resolve(e)
  return (
    <Text wrap="truncate">
      {barCells(parts, Math.max(16, Math.min(72, columns - 2))).map(c =>
        c.series === undefined ? <Text dimColor>{c.text}</Text> : <Text color={SERIES[c.series]![0]}>{c.text}</Text>)}
    </Text>
  )
}

/** A hairline between sections. */
function rule($: EngineInterface, e: RenderEvent): RenderChildren {
  const { Box, Text } = $.ui.resolve(e)
  // Clipped rather than truncated, which would end the line in an ellipsis
  return <Box height={1} overflow="hidden"><Text dimColor wrap="wrap">{'─'.repeat(120)}</Text></Box>
}

/** A section's small capitals label, as the frames set them. */
function label($: EngineInterface, e: RenderEvent, text: string): RenderChildren {
  const { Text } = $.ui.resolve(e)
  return <Text size="footnote" dimColor>{text.toUpperCase()}</Text>
}

/** A mono heading with a dim note beside it: `SESSION 47 msgs`, `TOOLS 8 tools · 27 calls`. */
function heading($: EngineInterface, e: RenderEvent, title: string, note: string): RenderChildren {
  const { Text } = $.ui.resolve(e)
  return <Text size="footnote" mono><Text mono bold>{title}</Text><Text mono dimColor>{`  ${note}`}</Text></Text>
}

/** A right-aligned mono cell of a fixed width, for the columns of a table. */
function cell($: EngineInterface, e: RenderEvent, text: string, width: number, style: { bold?: boolean; dim?: boolean } = {}): RenderChildren {
  const { Box, Text } = $.ui.resolve(e)
  return (
    <Box width={width} flexShrink={0} justifyContent="flex-end">
      <Text size="footnote" mono tabular bold={style.bold} dimColor={style.dim}>{text}</Text>
    </Box>
  )
}

/** Spend, Figma's "A · Hero number": the session against its limit as one figure and a
 *  bar, the rate breakdown and what each tool cost (folded under "details"), then every
 *  session today and this month. */
function spendTab($: EngineInterface, e: RenderEvent, r: Report): RenderChildren {
  const { Box, Text, Button } = $.ui.resolve(e)
  const s = r.session
  const allSessions = (
    <Box key="all" flexDirection="column" gap="xs">
      {label($, e, 'all sessions')}
      <Box>
        {([['Today', r.today], ['This month', r.month]] as const).map(([name, v]) => (
          <Box key={`all-${name}`} width="50%" flexDirection="column">
            <Text size="footnote" dimColor>{name}</Text>
            <Text size="heading" weight="bold" tabular>{usd(v)}</Text>
          </Box>
        ))}
      </Box>
    </Box>
  )
  if (s === null) {
    return (
      <Box flexDirection="column" gap={1}>
        <Text dimColor>No API call in this session yet; its spend shows from the first one.</Text>
        {rule($, e)}
        {allSessions}
      </Box>
    )
  }
  const tone = s.frac === null ? undefined : toneOf(s.frac, s.warn_at)
  const badge = s.frac === null || tone === undefined ? undefined
    : <Text size="footnote" weight="semibold" color={tone}>
        {tone === 'error' ? `Over · ${pct(s.frac)}` : tone === 'warning' ? `Warning · ${pct(s.frac)}` : `${pct(s.frac)} used`}
      </Text>
  const mode = `${s.hard_stop ? 'hard stop' : 'warn only'}, warns at ${s.warn_at}%`
  const under = [
    s.limit_usd !== null ? `of ${usd(s.limit_usd)}` : '',
    `${int(s.tokens)} tokens`,
    s.frac !== null ? mode : 'no session limit set',
  ].filter(x => x !== '').join(' · ')
  const rates = r.rates ?? []
  const series = (key: string) => (SERIES[key] ? key : 'input')
  const COLS = { tokens: 11, cost: 10, share: 5 }
  const row = (key: string, name: RenderChildren, tokens: string, cost: string, share: string, style: { bold?: boolean; dim?: boolean } = {}) => (
    <Box key={`rate-${key}`} alignItems="center">
      <Box flexGrow={1} flexShrink={1}>{name}</Box>
      {cell($, e, tokens, COLS.tokens, style)}
      {cell($, e, cost, COLS.cost, style)}
      {cell($, e, share, COLS.share, { dim: true, bold: style.bold })}
    </Box>
  )
  return (
    <Box flexDirection="column" gap={1}>
      <Box key="hero" flexDirection="column" gap="xs">
        <Box justifyContent="space-between" alignItems="center">
          {label($, e, 'session spend')}
          {badge}
        </Box>
        <Text size="title" weight="bold" tabular>{usd(s.cost)}</Text>
        {s.frac !== null && tone !== undefined && bar($, e, [{ series: tone, frac: s.frac }], `${pct(s.frac)} of the session limit`, 8)}
        <Text size="footnote" dimColor>{under}</Text>
      </Box>
      {rule($, e)}
      <Box key="details" flexDirection="column" gap="xs">
        <Button key="details-toggle" label="details" hotkey="d" plain
          onPress={() => { detailsOpen = !detailsOpen; $.ui.invalidate('ui.render') }}>
          <Text size="footnote" dimColor>{`${detailsOpen ? '▾' : '▸'} DETAILS`}</Text>
        </Button>
        {detailsOpen && (
          <Box key="details-body" flexDirection="column" gap="xs">
            {heading($, e, 'SESSION', `${int(s.msgs)} msgs`)}
            {bar($, e, rates.map(x => ({ series: series(x.key), frac: x.share })),
              `this session by rate: ${rates.map(x => `${x.label} ${pct(x.share)}`).join(', ')}`)}
            {row('head', <Text> </Text>, 'tokens', 'cost', '%', { dim: true })}
            {rates.map(x => row(x.key,
              <Text size="footnote" mono><Text color={SERIES[series(x.key)]![0]}>● </Text>{x.label}</Text>,
              int(x.tokens), usd(x.cost, 4), pct(x.share)))}
            {row('total', <Text size="footnote" mono bold>{'  total'}</Text>,
              int(r.total?.tokens ?? 0), usd(r.total?.cost ?? s.cost, 4), '', { bold: true })}
            {(r.unmatched ?? 0) > 0 && (
              <Text size="footnote" dimColor>{int(r.unmatched ?? 0)} output tok have no transcript line. Any thinking is counted in output.</Text>
            )}
            {rule($, e)}
            {toolsSection($, e, r)}
          </Box>
        )}
        {!r.prices_verified && <Text size="footnote" color="warning">Prices unverified: the figures are estimates at the default rates.</Text>}
      </Box>
      {rule($, e)}
      {allSessions}
    </Box>
  )
}

/** Spend's tools, Figma's "TokenTab / Tools": per tool its calls and cost, a bar of what
 *  it drew (asking, its answer into context, re-reading that) against the costliest
 *  tool, and the three figures under it. */
function toolsSection($: EngineInterface, e: RenderEvent, r: Report): RenderChildren {
  const { Box, Text } = $.ui.resolve(e)
  const tools = r.tools
  const calls = tools.reduce((n, t) => n + t.calls, 0)
  const top = Math.max(...tools.map(t => t.cost), 1e-9)
  return (
    <Box key="tools" flexDirection="column" gap={1}>
      <Box key="head" flexDirection="column" gap="xs">
        {heading($, e, 'TOOLS', `${tools.length} tools · ${calls} calls`)}
        <Text size="footnote" mono dimColor>cost = asking + answer into context + re-reading it (estimates)</Text>
      </Box>
      {tools.length === 0 && <Text dimColor>No tool calls in this session yet.</Text>}
      {tools.slice(0, TOOLS_SHOWN).map(t => (
        <Box key={`tool-${t.name}`} flexDirection="column">
          <Box alignItems="center">
            <Box flexGrow={1} flexShrink={1}><Text size="footnote" mono bold wrap="truncate">{t.name}</Text></Box>
            <Box flexShrink={0} gap={2}>
              <Text size="footnote" mono dimColor>{`${t.calls} ${t.calls === 1 ? 'call' : 'calls'}`}</Text>
              <Text size="footnote" mono tabular bold>{usd(t.cost, 4)}</Text>
            </Box>
          </Box>
          {bar($, e, [{ series: 'ask', frac: t.ask / top }, { series: 'ctx', frac: t.ctx / top }, { series: 'read', frac: t.read / top }],
            `${t.name}: ask ${usd(t.ask, 4)}, context ${usd(t.ctx, 4)}, re-read ${usd(t.read, 4)}`, 4)}
          <Text size="caption" mono dimColor wrap="truncate">
            {`ask ${usd(t.ask, 4)} · ctx ${usd(t.ctx, 4)} (${int(t.ctx_tokens)} tok) · re-read ${usd(t.read, 4)}`}
          </Text>
        </Box>
      ))}
      {tools.length > TOOLS_SHOWN && <Text size="footnote" mono dimColor>{`+${tools.length - TOOLS_SHOWN} more, in the browser view`}</Text>}
      {r.overhead > 0 && <Text size="footnote" mono dimColor>{`TokenTab overhead ${usd(r.overhead, 4)}: its hook text in context`}</Text>}
      {tools.length > 0 && (
        <Box key="legend" gap={2}>
          {([['ask', 'ask'], ['ctx', 'ctx'], ['read', 're-read']] as const).map(([k, name]) => (
            <Text size="caption" mono key={`legend-${k}`} dimColor><Text color={SERIES[k]![0]}>● </Text>{name}</Text>
          ))}
        </Box>
      )}
    </Box>
  )
}

/** Budget, Figma's "TokenTab / Limits": the pane's auto-open switch (the manifest's
 *  `userConfig` field, as in /config) first, then the budget limits over budget.json, a
 *  label on the left and its control on the right: a limit as an Input with "save", a
 *  switch as an on/off Button. Mobile has no Input, so it shows the values and says
 *  where to edit them. */
function budgetTab($: EngineInterface, e: RenderEvent): RenderChildren {
  const { Box, Text, Button } = $.ui.resolve(e)
  const o = options
  const row = (key: string, label: string, control: RenderChildren, error?: string) => (
    <Box key={`row-${key}`} flexDirection="column">
      <Box gap={1} alignItems="center" justifyContent="space-between">
        <Text>{label}</Text>
        {control}
      </Box>
      {error !== undefined && <Text color="error">{error}</Text>}
    </Box>
  )
  const section = (title: string, rows: RenderChildren[]) => (
    <Box key={`section-${title}`} flexDirection="column" gap="xs">
      <Text bold>{title}</Text>
      {rows}
    </Box>
  )
  const mobile = e.surface === 'mobile'
  const control = (s: Setting): RenderChildren => {
    const v = at(o?.budget, s.key)
    if (s.kind === 'switch') {
      const on = v === true
      return mobile ? <Text>{on ? 'on' : 'off'}</Text>
        : <Button key={`b-${s.key}`} label={on ? 'on' : 'off'} onPress={() => { void setBudget($, s.key, on ? 'off' : 'on') }} />
    }
    if (mobile) return <Text>{shown(v)}</Text>
    const { Input } = $.ui.resolve(e)
    return <Input key={`b-${s.key}`} value={shown(v)} placeholder="off" submitLabel="save" onSubmit={t => { void setBudget($, s.key, t) }} />
  }
  return (
    <Box flexDirection="column" gap={1}>
      {section('pane', [
        row('auto-open', 'open at session start',
          <Button key="auto-open" label={autoOpen() ? 'on' : 'off'} onPress={() => { void setAutoOpen($, !autoOpen()) }} />),
        <Text size="footnote" key="auto-open-note" dimColor>in a terminal also in /config as {$.plugin.name}.autoOpen</Text>,
      ])}
      {o?.error !== undefined && <Text color="error">{o.error}</Text>}
      {SECTIONS.map(sec => section(sec.title, sec.rows.map(s => row(s.key, s.label, control(s), o?.errors[s.key]))))}
      <Text size="footnote" dimColor>
        {mobile ? 'edit the limits from a desktop or the terminal, or ask: /tokentab:budget daily 30'
          : '"off" switches a limit off; Enter or save writes it, from the next prompt on.'}
      </Text>
      <Text size="footnote" dimColor>{o?.path ?? 'budget.json'}</Text>
    </Box>
  )
}
