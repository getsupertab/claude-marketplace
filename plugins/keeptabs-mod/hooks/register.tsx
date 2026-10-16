import type { EngineInterface, Register, TelemetryRecordEntry } from 'claude-code'

// keeptabs' hooks module (phase 0 of docs/plan-CORE-537-status-page-tabs.md): the
// ledger. Every API request the engine makes reaches `telemetry.log` as an
// `api_request` record for the operator's collector, with or without telemetry
// switched on, main loop and helpers alike. This module prices it and appends
// one line to the session's ledger file; the Python accounting reads those.

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
    return r
  })

  // Every API request's usage, in-process. The record goes on first (`next`), then our
  // line is written: the hook never denies, and a failure here never touches the record.
  on('telemetry.log', { to: 'collector' }, async ($, e, next) => {
    const r = await next(e)
    if (e.event === 'api_request') {
      try {
        const sid = str(e.attributes['session.id']) ?? await $.session.id()
        const ts = e.loggedAt || new Date(await $.clock.now()).toISOString()
        await append($, await home($), toLine(e, await loadPrices($), sid, ts))
      } catch { /* see above */ }
    }
    return r
  }).catch(($, e, next) => next(e))
}
