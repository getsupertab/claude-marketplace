"""Regression fixture for TokenTab cost accounting.

Builds a synthetic transcript with (1) two models, (2) one assistant message that calls
several tools, (3) a compaction/summary turn, feeds it to tokentab.Store in two scans
(before / after the compaction, which also exercises incremental tailing), and asserts:
  sum(cost_by) == cost, per-model cost sums, per-tool fields <= the rate they draw from,
  every Sankey column (plus pass-through ribbons) sums to the session total, and node
  inflow == outflow. Step-specific checks run when the new fields exist.

No dependencies, no pytest; run from anywhere, exits non-zero on a failure:

  python3 plugins/tokentab/tests/test_accounting.py
"""
import html, itertools, json, os, re, sys, tempfile
from xml.dom import minidom

PLUGIN = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(PLUGIN, "tokentab"))
import tokentab as kt
import sankey

OPUS, SONNET = "claude-opus-5-5", "claude-sonnet-5"
T = [0]


def ts():
    T[0] += 1
    return f"2026-10-13T10:{T[0] // 60:02d}:{T[0] % 60:02d}.000Z"


def usage(i=0, r=0, w1=0, w5=0, o=0, th=None):
    u = {"input_tokens": i, "cache_read_input_tokens": r, "cache_creation_input_tokens": w1 + w5,
         "cache_creation": {"ephemeral_1h_input_tokens": w1, "ephemeral_5m_input_tokens": w5},
         "output_tokens": o}
    if th is not None:
        u["output_tokens_details"] = {"thinking_tokens": th}
    return u


def asst(req, model, u, block, **kw):
    return dict(type="assistant", sessionId="fx", requestId=req, timestamp=ts(),
                message={"model": model, "usage": u, "content": [block]}, **kw)


def user(content, **kw):
    return dict(type="user", sessionId="fx", timestamp=ts(), message={"role": "user", "content": content}, **kw)


def hook(text):
    return dict(type="attachment", sessionId="fx", timestamp=ts(),
                attachment={"type": "hook_additional_context", "content": [text], "hookEvent": "UserPromptSubmit"})


def tu(i, name, inp):
    return {"type": "tool_use", "id": i, "name": name, "input": inp}


def tr(i, text):
    return {"type": "tool_result", "tool_use_id": i, "content": text}


uA = usage(i=10, w1=20000, o=1000, th=400)
part1 = [
    user("fix the bug"),
    hook("JOB CHECK (from TokenTab, not the user): " + "x" * 300),
    # (2) one message, several content lines, several tools; (1) model one
    asst("rA", OPUS, uA, {"type": "thinking", "thinking": "t" * 800}),
    asst("rA", OPUS, uA, {"type": "text", "text": "I'll look."}),
    asst("rA", OPUS, uA, tu("t1", "Bash", {"command": "ls -la"})),
    asst("rA", OPUS, uA, tu("t2", "Read", {"file_path": "/a/" + "p" * 200})),
    user([tr("t1", "a" * 4000), tr("t2", "b" * 1000)]),
    hook("BUDGET HINT (from TokenTab): " + "y" * 300),
    # (1) model two, mixed 1h/5m write
    asst("rB", SONNET, usage(i=5, r=20010, w1=1500, w5=500, o=300), tu("t3", "Grep", {"pattern": "foo"})),
    user([tr("t3", "c" * 2000)]),
    asst("rC", OPUS, usage(i=2, r=22000, w1=800, o=200), {"type": "text", "text": "Found it." * 20}),
    user("next"),
    asst("rD", OPUS, usage(i=2, r=22800, w1=100, o=150), tu("t4", "Bash", {"command": "make test"})),
    # streaming: a later line of the same request reports more output
    asst("rD", OPUS, usage(i=2, r=22800, w1=100, o=180), {"type": "text", "text": "running"}),
    user([tr("t4", "d" * 3000)]),
    asst("rE", OPUS, usage(i=2, r=22900, w1=900, o=100), {"type": "text", "text": "Tests pass."}),
]
part2 = [
    # (3) compaction: boundary marker + summary turn; context drops sharply
    dict(type="system", subtype="compact_boundary", sessionId="fx", timestamp=ts(), content="Conversation compacted"),
    user("This session is being continued from a previous conversation. " + "s" * 2000, isCompactSummary=True),
    asst("rF", OPUS, usage(i=3, r=4000, w1=3000, o=50), {"type": "text", "text": "Continuing."}),
    user("go on"),
    asst("rG", OPUS, usage(i=3, r=7000, w1=40, o=50), {"type": "text", "text": "Done."}),
]


def scan(root, lines):
    with open(os.path.join(root, "proj", "fx.jsonl"), "a", encoding="utf-8") as f:
        for o in lines:
            f.write(json.dumps(o, separators=(",", ":")) + "\n")


def tool_fields(b):
    """name -> model -> fields, whatever shape the bucket has (old: name -> fields)."""
    out = {}
    for name, e in b.tools.items():
        out[name] = e if all(isinstance(v, dict) for v in e.values()) else {"?": e}
    return out


DRAWS = {"ask": "output", "think": "thinking", "result_1h": "cache_write_1h",
         "result_5m": "cache_write_5m", "read": "cache_read"}


def check(b, label, tb=None):
    errs = []
    near = lambda a, c: abs(a - c) < 1e-9
    if not near(sum(b.cost_by.values()), b.cost):
        errs.append(f"sum(cost_by)={sum(b.cost_by.values())} != cost={b.cost}")
    bm = getattr(b, "cost_bm", None)
    if bm is not None:
        for k in b.cost_by:
            if not near(sum(m.get(k, 0) for m in bm.values()), b.cost_by[k]):
                errs.append(f"cost_bm[*][{k}] does not sum to cost_by[{k}]")
        # per (model, rate): what tools (+ overhead) draw <= what the rate has
        draw = {}
        entries = [(n, m, f) for n, ms in tool_fields(b).items() for m, f in ms.items()]
        entries += [("overhead", m, f) for m, f in (getattr(b, "overhead", None) or {}).items()]
        for n, m, f in entries:
            for fld, r in DRAWS.items():
                draw[(m, r)] = draw.get((m, r), 0) + f.get(fld, 0)
        for (m, r), v in draw.items():
            if v > bm.get(m, {}).get(r, 0) + 1e-9:
                errs.append(f"tools draw {v:.6f} from {m}/{r} > {bm.get(m, {}).get(r, 0):.6f}")
    # Sankey: every column plus the ribbons passing through it sums to the total
    cols, links, _, _ = sankey.graph(b, tb)
    total = sum(cols[0].values())
    if not near(total, b.cost):
        errs.append(f"sankey total {total} != cost {b.cost}")
    ci = {n: i for i, c in enumerate(cols) for n in c}
    for i, c in enumerate(cols):
        through = sum(v for s, d, v in links if ci[s] < i < ci[d])
        if abs(sum(c.values()) + through - total) > 1e-9:
            errs.append(f"sankey column {i} sums to {sum(c.values()) + through:.6f}, not {total:.6f}")
    for n, v in ((n, v) for c in cols for n, v in c.items()):
        i_ = sum(x for s, d, x in links if d == n); o_ = sum(x for s, d, x in links if s == n)
        if i_ and abs(i_ - v) > 1e-9: errs.append(f"node {n}: in {i_} != {v}")
        if o_ and abs(o_ - v) > 1e-9: errs.append(f"node {n}: out {o_} != {v}")
    if any(v < -1e-12 for _, _, v in links):
        errs.append("negative link")
    # the SVG parses
    minidom.parseString(sankey.svg(b, tb))
    print(f"[{label}] cost ${b.cost:.6f}  " + ("OK" if not errs else "FAIL"))
    for e in errs:
        print("   ", e)
    return errs


def verified_case(prices):
    """A model missing from the table is priced at the default rates and reported unverified."""
    b = kt.Bucket()
    errs = []
    if not kt.prices_verified(prices, b):
        errs.append("an empty bucket reads as unverified")
    b.cost_bm = {"claude-opus-5-5": {"input": 0.1}, "<synthetic>": {}}
    if not kt.prices_verified(prices, b):
        errs.append("listed models read as unverified")
    b.cost_bm["claude-next-6"] = {"input": 0.1}
    if kt.prices_verified(prices, b):
        errs.append("a default-priced model reads as verified")
    # Claude Code's figure below our 5m price: the table is too high for that model
    line = {"model": "claude-opus-5-5", "tokens": {"input": 1000, "output": 1000, "cache_write": 1000},
            "cost_usd_cc": 0.02}            # ours at 5m: (4 + 20 + 5) / 1000 = 0.029
    if abs(kt.price_drift(line, prices) - 0.009) > 1e-9:
        errs.append(f"drift {kt.price_drift(line, prices)} != 0.009")
    for cc in (0.029, 0.029 - 0.0004, 0.05, 0, None):   # matches, rounding, a fee, no figure
        if kt.price_drift(dict(line, cost_usd_cc=cc), prices):
            errs.append(f"a figure of {cc} reads as drift")
    b = kt.Bucket()
    b.cost_bm = {"claude-opus-5-5": {"input": 0.1}}
    b.drift = {"claude-opus-5-5": [1, 0.009]}
    if kt.prices_verified(prices, b):
        errs.append("a bucket with drift reads as verified")
    print("[verified] " + ("OK" if not errs else "FAIL"))
    return errs


def main():
    root = tempfile.mkdtemp()
    os.makedirs(os.path.join(root, "proj"))
    kt.ROOT = root
    kt.CFG = os.path.join(root, "cfg")              # no ledger: transcript bucket only
    prices = kt.load_prices()
    store = kt.Store(prices)
    scan(root, part1); store.scan()
    b = store.sessions["fx"]
    errs = check(b, "before compaction")
    before = json.loads(json.dumps(tool_fields(b)))
    scan(root, part2); store.scan()
    errs += check(b, "after compaction")
    after = tool_fields(b)
    for n, ms in after.items():
        for m, f in ms.items():
            print(f"    {n:<6}{m:<18}" + "  ".join(f"{k}={v:.6f}" if isinstance(v, float) else f"{k}={v}"
                                                for k, v in f.items()))
    if hasattr(b, "overhead"):
        for m, f in b.overhead.items():
            print(f"    {'(kt)':<6}{m:<18}" + "  ".join(f"{k}={v:.6f}" if isinstance(v, float) else f"{k}={v}"
                                                 for k, v in f.items()))
    print("    cost_by", {k: round(v, 6) for k, v in b.cost_by.items()})
    # residents reset on compaction: nothing re-reads a pre-compaction tool afterwards
    for n, ms in after.items():
        for m, f in ms.items():
            if "read" in f and abs(f["read"] - before[n][m]["read"]) > 1e-12:
                errs.append(f"{n}/{m} kept collecting read after compaction")
    # the snapshot is JSON and its cost_by sums to cost_usd
    snap = json.loads(json.dumps(kt.snapshot(store, "fx", prices)))
    if abs(sum(snap.get("cost_by", {}).values()) - snap["cost_usd"]) > 1e-5:
        errs.append("snapshot cost_by does not sum to cost_usd")
    kt.render(store, "fx", prices, {}, True)
    errs += pane_check(store, prices)
    errs += breakdown_check(store, prices)
    errs += series_check(b, "transcript", store)
    errs += ledger_case(root, store, prices)
    errs += ledger_5m_case(prices)
    errs += utf8_case(root, prices)
    errs += locked_page_case(root)
    errs += names_case(prices)
    errs += tokens_case()
    errs += page_case(store, prices)
    errs += requests_case(store, prices)
    errs += saved_state_case(prices)
    errs += verified_case(prices)
    for err in errs:
        print("  " + err)
    print("ALL OK" if not errs else f"{len(errs)} FAILURES")
    return errs


def pane_check(store, prices):
    """`--report`, what the pane draws: JSON, the session against its limit, the rates
    summing to the total, tools with the three draws; this month counts only this month."""
    errs = []
    budget = {"budgets_on": True, "session_limit_usd": 0.0001, "warn_at_percent": 70}
    rep = json.loads(json.dumps(kt.pane_report(store, "fx", prices, budget, windows={})))
    s = rep["session"]
    if not s or s["unit"] != "usd" or not s["limit"].endswith("of $0.00") or not s["frac"] or s["frac"] < 1:
        errs.append(f"pane report: session against its limit is wrong: {s}")
    # A plan with usage windows counts the 5-hour window in percent
    windows = {"five_hour": {"kind": "five_hour", "percentUsed": 40, "resetsAt": "2999-01-01T00:00:00Z"},
               "seven_day": {"kind": "seven_day", "percentUsed": 12.5, "resetsAt": "2000-01-01T00:00:00Z"}}
    plan = {**budget, "five_hour_limit_percent": 80, "weekly_limit_percent": 50}
    s = kt.pane_report(store, "fx", prices, plan, windows=windows)["session"]
    if s["unit"] != "percent" or abs(s["frac"] - 0.5) > 1e-9 or s["limit"] != "5-hour window 40% used, limit 80%":
        errs.append(f"pane report: the 5-hour window against its limit is wrong: {s}")
    if not s["plan"] or [w["scope"] for w in s["windows"]] != ["five_hour", "seven_day"] or s["windows"][1]["used"] != 0:
        errs.append(f"pane report: both windows should be listed, the weekly one reset to 0%: {s}")
    if kt.pane_report(store, "fx", prices, budget, windows={})["session"]["plan"]:
        errs.append("pane report: no plan windows means no subscription label")
    if kt.reset_text("2000-01-01T00:00:00Z") is not None or not kt.reset_text("2999-01-01T00:00:00Z"):
        errs.append("reset_text: a past reset should be None, a future one a time")
    if kt.pane_report(store, "fx", prices, {**plan, "limit_unit": "usd"}, windows=windows)["session"]["unit"] != "percent":
        errs.append("pane report: an old file's limit_unit should not override the account")
    # An account with no usage windows (Enterprise, API key) checks the USD limits, whatever an old file says
    unit, rows = kt.limit_rows({**plan, "limit_unit": "percent", "daily_limit_usd": 50},
                               {"session": 9, "today": 120}, {})
    if unit != "usd" or [(r["scope"], r["frac"] >= 1) for r in rows] != [("session", True), ("today", True)]:
        errs.append(f"limit_rows: no usage windows should check the USD limits: {unit} {rows}")
    unit, rows = kt.limit_rows(plan, {"session": 0, "today": 0}, windows)
    if [(r["scope"], r["frac"]) for r in rows] != [("five_hour", 0.5), ("seven_day", 0.0)]:
        errs.append(f"limit_rows: a window past its reset should count 0%: {rows}")
    if abs(sum(r["cost"] for r in rep["rates"]) - rep["total"]["cost"]) > 1e-6:
        errs.append("pane report: rates do not sum to the total")
    if len(rep["models"]) < 2 or abs(sum(m["cost"] for m in rep["models"]) - rep["total"]["cost"]) > 1e-6:
        errs.append("pane report: the models do not sum to the total")
    if not rep["tools"] or any(abs(t["ask"] + t["ctx"] + t["read"] - t["cost"]) > 1e-6 for t in rep["tools"]):
        errs.append("pane report: a tool's ask + ctx + re-read is not its cost")
    if abs(sum(x["cost"] for x in rep["sessions"]) - rep["month"]) > 1e-6:
        errs.append("pane report: this month's sessions do not sum to the month")
    if rep["sessions"] and abs(sum(x["share"] for x in rep["sessions"]) - 1) > 1e-6:
        errs.append("pane report: the sessions' shares do not sum to 100%")
    if kt.pane_report(store, "fx", prices, {}, windows={})["session"]["frac"] is not None:
        errs.append("pane report: no limit should give no fraction")
    if kt.pane_report(store, "fx", prices, {**budget, "budgets_on": False}, windows={})["session"]["frac"] is not None:
        errs.append("pane report: limits switched off should give no fraction")
    now = kt.datetime.now().timestamp()
    if abs(kt.month_total([(now, 1.5, 0), (now - 400 * 86400, 9.0, 0)]) - 1.5) > 1e-9:
        errs.append("month_total counts a day outside this month")
    return errs


def breakdown_check(store, prices):
    """A tool's layers: Bash by program and subcommand, file tools by file; in the pane
    report every layer's draws sum to its parent's."""
    errs = []
    cases = [("cd repo && FOO=1 git status -s", ("git", "status")), ("sudo brew install jq", ("brew", "install")),
             ("npx -y tsc", ("npx",)), ("python3 x.py | head", ("python3",)), ("export A=1; ls", ("ls",)), ("", ())]
    for cmd, want in cases:
        if kt.shell_layers(cmd) != want:
            errs.append(f"shell_layers({cmd!r}) = {kt.shell_layers(cmd)}, want {want}")
    for name, inp, want in [("Read", {"file_path": "/a/b/c.py"}, ("c.py",)), ("Agent", {"subagent_type": "Explore"}, ("Explore",)),
                            ("WebFetch", {"url": "https://example.com/x"}, ("example.com",)), ("Grep", {"pattern": "x"}, ())]:
        if kt.tool_layers(name, inp) != want:
            errs.append(f"tool_layers({name}) = {kt.tool_layers(name, inp)}, want {want}")
    rep = kt.pane_report(store, "fx", prices, {}, windows={})
    bash = next((t for t in rep["tools"] if t["name"] == "Bash"), None)
    if not bash or sorted(k["name"] for k in bash["children"]) != ["ls", "make"]:
        errs.append(f"pane report: Bash should break down into ls and make: {bash and bash['children']}")
    def sums(t):
        kids = t.get("children") or []
        if kids and any(abs(sum(k[f] for k in kids) - t[f]) > 1e-9 for f in ("calls", "cost", "ask", "ctx", "read")):
            errs.append(f"pane report: {t['name']}'s layers do not sum to it")
        for k in kids:
            sums(k)
    for t in rep["tools"]:
        sums(t)
    if any(t["children"] for t in rep["tools"] if t["name"] == "Grep"):
        errs.append("pane report: Grep has no layers")
    return errs


def ledger_case(root, store, prices):
    """Telemetry has no thinking count: LedgerStore.reconcile takes it from the transcript
    by request id. A ledger line can come before its transcript line, a background call
    has none, and reconciling twice must change nothing."""
    errs = []
    os.makedirs(os.path.join(kt.CFG, "ledger", "2026-10-13"), exist_ok=True)
    def led_line(rid, model, u, src="sdk"):
        return {"ts": "2026-10-13T10:00:00+00:00", "session_id": "fx", "model": model, "request_id": rid,
                "query_source": src, "tokens": {"input": u["input_tokens"], "output": u["output_tokens"],
                "cache_read": u["cache_read_input_tokens"], "cache_write": u["cache_creation_input_tokens"]}}
    # a line written by a collector with a stale price table: cost_usd and fees_usd are
    # wrong, cost_usd_cc (Claude Code's own figure) is right and includes a real $0.01
    # web search fee. On read, the fee must be worked out against today's prices.
    uS = usage(i=5, r=100, o=0)
    tok = (uS["input_tokens"] * kt.rate(prices, OPUS, "input") + uS["cache_read_input_tokens"] * kt.rate(prices, OPUS, "cache_read")
           + uS["cache_creation_input_tokens"] * kt.rate(prices, OPUS, "cache_write_1h")) / 1e6
    stale = dict(led_line(None, OPUS, uS, "web_search"), cost_usd=tok * 0.6, fees_usd=tok * 0.4 + 0.01, cost_usd_cc=tok + 0.01)
    del stale["request_id"]
    with open(os.path.join(kt.CFG, "ledger", "2026-10-13", "fx.jsonl"), "w", encoding="utf-8") as f:
        for e in (led_line("rA", OPUS, uA), led_line("rX", OPUS, usage(i=5, r=100, o=80), "prompt_suggestion"), stale):
            f.write(json.dumps(e) + "\n")
    led = kt.LedgerStore(prices); led.scan()
    lb = led.sessions["fx"]
    if abs(lb.cost_by.get("fees", 0) - 0.01) > 1e-9:
        errs.append(f"ledger: fees {lb.cost_by.get('fees', 0):.6f} from a stale-priced line, want 0.010000")
    cost0, out0 = lb.cost, lb.cost_by["output"]
    led.reconcile(kt.Store(prices))              # transcript not read yet: nothing moves
    if lb.cost_by.get("thinking", 0) or lb.think:
        errs.append("ledger: thinking filed without a transcript line")
    for _ in range(2):                           # then twice with the transcript
        led.reconcile(store)
    want = 400 * kt.rate(prices, OPUS, "output") / 1e6
    if abs(lb.cost_by.get("thinking", 0) - want) > 1e-12 or lb.think != 400:
        errs.append(f"ledger: thinking {lb.cost_by.get('thinking', 0):.6f} / {lb.think} tok, want {want:.6f} / 400")
    if abs(lb.cost - cost0) > 1e-12 or abs(lb.cost_by["output"] + lb.cost_by["thinking"] - out0) > 1e-12:
        errs.append("ledger: reconcile changed the total")
    if led.unmatched("fx", store) != 80:
        errs.append(f"ledger: unmatched output {led.unmatched('fx', store)}, want 80")
    errs += check(lb, "ledger + transcript tools", tb=store.sessions["fx"])
    errs += series_check(lb, "ledger", store)
    return errs


def ledger_5m_case(prices):
    """Telemetry does not say whether a cache write was 5m or 1h. A line whose Claude Code
    figure is below the 1h price was a 5m write (helper agents, SDK calls, compaction):
    filed at that rate, anything above the 5m price is a fee, and the line totals
    cost_usd_cc. A figure at or above the 1h price, or none, keeps the 1h rate."""
    errs = []
    toks = {"input": 3, "output": 200, "cache_read": 40000, "cache_write": 10000}
    tok = lambda key: (3 * kt.rate(prices, OPUS, "input") + 200 * kt.rate(prices, OPUS, "output")
                       + 40000 * kt.rate(prices, OPUS, "cache_read") + 10000 * kt.rate(prices, OPUS, key)) / 1e6
    p5, p1 = tok("cache_write_5m"), tok("cache_write_1h")
    lines = [(p5, "5m"), (p5 + 0.01, "5m"), (p1, "1h"), (p1 + 0.01, "1h"), (None, "1h"), (p5 - 0.01, "1h")]
    os.makedirs(os.path.join(kt.CFG, "ledger", "2026-10-14"), exist_ok=True)
    with open(os.path.join(kt.CFG, "ledger", "2026-10-14", "f5.jsonl"), "w", encoding="utf-8") as f:
        for n, (cc, _) in enumerate(lines):
            e = {"ts": "2026-10-14T10:00:00+00:00", "session_id": f"f5-{n}", "model": OPUS, "request_id": f"r5-{n}",
                 "query_source": "agent:builtin:Explore", "tokens": toks, "cost_usd": round(p1, 6), "cost_usd_cc": cc}
            f.write(json.dumps(e) + "\n")
            if kt.write_ttl(e, prices) != lines[n][1]:
                errs.append(f"ledger 5m: write_ttl {kt.write_ttl(e, prices)} for cost_usd_cc {cc}, want {lines[n][1]}")
    led = kt.LedgerStore(prices); led.scan()
    for n, (cc, ttl) in enumerate(lines):
        b = led.sessions[f"f5-{n}"]
        if (b.cw5, b.cw1h) != ((10000, 0) if ttl == "5m" else (0, 10000)):
            errs.append(f"ledger 5m: line {n} filed {b.cw5} at 5m and {b.cw1h} at 1h, want all at {ttl}")
        want = max(cc, p1 if ttl == "1h" else p5) if cc is not None else p1
        if abs(b.cost - want) > 1e-9:
            errs.append(f"ledger 5m: line {n} totals {b.cost:.6f}, want {want:.6f}")
    if abs(led.sessions["f5-1"].cost_by.get("fees", 0) - 0.01) > 1e-9:
        errs.append("ledger 5m: a 5m write with a search on top should leave the search as the fee")
    # the last figure is below even the 5m price: the table is too high for the model
    drift = {n: led.sessions[f"f5-{n}"].drift for n in range(len(lines))}
    if any(drift[n] for n in range(5)) or [c for c, _ in [drift[5].get(OPUS, (0, 0))]] != [1]:
        errs.append(f"ledger 5m: drift {drift}, want one call on line 5 only")
    if kt.report(kt.Store(prices), "f5-5", prices, {}, False, led)["prices_verified"] or not kt.report(kt.Store(prices), "f5-0", prices, {}, False, led)["prices_verified"]:
        errs.append("ledger 5m: the report's prices_verified does not follow the drift")
    return errs


def utf8_case(root, prices):
    """Non-ASCII in a transcript and on the status page is read and written as UTF-8,
    whatever the locale (Windows would use its ANSI code page)."""
    errs = []
    name = "mcp__café__日本語"
    lines = [dict(user("naïve — ünïcödé ✓"), sessionId="u8"),
             dict(asst("rU", OPUS, usage(i=10, o=20), tu("tu8", name, {"q": "Grüße 日本"})), sessionId="u8"),
             dict(user([tr("tu8", "résultat → 東京 " * 50)]), sessionId="u8")]
    with open(os.path.join(root, "proj", "u8.jsonl"), "w", encoding="utf-8") as f:
        for o in lines:
            f.write(json.dumps(o, ensure_ascii=False) + "\n")
    store = kt.Store(prices); store.scan()
    if name not in store.sessions.get("u8", kt.Bucket()).tools:
        errs.append(f"utf-8: tool {name} not read back from the transcript")
    page = os.path.join(root, "out", "status.html")
    text = sankey.page("<p>café · 日本 → ✓</p>")
    sankey.write(page, text)
    with open(page, "rb") as f:
        if f.read().decode("utf-8") != text:
            errs.append("utf-8: the status page did not round-trip")
    print("[utf-8] " + ("OK" if not errs else "FAIL"))
    return errs


def locked_page_case(root):
    """A target Windows keeps locked: the writer retries, then drops this redraw without
    raising or leaving its temporary file; the next redraw writes the page."""
    errs = []
    page = os.path.join(root, "locked", "status.html")
    sankey.write(page, "old")
    tries, waits = [0], []
    def locked(src, dst):
        tries[0] += 1
        raise PermissionError(13, "The process cannot access the file", dst)
    replace, sleep = sankey.os.replace, sankey.time.sleep
    sankey.os.replace, sankey.time.sleep = locked, waits.append
    try:
        sankey.write(page, "new")
    except Exception as e:
        errs.append(f"locked page: write raised {e!r}")
    finally:
        sankey.os.replace, sankey.time.sleep = replace, sleep
    if tries[0] != sankey.REPLACE_TRIES or len(waits) != sankey.REPLACE_TRIES - 1:
        errs.append(f"locked page: {tries[0]} tries, {len(waits)} waits")
    if os.path.exists(page + ".tmp"):
        errs.append("locked page: the temporary file was left behind")
    with open(page, encoding="utf-8") as f:
        if f.read() != "old":
            errs.append("locked page: the target changed")
    sankey.write(page, "new")
    with open(page, encoding="utf-8") as f:
        if f.read() != "new":
            errs.append("locked page: the next redraw did not write")
    print("[locked page] " + ("OK" if not errs else "FAIL"))
    return errs


def tokens_case():
    """hooks/tokens.ts reads as JSON for the page, every token is a [light, dark] pair of
    colours, and the page and its Sankey draw from them, not from colours of their own."""
    errs = []
    hexc = re.compile(r"^#[0-9a-fA-F]{6}$")
    t = sankey.load_tokens()
    for group in ("series", "surface"):
        for k, v in t[group].items():
            if not (isinstance(v, list) and len(v) == 2 and all(hexc.match(c) for c in v)):
                errs.append(f"tokens: {group}.{k} is not a [light, dark] pair of colours: {v}")
    page = sankey.page("<p>x</p>")
    for k, (light, dark) in t["surface"].items():
        if f"--{k.replace('_', '-')}:{light}" not in page or f"--{k.replace('_', '-')}:{dark}" not in page:
            errs.append(f"tokens: surface.{k} is missing from the page's CSS")
    allowed = {v[0] for v in t["series"].values()}
    stray = {c for c in sankey.COLOR.values() if c not in allowed and c != sankey.NEUTRAL}
    stray |= {c for c in sankey.TOOL_COLORS + sankey.MODEL_COLORS if c not in allowed}
    if stray:
        errs.append(f"tokens: the Sankey uses colours outside the tokens: {sorted(stray)}")
    print("[tokens] " + ("OK" if not errs else "FAIL"))
    return errs


def names_case(prices):
    """The sessions list names an untitled session by its project, keeps the short id
    beside it, and marks a session whose transcript is gone as deleted."""
    errs, saved = [], kt.ROOT
    kt.ROOT = tempfile.mkdtemp()
    pdir = os.path.join(kt.ROOT, "-Users-x-work-sportsort-engine")
    os.makedirs(pdir)
    def write(sid, *lines):
        with open(os.path.join(pdir, sid + ".jsonl"), "w", encoding="utf-8") as f:
            for o in lines:
                f.write(json.dumps({"sessionId": sid, **o}, separators=(",", ":")) + "\n")
    cwd = "/Users/x/work/sportsort-engine"
    titled, untitled, gone, unknown = ("a" * 8 + "-1", "b" * 8 + "-2", "c" * 8 + "-3", "d" * 8 + "-4")
    write(titled, {"type": "user", "cwd": cwd, "message": {"role": "user", "content": "hi"}},
          {"type": "custom-title", "customTitle": "PR-Audit"})
    write(untitled, {"type": "user", "cwd": cwd, "message": {"role": "user", "content": "hi"}})
    store = kt.Store(prices); store.scan()
    os.remove(os.path.join(pdir, titled + ".jsonl"))
    for sid in (titled, gone):
        with open(os.path.join(pdir, sid + ".desktop-released.json"), "w") as f:
            json.dump({"v": 1, "reason": "delete"}, f)
    store.scan()
    want = {titled: ("PR-Audit", "deleted"), untitled: ("sportsort-engine", "bbbbbbbb"),
            gone: ("sportsort-engine", "cccccccc · deleted"), unknown: ("dddddddd", "deleted")}
    for sid, (name, detail) in want.items():
        row = kt.session_row(store, sid, 1.0, 4.0)
        if (row["name"], row["detail"]) != (name, detail):
            errs.append(f"session_row({sid}) = {row['name']!r} / {row['detail']!r}, want {name!r} / {detail!r}")
    kt.ROOT = saved
    return errs


def series_check(b, label, store):
    """The cost-over-time chart: per-call entries sum to the rate totals (thinking moved
    by reconcile included), one entry per call, and the panel is well-formed SVG."""
    errs = []
    for k, v in b.cost_by.items():
        got = sum(e[1].get(k, 0.0) for e in b.series)
        if abs(got - v) > 1e-9:
            errs.append(f"{label}: series {k} sums to {got:.6f}, want {v:.6f}")
    if len(b.series) != b.msgs:
        errs.append(f"{label}: {len(b.series)} series entries for {b.msgs} calls")
    R = {"bucket": b, "prompts": store.prompts.get("fx", []),
         "rates": [{"key": k} for k, v in b.cost_by.items() if v]}
    out = sankey.timeline(R)
    try:
        minidom.parseString(f"<div>{out}</div>")
    except Exception as e:
        errs.append(f"{label}: chart is not well-formed: {e}")
    if out.count("<svg") != 2 or 'class="ln"' not in out:
        errs.append(f"{label}: chart is missing an axis or its lines")
    return errs


def page_case(store, prices):
    """The status page draws the pane's report: its first tile leads with the pane's figure
    (the plan window under percent, else this session's dollars), a session over its limit
    gets a banner, every tool and layer of the pane's tree is on the page with where it comes
    from, and the Sankey ends in the pane's three parts and balances with any set of columns
    hidden."""
    errs = []
    budget = {"budgets_on": True, "session_limit_usd": 0.0001, "warn_at_percent": 70,
              "five_hour_limit_percent": 80, "weekly_limit_percent": 50}
    b = store.sessions["fx"]
    R = kt.report(store, "fx", prices, budget, True)
    windows = {"five_hour": {"kind": "five_hour", "percentUsed": 40, "resetsAt": "2999-01-01T00:00:00Z"}}
    for win, want in (({}, sankey.usd(b.cost)), (windows, "40%")):
        R["pane"] = P = kt.pane_report(store, "fx", prices, budget, windows=win)
        p = sankey.parts(R)
        if f"<b>{want}</b>" not in p["hero"]:
            errs.append(f"page: hero does not lead with {want}: {p['hero'][:200]}")
        names, stack = [], list(P["tools"])
        while stack:
            t = stack.pop()
            names.append(t["name"]); stack += t["children"]
        missing = [n for n in names if f">{html.escape(n)}</span>" not in p["status"]]
        if missing:
            errs.append(f"page: tools of the pane's report missing from the page: {missing}")
        try:
            minidom.parseString(f"<div>{p['hero']}{p['chart']}{p['status']}</div>")
        except Exception as e:
            errs.append(f"page: sections are not well-formed: {e}")
        # in dollars the session is far over its limit (budget above): a banner says so and what
        # TokenTab does about it; under percent the window is at half its limit: no banner
        if ('class="alert error"' in p["hero"]) != (not win) or (not win and "TokenTab only warns" not in p["hero"]):
            errs.append(f"page: over-limit banner wrong with windows {win}")
    if '"plan usage"' in p["hero"] or "plan usage" not in p["hero"] or "at API rates" not in p["hero"]:
        errs.append("page: under percent the hero is not plan usage with dollars at API rates")
    tags = {n: sankey.source_tag(n) for n in ("Bash", "mcp__claude_ai_Figma__use_figma", "mcp__Claude_Browser__navigate",
                                                "mcp__40a9e9ea-a265-43ac-9423-5f01011bcd27__get_screenshot")}
    if tags != {"Bash": "built-in", "mcp__claude_ai_Figma__use_figma": "claude ai Figma",
                "mcp__Claude_Browser__navigate": "Claude Browser",
                "mcp__40a9e9ea-a265-43ac-9423-5f01011bcd27__get_screenshot": "MCP"}:
        errs.append(f"page: tool source tags: {tags}")
    sinks = {n for nodes, _ in sankey.flows(b)[0] for n in nodes[-1:]}
    if "tools" in sinks or not sinks <= {sankey.ASKING, sankey.INTO_CTX, sankey.ANSWERS_REREAD, sankey.REREADS, sankey.PROMPTS, sankey.OVERHEAD}:
        errs.append(f"page: Sankey sinks are not the pane's parts: {sinks}")
    keys = [k for k, _, _ in sankey.COLUMNS]
    for n in range(len(keys) + 1):
        for hide in itertools.combinations(keys, n):
            sankey.HIDE = frozenset(hide)
            try:
                cols, links, _, _ = sankey.graph(b)
                bad = [i for i, c in enumerate(cols) if abs(sum(c.values()) - b.cost) > 1e-6 and i in (0, len(cols) - 1)]
                if bad:
                    errs.append(f"page: Sankey with {hide or 'nothing'} hidden does not balance in columns {bad}")
                # every node is as tall as the ribbons arriving at it, so none spills past it
                w, pos = sankey.layout(cols, links)
                into = {}
                for (src, dst, _), width in zip(links, w):
                    into[dst] = into.get(dst, 0.0) + width
                spill = [d for d, h in into.items() if abs(pos[d][2] - h) > 0.01]
                if spill:
                    errs.append(f"page: Sankey with {hide or 'nothing'} hidden: ribbons spill past {spill}")
            finally:
                sankey.HIDE = frozenset()
    print("[page] " + ("OK" if not errs else "FAIL"))
    return errs


def requests_case(store, prices):
    """A prompt that never reached the API (a local slash command such as /tokentab:status,
    or one the expired-cache check held back) is not a request: latest, previous, average
    and count cover only prompts that made a call, in the terminal view, on the page and in
    --json alike. A session whose prompts made no call has no Requests card at all."""
    errs = []
    b = store.sessions["fx"]
    saved = store.prompts.get("fx")
    first, last = b.calls[0][0], b.calls[-1][0]
    spent = sum(c for _, c in b.calls)
    budget = {}
    card = "<h2>Requests</h2>"

    def page(R):
        R["pane"] = kt.pane_report(store, "fx", prices, budget, windows={})
        return sankey.parts(R)["status"]

    try:
        # two prompts that set off the calls, then two that made none after them
        store.prompts["fx"] = [first - 1, last - 0.5, last + 10, last + 20]
        R = kt.report(store, "fx", prices, budget, True)
        q = R["requests"]
        if not q or q["count"] != 2:
            errs.append(f"requests: prompts without a call are counted: {q and q['count']} requests, want 2")
        else:
            if q["latest"][0] != last - 0.5 or q["latest"][2] < 1:
                errs.append(f"requests: latest is a prompt without a call: {q['latest']}")
            if not q["previous"] or q["previous"][0] != first - 1 or q["previous"][2] < 1:
                errs.append(f"requests: previous is a prompt without a call: {q['previous']}")
            if abs(q["average"] - spent / 2) > 1e-9:
                errs.append(f"requests: average {q['average']:.6f} is not over the 2 requests ({spent / 2:.6f})")
        if re.search(r"(?<!\d)0 calls · ", page(R)):
            errs.append("requests: the page's Requests card shows a request with 0 calls")
        if re.search(r"(?<!\d)0 calls · ", kt.render(store, "fx", prices, budget, True)):
            errs.append("requests: the terminal view shows a request with 0 calls")
        snap = kt.snapshot(store, "fx", prices)["requests"]
        if len(snap) != 2 or any(r["calls"] < 1 for r in snap):
            errs.append(f"requests: --json lists prompts without a call: {snap}")
        # every prompt of the session made no call: no card, not a $0 average
        store.prompts["fx"] = [last + 10, last + 20]
        R = kt.report(store, "fx", prices, budget, True)
        if R["requests"]:
            errs.append(f"requests: a session whose prompts made no call has requests: {R['requests']}")
        if card in page(R):
            errs.append("requests: the page shows a Requests card for a session whose prompts made no call")
        if kt.snapshot(store, "fx", prices)["requests"]:
            errs.append("requests: --json lists requests for a session whose prompts made no call")
    finally:
        if saved is None:
            store.prompts.pop("fx", None)
        else:
            store.prompts["fx"] = saved
    print("[requests] " + ("OK" if not errs else "FAIL"))
    return errs


def saved_state_case(prices):
    """kt.scanned(): the stores saved between runs give the same figures as a full read,
    after new lines, and are rebuilt when a log shrinks or vanishes or the key changes."""
    errs = []
    keep = kt.ROOT, kt.CFG
    root = tempfile.mkdtemp()
    kt.ROOT, kt.CFG = os.path.join(root, "projects"), os.path.join(root, "cfg")
    try:
        os.makedirs(os.path.join(kt.ROOT, "p"))
        os.makedirs(os.path.join(kt.CFG, "ledger", "2026-10-13"))
        log = lambda sid: os.path.join(kt.ROOT, "p", f"{sid}.jsonl")
        ledger = os.path.join(kt.CFG, "ledger", "2026-10-13", "s1.jsonl")

        def add(sid, n):
            with open(log(sid), "a", encoding="utf-8") as f:
                for i in range(n):
                    req = f"{sid}-{os.path.getsize(log(sid)) if os.path.exists(log(sid)) else 0}-{i}"
                    f.write(json.dumps(dict(user("hi"), sessionId=sid)) + "\n")
                    f.write(json.dumps(dict(asst(req, OPUS, usage(i=10, r=100, w1=50, o=20, th=5),
                                                 {"type": "text", "text": "ok"}), sessionId=sid)) + "\n")
            with open(ledger, "a", encoding="utf-8") as f:
                f.write(json.dumps({"ts": ts(), "session_id": sid, "model": OPUS, "query_source": "repl_main_thread",
                                    "request_id": f"L{T[0]}", "tokens": {"input": 10, "output": 20,
                                    "cache_read": 100, "cache_write": 50}}) + "\n")

        def figures(store, led):
            led.reconcile(store)
            return json.dumps({k: {sid: [round(b.cost, 12), sorted((r, round(v, 12)) for r, v in b.cost_by.items()),
                                         b.msgs, len(b.calls)] for sid, b in st.sessions.items()}
                               for k, st in (("store", store), ("led", led))}, sort_keys=True)

        def fresh():
            store, led = kt.Store(prices), kt.LedgerStore(prices)
            store.scan(); led.scan()
            return figures(store, led)

        def run(label, kind, reason=None):
            kt.PERF.clear()
            got = figures(*kt.scanned(prices))
            if got != fresh():
                errs.append(f"saved state: {label}: figures differ from a full read")
            if kt.PERF.get("kind") != kind or kt.PERF.get("reason") != reason:
                errs.append(f"saved state: {label}: {kt.PERF.get('kind')}/{kt.PERF.get('reason')}, want {kind}/{reason}")

        add("s1", 3); add("s2", 2)
        run("first run", "full", "none")
        run("nothing new", "incremental")
        if kt.PERF.get("read_bytes") or kt.PERF.get("save_s", 1) > 0.05:
            errs.append(f"saved state: a run with nothing new read {kt.PERF.get('read_bytes')} bytes")
        add("s1", 2)
        run("lines appended", "incremental")
        with open(log("s2"), "a", encoding="utf-8") as f:
            f.write('{"type":"assistant","cut')          # a line still being written
        run("partial line", "incremental")
        with open(log("s2"), "a", encoding="utf-8") as f:
            f.write('"}\n')
        add("s2", 1)
        run("partial line completed", "incremental")
        with open(log("s1"), "r+", encoding="utf-8") as f:
            f.truncate(f.readline().__len__())
        run("a log shrank", "full", "shrank")
        os.remove(log("s2"))
        run("a log vanished", "full", "vanished")
        prices2 = json.loads(json.dumps(prices))
        prices2["_test"] = 1
        kt.PERF.clear(); kt.scanned(prices2)
        if kt.PERF.get("reason") != "changed":
            errs.append(f"saved state: other prices: reason {kt.PERF.get('reason')}, want changed")
        with open(os.path.join(kt.CFG, "state", "scan.pickle"), "wb") as f:
            f.write(b"not a pickle")
        run("an unreadable file", "full", "unreadable")
        errs += both_scripts_case()
        kt.perf_done("test", "case")
        with open(os.path.join(kt.CFG, "state", "perf.jsonl"), encoding="utf-8") as f:
            last = json.loads(f.readlines()[-1])
        if last.get("script") != "test" or "total_s" not in last or last.get("kind") != "full":
            errs.append(f"saved state: timing log line {last}")
    finally:
        kt.ROOT, kt.CFG = keep
    print("[saved state] " + ("OK" if not errs else "FAIL"))
    for e in errs:
        print("   ", e)
    return errs


def both_scripts_case():
    """tokentab.py and sankey.py, run as the hooks module runs them, share one state file:
    after one full read, the other script only catches up."""
    import subprocess
    errs, cfg = [], tempfile.mkdtemp()
    os.makedirs(os.path.join(cfg, "projects", "p"))
    with open(os.path.join(cfg, "projects", "p", "s.jsonl"), "w", encoding="utf-8") as f:
        f.write(json.dumps(dict(asst("rB", OPUS, usage(i=10, o=20), {"type": "text", "text": "ok"}), sessionId="s")) + "\n")
    env = dict(os.environ, CLAUDE_CONFIG_DIR=cfg)
    here = os.path.join(PLUGIN, "tokentab")
    for argv in (["tokentab.py", "--status"], ["sankey.py", "--flow-svg"], ["tokentab.py", "--report"]):
        r = subprocess.run([sys.executable, os.path.join(here, argv[0])] + argv[1:], env=env, capture_output=True)
        if r.returncode:
            errs.append(f"saved state: {' '.join(argv)} failed: {r.stderr.decode()[-300:]}")
    log = os.path.join(cfg, "plugins", "data", "tokentab-supertab", "state", "perf.jsonl")
    try:
        with open(log, encoding="utf-8") as f:
            kinds = [json.loads(x).get("kind") for x in f]
    except OSError:
        kinds = []
    if kinds != ["full", "incremental", "incremental"]:
        errs.append(f"saved state: the two scripts' runs were {kinds}, want one full read then catch-ups")
    return errs


if __name__ == "__main__":
    sys.exit(1 if main() else 0)
