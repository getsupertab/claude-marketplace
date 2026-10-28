import { test, expect, mock } from 'claude-code/testing'
import type { Engine } from 'claude-code/testing'
import type { On, RenderElement, RenderNode } from 'claude-code'
import { at, barCells, barSvg, dataDir, int, liveLines, pct, SERIES, shown, stripAnsi, toneOf, usd } from './register'
import type { Report } from './register'

// The world beneath the plugin: `$.fs` reaches nothing under `claude plugin test`, and
// nothing answers `telemetry.log`, `session.id`, the pane, the command, the store, the
// config menu or a process, so the test stands in for them. Files are matched by their
// tail, since the plugin's root is the engine's to know. `ui` records what the plugin
// asked the surface for, and `placed` says what `$.ui.panes()` reports for the pane.
// `rows` are the /config keys `$.config.list()` returns: an SDK host (Desktop) has none.
// `surfaces` is what `$.session.surfaces()` answers: none in a -p run or before Desktop attaches.
type Ui = {
  opens: number; commands: string[]; status: (string | undefined)[]; invalidates: number
  runs: string[][]; toasts: string[]; config: [string, unknown][]; store: Map<string, unknown>; rows: string[]
  surfaces: ('terminal' | 'desktop')[]
  // `pythons`: launcher -> the major version it prints ('' for the Store stub, which prints
  // nothing); `probes` the launchers asked for their version, `launchers` those that ran a helper
  pythons: Record<string, string>; probes: string[]; launchers: string[]
  // `uname` what `uname -s` prints; `openers` the browser openers installed, each with the exit code it returns
  uname: string; openers: Record<string, number>
}
function world(on: On, initial: Record<string, string>, placed = { is: true }, report: { value: Report } = { value: REPORT }) {
  const fs = new Map(Object.entries(initial))
  // the engine resolves paths natively first: on Windows /home/t arrives as D:\home\t
  const posix = (path: string) => path.replace(/\\/g, '/').replace(/^[A-Za-z]:/, '')
  const find = (path: string) => [...fs.keys()].find(k => k === posix(path) || posix(path).endsWith(k))
  on('fs.read', ($, e) => { const k = find(e.path); return k ? { value: fs.get(k)! } : { deny: `ENOENT: ${e.path}` } })
  on('fs.write', ($, e) => { fs.set(posix(e.path), e.text); return { value: undefined } })
  on('fs.exists', ($, e) => ({ value: find(e.path) !== undefined }))
  on('session.id', () => ({ value: SID }))
  on('session.start', ($, e) => ({ cwd: e.cwd }))
  on('telemetry.log', { to: 'collector' }, () => ({ value: undefined }))
  const ui: Ui = {
    opens: 0, commands: [], status: [], invalidates: 0, runs: [], toasts: [], config: [], store: new Map(), rows: ['tokentab.autoOpen'], surfaces: [],
    pythons: { python3: '3' }, probes: [], launchers: [],
    uname: 'Darwin', openers: { open: 0, 'xdg-open': 0, 'explorer.exe': 1 },
  }
  on('session.surfaces', () => ({ value: ui.surfaces }))
  on('session.attach', ($, e) => ({ clientId: e.clientId }))
  on('ui.open', () => { ui.opens++; return { value: placed.is ? { isPlaced: true } : { isPlaced: false, reason: 'narrower than 144 columns' } } })
  on('ui.panes', () => ({ value: [{ id: 'tokentab', title: 'TokenTab', isShown: placed.is, isFocused: false, isPlaced: placed.is }] }))
  on('ui.status', ($, e) => { ui.status.push(e.text); return { value: undefined } })
  on('ui.invalidate', () => { ui.invalidates++; return { value: undefined } })
  on('ui.toast', ($, e) => { ui.toasts.push(e.text); return { value: undefined } })
  on('command.register', ($, e) => { ui.commands.push(e.name); return { value: { command: e.name } } })
  on('store.get', ($, e) => ({ value: ui.store.get(e.key) }))
  on('store.set', ($, e) => { ui.store.set(e.key, e.value); return { value: undefined } })
  on('store.delete', ($, e) => { ui.store.delete(e.key); return { value: undefined } })
  on('config.list', () => ({ value: ui.rows.map(key => ({ key, label: key, kind: 'boolean' as const, value: true, provider: { plugin: 'tokentab@supertab', tier: 'user' as const }, isLocked: false })) }))
  on('config.set', ($, e) => { ui.config.push([e.key, e.value]); return { value: e.value } })
  on('process.run', ($, e) => {
    const argv = [...e.argv]
    if (argv[0] === 'uname') return { value: { exitCode: 0, stdout: `${ui.uname}\n`, stderr: '', isStdoutTruncated: false, isStderrTruncated: false } }
    if (['open', 'xdg-open', 'explorer.exe'].includes(argv[0]!)) {
      ui.runs.push(argv)
      if (!(argv[0]! in ui.openers)) return { deny: `spawn ${argv[0]} ENOENT` }
      return { value: { exitCode: ui.openers[argv[0]!]!, stdout: '', stderr: '', isStdoutTruncated: false, isStderrTruncated: false } }
    }
    // the interpreters on this machine: what each launcher's major version prints, absent
    // when it cannot start; a probe (`-c`) is kept apart from the helper runs
    const at = argv.indexOf('-c') >= 0 ? argv.indexOf('-c') : argv.findIndex(a => a.endsWith('.py'))
    const launcher = at < 0 ? '' : argv.slice(0, at).join(' ')
    if (at >= 0 && !(launcher in ui.pythons)) { ui.probes.push(launcher); throw new Error(`spawn ${argv[0]} ENOENT`) }
    if (argv[at] === '-c') {
      ui.probes.push(launcher)
      const v = ui.pythons[launcher]!
      return { value: { exitCode: v === '' ? 9009 : 0, stdout: v === '' ? '' : `${v}\n`, stderr: '', isStdoutTruncated: false, isStderrTruncated: false } }
    }
    if (at >= 0) {
      if (launcher !== 'python3') argv.splice(0, at, 'python3')   // the helper runs read alike whichever launcher ran them
      ui.launchers.push(launcher)
    }
    ui.runs.push(argv)
    const script = (argv[1] ?? '').split('/').pop()
    let exitCode = 0, stdout = '', stderr = ''
    if (script === 'tokentab.py') stdout = argv.includes('--status') ? STATUS + '\n' : argv.includes('--report') ? JSON.stringify(report.value) + '\n' : argv.includes('--once') ? ONCE : ''
    else if (script === 'sankey.py') stdout = argv.includes('--out') ? argv[argv.indexOf('--out') + 1] + '\n' : ''
    else if (script === 'budget.py') {
      if (argv[argv.length - 1] === 'bad') { exitCode = 1; stderr = `${argv[3]}: a number is needed, not 'bad'\n` }
      else stdout = JSON.stringify(argv[argv.length - 1]) + '\n'
    }
    return { value: { exitCode, stdout, stderr, isStdoutTruncated: false, isStderrTruncated: false } }
  })
  return { fs, ui }
}

// What `tokentab.py --once` prints (cut down): the header, a dim line, a bold total, a
// green budget bar, the blank line and the `ctrl-c to quit` line a pane has no use for.
const ONCE = [
  '\x1b[1mTokenTab\x1b[0m \x1b[2m· live\x1b[0m\x1b[2m        12:43:54\x1b[0m',
  '',
  '  \x1b[2mdata: ledger (every API call)\x1b[0m',
  '    \x1b[1mtotal                  960,195     $2.0228\x1b[0m   \x1b[2m11 msgs\x1b[0m',
  '    session   \x1b[32m██\x1b[2m····\x1b[0m     8%   \x1b[2m$2.0228 of $25.00\x1b[0m',
  '',
  '  \x1b[2mctrl-c to quit\x1b[0m',
  '',
].join('\n')
const STATUS = 'session $2.0228 · today $7.9406'
// What `tokentab.py --report` prints, the figures of Figma's frames: a session over its
// $2.00 limit (181%), warn only, and three tools.
const REPORT: Report = {
  sid: 'a0b1c2d3-session', prices_verified: true, today: 5.94, month: 28.38, overhead: 0.0039,
  session: { title: 'Token type sidebar', cost: 3.613, tokens: 532533, msgs: 47, limit_usd: 2, frac: 1.8065, warn_at: 70, hard_stop: false },
  rates: [
    { label: 'input', key: 'input', tokens: 5134, cost: 0.0205, share: 0.0057 },
    { label: 'cache write 1h', key: 'cache_write_1h', tokens: 226784, cost: 1.8143, share: 0.5022 },
    { label: 'cache read', key: 'cache_read', tokens: 5293202, cost: 1.0586, share: 0.293 },
    { label: 'output text', key: 'output', tokens: 23918, cost: 0.4784, share: 0.1324 },
    { label: 'thinking', key: 'thinking', tokens: 12061, cost: 0.2412, share: 0.0668 },
  ],
  total: { tokens: 5561099, cost: 3.613 }, unmatched: 125,
  tools: [
    { name: 'use_figma', calls: 12, cost: 0.5457, ask: 0.3332, ctx: 0.1498, read: 0.0627, ctx_tokens: 18721 },
    { name: 'Bash', calls: 5, cost: 0.0373, ask: 0.0183, ctx: 0.0148, read: 0.0042, ctx_tokens: 1853 },
    { name: 'computer', calls: 1, cost: 0.0072, ask: 0.0017, ctx: 0.004, read: 0.0015, ctx_tokens: 503 },
  ],
}
const BUDGET = JSON.stringify({
  _comment: 'kept', session_limit_usd: null,
  daily_limit_usd: 40, warn_at_percent: 70, hard_stop: false,
  cold_resume: { confirm_seconds: 120, min_context_tokens: 100000 },
}, null, 2)

const PANE_PROPS = { title: 'TokenTab', isFocused: false, bodyColumns: 80, placement: 'dock' as const, scroll: { offset: 0, bodyRows: 40 }, view: {} }

/** The `source` of every element of one type (Svg, Code) in a tree. */
function sources(node: RenderNode, type: string): string[] {
  if (typeof node === 'string') return []
  const el = node as RenderElement & { children?: RenderNode[]; props?: { source?: string } }
  const below = (el.children ?? []).flatMap(c => sources(c, type))
  return el.type === type ? [el.props?.source ?? '', ...below] : below
}

/** Every element of one type in a tree, with its props. */
function elements(node: RenderNode, type: string): Record<string, unknown>[] {
  if (typeof node === 'string') return []
  const el = node as RenderElement & { children?: RenderNode[]; props?: Record<string, unknown> }
  const below = (el.children ?? []).flatMap(c => elements(c, type))
  return el.type === type ? [(el.props ?? {}) as Record<string, unknown>, ...below] : below
}

/** Every Text in a tree, each as its own string plus its style props. */
function texts(node: RenderNode): { text: string; props: Record<string, unknown> }[] {
  if (typeof node === 'string') return []
  const el = node as RenderElement & { children?: RenderNode[] }
  const own = (el.children ?? []).filter((c): c is string => typeof c === 'string').join('')
  const below = (el.children ?? []).flatMap(texts)
  return el.type === 'Text' ? [{ text: own, props: (el.props ?? {}) as Record<string, unknown> }, ...below] : below
}

const SID = 'a0b1c2d3-session'
const PRICES = {
  _verified: true,
  models: { 'claude-fable-5-1': { input: 10, output: 50, cache_write_5m: 12.5, cache_write_1h: 20, cache_read: 0.25 } },
  default: { input: 4, output: 20, cache_write_5m: 5, cache_write_1h: 8, cache_read: 0.2 },
}
const DATA = '/home/t/.claude/plugins/data/tokentab-supertab'
const T0 = Date.UTC(2026, 9, 15, 14, 3, 11, 412)
const FILES = { '/tokentab/prices.json': JSON.stringify(PRICES), '/tokentab/budget.json': BUDGET }
const press = (key: string, surface?: 'terminal' | 'desktop') => ({ plugin: 'tokentab', key, requestId: 'tokentab', ...(surface ? { surface } : {}) })

function request(attributes: Record<string, string | number>) {
  return {
    to: 'collector' as const, event: 'api_request', loggedAt: '2026-10-15T14:03:11.412Z',
    attributes: {
      model: 'claude-fable-5-1', input_tokens: 12, output_tokens: 480, cache_read_tokens: 71000,
      cache_creation_tokens: 2100, query_source: 'main_loop', request_id: 'req_1', duration_ms: 2530, ...attributes,
    },
  }
}

test('session.start writes the loaded marker and a first budget.json', async ($, on) => {
  mock.env(on, { HOME: '/home/t' })
  mock.clock(on, { now: T0 })
  const { fs } = world(on, { '/tokentab/budget.json': '{"session_limit_usd": 25}\n' })
  await $.session.start({ cwd: '/w', surface: null, isInteractive: false })
  expect(fs.get(`${DATA}/state/loaded/${SID}`)).toBe('2026-10-15T14:03:11.412Z\n')
  expect(fs.get(`${DATA}/budget.json`)).toBe('{"session_limit_usd": 25}\n')
  fs.set(`${DATA}/budget.json`, 'edited by the user')
  await $.session.start({ cwd: '/w', surface: null, isInteractive: false })
  expect(fs.get(`${DATA}/budget.json`)).toBe('edited by the user')
})

test('with CLAUDE_CONFIG_DIR set, the data folder is under that config folder', async ($, on) => {
  mock.env(on, { HOME: '/home/t', CLAUDE_CONFIG_DIR: '/home/t/.claude-work' })
  mock.clock(on, { now: T0 })
  const { fs } = world(on, { '/tokentab/budget.json': '{"session_limit_usd": 25}\n' })
  await $.session.start({ cwd: '/w', surface: null, isInteractive: false })
  const data = '/home/t/.claude-work/plugins/data/tokentab-supertab'
  expect(fs.get(`${data}/state/loaded/${SID}`)).toBe('2026-10-15T14:03:11.412Z\n')
  expect(fs.get(`${data}/budget.json`)).toBe('{"session_limit_usd": 25}\n')
  expect(fs.has(`${DATA}/budget.json`)).toBe(false)
})

test('on Windows, with only USERPROFILE set, the ledger is under its .claude folder', async ($, on) => {
  mock.env(on, { USERPROFILE: 'C:\\Users\\t\\' })
  mock.clock(on, { now: T0 })
  const { fs } = world(on, { '/tokentab/prices.json': JSON.stringify(PRICES) })
  await $.telemetry.log(request({}))
  // the drive is dropped from the kept path; off Windows the engine takes C:\ as relative and puts its own folder in front
  const ledger = `/Users/t/.claude/plugins/data/tokentab-supertab/ledger/2026-10-15/${SID}.jsonl`
  expect([...fs.keys()].some(path => path.endsWith(ledger))).toBe(true)
})

test('the data folder comes from CLAUDE_CONFIG_DIR, then USERPROFILE, then HOME', () => {
  const tail = '/plugins/data/tokentab-supertab'
  expect(dataDir('/cfg/', 'C:\\Users\\t', '/home/t')).toBe(`/cfg${tail}`)
  expect(dataDir('C:\\cfg\\', undefined, undefined)).toBe(`C:\\cfg${tail}`)
  expect(dataDir(undefined, 'C:\\Users\\t\\', '/home/t/')).toBe(`C:\\Users\\t/.claude${tail}`)
  expect(dataDir('', '', '/home/t/')).toBe(`/home/t/.claude${tail}`)
  expect(() => dataDir(undefined, undefined, undefined)).toThrow('USERPROFILE and HOME are unset')
})

test('an api_request record becomes one priced ledger line per request', async ($, on) => {
  mock.env(on, { HOME: '/home/t' })
  mock.clock(on, { now: T0 })
  const { fs } = world(on, { '/tokentab/prices.json': JSON.stringify(PRICES) })
  const path = `${DATA}/ledger/2026-10-15/${SID}.jsonl`

  // tokens: 12 in, 480 out, 71000 cache read, 2100 cache write (at the 1h rate)
  const tokens = (12 * 10 + 480 * 50 + 71000 * 0.25 + 2100 * 20) / 1e6   // 0.08387
  await $.telemetry.log(request({ cost_usd: tokens + 0.01 }))              // one web search on top
  const lines = fs.get(path)!.trimEnd().split('\n').map(l => JSON.parse(l))
  expect(lines.length).toBe(1)
  expect(lines[0]).toEqual({
    ts: '2026-10-15T14:03:11.412Z', source: 'mod', session_id: SID, model: 'claude-fable-5-1',
    tokens: { input: 12, output: 480, cache_read: 71000, cache_write: 2100 },
    cost_usd: 0.08387, cost_usd_cc: tokens + 0.01, fees_usd: 0.01, duration_ms: 2530,
    query_source: 'main_loop', request_id: 'req_1', prices_verified: true,
  })

  // a second request appends; an unknown model is priced at the default rates, and a
  // difference under half a tenth of a cent is not a fee
  await $.telemetry.log(request({ model: 'claude-new', request_id: 'req_2', cost_usd: 0.0, query_source: 'sdk' }))
  const two = fs.get(path)!.trimEnd().split('\n').map(l => JSON.parse(l))
  expect(two.length).toBe(2)
  expect(two[0].request_id).toBe('req_1')
  expect(two[1].request_id).toBe('req_2')
  expect(two[1].cost_usd).toBe(Math.round((12 * 4 + 480 * 20 + 71000 * 0.2 + 2100 * 8) / 1e6 * 1e6) / 1e6)
  expect(two[1].fees_usd).toBe(0)
  expect(two[1].cost_usd_cc).toBe(0)

  // a crash in the middle of the in-place write leaves a cut last line: the next append
  // drops that tail and keeps the rest; no `.tmp` twin is written beside the file
  fs.set(path, fs.get(path)!.slice(0, -20))
  await $.telemetry.log(request({ request_id: 'req_3' }))
  const three = fs.get(path)!.trimEnd().split('\n').map(l => JSON.parse(l))
  expect(three.map(l => l.request_id)).toEqual(['req_1', 'req_3'])
  expect([...fs.keys()].some(k => k.endsWith('.tmp'))).toBe(false)
})

test('records that are not api_request, and a failure in the module, leave the record alone', async ($, on) => {
  mock.env(on, { HOME: '/home/t' })
  mock.clock(on, { now: T0 })
  const { fs } = world(on, {})   // no prices.json: the module's own read fails
  await $.telemetry.log({ to: 'collector', event: 'tool_result', attributes: { tool_name: 'Bash' }, loggedAt: '2026-10-15T14:03:11.412Z' })
  await expect($.telemetry.log(request({}))).resolves.toEqual({ value: undefined })
  expect([...fs.keys()].filter(k => k.includes('/ledger/')).length).toBe(0)
})

test('the figures as the design writes them, and the bars in Svg and in cells', async () => {
  expect([usd(3.613), usd(3.613, 4), usd(1234.5), int(5561099)]).toEqual(['$3.61', '$3.6130', '$1,234.50', '5,561,099'])
  expect(pct(1.8065)).toBe('181%')
  expect([toneOf(1.81, 70), toneOf(0.75, 70), toneOf(0.1, 70)]).toEqual(['error', 'warning', 'success'])
  // a track the full width, the parts on it; past the end a part is cut at the track's end;
  // light and dark fills by class, no ground of its own
  const svg = barSvg([{ series: 'ask', frac: 0.5 }, { series: 'ctx', frac: 0.8 }])
  expect(svg).toContain('<rect class="track" x="0" y="0" width="1000" height="6"/>')
  expect(svg).toContain('<rect class="ask" x="0.0" y="0" width="497.0" height="6"/>')
  expect(svg).toContain('<rect class="ctx" x="500.0" y="0" width="500.0" height="6"/>')
  expect(svg).toContain('@media(prefers-color-scheme:dark)')
  expect(barCells([{ series: 'ask', frac: 0.5 }, { series: 'ctx', frac: 0.2 }], 10)).toEqual([
    { text: '█████', series: 'ask' }, { text: '██', series: 'ctx' }, { text: '···' },
  ])
  expect(barCells([{ series: 'error', frac: 1.8 }], 4)).toEqual([{ text: '████', series: 'error' }])
  // the ctrl-c line and the blank lines after the view are dropped (the figures for Claude)
  expect(liveLines(ONCE).map(stripAnsi).slice(-1)).toEqual(['    session   ██····     8%   $2.0228 of $25.00'])
  // the budget fields: dotted keys, "off" for a limit switched off, grouped tokens
  const cfg = JSON.parse(BUDGET)
  expect(at(cfg, 'cold_resume.confirm_seconds')).toBe(120)
  expect(at(cfg, 'cold_resume.missing')).toBe(undefined)
  expect(shown(at(cfg, 'session_limit_usd'))).toBe('off')
  expect(shown(at(cfg, 'daily_limit_usd'))).toBe('40')
  expect(shown(at(cfg, 'cold_resume.min_context_tokens'))).toBe('100,000')
})

test('an interactive session opens the pane and draws the Spend tab', async ($, on) => {
  mock.env(on, { HOME: '/home/t' })
  const clock = mock.clock(on, { now: T0 })
  const { ui } = world(on, FILES)
  await $.session.start({ cwd: '/w', surface: 'terminal', isInteractive: true })
  await clock.settle()                                        // the redraw session.start left running
  expect(ui.opens).toBe(1)
  expect(ui.commands).toEqual([])                             // nothing registered: the skill is the command
  // placed: the report was fetched once, with the session pinned, and the status line cleared
  expect(ui.runs.map(a => a.slice(2))).toEqual([['--report', '--session', SID]])
  expect(ui.status).toEqual([undefined])

  const folded = await $.ui.render({ surface: 'terminal', component: 'Pane', requestId: 'tokentab', props: PANE_PROPS })
  // the tab row: Spend is the primary one, then Budget and the browser button; then the details toggle
  expect(elements(folded, 'Button').map(b => [b.key, b.variant, b.hotkey])).toEqual([
    ['tab-spend', 'primary', 's'], ['tab-budget', 'secondary', 'b'], ['open-browser', undefined, 'o'],
    ['details-toggle', undefined, 'd'],
  ])
  // the breakdown starts folded: the toggle shows, the rate table and the tools do not
  expect(texts(folded).some(x => x.text === '▸ DETAILS')).toBe(true)
  expect(texts(folded).some(x => x.text === '  total' || x.text === 'TOOLS')).toBe(false)
  await $.ui.press(press('details-toggle'))

  const tree = await $.ui.render({ surface: 'terminal', component: 'Pane', requestId: 'tokentab', props: PANE_PROPS })
  const t = texts(tree)
  expect(t.some(x => x.text === '▾ DETAILS')).toBe(true)
  const find = (s: string) => t.find(x => x.text === s)
  // the hero: the figure at the title size, the badge in the error tone, the line under the bar
  expect(find('SESSION SPEND')?.props).toEqual({ size: 'footnote', dimColor: true })
  expect(find('$3.61')?.props).toEqual({ size: 'title', weight: 'bold', tabular: true })
  expect(find('Over · 181%')?.props).toEqual({ size: 'footnote', weight: 'semibold', color: 'error' })
  expect(find('of $2.00 · 532,533 tokens · warn only, warns at 70%')).toBeDefined()
  // the breakdown: a row per rate with its dot in the series colour, the total, the note;
  // then the tools' legend
  expect(t.filter(x => x.text === '● ').map(x => x.props.color)).toEqual(
    ['input', 'cache_write_1h', 'cache_read', 'output', 'thinking', 'ask', 'ctx', 'read'].map(k => SERIES[k]![0]))
  for (const s of ['cache write 1h', '226,784', '$1.8143', '50%', '  total', '5,561,099', '$3.6130']) expect(find(s)).toBeDefined()
  expect(t.some(x => x.text.startsWith('125 output tok have no transcript line'))).toBe(true)
  // every session, today and this month
  expect([find('Today'), find('$5.94'), find('This month'), find('$28.38')].every(x => x !== undefined)).toBe(true)
  // the terminal has no Svg: the bars are block characters, the session's in the error tone
  expect(sources(tree, 'Svg')).toEqual([])
  expect(t.find(x => x.props.color === SERIES.error![0])?.text).toMatch(/^█+$/)

  // a remote surface: the same tree with the bars as Svg, the session's, the rates' and one per tool
  for (const surface of ['desktop', 'vscode', 'mobile'] as const) {
    const remote = await $.ui.render({ surface, component: 'Pane', requestId: 'tokentab', props: PANE_PROPS })
    const svgs = elements(remote, 'Svg').filter(s => s.alt !== 'separator')
    expect(svgs.length).toBe(2 + REPORT.tools.length)
    expect(svgs[0]!.source).toBe(barSvg([{ series: 'error', frac: 1.8065 }], 8))
    expect(svgs[0]!.alt).toBe('181% of the session limit')
    expect(svgs[1]!.alt).toBe('this session by rate: input 1%, cache write 1h 50%, cache read 29%, output text 13%, thinking 7%')
    expect(texts(remote).find(x => x.text === '$3.61')?.props.size).toBe('title')
  }

  // a ledger write redraws on the clock's next beat, not in the request's own dispatch
  // (a subagent's, while one works): one report run, one invalidate
  await clock.settle()
  const runs = ui.runs.length, invalidates = ui.invalidates
  await $.telemetry.log(request({}))
  await clock.settle()
  expect(ui.runs.length).toBe(runs)
  await clock.advance(1000)
  expect(ui.runs.slice(runs).map(a => a.slice(2))).toEqual([['--report', '--session', SID]])
  expect(ui.invalidates).toBe(invalidates + 1)

  // a burst of requests, parallel subagents', is one redraw; a quiet beat is none
  const burst = ui.runs.length
  for (const n of [2, 3, 4]) await $.telemetry.log(request({ request_id: `req_${n}` }))
  await clock.advance(1000)
  await clock.advance(1000)
  expect(ui.runs.length).toBe(burst + 1)
})

test('Spend without a limit has no bar and no badge; before the first call it says so', async ($, on) => {
  mock.env(on, { HOME: '/home/t' })
  const clock = mock.clock(on, { now: T0 })
  const report: { value: Report } = { value: { ...REPORT, session: { ...REPORT.session!, limit_usd: null, frac: null } } }
  world(on, FILES, { is: true }, report)
  await $.session.start({ cwd: '/w', surface: 'desktop', isInteractive: true })
  await clock.settle()
  await $.ui.render({ surface: 'desktop', component: 'Pane', requestId: 'tokentab', props: PANE_PROPS })
  await $.ui.press(press('details-toggle', 'desktop'))
  const tree = await $.ui.render({ surface: 'desktop', component: 'Pane', requestId: 'tokentab', props: PANE_PROPS })
  expect(elements(tree, 'Svg').filter(s => s.alt !== 'separator').map(s => s.alt)[0]).toMatch(/^this session by rate/)
  expect(texts(tree).some(x => x.text.startsWith('Over') || x.text.endsWith('used'))).toBe(false)
  expect(texts(tree).some(x => x.text === '532,533 tokens · no session limit set')).toBe(true)

  report.value = { ...REPORT, session: null, tools: [] }
  await $.telemetry.log(request({}))
  await clock.advance(1000)          // the clock's next beat
  const none = await $.ui.render({ surface: 'desktop', component: 'Pane', requestId: 'tokentab', props: PANE_PROPS })
  expect(texts(none).some(x => x.text.startsWith('No API call in this session yet'))).toBe(true)
  expect(texts(none).some(x => x.text === '$28.38')).toBe(true)

  report.value = { ...REPORT, tools: [] }
  await $.telemetry.log(request({}))
  await clock.advance(1000)
  const tools = await $.ui.render({ surface: 'desktop', component: 'Pane', requestId: 'tokentab', props: PANE_PROPS })
  expect(texts(tools).some(x => x.text === 'No tool calls in this session yet.')).toBe(true)
})

test("Spend's details: per tool its calls, cost, a bar against the costliest and its three figures", async ($, on) => {
  mock.env(on, { HOME: '/home/t' })
  const clock = mock.clock(on, { now: T0 })
  world(on, FILES)
  await $.session.start({ cwd: '/w', surface: 'desktop', isInteractive: true })
  await clock.settle()
  await $.ui.render({ surface: 'desktop', component: 'Pane', requestId: 'tokentab', props: PANE_PROPS })
  await $.ui.press(press('details-toggle', 'desktop'))
  const tree = await $.ui.render({ surface: 'desktop', component: 'Pane', requestId: 'tokentab', props: PANE_PROPS })
  const t = texts(tree)
  for (const s of ['TOOLS', '  3 tools · 18 calls', 'use_figma', '12 calls', '$0.5457', 'computer', '1 call',
    'ask $0.3332 · ctx $0.1498 (18,721 tok) · re-read $0.0627', 'TokenTab overhead $0.0039: its hook text in context']) {
    expect(t.some(x => x.text === s)).toBe(true)
  }
  // one bar per tool after the session's and the rates', the costliest filling its track
  const svgs = elements(tree, 'Svg').filter(s => s.alt !== 'separator').slice(2)
  expect(svgs.length).toBe(3)
  const r = REPORT.tools[0]!
  expect(svgs[0]!.source).toBe(barSvg([{ series: 'ask', frac: r.ask / r.cost }, { series: 'ctx', frac: r.ctx / r.cost }, { series: 'read', frac: r.read / r.cost }], 4))
  // the legend, after the rates' dots
  expect(t.filter(x => x.text === '● ').map(x => x.props.color).slice(-3)).toEqual(['ask', 'ctx', 'read'].map(k => SERIES[k]![0]))
})

test('while the pane is not placed, the status line carries the numbers', async ($, on) => {
  mock.env(on, { HOME: '/home/t' })
  const clock = mock.clock(on, { now: T0 })
  const { ui } = world(on, FILES, { is: false })
  await $.session.start({ cwd: '/w', surface: 'terminal', isInteractive: true })
  await clock.settle()
  expect(ui.opens).toBe(1)
  expect(ui.runs.map(a => a.slice(2))).toEqual([['--status', '--session', SID]])
  expect(ui.status).toEqual([STATUS])
})

test('an auto-open the engine left undrawn is opened again, asked, by the first prompt the person enters', async ($, on) => {
  mock.env(on, { HOME: '/home/t' })
  const clock = mock.clock(on, { now: T0 })
  const { ui } = world(on, FILES, { is: false })
  on('prompt.submit', ($, e) => ({ text: e.text }))
  await $.session.start({ cwd: '/w', surface: 'terminal', isInteractive: true })
  await clock.settle()
  expect(ui.opens).toBe(1)
  expect(ui.toasts).toEqual(['TokenTab: the pane opens with your first prompt (narrower than 144 columns)'])
  // a prompt the person did not type is not an ask
  await $.prompt.submit({ text: 'from a schedule', wait: false, origin: { kind: 'scheduled-trigger' } })
  expect(ui.opens).toBe(1)
  const r = await $.prompt.submit({ text: 'hello', wait: false, origin: { kind: 'composer' } })
  expect(r).toEqual({ text: 'hello' })
  expect(ui.opens).toBe(2)
  // once a session
  await $.prompt.submit({ text: 'again', wait: false, origin: { kind: 'composer' } })
  expect(ui.opens).toBe(2)
})

test('with autoOpen off, the first prompt does not open the pane either', { options: { autoOpen: false } }, async ($, on) => {
  mock.env(on, { HOME: '/home/t' })
  const clock = mock.clock(on, { now: T0 })
  const { ui } = world(on, FILES, { is: false })
  on('prompt.submit', ($, e) => ({ text: e.text }))
  await $.session.start({ cwd: '/w', surface: 'terminal', isInteractive: true })
  await clock.settle()
  await $.prompt.submit({ text: 'hello', wait: false, origin: { kind: 'composer' } })
  expect(ui.opens).toBe(0)
})

test('in the Desktop app (an SDK host, no surface at session.start) the pane opens when Desktop attaches, once', async ($, on) => {
  mock.env(on, { HOME: '/home/t' })
  const clock = mock.clock(on, { now: T0 })
  const { ui } = world(on, FILES)
  await $.session.start({ cwd: '/w', surface: null, isInteractive: false })
  await clock.settle()
  expect(ui.opens).toBe(0)
  ui.surfaces = ['desktop']
  await $.session.attach({ surface: 'desktop', clientId: 'desktop:default' })
  await clock.settle()
  expect(ui.opens).toBe(1)
  expect(ui.runs.map(a => a.slice(2))).toContainEqual(['--report', '--session', SID])
  // a second client joining does not reopen it
  await $.session.attach({ surface: 'desktop', clientId: 'desktop:2' })
  expect(ui.opens).toBe(1)
  // a reload (session.start again, Desktop already attached) opens it at once
  await $.session.start({ cwd: '/w', surface: null, isInteractive: false })
  await clock.settle()
  expect(ui.opens).toBe(2)
})

test('in Desktop, a pane left undrawn opens with the first prompt typed there (origin sdk)', async ($, on) => {
  mock.env(on, { HOME: '/home/t' })
  const clock = mock.clock(on, { now: T0 })
  const { ui } = world(on, FILES, { is: false })
  on('prompt.submit', ($, e) => ({ text: e.text }))
  await $.session.start({ cwd: '/w', surface: null, isInteractive: false })
  await $.session.attach({ surface: 'desktop', clientId: 'desktop:default' })
  await clock.settle()
  expect(ui.opens).toBe(1)
  await $.prompt.submit({ text: 'hello', wait: false, origin: { kind: 'sdk' } })
  expect(ui.opens).toBe(2)
})

test('under claude -p, or with no surface, nothing visual happens and the ledger is written', async ($, on) => {
  mock.env(on, { HOME: '/home/t' })
  const clock = mock.clock(on, { now: T0 })
  const { fs, ui } = world(on, FILES)
  await $.session.start({ cwd: '/w', surface: null, isInteractive: false })
  await $.telemetry.log(request({}))
  await clock.settle()
  expect(ui.opens).toBe(0)
  expect(ui.runs).toEqual([])
  expect(ui.status).toEqual([])
  expect(fs.get(`${DATA}/ledger/2026-10-15/${SID}.jsonl`)!.trimEnd().split('\n').length).toBe(1)
})

test('with autoOpen off, an interactive session does not open the pane; the status line still runs', { options: { autoOpen: false } }, async ($, on) => {
  mock.env(on, { HOME: '/home/t' })
  const clock = mock.clock(on, { now: T0 })
  const { ui } = world(on, FILES, { is: false })
  await $.session.start({ cwd: '/w', surface: 'terminal', isInteractive: true })
  await clock.settle()
  expect(ui.opens).toBe(0)
  expect(ui.status).toEqual([STATUS])
})

test('/tokentab:status (the skill) is answered by the hook: the pane opens, the figures go to the person and to Claude', async ($, on) => {
  mock.env(on, { HOME: '/home/t' })
  mock.clock(on, { now: T0 })
  const { ui } = world(on, FILES)
  await $.session.start({ cwd: '/w', surface: 'terminal', isInteractive: true })
  const opens = ui.opens
  const r = await $.command.run({ command: 'tokentab:status', args: '', origin: { kind: 'composer' }, presentation: { isFullscreen: true, columns: 200 } })
  expect(ui.opens).toBe(opens + 1)
  expect(r.text).toBe(`${STATUS}\nThe TokenTab pane is open beside the chat; "open in browser" there writes the status page.`)
  expect(r.context).toEqual([`TokenTab, this session's figures as of now:\n${liveLines(ONCE).map(stripAnsi).join('\n')}`])
})

test('the open-in-browser button writes the page for this session, opens it, and keeps it live', async ($, on) => {
  mock.env(on, { HOME: '/home/t' })
  const clock = mock.clock(on, { now: T0 })
  const { ui } = world(on, FILES)
  await $.session.start({ cwd: '/w', surface: 'terminal', isInteractive: true })
  await clock.settle()
  await $.ui.render({ surface: 'terminal', component: 'Pane', requestId: 'tokentab', props: PANE_PROPS })
  const runs = ui.runs.length
  await $.ui.press(press('open-browser'))
  const page = `${DATA}/status/${SID}.html`
  const named = (a: string[]) => a[0] === 'python3' ? ['python3', a[1]!.split('/').pop()!, ...a.slice(2)] : a
  expect(ui.runs.slice(runs).map(named)).toEqual([
    ['python3', 'sankey.py', '--session', SID, '--out', page],
    ['open', page],
  ])
  expect(ui.toasts).toEqual([`TokenTab: status page opened (${page}); it stays live for 30 minutes`])
  expect(ui.store.get(`pageOpenedAt:${SID}`)).toBe(T0)

  // from now on a redraw writes the page's data file beside it too, in a second run
  const before = ui.runs.length
  await $.telemetry.log(request({}))
  await clock.advance(1000)
  expect(ui.runs.slice(before).map(named)).toEqual([
    ['python3', 'tokentab.py', '--report', '--session', SID],
    ['python3', 'sankey.py', '--session', SID, '--push', `${DATA}/status/${SID}.js`],
  ])
})

/** Presses "open in browser" on a machine whose `uname -s` prints `uname`; the runs and toasts after. */
async function openOn($: Engine, on: On, env: Record<string, string>, uname: string, openers?: Record<string, number>) {
  mock.env(on, env)
  const clock = mock.clock(on, { now: T0 })
  const { ui } = world(on, FILES)
  ui.uname = uname
  if (openers) ui.openers = openers
  await $.session.start({ cwd: '/w', surface: 'terminal', isInteractive: true })
  await clock.settle()
  await $.ui.render({ surface: 'terminal', component: 'Pane', requestId: 'tokentab', props: PANE_PROPS })
  await $.ui.press(press('open-browser'))
  return ui
}

test('on macOS the page opens with open', async ($, on) => {
  const ui = await openOn($, on, { HOME: '/home/t' }, 'Darwin')
  const page = `${DATA}/status/${SID}.html`
  expect(ui.runs[ui.runs.length - 1]).toEqual(['open', page])
  expect(ui.toasts).toEqual([`TokenTab: status page opened (${page}); it stays live for 30 minutes`])
})

test('on Linux the page opens with xdg-open', async ($, on) => {
  const ui = await openOn($, on, { HOME: '/home/t' }, 'Linux')
  const page = `${DATA}/status/${SID}.html`
  expect(ui.runs[ui.runs.length - 1]).toEqual(['xdg-open', page])
  expect(ui.toasts).toEqual([`TokenTab: status page opened (${page}); it stays live for 30 minutes`])
})

test('on Windows the page opens with explorer.exe, whose exit code 1 is not a failure', async ($, on) => {
  const ui = await openOn($, on, { OS: 'Windows_NT', USERPROFILE: 'C:\\Users\\t' }, '', { 'explorer.exe': 1 })
  const page = `C:\\Users\\t/.claude/plugins/data/tokentab-supertab/status/${SID}.html`
  expect(ui.runs[ui.runs.length - 1]).toEqual(['explorer.exe', page.replace(/\//g, '\\')])
  expect(ui.toasts).toEqual([`TokenTab: status page opened (${page}); it stays live for 30 minutes`])
  expect(ui.store.get(`pageOpenedAt:${SID}`)).toBe(T0)
})

test('on Linux without xdg-open, the toast says the page could not be opened and where it is', async ($, on) => {
  const ui = await openOn($, on, { HOME: '/home/t' }, 'Linux', {})
  const page = `${DATA}/status/${SID}.html`
  expect(ui.toasts).toEqual([`TokenTab: could not open the status page (${page}): tokentab: $.process.run: spawn xdg-open ENOENT`])
  expect(ui.store.get(`pageOpenedAt:${SID}`)).toBeUndefined()
})

test('the Budget tab: auto-open through the config menu, the limits through the validator', async ($, on) => {
  mock.env(on, { HOME: '/home/t' })
  const clock = mock.clock(on, { now: T0 })
  const { ui } = world(on, FILES)
  await $.session.start({ cwd: '/w', surface: 'terminal', isInteractive: true })
  await clock.settle()
  await $.ui.render({ surface: 'terminal', component: 'Pane', requestId: 'tokentab', props: PANE_PROPS })
  await $.ui.press(press('tab-budget'))
  const tree = await $.ui.render({ surface: 'terminal', component: 'Pane', requestId: 'tokentab', props: PANE_PROPS })
  const t = texts(tree)
  expect(t.some(x => x.text === 'Open pane at session start')).toBe(true)
  expect(t.filter(x => /^[A-Z][A-Z ]+$/.test(x.text) && x.props.dimColor).map(x => x.text)).toEqual(['OPTIONS', 'LIMITS'])
  // the units are in the names
  expect(t.filter(x => x.text.endsWith(')')).map(x => x.text)).toEqual(['This session (USD)', 'Today (USD)', 'Warn at (%)'])
  expect(elements(tree, 'Button').find(b => b.key === 'auto-open')?.label).toBe('on')
  // the fields hold budget.json's values, "off" for null, grouped tokens
  const inputs = elements(tree, 'Input')
  expect(inputs.map(i => i.key)).toEqual(['b-session_limit_usd', 'b-daily_limit_usd', 'b-warn_at_percent'])
  expect(inputs.find(i => i.key === 'b-daily_limit_usd')?.value).toBe('40')
  expect(inputs.find(i => i.key === 'b-session_limit_usd')?.value).toBe('off')
  expect(elements(tree, 'Button').find(b => b.key === 'b-budgets_on')?.label).toBe('off')
  expect(elements(tree, 'Button').find(b => b.key === 'b-hard_stop')?.label).toBe('off')

  // the switch is the manifest's userConfig field, written as the menu would
  ui.store.set('autoOpen', true)
  await $.ui.press(press('auto-open'))
  expect(ui.config).toEqual([['tokentab.autoOpen', false]])
  expect(ui.store.has('autoOpen')).toBe(false)

  // a limit goes through budget.py; a refused value is shown under its field
  await $.ui.input({ plugin: 'tokentab', key: 'b-daily_limit_usd', text: '30' })
  expect(ui.runs[ui.runs.length - 1]!.slice(1).map((s, i) => i === 0 ? s.split('/').pop() : s)).toEqual(['budget.py', 'set', 'daily_limit_usd', '30'])
  await $.ui.input({ plugin: 'tokentab', key: 'b-session_limit_usd', text: 'bad' })
  const after = await $.ui.render({ surface: 'terminal', component: 'Pane', requestId: 'tokentab', props: PANE_PROPS })
  expect(texts(after).find(x => x.props.color === 'error')?.text.trim()).toBe("session_limit_usd: a number is needed, not 'bad'")
  await $.ui.press(press('b-hard_stop'))
  expect(ui.runs[ui.runs.length - 1]!.slice(2)).toEqual(['set', 'hard_stop', 'on'])

  // mobile has no Input: the values are shown
  const mobile = await $.ui.render({ surface: 'mobile', component: 'Pane', requestId: 'tokentab', props: PANE_PROPS })
  expect(elements(mobile, 'Input')).toEqual([])
  expect(texts(mobile).some(x => x.text === '40')).toBe(true)
})

test('where /config has no row for it (Desktop), the auto-open switch is kept in the store until /config changes it', async ($, on) => {
  mock.env(on, { HOME: '/home/t' })
  const clock = mock.clock(on, { now: T0 })
  const { ui } = world(on, FILES)
  ui.rows = []
  await $.session.start({ cwd: '/w', surface: 'desktop', isInteractive: true })
  await clock.settle()
  expect(ui.opens).toBe(1)
  await $.ui.render({ surface: 'desktop', component: 'Pane', requestId: 'tokentab', props: PANE_PROPS })
  await $.ui.press(press('tab-budget'))
  await $.ui.render({ surface: 'desktop', component: 'Pane', requestId: 'tokentab', props: PANE_PROPS })
  await $.ui.press(press('auto-open'))
  expect(ui.config).toEqual([])
  expect(ui.toasts).toEqual([])
  expect(ui.store.get('autoOpen')).toBe(false)
  const tree = await $.ui.render({ surface: 'desktop', component: 'Pane', requestId: 'tokentab', props: PANE_PROPS })
  expect(elements(tree, 'Button').find(b => b.key === 'auto-open')?.label).toBe('off')

  // the next session reads it back and opens nothing
  await $.session.start({ cwd: '/w', surface: 'desktop', isInteractive: true })
  await clock.settle()
  expect(ui.opens).toBe(1)

  // a change in /config stands over it
  await $.config.set({ key: 'tokentab.autoOpen', value: true, previous: true, provider: { plugin: 'tokentab@supertab', tier: 'user' }, origin: { kind: 'composer' } })
  expect(ui.store.has('autoOpen')).toBe(false)
})

test('with only `py` installed and a Store stub as python3, the helpers run under `py -3`, found once', async ($, on) => {
  mock.env(on, { HOME: '/home/t' })
  const clock = mock.clock(on, { now: T0 })
  const { ui } = world(on, FILES, { is: false })
  ui.pythons = { python3: '', py: '3', 'py -3': '3' }
  await $.session.start({ cwd: '/w', surface: 'terminal', isInteractive: true })
  await clock.settle()
  // python3 answers with nothing (the stub), python is absent, py -3 is Python 3
  expect(ui.probes).toEqual(['python3', 'python', 'py -3'])
  expect(ui.status).toEqual([STATUS])
  expect(ui.store.get('python')).toEqual(['py', '-3'])
  // the status command and a budget change use the same launcher, with no second probe
  const r = await $.command.run({ command: 'tokentab:status', args: '', origin: { kind: 'composer' }, presentation: { isFullscreen: true, columns: 200 } })
  expect(r.text?.startsWith(STATUS)).toBe(true)
  await $.ui.render({ surface: 'terminal', component: 'Pane', requestId: 'tokentab', props: PANE_PROPS })
  await $.ui.press(press('tab-budget'))
  await $.ui.render({ surface: 'terminal', component: 'Pane', requestId: 'tokentab', props: PANE_PROPS })
  await $.ui.input({ plugin: 'tokentab', key: 'b-daily_limit_usd', text: '30' })
  await clock.settle()
  expect(ui.probes.length).toBe(3)
  expect(new Set(ui.launchers)).toEqual(new Set(['py -3']))
  expect(ui.runs[ui.runs.length - 1]!.slice(2)).toEqual(['set', 'daily_limit_usd', '30'])
})

test('a python3 that is Python 2 is passed over for `python`', async ($, on) => {
  mock.env(on, { HOME: '/home/t' })
  const clock = mock.clock(on, { now: T0 })
  const { ui } = world(on, FILES, { is: false })
  ui.pythons = { python3: '2', python: '3' }
  await $.session.start({ cwd: '/w', surface: 'terminal', isInteractive: true })
  await clock.settle()
  expect(ui.probes).toEqual(['python3', 'python'])
  expect(ui.launchers).toEqual(['python'])
})

test('the launcher the store kept is used without a probe; once it fails to start, the next one is found and kept', async ($, on) => {
  mock.env(on, { HOME: '/home/t' })
  const clock = mock.clock(on, { now: T0 })
  const { ui } = world(on, FILES, { is: false })
  ui.pythons = { python3: '3', python: '3' }
  ui.store.set('python', ['python'])
  await $.session.start({ cwd: '/w', surface: 'terminal', isInteractive: true })
  await clock.settle()
  expect(ui.probes).toEqual([])
  expect(ui.launchers).toEqual(['python'])

  // `python` uninstalled: that run fails to start, a probe finds python3, the run is retried
  delete ui.pythons.python
  await clock.advance(30_000)
  expect(ui.probes).toEqual(['python', 'python3'])
  expect(ui.launchers).toEqual(['python', 'python3'])
  expect(ui.status).toEqual([STATUS, STATUS])
  expect(ui.store.get('python')).toEqual(['python3'])
  await clock.advance(30_000)
  expect(ui.probes.length).toBe(2)
})

test('with no Python 3 at all, the error says what was tried', async ($, on) => {
  mock.env(on, { HOME: '/home/t' })
  const clock = mock.clock(on, { now: T0 })
  const { ui } = world(on, FILES, { is: false })
  ui.pythons = { python3: '' }
  await $.session.start({ cwd: '/w', surface: 'terminal', isInteractive: true })
  await clock.settle()
  const r = await $.command.run({ command: 'tokentab:status', args: '', origin: { kind: 'composer' }, presentation: { isFullscreen: true, columns: 200 } })
  expect(r.text?.split('\n')[0]).toBe('TokenTab: no Python 3 found (tried python3, python, py -3)')
})
