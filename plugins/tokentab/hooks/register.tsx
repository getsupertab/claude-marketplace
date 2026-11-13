import type { EngineInterface, PluginOptions, PromptSubmitInput, Register, RenderChildren, TelemetryRecordEntry, Timer, UiOpenResult } from 'claude-code'
import { TOKENS } from './tokens'

// TokenTab's hooks module, three parts:
//
// The ledger. Every API request the engine makes reaches `telemetry.log` as
// an `api_request` record for the operator's collector, with or without telemetry
// switched on, main loop and helpers alike. This module prices it and appends one
// line to the session's ledger file; the Python accounting reads those.
//
// The pane.
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
  price_drift_usd?: number
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

type Ttl = '1h' | '5m'
/** Within this, Claude Code's figure and ours count as the same price. */
const MATCH_USD = 0.0005

/** USD for the tokens at the plugin's prices, the cache write at the 1h or 5m rate. */
export function price(prices: Prices, model: string, t: Tokens, ttl: Ttl = '1h'): number {
  return (t.input * rate(prices, model, 'input') + t.output * rate(prices, model, 'output')
    + t.cache_read * rate(prices, model, 'cache_read') + t.cache_write * rate(prices, model, `cache_write_${ttl}`)) / 1_000_000
}

/** The rate a call's cache write was billed at. Telemetry does not say whether it was 5m
 *  or 1h. Claude Code's main loop writes 1h; helper agents, SDK calls and compaction write
 *  5m. Claude Code's own figure tells them apart: fees only add to it, so a figure below
 *  the 1h price (and not below the 5m one) means a 5m write; what it has above the 5m
 *  price is then a fee. Without a figure, 1h. */
export function ttlOf(prices: Prices, model: string, t: Tokens, cc: number | null): Ttl {
  if (cc === null || !t.cache_write) return '1h'
  return cc < price(prices, model, t, '1h') - MATCH_USD && cc > price(prices, model, t, '5m') - MATCH_USD ? '5m' : '1h'
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
  // Claude Code's own figure: it says which rate the cache write was billed at, and what
  // it has on top of the token price is a non-token charge (web search is billed per
  // search). cost_usd stays tokens only.
  const cc = a.cost_usd === undefined || a.cost_usd === null ? null : num(a.cost_usd)
  const ours = price(prices, model, tokens, ttlOf(prices, model, tokens, cc))
  const fees = cc !== null && cc - ours > MATCH_USD ? round6(cc - ours) : 0
  // Fees only add to Claude Code's figure, so one below our lowest price for the tokens
  // means our rates for this model are too high. A zero figure is no figure.
  const floor = price(prices, model, tokens, '5m')
  const drift = cc !== null && cc > 0 && floor - cc > MATCH_USD ? round6(floor - cc) : 0
  const line: LedgerLine = {
    ts, source: 'mod', session_id: sessionId, model, tokens,
    cost_usd: round6(ours), cost_usd_cc: cc, fees_usd: fees,
    query_source: str(a.query_source), request_id: str(a.request_id),
    // an unlisted model was priced at the default rates, or a listed one above Claude Code: the table is behind
    prices_verified: Boolean(prices._verified) && model in (prices.models ?? {}) && !drift,
  }
  if (a.duration_ms !== undefined) line.duration_ms = Math.trunc(num(a.duration_ms))
  if (drift) line.price_drift_usd = drift
  return line
}

let prices: Prices | undefined        // read once per load from the plugin's own file
let queue: Promise<void> = Promise.resolve()   // $.fs.write replaces the file: appends are serialised
const part = new Map<string, number>()         // session id -> which -N file is current

async function loadPrices($: EngineInterface): Promise<Prices> {
  if (prices === undefined) prices = JSON.parse(await $.fs.read(`${$.plugin.root}/tokentab/prices.json`)) as Prices
  return prices
}

async function dataRoot($: EngineInterface): Promise<string> {
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
// The Python helpers. A run normally takes well under a second (the scan only reads what the
// transcripts gained since the last run, see scanned() in tokentab.py); the first after an
// update, or after a log shrank or vanished, reads every transcript again, which can take
// half a minute or more after a busy month. Refreshes run one at a time, so a long one delays
// the next instead of piling up.
const SCRIPT_TIMEOUT_MS = 120_000
// The status page is kept current (its data file written on every redraw) this long
// after it was last opened from the pane; the page says "stale" once the pushes stop.
const PAGE_MS = 30 * 60_000

const ANSI = /\x1b\[([0-9;]*)m/g
// tokentab/guard.py's cold-resume message opens with this.
const typed = (kind: string) => kind === 'composer' || kind === 'bridge' || kind === 'sdk'
export const CONTINUE = 'Continue'
export const DONT_SEND = "Don't send"
export const HELD = 'TokenTab: held back, the prompt cache has expired. Continue (or 1) sends it; so does sending it again.'

// The held prompt the band above the prompt box asks about. A hook may not wait on the
// person (its 10 s budget runs on), so the prompt is dropped and Continue sends it again.
type ColdAsk = { text: string, prompt: string, attachments: PromptSubmitInput['attachments'], failed?: string }
let coldAsk: ColdAsk | undefined
// Continue's resend passes unchecked: Desktop hands it back as typed, not as the plugin's.
let resending = false

// The cold-cache sentence, or undefined: a warm cache, the check failed, or no surface to
// ask on (a -p run), where the guard beneath blocks once as before.
async function coldCheck($: EngineInterface): Promise<string | undefined> {
  try {
    const cold = (JSON.parse(await python($, 'guard.py', ['--cold-check', await sid($)])) as { cold?: string }).cold
    if (!cold || (await $.session.surfaces()).length === 0) return undefined
    return `${cold[0]!.toUpperCase()}${cold.slice(1)}`
  } catch { return undefined }
}

async function answerCold($: EngineInterface, ask: ColdAsk, send: boolean): Promise<void> {
  if (coldAsk !== ask) return
  coldAsk = undefined
  $.ui.invalidate('ui.render')
  if (!send) return
  const failed = (step: string, err: unknown) => {
    coldAsk = { ...ask, failed: `${step}: ${err instanceof Error ? err.message : String(err)}` }
    $.ui.invalidate('ui.render')
  }
  // Confirmed first, so the guard lets the resent prompt through.
  try { await python($, 'guard.py', ['--cold-confirm', await sid($)]) } catch (err) { return failed('confirming with the guard failed', err) }
  resending = true
  try {
    const sent = await $.prompt.submit({ text: ask.prompt, attachments: ask.attachments, asUser: true })
    if (sent.drop !== undefined) failed('the prompt was held back', sent.drop)
  } catch (err) { failed('sending failed', err) } finally { resending = false }
}

// The person sent again with the question up: that is their go-ahead, as with the guard.
async function sentAgain($: EngineInterface): Promise<void> {
  coldAsk = undefined
  $.ui.invalidate('ui.render')
  try { await python($, 'guard.py', ['--cold-confirm', await sid($)]) } catch { /* the guard then blocks once */ }
}

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
    title: string; cost: number; tokens: number; msgs: number; minutes?: number
    limits_on: boolean; unit: Unit
    /** The account reports plan windows (Pro/Max): its dollars are API rates, not a bill. */
    plan?: boolean
    /** Under percent, each window with a reading; one with a checked limit leads the hero. */
    windows?: PlanWindow[]
    /** The hero's limit as text: `$0.82 of $1.00`, `5-hour window 42% used, limit 80%`. */
    limit: string | null; frac: number | null
    warn_at: number; hard_stop: boolean
  }
  rates?: { label: string; key: string; tokens: number; cost: number; share: number }[]
  total?: { tokens: number; cost: number }
  unmatched?: number
  models?: { name: string; msgs: number; cost: number; share: number }[]
  tools: ToolRow[]
  /** This month's spend per session, costliest first; `detail` is the short id of an untitled session and "deleted" once its transcript is gone. */
  sessions?: { sid: string; name: string; detail?: string; cost: number; share: number }[]
}

/** `limit` and `frac` are null where no limit is checked: limits off, or none set for that window. */
export type PlanWindow = { scope: 'five_hour' | 'seven_day'; used: number; limit: number | null; frac: number | null; resets: string | null }
const WINDOW_NAME = { five_hour: '5-hour window', seven_day: 'weekly window' } as const
const WINDOW_SHORT = { five_hour: '5-hour', seven_day: 'week' } as const
const percent = (n: number) => `${Math.round(n * 10) / 10}%`

/** A tool, or a layer of its breakdown (Bash's program, then its subcommand). */
export type ToolRow = {
  name: string; calls: number; cost: number; ask: number; ctx: number; read: number; ctx_tokens: number
  children?: ToolRow[]
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

// The series colours from the Figma frames, a light and a dark value each (hooks/tokens.ts,
// shared with the status page): the Svg bars pick one through prefers-color-scheme (the
// API carries no theme), the dots in the tables take the light one, a mid tone that reads
// on both grounds. Spend's models (model_1..5) and its sessions this month (session_1..10,
// then session_other) go costliest first.
export const SERIES = TOKENS.series
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
// A section's `toggle` sits in its heading.
// A limit with a `percent` twin is one field: the twin stands in while the unit in
// effect is percent. Every write goes through tokentab/budget.py, the one validator.
export type Unit = 'usd' | 'percent'
export type Setting = {
  key: string; label: string; kind: 'limit' | 'switch'; unit?: string
  percent?: { key: string; label: string }
}
export const SECTIONS: { title: string; toggle?: Setting; rows: Setting[] }[] = [
  { title: 'limits', toggle: { key: 'budgets_on', label: 'check the limits', kind: 'switch' }, rows: [
    { key: 'session_limit_usd', label: 'This session', unit: 'USD', kind: 'limit',
      percent: { key: 'five_hour_limit_percent', label: '5-hour window' } },
    { key: 'daily_limit_usd', label: 'Today', unit: 'USD', kind: 'limit',
      percent: { key: 'weekly_limit_percent', label: 'This week' } },
    { key: 'warn_at_percent', label: 'Warn at', unit: '%', kind: 'limit' },
    { key: 'hard_stop', label: 'Enable hard stop', kind: 'switch' },
  ] },
]

type Json = Record<string, unknown>

const WINDOWS = ['five_hour', 'seven_day']

/** The unit in effect, decided by the account, not a setting: percent where the plan reports
 *  a usage window (Pro, Max), else USD. The same rule as limit_unit() in tokentab.py. */
export function unitOf(windows: { kind: string }[]): Unit {
  return windows.some(window => WINDOWS.includes(window.kind)) ? 'percent' : 'usd'
}

/** A row as the unit in effect shows it: a limit's percent twin under percent. */
export function inUnit(s: Setting, unit: Unit): Setting {
  return unit === 'percent' && s.percent ? { ...s, key: s.percent.key, label: s.percent.label, unit: '%' } : s
}

/** The user's budget.json with the keys a newer shipped file has added, in the shipped
 *  order; undefined when nothing is missing. */
export function withNewKeys(user: Json, shipped: Json): Json | undefined {
  if (Object.keys(shipped).every(k => k in user)) return undefined
  const out: Json = {}
  for (const k of Object.keys(shipped)) out[k] = k in user ? user[k] : shipped[k]
  return { ...out, ...user }
}

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

type Options = { budget?: Json; path: string; errors: Record<string, string>; error?: string; windows: { kind: string }[] }

let opts: PluginOptions = {}           // the manifest's userConfig values, from register()
let view: View = 'spend'
let detailsOpen = false                 // Spend's rate breakdown and tools, folded until asked for
let sessionsOpen = false                // Spend's table of this month's sessions, likewise
const toolsOpen = new Set<string>()     // the tool rows opened to their breakdown, by path
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
let measured = false                    // this load has written the usage windows once
let autoOpenHere: boolean | undefined   // the switch set where /config had no row for it, `$.store`'s AUTO_OPEN

// An SDK host (the Desktop app, `claude -p`) lists no plugin's /config row, so there the
// Budget tab's switch is kept in `$.store` with the option it was set over; it stands
// until /config changes that option, which a load sees without hooking `config.set`.
const AUTO_OPEN = 'autoOpen'
const optionAutoOpen = () => opts.autoOpen !== false
const autoOpen = () => autoOpenHere ?? optionAutoOpen()
const keepAutoOpen = ($: EngineInterface, value: boolean) => $.store.set(AUTO_OPEN, { value, over: optionAutoOpen() })

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
 *  A launcher that no longer starts is probed for again, once. A run that times out is not
 *  retried (it would only take as long again): it goes to the timing log and fails. */
async function python($: EngineInterface, script: string, args: readonly string[]): Promise<string> {
  const argv = (py: readonly string[]) => [...py, `${$.plugin.root}/tokentab/${script}`, ...args]
  const py = await findPython($)
  let r
  try {
    r = await $.process.run(argv(py), { timeoutMs: SCRIPT_TIMEOUT_MS })
  } catch (err) {
    if (timedOut(err)) {
      void logTimeout($, py, script, args)
      throw err
    }
    r = await $.process.run(argv(await findPython($, py)), { timeoutMs: SCRIPT_TIMEOUT_MS })
  }
  if (r.exitCode !== 0) throw new Error(r.stderr.trim().split('\n').pop() || `${script} exited ${r.exitCode}`)
  return r.stdout
}

/** `$.process.run` rejects both when the command cannot start and when it is still running at
 *  the timeout; only the message tells them apart. */
export function timedOut(err: unknown): boolean {
  return /still running after/i.test(err instanceof Error ? err.message : String(err))
}

/** A helper killed at the timeout cannot log itself: one line for it in state/perf.jsonl. */
async function logTimeout($: EngineInterface, py: readonly string[], script: string, args: readonly string[]): Promise<void> {
  const mode = args.filter(a => a.startsWith('--') && a !== '--session').map(a => a.slice(2)).join(',')
  const note = JSON.stringify({ script, mode, kind: 'timeout', timeout_s: SCRIPT_TIMEOUT_MS / 1000 })
  try {
    await $.process.run([...py, `${$.plugin.root}/tokentab/tokentab.py`, '--perf-note', note], { timeoutMs: HELPER_TIMEOUT_MS })
  } catch { /* the log is best effort */ }
}

async function isPlaced($: EngineInterface): Promise<boolean> {
  try { return (await $.ui.panes()).some(p => p.id === PANE && p.isPlaced) } catch { return false }
}

const pagePath = (data: string, id: string) => `${data}/status/${id}.html`
const pushPath = (data: string, id: string) => `${data}/status/${id}.js`

async function pageIsOpen($: EngineInterface): Promise<boolean> {
  return pageOpenedAt !== undefined && (await $.clock.now()) - pageOpenedAt < PAGE_MS
}

/** This session's usage windows; none before its first reading (another session's may be another login's). */
async function readWindows($: EngineInterface): Promise<{ kind: string }[]> {
  let doc: Json
  try {
    doc = JSON.parse(await $.fs.read(`${await dataRoot($)}/state/windows/${await sid($)}.json`)) as Json
  } catch { return [] }
  return Array.isArray(doc.windows) ? doc.windows as { kind: string }[] : []
}

async function readBudget($: EngineInterface): Promise<void> {
  const path = `${await dataRoot($)}/budget.json`
  const errors = options?.errors ?? {}
  const windows = await readWindows($)
  try {
    options = { budget: JSON.parse(await $.fs.read(path)) as Json, path, errors, windows }
  } catch (err) {
    options = { path, errors, windows, error: `budget.json: ${err instanceof Error ? err.message : String(err)}` }
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
    if (await pageIsOpen($)) await python($, 'sankey.py', ['--session', id, '--push', pushPath(await dataRoot($), id)])
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
    const path = pagePath(await dataRoot($), id)
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

const AUTO_OPEN_KEY = 'tokentab.autoOpen'   // the /config key

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
      const data = await dataRoot($)
      // The loaded marker: scripts/loaded-check.sh looks for it on the first prompt.
      await $.fs.write(`${data}/state/loaded/${await $.session.id()}`, new Date(await $.clock.now()).toISOString() + '\n')
      // First run: the shipped limits become the user's budget.json. After that only keys
      // a newer version ships are added, the user's values kept.
      const shipped = await $.fs.read(`${$.plugin.root}/tokentab/budget.json`)
      if (!(await $.fs.exists(`${data}/budget.json`))) {
        await $.fs.write(`${data}/budget.json`, shipped)
      } else {
        try {
          const added = withNewKeys(JSON.parse(await $.fs.read(`${data}/budget.json`)) as Json, JSON.parse(shipped) as Json)
          if (added) await $.fs.write(`${data}/budget.json`, JSON.stringify(added, null, 2) + '\n')
        } catch { /* a file that does not parse is the guard's to report, never overwritten */ }
      }
    } catch { /* nothing to track yet; the shell gate reports a module that did not load */ }

    // A reload runs this again: every step below is idempotent (an open id retitles).
    report = undefined
    error = undefined
    options = undefined
    sessionId = undefined
    waiting = false
    try {
      const kept = await $.store.get(AUTO_OPEN) as { value?: unknown, over?: unknown } | undefined
      autoOpenHere = typeof kept?.value === 'boolean' && kept.over === optionAutoOpen() ? kept.value : undefined
      if (kept !== undefined && autoOpenHere === undefined) await $.store.delete(AUTO_OPEN)
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

  // The cold-cache question, in the band above the prompt.
  on('ui.render', { component: 'AbovePrompt' }, ($, e, next) => {
    if (!coldAsk || e.props.hasSurvey) return next(e)
    const { Box, Text, Button } = $.ui.resolve(e)
    const ask = coldAsk
    return (
      <Box flexDirection="column" gap={1}>
        <Text>{ask.text}</Text>
        {ask.failed ? <Text color="red">TokenTab: {ask.failed}</Text> : null}
        <Text bold>Send your prompt anyway?</Text>
        <Box gap={1}>
          <Button key="cold-send" label={CONTINUE} hotkey="1" plain variant="primary" onPress={() => { void answerCold($, ask, true) }} />
          <Button key="cold-hold" label={DONT_SEND} hotkey="2" plain onPress={() => { void answerCold($, ask, false) }} />
        </Box>
      </Box>
    )
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

  // The Budget tab's auto-open switch is the manifest's `userConfig` field: written as the
  // config menu would, so the menu, the install screen and the pane show one value. The
  // write reloads the module; `session.start` then runs again and is idempotent. Where
  // /config has no row for it (an SDK host), it is kept in `$.store` instead. A stored
  // value is cleared before the /config write, so the reload cannot read it back.
  // Answered here, not in the Button's closure: the directory reads config.set only on a hook's own `$`.
  on('ui.press', { element: 'auto-open', requestId: PANE }, async ($, e) => {
    const value = !autoOpen()
    try {
      if (!(await $.config.list()).some(row => row.key === AUTO_OPEN_KEY)) {
        autoOpenHere = value
        await keepAutoOpen($, value)
        $.ui.invalidate('ui.render')
        return { element: e.element }
      }
      const stored = autoOpenHere
      await $.store.delete(AUTO_OPEN)
      const r = await $.config.set({ key: 'tokentab.autoOpen', value: value })
      if ('deny' in r && r.deny !== undefined) {
        if (stored !== undefined) await keepAutoOpen($, stored)
        $.ui.toast(`TokenTab: auto-open not changed: ${r.deny}`)
      }
    } catch (err) {
      $.ui.toast(`TokenTab: auto-open not changed: ${err instanceof Error ? err.message : String(err)}`)
    }
    return { element: e.element }
  })

  // The person's first prompt after an auto-open the engine left undrawn: an open from
  // their own Enter is asked, so placed at any width. Once a session; the pane is opened
  // before the prompt enters, and a failure never holds the prompt back.
  on('prompt.submit', async ($, e, next) => {
    // `sdk`: a prompt typed in the Desktop app, an SDK host; `waiting` is only ever set
    // once a surface is there, so a `-p` run's prompt never opens anything.
    if (waiting && typed(e.origin.kind)) {
      waiting = false
      if (!(await isPlaced($)) && (await openPane($)).isPlaced) void refresh($)
    }
    if (!typed(e.origin.kind) || e.text.trim() === '' || resending) return next(e)
    if (coldAsk) { await sentAgain($); return next(e) }
    const cold = await coldCheck($)
    if (cold) {
      coldAsk = { text: cold, prompt: e.text, attachments: e.attachments }
      $.ui.invalidate('ui.render')
      return { drop: HELD }
    }
    return next(e)
  }).catch(($, e, next) => next(e))

  // The plan's usage windows (5-hour, weekly; none off a subscription) for the guard and the
  // report, which run outside the engine: the session's own, and the last of any for the
  // budget skill. Written when a window moved and on this load's first measurement, which
  // also records "none".
  on('session.measure', async ($, e, next) => {
    const r = await next(e)
    if (!measured || e.changed.includes('rateLimits')) {
      measured = true
      try {
        const windows = e.rateLimits.map(w => ({ kind: w.kind, percentUsed: w.percentUsed, resetsAt: w.resetsAt ?? null }))
        const data = await dataRoot($), at = new Date(await $.clock.now()).toISOString()
        await $.fs.write(`${data}/state/windows/${await sid($)}.json`, JSON.stringify({ at, windows }) + '\n')
        await $.fs.write(`${data}/state/rate_limits.json`,
          JSON.stringify({ at, windows }) + '\n')
        // A first reading or a new login can change the unit: redraw now, not on the next 30 s beat
        if (tick !== undefined) stale = true
      } catch { /* the limits stay in the unit last read */ }
    }
    return r
  }).catch(($, e, next) => next(e))

  // Every API request's usage, in-process. The record goes on first (`next`), then our
  // line is written: the hook never denies, and a failure here never touches the record.
  on('telemetry.log', { to: 'collector' }, async ($, e, next) => {
    const r = await next(e)
    if (e.event === 'api_request') {
      try {
        const id = str(e.attributes['session.id']) ?? await $.session.id()
        const ts = e.loggedAt || new Date(await $.clock.now()).toISOString()
        await append($, await dataRoot($), toLine(e, await loadPrices($), id, ts))
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

/** A hairline between sections: the bars' track, one pixel high, where the surface draws
 *  an Svg; box-drawing characters in the terminal. */
function rule($: EngineInterface, e: RenderEvent, key?: string): RenderChildren {
  const { Box, Text } = $.ui.resolve(e)
  if (e.surface !== 'terminal') {
    const { Svg } = $.ui.resolve(e)
    // Desktop clips a box one unit high around wrapped text to stray slices of it
    return <Box key={key} flexDirection="column"><Svg source={barSvg([], 1)} alt="separator" height={1} /></Box>
  }
  // Clipped rather than truncated, which would end the line in an ellipsis
  return <Box key={key} height={1} overflow="hidden"><Text dimColor wrap="wrap">{'─'.repeat(120)}</Text></Box>
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

/** The colour of a share by its rank: four hues, then grey for the rest. */
const shareSeries = (rank: number) => `model_${Math.min(rank + 1, 5)}`
const SESSIONS_COLOURED = 10
const sessionSeries = (rank: number) => (rank < SESSIONS_COLOURED ? `session_${rank + 1}` : 'session_other')

/** Spend, Figma's "A · Hero number": the session against its limit as one figure and a
 *  bar, the rate breakdown, what each tool and each model cost (folded under "details"), then every
 *  session today and this month, with this month's sessions as a table (folded too). */
function spendTab($: EngineInterface, e: RenderEvent, r: Report): RenderChildren {
  const { Box, Text, Button } = $.ui.resolve(e)
  const s = r.session
  const sessions = r.sessions ?? []
  const othersShare = sessions.slice(SESSIONS_COLOURED).reduce((sum, session) => sum + session.share, 0)
  const sessionParts = sessions.slice(0, SESSIONS_COLOURED).map((session, rank) => ({ series: sessionSeries(rank), frac: session.share }))
    .concat(othersShare > 0 ? [{ series: 'session_other', frac: othersShare }] : [])
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
      {sessions.length > 0 && (
        <Box key="all-toggle" alignItems="center" gap={2}>
          <Button key="sessions-toggle" label="SESSIONS" hotkey="a" plain
            onPress={() => { sessionsOpen = !sessionsOpen; $.ui.invalidate('ui.render') }}>
            <Text size="footnote" dimColor>{`${sessionsOpen ? '▾' : '▸'} SESSIONS`}</Text>
          </Button>
          {!sessionsOpen && (
            <Box flexGrow={1} flexShrink={1}>
              <Text size="footnote" mono dimColor wrap="truncate">{`${sessions.length} this month`}</Text>
            </Box>
          )}
        </Box>
      )}
      {sessionsOpen && sessions.length > 0 && (
        <Box key="all-table" flexDirection="column" gap="xs">
          {bar($, e, sessionParts, `this month by session: ${sessions.slice(0, 5).map(session => `${session.name} ${pct(session.share)}`).join(', ')}`
            + (othersShare > 0 ? `, others ${pct(othersShare)}` : ''))}
          <Box flexDirection="column">
            <Box key="all-head" alignItems="center">
              <Box flexGrow={1} flexShrink={1}><Text size="footnote" mono dimColor>{'  Name'}</Text></Box>
              {cell($, e, 'Cost', 10, { dim: true })}
              {cell($, e, '% of costs', 11, { dim: true })}
            </Box>
            {rule($, e, 'all-head-rule')}
            {sessions.map((session, rank) => (
              <Box key={`all-${session.sid}`} alignItems="center">
                {/* minWidth 0 lets the row shrink below the title, so Desktop truncates it instead of wrapping */}
                <Box flexGrow={1} flexShrink={1} minWidth={0}>
                  <Text size="footnote" mono bold={session.sid === r.sid} wrap="truncate">
                    <Text color={SERIES[sessionSeries(rank)]![0]}>● </Text>{session.name}
                    {session.detail ? <Text dimColor>{` · ${session.detail}`}</Text> : null}
                  </Text>
                </Box>
                {cell($, e, usd(session.cost), 10, { bold: session.sid === r.sid })}
                {cell($, e, pct(session.share), 11, { dim: true })}
              </Box>
            ))}
          </Box>
        </Box>
      )}
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
  // Under percent the window leads, limits on or off, and dollars move below; with no window reading, dollars lead
  const planWindows = s.unit === 'percent' ? s.windows ?? [] : []
  const lead = planWindows.find(w => w.frac !== null) ?? planWindows[0]
  const otherWindows = planWindows.filter(w => w !== lead)
  const apiRates = s.plan ? ' at API rates' : ''
  const frac = lead ? lead.frac : s.frac
  const fracs = planWindows.flatMap(w => (w.frac === null ? [] : [w.frac]))
  const worst = lead ? (fracs.length > 0 ? Math.max(...fracs) : null) : frac
  const tone = worst === null ? undefined : toneOf(worst, s.warn_at)
  const badge = worst === null || tone === undefined ? undefined
    : <Text size="footnote" weight="semibold" color={tone}>
        {tone === 'error' ? `Over · ${pct(worst)}` : tone === 'warning' ? `Warning · ${pct(worst)}` : `${pct(worst)} used`}
      </Text>
  const mode = `${s.hard_stop ? 'hard stop' : 'warn only'}, warns at ${s.warn_at}%`
  const resets = (w: PlanWindow) => (w.resets ? ` · resets ${w.resets}` : '')
  const limitOf = (w: PlanWindow, sep: string) => (w.limit === null ? '' : `${sep}limit ${percent(w.limit)}`)
  const under = [
    lead ? `${usd(s.cost)}${apiRates}` : s.limit ?? '',
    `${int(s.tokens)} tokens`,
    frac !== null ? mode : !s.limits_on ? 'limits off'
      : s.unit === 'percent' ? 'no window limit set or no reading yet' : 'no session limit set',
  ].filter(x => x !== '').join(' · ')
  const rates = r.rates ?? []
  const series = (key: string) => (SERIES[key] ? key : 'input')
  const models = r.models ?? []
  const toolCalls = r.tools.reduce((n, t) => n + t.calls, 0)
  const detailsSummary = `${int(s.msgs)} msgs · ${r.tools.length} tools · ${toolCalls} calls · ${models.length} ${models.length === 1 ? 'model' : 'models'}`
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
          {label($, e, lead ? 'plan usage' : 'session spend')}
          {badge}
        </Box>
        <Box justifyContent="space-between" alignItems="center">
          <Box alignItems="center" gap={1} flexShrink={1}>
            <Text size="title" weight="bold" tabular>{lead ? percent(lead.used) : usd(s.cost)}</Text>
            {lead
              ? <Text size="footnote" dimColor>{`of ${WINDOW_NAME[lead.scope]}${limitOf(lead, ' · ')}${resets(lead)}`}</Text>
              : s.plan && <Text size="footnote" dimColor>at API rates</Text>}
          </Box>
          <Button key="share" label="↑ Share" hotkey="h"
            onPress={() => { void openSharePage($, r) }} />
        </Box>
        {frac !== null && bar($, e, [{ series: toneOf(frac, s.warn_at), frac }],
          `${pct(frac)} of the ${lead ? WINDOW_NAME[lead.scope] : 'session'} limit`, 8)}
        {otherWindows.map(w => (
          <Text key={`window-${w.scope}`} size="footnote" dimColor>
            {`${WINDOW_SHORT[w.scope]} ${percent(w.used)}${limitOf(w, ', ')}${resets(w)}`}
          </Text>
        ))}
        <Text size="footnote" dimColor>{under}</Text>
      </Box>
      <Box key="details" flexDirection="column" gap="xs">
        {/* Desktop draws the Button's label and drops its children, so the summary sits beside it */}
        <Box alignItems="center" gap={2}>
          <Button key="details-toggle" label="DETAILS" hotkey="d" plain
            onPress={() => { detailsOpen = !detailsOpen; $.ui.invalidate('ui.render') }}>
            <Text size="footnote" dimColor>{`${detailsOpen ? '▾' : '▸'} DETAILS`}</Text>
          </Button>
          {!detailsOpen && (
            <Box flexGrow={1} flexShrink={1}>
              <Text size="footnote" mono dimColor wrap="truncate">{detailsSummary}</Text>
            </Box>
          )}
        </Box>
        {detailsOpen && (
          <Box key="details-body" flexDirection="column" gap="lg">
            <Box key="session" flexDirection="column" gap="xs">
              {heading($, e, 'SESSION', `${int(s.msgs)} msgs`)}
              {bar($, e, rates.map(x => ({ series: series(x.key), frac: x.share })),
                `this session by rate: ${rates.map(x => `${x.label} ${pct(x.share)}`).join(', ')}`)}
              {row('head', <Text> </Text>, 'tokens', 'cost', '%', { dim: true })}
              {rates.map(x => row(x.key,
                <Text size="footnote" mono><Text color={SERIES[series(x.key)]![0]}>● </Text>{x.label}</Text>,
                int(x.tokens), usd(x.cost, 4), pct(x.share)))}
              {row('total', <Text size="footnote" mono bold>{`  total${s.plan ? ' at API rates' : ''}`}</Text>,
                int(r.total?.tokens ?? 0), usd(r.total?.cost ?? s.cost, 4), '', { bold: true })}
              {(r.unmatched ?? 0) > 0 && (
                <Text size="footnote" dimColor>{int(r.unmatched ?? 0)} output tok have no transcript line. Any thinking is counted in output.</Text>
              )}
            </Box>
            {rule($, e)}
            {toolsSection($, e, r)}
            {models.length > 0 && rule($, e, 'models-rule')}
            {models.length > 0 && (
              <Box key="models" flexDirection="column" gap="xs">
                {heading($, e, 'MODELS', `${models.length} used`)}
                {bar($, e, models.map((model, rank) => ({ series: shareSeries(rank), frac: model.share })),
                  `this session by model: ${models.map(model => `${model.name} ${pct(model.share)}`).join(', ')}`)}
                {models.map((model, rank) => (
                  <Box key={`model-${model.name}`} alignItems="center">
                    <Box flexGrow={1} flexShrink={1}>
                      <Text size="footnote" mono wrap="truncate"><Text color={SERIES[shareSeries(rank)]![0]}>● </Text>{model.name.replace(/^claude-/, '')}</Text>
                    </Box>
                    {cell($, e, `${int(model.msgs)} msgs`, COLS.tokens, { dim: true })}
                    {cell($, e, usd(model.cost, 4), COLS.cost)}
                    {cell($, e, pct(model.share), COLS.share, { dim: true })}
                  </Box>
                ))}
              </Box>
            )}
          </Box>
        )}
        {!r.prices_verified && <Text size="footnote" color="warning">Prices unverified: TokenTab's price table is behind for a model used here, so the figures are estimates.</Text>}
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
      {tools.slice(0, TOOLS_SHOWN).map(t => toolRow($, e, t, t.name, top, 0))}
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

/** One tool row: name, calls, cost, its bar against the costliest sibling and the three
 *  figures. A row with a breakdown is a toggle that opens its layers beneath it, indented. */
function toolRow($: EngineInterface, e: RenderEvent, t: ToolRow, path: string, top: number, depth: number): RenderChildren {
  const { Box, Text, Button } = $.ui.resolve(e)
  const kids = t.children ?? []
  const open = kids.length > 0 && toolsOpen.has(path)
  const name = kids.length === 0
    ? <Text size="footnote" mono bold={depth === 0} wrap="truncate">{depth === 0 ? t.name : `  ${t.name}`}</Text>
    : <Button key={`tool-toggle-${path}`} label={`${open ? '▾' : '▸'} ${t.name}`} plain
        onPress={() => { if (!toolsOpen.delete(path)) toolsOpen.add(path); $.ui.invalidate('ui.render') }}>
        <Text size="footnote" mono bold={depth === 0}>{`${open ? '▾' : '▸'} ${t.name}`}</Text>
      </Button>
  const kidTop = Math.max(...kids.map(kid => kid.cost), 1e-9)
  return (
    <Box key={`tool-${path}`} flexDirection="column" paddingLeft={depth === 0 ? 0 : 2}>
      <Box alignItems="center">
        <Box flexGrow={1} flexShrink={1} minWidth={0}>{name}</Box>
        <Box flexShrink={0} gap={2}>
          <Text size="footnote" mono dimColor>{`${t.calls} ${t.calls === 1 ? 'call' : 'calls'}`}</Text>
          <Text size="footnote" mono tabular bold={depth === 0}>{usd(t.cost, 4)}</Text>
        </Box>
      </Box>
      {bar($, e, [{ series: 'ask', frac: t.ask / top }, { series: 'ctx', frac: t.ctx / top }, { series: 'read', frac: t.read / top }],
        `${t.name}: ask ${usd(t.ask, 4)}, context ${usd(t.ctx, 4)}, re-read ${usd(t.read, 4)}`, 4)}
      <Text size="caption" mono dimColor wrap="truncate">
        {`ask ${usd(t.ask, 4)} · ctx ${usd(t.ctx, 4)} (${int(t.ctx_tokens)} tok) · re-read ${usd(t.read, 4)}`}
      </Text>
      {open && (
        <Box key={`tool-kids-${path}`} flexDirection="column" gap="xs" paddingTop={1}>
          {kids.map(kid => toolRow($, e, kid, `${path}/${kid.name}`, kidTop, depth + 1))}
        </Box>
      )}
    </Box>
  )
}

/** Budget, Figma's "TokenTab / Limits", set like Spend: the pane's auto-open switch
 *  (the manifest's `userConfig` field, as in /config) first, then the budget limits
 *  over budget.json, a label on the left and its control on the right: a limit as an
 *  Input with "save", a switch as an on/off Button. Mobile has no Input, so it shows
 *  the values. */
function budgetTab($: EngineInterface, e: RenderEvent): RenderChildren {
  const { Box, Text, Button } = $.ui.resolve(e)
  const o = options
  const mobile = e.surface === 'mobile'
  // One column for every control, so the fields share a width and the switches line up with them
  const column = (ctl: RenderChildren, stretch = false) => (
    <Box width={16} flexShrink={0} flexDirection="column" alignItems={stretch ? 'stretch' : 'flex-start'}>{ctl}</Box>
  )
  const toggle = (key: string, isOn: boolean, onPress: () => void): RenderChildren => mobile
    ? <Text size="footnote" mono weight="semibold">{isOn ? 'on' : 'off'}</Text>
    : <Button key={key} label={isOn ? 'on' : 'off'} variant={isOn ? 'primary' : 'secondary'} onPress={onPress} />
  const unit = unitOf(o?.windows ?? [])
  const control = (s: Setting): RenderChildren => {
    const v = at(o?.budget, s.key)
    if (s.kind === 'switch') return column(toggle(`b-${s.key}`, v === true, () => { void setBudget($, s.key, v === true ? 'off' : 'on') }))
    if (mobile) return column(<Text size="footnote" mono tabular weight="semibold">{shown(v)}</Text>)
    const { Input } = $.ui.resolve(e)
    return column(<Input key={`b-${s.key}`} value={shown(v)} placeholder="off" submitLabel="save" onSubmit={t => { void setBudget($, s.key, t) }} />, true)
  }
  const row = (key: string, name: string, ctl: RenderChildren, error?: string) => (
    <Box key={`row-${key}`} flexDirection="column">
      <Box gap={1} alignItems="center" justifyContent="space-between">
        <Text size="footnote">{name}</Text>
        {ctl}
      </Box>
      {error !== undefined && <Text size="footnote" color="error">{error}</Text>}
    </Box>
  )
  const section = (title: string, head: RenderChildren, rows: RenderChildren[]) => (
    <Box key={`section-${title}`} flexDirection="column" gap="xs">
      <Box justifyContent="space-between" alignItems="center">
        {label($, e, title)}
        {head}
      </Box>
      {rows}
    </Box>
  )
  return (
    <Box flexDirection="column" gap={1}>
      {section('options', undefined, [
        row('auto-open', 'Open pane at session start', column(toggle('auto-open', autoOpen(), () => {}))),
      ])}
      {o?.error !== undefined && <Text size="footnote" color="error">{o.error}</Text>}
      {SECTIONS.flatMap(sec => [
        rule($, e, `rule-${sec.title}`),
        section(sec.title, sec.toggle && control(sec.toggle), [
          ...sec.rows.map(r => inUnit(r, unit)).map(s => row(s.key, s.unit === undefined ? s.label : `${s.label} (${s.unit})`, control(s), o?.errors[s.key])),
          sec.toggle !== undefined && o?.errors[sec.toggle.key] !== undefined
            && <Text size="footnote" key="toggle-error" color="error">{o.errors[sec.toggle.key]}</Text>,
        ]),
      ])}
    </Box>
  )
}

// ---------- the share page ----------

/** What the share card shows: the session's figures, its title only while the switch is on. */
export type ShareCard = {
  date: string; cost: number; description?: string
  minutes: number; msgs: number; calls: number; tokens: number; model?: string
}

export function shareCard(r: Report, now: Date, withDescription: boolean): ShareCard | undefined {
  const s = r.session
  if (s === null) return undefined
  return {
    date: now.toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' }),
    cost: s.cost,
    description: withDescription && s.title ? s.title : undefined,
    minutes: s.minutes ?? 0,
    msgs: s.msgs,
    calls: r.tools.reduce((n, t) => n + t.calls, 0),
    tokens: s.tokens,
    model: r.models?.[0]?.name,
  }
}

const duration = (minutes: number) => minutes < 60 ? `${minutes} min` : `${Math.floor(minutes / 60)} h ${minutes % 60} min`

const shareRows = (card: ShareCard): [string, string][] => [
  ['time', duration(card.minutes)],
  ['messages', int(card.msgs)],
  ['tool calls', int(card.calls)],
  ['tokens', int(card.tokens)],
  ...(card.model ? [['model', card.model] as [string, string]] : []),
]

/** The card as plain text, what "Copy as text" and the post links carry. */
export function shareText(card: ShareCard): string {
  return [
    `This task cost me ${usd(card.cost)} with Claude.`,
    ...(card.description ? [card.description] : []),
    shareRows(card).map(([name, value]) => `${name}: ${value}`).join(' · '),
    'Tracked with TokenTab · supertab.co',
  ].join('\n')
}

const xml = (text: string) => text.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;')

/** Words into lines of at most `width` characters, at most `max` lines, the last cut with an ellipsis. */
export function wrapWords(text: string, width: number, max: number): string[] {
  const lines: string[] = []
  for (const word of text.split(/\s+/).filter(Boolean)) {
    const last = lines[lines.length - 1]
    if (last !== undefined && last.length + 1 + word.length <= width) lines[lines.length - 1] = `${last} ${word}`
    else lines.push(word)
  }
  if (lines.length <= max) return lines
  const kept = lines.slice(0, max)
  kept[max - 1] = `${kept[max - 1]!.slice(0, width - 1)}…`
  return kept
}

/** Figma's "share-card (image)", 320 wide: logo and date, the cost, the description, the
 *  figures, the footer. Also the file "Save image" writes, so it carries its own fonts and colours. */
export function shareCardSvg(card: ShareCard): string {
  const W = 320, PAD = 19
  const sans = 'font-family="-apple-system,BlinkMacSystemFont,Segoe UI,Inter,sans-serif"'
  const mono = 'font-family="ui-monospace,SFMono-Regular,Menlo,Consolas,monospace"'
  const parts: string[] = []
  let y = 19
  parts.push(`<rect x="${PAD}" y="${y}" width="20" height="20" rx="5" fill="#1a1a1a"/>`,
    `<text x="${PAD + 10}" y="${y + 14.5}" text-anchor="middle" ${sans} font-size="12" font-weight="700" fill="#fff">T</text>`,
    `<text x="${PAD + 28}" y="${y + 14.5}" ${sans} font-size="13" font-weight="600" fill="#1a1a1a">TokenTab</text>`,
    `<text x="${W - PAD}" y="${y + 14}" text-anchor="end" ${sans} font-size="11" fill="#8a8a85">${xml(card.date)}</text>`)
  y = 51
  parts.push(`<text x="${PAD}" y="${y + 13}" ${sans} font-size="12" fill="#6b6b66">This task cost me</text>`,
    `<text x="${PAD}" y="${y + 60}" ${sans} font-size="44" font-weight="700" letter-spacing="-1" fill="#1a1a1a">${xml(usd(card.cost))}</text>`)
  y = 135
  if (card.description) {
    for (const line of wrapWords(card.description, 40, 3)) {
      parts.push(`<text x="${PAD}" y="${y + 14}" ${sans} font-size="14" font-weight="500" fill="#1a1a1a">${xml(line)}</text>`)
      y += 19
    }
    y += 12
  }
  parts.push(`<rect x="${PAD}" y="${y}" width="${W - 2 * PAD}" height="1" fill="#e6e5df"/>`)
  y += 13
  for (const [name, value] of shareRows(card)) {
    parts.push(`<text x="${PAD}" y="${y + 13}" ${sans} font-size="12" fill="#6b6b66">${xml(name)}</text>`,
      `<text x="${W - PAD}" y="${y + 13}" text-anchor="end" ${mono} font-size="12" fill="#1a1a1a">${xml(value)}</text>`)
    y += 23
  }
  y += 4
  parts.push(`<rect x="${PAD}" y="${y}" width="${W - 2 * PAD}" height="1" fill="#e6e5df"/>`,
    `<text x="${PAD}" y="${y + 24}" ${sans} font-size="10" fill="#8a8a85">Tracked with TokenTab · supertab.co</text>`)
  const H = y + 40
  return `<svg xmlns="http://www.w3.org/2000/svg" width="${W}" height="${H}" viewBox="0 0 ${W} ${H}">`
    + `<defs><linearGradient id="bg" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#ffffff"/><stop offset="1" stop-color="#f1effa"/></linearGradient></defs>`
    + `<rect x="0.5" y="0.5" width="${W - 1}" height="${H - 1}" rx="12" fill="url(#bg)" stroke="#e6e5df"/>`
    + parts.join('') + '</svg>'
}

/** Post links: X takes the text; LinkedIn's documented share link takes only a URL, so the
 *  feed's `text` parameter (undocumented) carries it. Neither can attach the image. */
export const shareLinks = (text: string) => ({
  x: `https://x.com/intent/post?text=${encodeURIComponent(text)}`,
  linkedin: `https://www.linkedin.com/feed/?shareActive=true&text=${encodeURIComponent(text)}`,
})

/** The share page: the card with and without its description, and the ways out the pane
 *  lacks, done by the browser: the image to the clipboard and as a PNG, through a canvas. */
export function sharePageHtml(withDescription: ShareCard, without: ShareCard, title: string): string {
  const variants = [withDescription, without].map(card => {
    const text = shareText(card)
    return { svg: shareCardSvg(card), text, links: shareLinks(text) }
  })
  // `<` escaped so a title can never close the script
  const data = JSON.stringify({ variants, file: `tokentab-${withDescription.date.replace(/[^0-9A-Za-z]+/g, '-')}.png` }).replace(/</g, '\\u003c')
  return `<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Share · TokenTab</title>
<style>
:root{--bg:#f4f3ee;--panel:#fff;--stage:#f1f0ea;--text:#1a1a1a;--dim:#6b6b66;--line:#e6e5df;--btn:#f1f0ea;--btn-hover:#e8e7e0;--primary:#1a1a1a;--on-primary:#fff}
@media(prefers-color-scheme:dark){:root{--bg:#141413;--panel:#1f1f1d;--stage:#2a2a27;--text:#f2f1ec;--dim:#a3a29b;--line:#34342f;--btn:#2a2a27;--btn-hover:#34342f;--primary:#f2f1ec;--on-primary:#1a1a1a}}
*{box-sizing:border-box}
body{margin:0;min-height:100vh;display:grid;place-items:center;background:var(--bg);color:var(--text);font:14px/1.4 -apple-system,BlinkMacSystemFont,"Segoe UI",Inter,sans-serif;padding:16px}
.modal{background:var(--panel);border-radius:14px;box-shadow:0 20px 60px rgba(0,0,0,.18);width:min(680px,100%);padding:18px 20px 20px}
h1{font-size:17px;margin:0}.sub{color:var(--dim);font-size:12px;margin:2px 0 16px}
.body{display:flex;gap:20px;flex-wrap:wrap}
.stage{background:var(--stage);border-radius:10px;padding:20px;flex:1 1 300px;display:grid;place-items:center}
.stage svg{max-width:100%;height:auto;filter:drop-shadow(0 6px 16px rgba(0,0,0,.08))}
.actions{flex:1 1 220px;display:flex;flex-direction:column;gap:6px}
.toggle{display:flex;justify-content:space-between;align-items:center;padding:8px 0 12px;border-bottom:1px solid var(--line);margin-bottom:8px;cursor:pointer}
.toggle input{width:30px;height:17px;accent-color:var(--primary)}
button[hidden]{display:none}
button,a.btn{all:unset;box-sizing:border-box;display:flex;gap:10px;align-items:center;padding:9px 14px;border-radius:8px;background:var(--btn);cursor:pointer;color:var(--text)}
button:hover,a.btn:hover{background:var(--btn-hover)}
button:focus-visible,a.btn:focus-visible{outline:2px solid var(--primary);outline-offset:2px}
button.primary{background:var(--primary);color:var(--on-primary)}
.icon{width:16px;text-align:center}
.status{color:var(--dim);font-size:12px;min-height:16px;margin-top:4px}
</style></head><body>
<main class="modal">
<h1>Share this session</h1><div class="sub">${xml(title)} · ${xml(withDescription.date)}</div>
<div class="body">
<div class="stage" id="card"></div>
<div class="actions">
<label class="toggle">Show task description <input type="checkbox" id="desc" checked></label>
<button class="primary" id="copy-image"><span class="icon">⧉</span>Copy image</button>
<button id="download"><span class="icon">↓</span>Download PNG</button>
<a class="btn" id="x" target="_blank" rel="noopener"><span class="icon">𝕏</span>Share on X</a>
<a class="btn" id="linkedin" target="_blank" rel="noopener"><span class="icon">in</span>Share on LinkedIn</a>
<button id="native" hidden><span class="icon">⇪</span>Share…</button>
<button id="copy-text"><span class="icon">¶</span>Copy as text</button>
<div class="status" id="status" role="status"></div>
</div></div></main>
<script>
const DATA = ${data};
const byId = id => document.getElementById(id);
const current = () => DATA.variants[byId('desc').checked ? 0 : 1];
const say = text => { byId('status').textContent = text; };
function draw() {
  const v = current();
  byId('card').innerHTML = v.svg;
  byId('x').href = v.links.x;
  byId('linkedin').href = v.links.linkedin;
}
function png() {
  const svg = current().svg;
  return new Promise((resolve, reject) => {
    const img = new Image();
    img.onload = () => {
      const scale = 3, canvas = document.createElement('canvas');
      canvas.width = img.width * scale; canvas.height = img.height * scale;
      const ctx = canvas.getContext('2d'); ctx.scale(scale, scale); ctx.drawImage(img, 0, 0);
      canvas.toBlob(blob => blob ? resolve(blob) : reject(new Error('no image')), 'image/png');
    };
    img.onerror = () => reject(new Error('the card did not render'));
    img.src = 'data:image/svg+xml;charset=utf-8,' + encodeURIComponent(svg);
  });
}
byId('desc').addEventListener('change', draw);
// A promise in the ClipboardItem keeps Safari's user gesture alive while the PNG renders
const copyImage = () => navigator.clipboard.write([new ClipboardItem({ 'image/png': png() })]);
byId('copy-image').addEventListener('click', async () => {
  try { await copyImage(); say('Image copied.'); }
  catch (err) { say('Not copied: ' + err.message + '. Use Download PNG instead.'); }
});
// Post links take only text, so the image goes on the clipboard first, to paste into the post
for (const [id, name] of [['x', 'X'], ['linkedin', 'LinkedIn']]) {
  byId(id).addEventListener('click', async event => {
    event.preventDefault();
    const href = byId(id).href;
    let copied = true;
    try { await copyImage(); } catch { copied = false; }
    const tab = window.open(href, '_blank', 'noopener');
    say((copied ? 'Image copied: paste it into the post with ⌘V / Ctrl+V.' : 'The image was not copied; use Download PNG and attach it.')
      + (tab ? '' : ' Your browser blocked the new tab; click again.'));
  });
}
// Rendered ahead: Safari refuses a share that waits on the render after the click
let file;
const prepare = () => { file = undefined; return png().then(blob => { file = new File([blob], DATA.file, { type: 'image/png' }); }, () => { file = undefined; }); };
if (navigator.canShare && navigator.canShare({ files: [new File([''], DATA.file, { type: 'image/png' })] })) {
  byId('native').hidden = false;
  byId('desc').addEventListener('change', prepare);
  prepare();
  byId('native').addEventListener('click', async () => {
    if (!file) { say('Still preparing the image; try again.'); return; }
    try { await navigator.share({ files: [file], text: current().text }); }
    catch (err) { if (err.name !== 'AbortError') say('Not shared: ' + err.message); }
  });
}
byId('download').addEventListener('click', async () => {
  try {
    const a = document.createElement('a');
    a.href = URL.createObjectURL(await png()); a.download = DATA.file; a.click();
    setTimeout(() => URL.revokeObjectURL(a.href), 1000);
    say('PNG downloaded.');
  } catch (err) { say('Not downloaded: ' + err.message); }
});
byId('copy-text').addEventListener('click', async () => {
  try { await navigator.clipboard.writeText(current().text); say('Text copied.'); }
  catch (err) { say('Not copied: ' + err.message); }
});
draw();
</script></body></html>
`
}

/** "Open share page": the page written to the data folder and opened in the default browser. */
async function openSharePage($: EngineInterface, r: Report): Promise<void> {
  let written: string | undefined
  try {
    const now = new Date()
    const path = `${await dataRoot($)}/share/${(await sid($)).slice(0, 8)}.html`
    await $.fs.write(path, sharePageHtml(shareCard(r, now, true)!, shareCard(r, now, false)!, r.session?.title ?? r.sid.slice(0, 8)))
    written = path
    const { argv, checkExit } = await opener($, path)
    const opened = await $.process.run(argv, { timeoutMs: HELPER_TIMEOUT_MS })
    if (checkExit && opened.exitCode !== 0) throw new Error(opened.stderr.trim() || `${argv[0]} exited ${opened.exitCode}`)
    $.ui.toast(`TokenTab: share page opened (${path})`)
  } catch (err) {
    $.ui.toast(`TokenTab: could not open the share page${written ? ` (${written})` : ''}: ${err instanceof Error ? err.message : String(err)}`)
  }
}
