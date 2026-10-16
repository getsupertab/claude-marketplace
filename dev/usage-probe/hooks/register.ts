import type { Register } from 'claude-code'

// Where the probe appends one JSON line per event. Compared afterwards with
// keeptabs' OpenTelemetry ledger for the same session.
const LOG = '/private/tmp/claude-501/-Users-jannes-Projects-supertab-experimental-keeptabs/d03c0057-3149-497b-bee8-0c45bd537f1f/scratchpad/usage-probe.jsonl'

// $.fs.write replaces the file, so appends are serialised through one chain.
let queue: Promise<void> = Promise.resolve()
function log($: any, row: Record<string, unknown>): Promise<void> {
  queue = queue.then(async () => {
    let prev = ''
    try { prev = await $.fs.read(LOG) } catch { /* first line */ }
    const t = new Date(await $.clock.now()).toISOString()
    await $.fs.write(LOG, prev + JSON.stringify({ t, ...row }) + '\n')
  }).catch(() => {})
  return queue
}

export const register: Register = on => {
  on('session.start', async ($, e, next) => {
    const r = await next(e)
    await log($, { ev: 'session.start', session: await $.session.id() })
    return r
  })

  // Every model request inside a turn, main loop and subagents alike.
  on('turn.step', async function* ($, e, next) {
    const r: any = yield* next(e)
    await log($, {
      ev: 'turn.step', turnId: e.turnId, index: e.index, model: e.model, agentId: e.agentId,
      messageCount: e.messageCount, stopReason: r?.stopReason, usage: r?.usage,
      tools: (r?.toolUses ?? []).map((t: any) => t.name ?? t.tool ?? t),
      serverTools: (r?.serverToolUses ?? []).map((t: any) => t.name ?? t.type ?? t),
    })
    return r
  })

  on('turn.complete', async ($, e: any, next) => {
    const r: any = await next(e)
    let s: any = null
    try { s = await $.session.usage() } catch (err) { s = { error: String(err) } }
    await log($, {
      ev: 'turn.complete', reason: e.reason, usage: e.usage ?? r?.usage,
      sessionCost: s?.cost, context: s?.context?.tokens ?? s?.context, rateLimits: s?.rateLimits, error: s?.error,
    })
    return r
  })

  on('session.compact', async ($, e: any, next) => {
    const r: any = await next(e)
    await log($, { ev: 'session.compact', trigger: e.trigger, agentId: e.agentId, usage: r?.usage, before: r?.tokensBefore, after: r?.tokensAfter })
    return r
  })

  // Helper completions made through the engine interface (by plugins or built-ins).
  on('model.complete', async ($, e: any, next) => {
    const r: any = await next(e)
    await log($, { ev: 'model.complete', model: e.model, promptChars: e.prompt?.length, isAnswered: r?.isAnswered, usage: r?.usage })
    return r
  })

  // The records the engine exports to an OpenTelemetry collector, seen in-process.
  on('telemetry.log', { to: 'collector' }, async ($, e: any, next) => {
    const r = await next(e)
    if (e.event === 'api_request') {
      const a = e.attributes ?? {}
      await log($, {
        ev: 'otel.api_request', model: a.model, input: a.input_tokens, output: a.output_tokens,
        cache_read: a.cache_read_tokens, cache_write: a.cache_creation_tokens,
        query_source: a.query_source, request_id: a.request_id, cost: a.cost_usd,
      })
    }
    return r
  })
}
