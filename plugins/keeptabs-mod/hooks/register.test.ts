import { test, expect, mock } from 'claude-code/testing'
import type { On } from 'claude-code'

// The world beneath the plugin: `$.fs` reaches nothing under `claude plugin test`, and
// nothing answers `telemetry.log` or `session.id`, so the test stands in for them. Files
// are matched by their tail, since the plugin's root is the engine's to know.
function world(on: On, initial: Record<string, string>) {
  const fs = new Map(Object.entries(initial))
  const find = (path: string) => [...fs.keys()].find(k => k === path || path.endsWith(k))
  on('fs.read', ($, e) => { const k = find(e.path); return k ? { value: fs.get(k)! } : { deny: `ENOENT: ${e.path}` } })
  on('fs.write', ($, e) => { fs.set(e.path, e.text); return { value: undefined } })
  on('fs.exists', ($, e) => ({ value: find(e.path) !== undefined }))
  on('session.id', () => ({ value: SID }))
  on('session.start', ($, e) => ({ cwd: e.cwd }))
  on('telemetry.log', { to: 'collector' }, () => ({ value: undefined }))
  return fs
}

const SID = 'a0b1c2d3-session'
const PRICES = {
  _verified: true,
  models: { 'claude-fable-5-1': { input: 10, output: 50, cache_write_5m: 12.5, cache_write_1h: 20, cache_read: 0.25 } },
  default: { input: 4, output: 20, cache_write_5m: 5, cache_write_1h: 8, cache_read: 0.2 },
}
const DATA = '/home/t/.claude/plugins/data/keeptabs-mod-supertab'
const T0 = Date.UTC(2026, 9, 15, 14, 3, 11, 412)

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
  const fs = world(on, { '/keeptabs/budget.json': '{"session_limit_usd": 25}\n' })
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
  const fs = world(on, { '/keeptabs/prices.json': JSON.stringify(PRICES) })
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
  expect(fs.get(`${path}.tmp`)).toBe(fs.get(path))

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
})

test('records that are not api_request, and a failure in the module, leave the record alone', async ($, on) => {
  mock.env(on, { HOME: '/home/t' })
  mock.clock(on, { now: T0 })
  const fs = world(on, {})   // no prices.json: the module's own read fails
  await $.telemetry.log({ to: 'collector', event: 'tool_result', attributes: { tool_name: 'Bash' }, loggedAt: '2026-10-15T14:03:11.412Z' })
  await expect($.telemetry.log(request({}))).resolves.toEqual({ value: undefined })
  expect([...fs.keys()].filter(k => k.includes('/ledger/')).length).toBe(0)
})
