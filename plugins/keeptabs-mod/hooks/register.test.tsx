import { test, expect, mock } from 'claude-code/testing'
import type { On, RenderElement, RenderNode } from 'claude-code'
import { at, liveLines, shown, spansOf, stripAnsi, svgOf } from './register'

// The world beneath the plugin: `$.fs` reaches nothing under `claude plugin test`, and
// nothing answers `telemetry.log`, `session.id`, the pane, the command, the store, the
// config menu or a process, so the test stands in for them. Files are matched by their
// tail, since the plugin's root is the engine's to know. `ui` records what the plugin
// asked the surface for, and `placed` says what `$.ui.panes()` reports for the pane.
type Ui = {
  opens: number; commands: string[]; status: (string | undefined)[]; invalidates: number
  runs: string[][]; toasts: string[]; config: [string, unknown][]; store: Map<string, unknown>
}
function world(on: On, initial: Record<string, string>, placed = { is: true }) {
  const fs = new Map(Object.entries(initial))
  const find = (path: string) => [...fs.keys()].find(k => k === path || path.endsWith(k))
  on('fs.read', ($, e) => { const k = find(e.path); return k ? { value: fs.get(k)! } : { deny: `ENOENT: ${e.path}` } })
  on('fs.write', ($, e) => { fs.set(e.path, e.text); return { value: undefined } })
  on('fs.exists', ($, e) => ({ value: find(e.path) !== undefined }))
  on('session.id', () => ({ value: SID }))
  on('session.start', ($, e) => ({ cwd: e.cwd }))
  on('telemetry.log', { to: 'collector' }, () => ({ value: undefined }))
  const ui: Ui = { opens: 0, commands: [], status: [], invalidates: 0, runs: [], toasts: [], config: [], store: new Map() }
  on('ui.open', () => { ui.opens++; return { value: placed.is ? { isPlaced: true } : { isPlaced: false, reason: 'narrower than 144 columns' } } })
  on('ui.panes', () => ({ value: [{ id: 'keeptabs', title: 'keeptabs', isShown: placed.is, isFocused: false, isPlaced: placed.is }] }))
  on('ui.status', ($, e) => { ui.status.push(e.text); return { value: undefined } })
  on('ui.invalidate', () => { ui.invalidates++; return { value: undefined } })
  on('ui.toast', ($, e) => { ui.toasts.push(e.text); return { value: undefined } })
  on('command.register', ($, e) => { ui.commands.push(e.name); return { value: { command: e.name } } })
  on('store.get', ($, e) => ({ value: ui.store.get(e.key) }))
  on('store.set', ($, e) => { ui.store.set(e.key, e.value); return { value: undefined } })
  on('config.set', ($, e) => { ui.config.push([e.key, e.value]); return { value: e.value } })
  on('process.run', ($, e) => {
    const argv = [...e.argv]
    ui.runs.push(argv)
    const script = (argv[1] ?? '').split('/').pop()
    let exitCode = 0, stdout = '', stderr = ''
    if (script === 'keeptabs.py') stdout = argv.includes('--status') ? STATUS + '\n' : argv.includes('--once') ? ONCE : ''
    else if (script === 'sankey.py') stdout = argv.includes('--flow-svg') ? FLOW : argv.includes('--out') ? argv[argv.indexOf('--out') + 1] + '\n' : ''
    else if (script === 'budget.py') {
      if (argv[argv.length - 1] === 'bad') { exitCode = 1; stderr = `${argv[3]}: a number is needed, not 'bad'\n` }
      else stdout = JSON.stringify(argv[argv.length - 1]) + '\n'
    }
    return { value: { exitCode, stdout, stderr, isStdoutTruncated: false, isStderrTruncated: false } }
  })
  return { fs, ui }
}

// What `keeptabs.py --once` prints (cut down): the header, a dim line, a bold total, a
// green budget bar, the blank line and the `ctrl-c to quit` line a pane has no use for.
const ONCE = [
  '\x1b[1mkeeptabs\x1b[0m \x1b[2m· live\x1b[0m\x1b[2m        12:43:54\x1b[0m',
  '',
  '  \x1b[2mdata: ledger (every API call)\x1b[0m',
  '    \x1b[1mtotal                  960,195     $2.0228\x1b[0m   \x1b[2m11 msgs\x1b[0m',
  '    session   \x1b[32m██\x1b[2m····\x1b[0m     8%   \x1b[2m$2.0228 of $25.00\x1b[0m',
  '',
  '  \x1b[2mctrl-c to quit\x1b[0m',
  '',
].join('\n')
const STATUS = 'session $2.0228 · today $7.9406 · job $17.98 left (a pizza ($20.00))'
const FLOW = '<svg viewBox="0 0 760 380" width="760" height="380" xmlns="http://www.w3.org/2000/svg"><style>text{fill:#d4d4d4}</style><rect/></svg>'
const BUDGET = JSON.stringify({
  _comment: 'kept', session_limit_usd: 25, session_limit_tokens: 90000000, turn_limit_usd: 2, turn_limit_tokens: null,
  daily_limit_usd: 40, daily_limit_tokens: 40000000, warn_at_percent: 70, hard_stop: false,
  job: { ask: true, ask_over_usd: 1.0, hard_stop: true },
}, null, 2)

const PANE_PROPS = { title: 'keeptabs', isFocused: false, bodyColumns: 80, placement: 'dock' as const, scroll: { offset: 0, bodyRows: 40 }, view: {} }

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
const DATA = '/home/t/.claude/plugins/data/keeptabs-mod-supertab'
const T0 = Date.UTC(2026, 9, 15, 14, 3, 11, 412)
const FILES = { '/keeptabs/prices.json': JSON.stringify(PRICES), '/keeptabs/budget.json': BUDGET }
const press = (key: string, surface?: 'terminal' | 'desktop') => ({ plugin: 'keeptabs-mod', key, requestId: 'keeptabs', ...(surface ? { surface } : {}) })

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
  const { fs } = world(on, { '/keeptabs/budget.json': '{"session_limit_usd": 25}\n' })
  await $.session.start({ cwd: '/w', surface: null, isInteractive: false })
  expect(fs.get(`${DATA}/state/loaded/${SID}`)).toBe('2026-10-15T14:03:11.412Z\n')
  expect(fs.get(`${DATA}/budget.json`)).toBe('{"session_limit_usd": 25}\n')
  fs.set(`${DATA}/budget.json`, 'edited by the user')
  await $.session.start({ cwd: '/w', surface: null, isInteractive: false })
  expect(fs.get(`${DATA}/budget.json`)).toBe('edited by the user')
})

test('an api_request record becomes one priced ledger line per request', async ($, on) => {
  mock.env(on, { HOME: '/home/t' })
  mock.clock(on, { now: T0 })
  const { fs } = world(on, { '/keeptabs/prices.json': JSON.stringify(PRICES) })
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

test('the ANSI codes of the live view become spans: bold, dim and the four colours as hex', async () => {
  expect(spansOf('plain')).toEqual([{ text: 'plain' }])
  expect(spansOf('\x1b[1mkeeptabs\x1b[0m \x1b[2m· live\x1b[0m')).toEqual([
    { text: 'keeptabs', bold: true }, { text: ' ' }, { text: '· live', dim: true },
  ])
  // a colour and dim side by side, as the budget bar is drawn; a reset ends both
  expect(spansOf('\x1b[32m██\x1b[2m··\x1b[0m 8%')).toEqual([
    { text: '██', color: '#00cd00' }, { text: '··', dim: true, color: '#00cd00' }, { text: ' 8%' },
  ])
  expect(spansOf('\x1b[31m\x1b[1mOVER\x1b[0m')).toEqual([{ text: 'OVER', bold: true, color: '#cd0000' }])
  expect(spansOf('\x1b[33mwarn\x1b[39m \x1b[36mcyan\x1b[22m')).toEqual([{ text: 'warn', color: '#cdcd00' }, { text: ' ' }, { text: 'cyan', color: '#00cdcd' }])
  // the ctrl-c line and the blank lines after the view are dropped; nothing else is
  expect(liveLines(ONCE).map(stripAnsi)).toEqual([
    'keeptabs · live        12:43:54', '', '  data: ledger (every API call)',
    '    total                  960,195     $2.0228   11 msgs', '    session   ██····     8%   $2.0228 of $25.00',
  ])
  // the budget fields: dotted keys, "off" for a limit switched off, grouped tokens
  const cfg = JSON.parse(BUDGET)
  expect(at(cfg, 'job.ask_over_usd')).toBe(1)
  expect(at(cfg, 'job.missing')).toBe(undefined)
  expect(shown(at(cfg, 'turn_limit_tokens'))).toBe('off')
  expect(shown(at(cfg, 'daily_limit_tokens'))).toBe('40,000,000')
  expect(shown(at(cfg, 'turn_limit_usd'))).toBe('2')
})

test('an interactive session opens the pane and draws the live view', async ($, on) => {
  mock.env(on, { HOME: '/home/t' })
  const clock = mock.clock(on, { now: T0 })
  const { ui } = world(on, FILES)
  await $.session.start({ cwd: '/w', surface: 'terminal', isInteractive: true })
  await clock.settle()                                        // the redraw session.start left running
  expect(ui.opens).toBe(1)
  expect(ui.commands).toEqual([])                             // nothing registered: the skill is the command
  // placed: the live view was fetched once, with the session pinned, and the status line cleared
  expect(ui.runs.map(a => a.slice(2))).toEqual([['--once', '--session', SID]])
  expect(ui.status).toEqual([undefined])

  // the terminal: one Text per line (wrap end), the blank line a space so it keeps its row
  const tree = await $.ui.render({ surface: 'terminal', component: 'Pane', requestId: 'keeptabs', props: PANE_PROPS })
  const drawn = texts(tree)
  const lines = drawn.filter(t => t.props.wrap === 'end')
  expect(lines.length).toBe(5)
  expect(drawn.find(t => t.text === 'keeptabs')?.props).toEqual({ bold: true })
  expect(drawn.find(t => t.text === '· live')?.props).toEqual({ dimColor: true })
  expect(drawn.find(t => t.text === '██')?.props).toEqual({ color: '#00cd00' })
  expect(drawn.filter(t => t.text === ' ').length).toBe(2)     // the space after 'keeptabs', and the blank line
  expect(drawn.some(t => t.text.includes('ctrl-c'))).toBe(false)
  // the tab row: live is the primary one, then flow, options and the browser button
  expect(elements(tree, 'Button').map(b => [b.key, b.variant])).toEqual([
    ['tab-live', 'primary'], ['tab-flow', 'secondary'], ['tab-options', 'secondary'], ['open-browser', undefined],
  ])

  // a remote surface: one Svg, monospace text with the spans as fills and weights
  for (const surface of ['desktop', 'vscode', 'mobile'] as const) {
    const remote = await $.ui.render({ surface, component: 'Pane', requestId: 'keeptabs', props: PANE_PROPS })
    const svg = sources(remote, 'Svg')
    expect(svg.length).toBe(1)
    expect(svg[0]).toBe(svgOf(liveLines(ONCE)))
    expect(svg[0]!.startsWith('<svg xmlns="http://www.w3.org/2000/svg" ')).toBe(true)
    expect(svg[0]).toContain('<tspan font-weight="700">keeptabs</tspan>')
    expect(svg[0]).toContain('<tspan class="d">· live</tspan>')
    expect(svg[0]).toContain('<tspan class="g">██</tspan><tspan class="g" opacity="0.6">····</tspan>')
    expect(svg[0]).toContain('@media(prefers-color-scheme:light)')   // follows the app's theme; no ground of its own
    expect(svg[0]).not.toContain('<rect')
    expect((svg[0]!.match(/<text /g) ?? []).length).toBe(5)       // one <text> per line, the blank one too
    expect(svg[0]).not.toContain('ctrl-c')
    expect(texts(remote).some(t => t.props.wrap === 'end')).toBe(false)
  }
  // markup in the figures is escaped; a line past 100 columns is cut with an ellipsis
  expect(svgOf(['a<b&c'])).toContain('<tspan>a&lt;b&amp;c</tspan>')
  expect(svgOf(['x'.repeat(120)])).toContain('<tspan>' + 'x'.repeat(99) + '…</tspan>')

  // a ledger write redraws: one more helper run, one invalidate
  const runs = ui.runs.length, invalidates = ui.invalidates
  await $.telemetry.log(request({}))
  await clock.settle()
  expect(ui.runs.length).toBe(runs + 1)
  expect(ui.invalidates).toBe(invalidates + 1)
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

test('/keeptabs-mod:status (the skill) is answered by the hook: the pane opens, the figures go to the person and to Claude', async ($, on) => {
  mock.env(on, { HOME: '/home/t' })
  mock.clock(on, { now: T0 })
  const { ui } = world(on, FILES)
  await $.session.start({ cwd: '/w', surface: 'terminal', isInteractive: true })
  const opens = ui.opens
  const r = await $.command.run({ command: 'keeptabs-mod:status', args: '', origin: { kind: 'composer' }, presentation: { isFullscreen: true, columns: 200 } })
  expect(ui.opens).toBe(opens + 1)
  expect(r.text).toBe(`${STATUS}\nThe keeptabs pane is open beside the chat; "open in browser" there writes the status page.`)
  expect(r.context).toEqual([`keeptabs, this session's figures as of now:\n${liveLines(ONCE).map(stripAnsi).join('\n')}`])
})

test('the flow tab draws the compact Sankey on a remote surface; the terminal points at the browser', async ($, on) => {
  mock.env(on, { HOME: '/home/t' })
  const clock = mock.clock(on, { now: T0 })
  const { ui } = world(on, FILES)
  await $.session.start({ cwd: '/w', surface: 'desktop', isInteractive: true })
  await clock.settle()
  await $.ui.render({ surface: 'desktop', component: 'Pane', requestId: 'keeptabs', props: PANE_PROPS })
  const runs = ui.runs.length
  await $.ui.press(press('tab-flow', 'desktop'))
  // one helper run for the drawing, the session pinned, no page push (none is open)
  expect(ui.runs.slice(runs).map(a => [a[1]!.split('/').pop(), ...a.slice(2)])).toEqual([['sankey.py', '--session', SID, '--flow-svg']])
  const desktop = await $.ui.render({ surface: 'desktop', component: 'Pane', requestId: 'keeptabs', props: PANE_PROPS })
  expect(sources(desktop, 'Svg')).toEqual([FLOW])
  expect(elements(desktop, 'Button').map(b => [b.key, b.variant])).toEqual([
    ['tab-live', 'secondary'], ['tab-flow', 'primary'], ['tab-options', 'secondary'], ['open-browser', undefined],
  ])
  const terminal = await $.ui.render({ surface: 'terminal', component: 'Pane', requestId: 'keeptabs', props: PANE_PROPS })
  expect(sources(terminal, 'Svg')).toEqual([])
  expect(texts(terminal).some(t => t.text.includes('open in browser'))).toBe(true)
  // a ledger write redraws the tab that is up: the flow again, not the live view
  const before = ui.runs.length
  await $.telemetry.log(request({}))
  await clock.settle()
  expect(ui.runs.slice(before).map(a => a.slice(2))).toEqual([['--session', SID, '--flow-svg']])
  // back to live: the live view is fetched again
  await $.ui.press(press('tab-live', 'desktop'))
  expect(ui.runs[ui.runs.length - 1]!.slice(2)).toEqual(['--once', '--session', SID])
})

test('the open-in-browser button writes the page for this session, opens it, and keeps it live', async ($, on) => {
  mock.env(on, { HOME: '/home/t' })
  const clock = mock.clock(on, { now: T0 })
  const { ui } = world(on, FILES)
  await $.session.start({ cwd: '/w', surface: 'terminal', isInteractive: true })
  await clock.settle()
  await $.ui.render({ surface: 'terminal', component: 'Pane', requestId: 'keeptabs', props: PANE_PROPS })
  const runs = ui.runs.length
  await $.ui.press(press('open-browser'))
  const page = `${DATA}/status/${SID}.html`
  const named = (a: string[]) => a[0] === 'python3' ? ['python3', a[1]!.split('/').pop()!, ...a.slice(2)] : a
  expect(ui.runs.slice(runs).map(named)).toEqual([
    ['python3', 'sankey.py', '--session', SID, '--out', page],
    ['open', page],
  ])
  expect(ui.toasts).toEqual([`keeptabs: status page opened (${page}); it stays live for 30 minutes`])
  expect(ui.store.get(`pageOpenedAt:${SID}`)).toBe(T0)

  // from now on a redraw writes the page's data file beside it too, in a second run
  const before = ui.runs.length
  await $.telemetry.log(request({}))
  await clock.settle()
  expect(ui.runs.slice(before).map(named)).toEqual([
    ['python3', 'keeptabs.py', '--once', '--session', SID],
    ['python3', 'sankey.py', '--session', SID, '--push', `${DATA}/status/${SID}.js`],
  ])
  // with the flow tab up, one run does both
  await $.ui.press(press('tab-flow'))
  expect(named(ui.runs[ui.runs.length - 1]!)).toEqual(['python3', 'sankey.py', '--session', SID, '--flow-svg', '--push', `${DATA}/status/${SID}.js`])
})

test('the options tab: auto-open through the config menu, the limits through the validator', async ($, on) => {
  mock.env(on, { HOME: '/home/t' })
  const clock = mock.clock(on, { now: T0 })
  const { ui } = world(on, FILES)
  await $.session.start({ cwd: '/w', surface: 'terminal', isInteractive: true })
  await clock.settle()
  await $.ui.render({ surface: 'terminal', component: 'Pane', requestId: 'keeptabs', props: PANE_PROPS })
  await $.ui.press(press('tab-options'))
  const tree = await $.ui.render({ surface: 'terminal', component: 'Pane', requestId: 'keeptabs', props: PANE_PROPS })
  const t = texts(tree)
  expect(t.some(x => x.text === 'open the pane at session start:')).toBe(true)
  expect(elements(tree, 'Button').find(b => b.key === 'auto-open')?.label).toBe('on')
  // the fields hold budget.json's values, "off" for null, grouped tokens
  const inputs = elements(tree, 'Input')
  expect(inputs.map(i => i.key)).toEqual(['b-session_limit_usd', 'b-session_limit_tokens', 'b-daily_limit_usd', 'b-daily_limit_tokens',
    'b-turn_limit_usd', 'b-turn_limit_tokens', 'b-warn_at_percent', 'b-job.ask_over_usd'])
  expect(inputs.find(i => i.key === 'b-daily_limit_usd')?.value).toBe('40')
  expect(inputs.find(i => i.key === 'b-turn_limit_tokens')?.value).toBe('off')
  expect(inputs.find(i => i.key === 'b-daily_limit_tokens')?.value).toBe('40,000,000')
  expect(elements(tree, 'Button').find(b => b.key === 'b-hard_stop')?.label).toBe('off')
  expect(elements(tree, 'Button').find(b => b.key === 'b-job.hard_stop')?.label).toBe('on')

  // the switch is the manifest's userConfig field, written as the menu would
  await $.ui.press(press('auto-open'))
  expect(ui.config).toEqual([['keeptabs-mod.autoOpen', false]])

  // a limit goes through budget.py; a refused value is shown under its field
  await $.ui.input({ plugin: 'keeptabs-mod', key: 'b-daily_limit_usd', text: '30' })
  expect(ui.runs[ui.runs.length - 1]!.slice(1).map((s, i) => i === 0 ? s.split('/').pop() : s)).toEqual(['budget.py', 'set', 'daily_limit_usd', '30'])
  await $.ui.input({ plugin: 'keeptabs-mod', key: 'b-turn_limit_usd', text: 'bad' })
  const after = await $.ui.render({ surface: 'terminal', component: 'Pane', requestId: 'keeptabs', props: PANE_PROPS })
  expect(texts(after).find(x => x.props.color === 'error')?.text.trim()).toBe("turn_limit_usd: a number is needed, not 'bad'")
  await $.ui.press(press('b-hard_stop'))
  expect(ui.runs[ui.runs.length - 1]!.slice(2)).toEqual(['set', 'hard_stop', 'on'])

  // mobile has no Input: the values are shown, with where to edit them
  const mobile = await $.ui.render({ surface: 'mobile', component: 'Pane', requestId: 'keeptabs', props: PANE_PROPS })
  expect(elements(mobile, 'Input')).toEqual([])
  expect(texts(mobile).some(x => x.text.includes('today, USD') && x.text.includes('40'))).toBe(true)
  expect(texts(mobile).some(x => x.text.includes('/keeptabs-mod:budget'))).toBe(true)
})
