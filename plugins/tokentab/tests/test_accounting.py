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
    errs += hover_list_check(b, tb)
    # the SVG parses, the pane's and the page's
    minidom.parseString(sankey.svg(b, tb))
    minidom.parseString(sankey.page_svg(b, tb))
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


def hover_list_check(b, tb=None):
    """The list beside the page's Sankey, in its hover state: for every target (a node, a
    ribbon), each column's slices sum to the target's total, except the tool column, which
    sums to the part of it that passes a tool (the rest runs past the tool column); every
    slice is in the list's markup, and the markup parses."""
    errs = []
    paths, _, _ = sankey.flows(b, tb)
    _, links = sankey.columns(paths)
    lid = {(s, d): i for i, (s, d, _) in enumerate(links)}
    models = set(b.cost_bm)
    on = lambda t, nodes: (t[1:] in {sankey.slug(n) for n in nodes} if t.startswith("n")
                           else int(t[1:]) in {lid[e] for e in zip(nodes, nodes[1:])})
    html = sankey.cost_list(b, tb)
    minidom.parseString(html)
    targets = sankey.hover_slices(paths, links)
    if len(targets) != len({n for p, _ in paths for n in p}) + len(links):
        errs.append(f"hover list: {len(targets)} targets, not one per node and ribbon")
    for t, (tot, sl) in targets.items():
        want = sum(v for nodes, v in paths if on(t, nodes))
        via_tool = sum(v for nodes, v in paths if on(t, nodes) and any(sankey.role(n, models) == "tool" for n in nodes))
        if abs(tot - want) > 1e-9:
            errs.append(f"hover {t}: total {tot:.6f} != {want:.6f}")
        for r in ("model", "kind", "rate", "tool", "sink"):
            got = sum(v for n, v in sl.items() if sankey.role(n, models) == r)
            if abs(got - (via_tool if r == "tool" else tot)) > 1e-9:
                errs.append(f"hover {t}: {r} slices sum to {got:.6f}, not {via_tool if r == 'tool' else tot:.6f}")
        for n, v in sl.items():
            if n != "session" and f"--v{t}:'{sankey.usd(v, 4)}'" not in html:
                errs.append(f"hover {t}: the row of {n} lacks its slice {sankey.usd(v, 4)}")
        if f".cf:has({sankey.hover_sel(t, True)}) .cl{{--h0:'{sankey.usd(tot, 4)}'" not in html:
            errs.append(f"hover {t}: the first header does not show {sankey.usd(tot, 4)}")
    print(f"    hover list: {len(targets)} targets checked")
    return errs


def day_budget_case():
    """day_budget: what is left of the week over the time left, fixed at the start of the local
    day. Unused budget and overspend roll on through the base; spending today leaves today's alone."""
    from datetime import datetime, timedelta, timezone
    errs = []
    saved = kt.BASELINE_PATH
    kt.BASELINE_PATH = os.path.join(tempfile.mkdtemp(), "week_baseline.json")
    L = lambda m, d, h=0: datetime(2026, m, d, h).astimezone()      # a local wall-clock time
    iso = lambda dt: dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    win = lambda pct, reset: {"seven_day": {"kind": "seven_day", "percentUsed": pct, "resetsAt": iso(reset)}}
    # The figure needs no switch: whether it is checked is the budget type's business
    day = kt.day_budget

    def entries(reset, day, base, last):
        with open(kt.BASELINE_PATH, "w", encoding="utf-8") as f:
            json.dump({iso(reset): {"day": day, "base": base, "last": last}}, f)

    def want(label, got, allowed, used=None, tol=0.05):
        if got is None or abs(got["allowed"] - allowed) > tol or (used is not None and abs(got["used"] - used) > 1e-9):
            errs.append(f"day_budget {label}: want allowed {allowed:.3f} used {used}, got {got}")

    try:
        # A fresh week at midnight, no baseline yet: 100/7
        reset, now = L(11, 26), L(11, 19, 10).timestamp()
        got = day({}, win(0, reset), now)
        want("fresh week", got, 100 / 7, 0)
        if got and (got["base"] != 0 or got["cap"] != 100 or got["frac"] != 0):
            errs.append(f"day_budget fresh week: {got}")
        got = day({}, win(2, reset), now)
        want("fresh week, 2% used", got, 100 / 7, 2)
        if got and abs(got["frac"] - 2 / got["allowed"]) > 1e-9:
            errs.append(f"day_budget fresh week frac: {got}")
        # Day 1 unused: day 2 is 100/6. Day 1 used 20%: day 2 is 80/6
        now = L(11, 20, 9).timestamp()
        entries(reset, "2026-11-19", 0, 0)
        want("day 1 unused", day({}, win(0, reset), now), 100 / 6, 0)
        entries(reset, "2026-11-19", 0, 20)
        want("day 1 used 20%", day({}, win(20, reset), now), 80 / 6, 0)
        # No entry for today yet, or the hook's write failed: yesterday's `last` stays the base, so the
        # readings that follow still count toward today instead of each becoming the base
        for p in (23, 30):
            got = day({}, win(p, reset), now)
            want(f"stale entry, reading {p}", got, 80 / 6, p - 20)
            if got and got["base"] != 20:
                errs.append(f"day_budget stale entry: base {got['base']}, want 20")
        # The budget is fixed for the day: spending today does not change it
        entries(reset, "2026-11-20", 20, 20)
        a, b = (day({}, win(p, reset), now) for p in (21, 30))
        if not a or not b or a["allowed"] != b["allowed"] or (a["used"], b["used"]) != (1, 10):
            errs.append(f"day_budget: spending moved today's allowance: {a} {b}")
        # This session's reading is behind the baseline another session moved: the newer `last` counts
        entries(reset, "2026-11-20", 20, 26)
        want("stale session reading", day({}, win(21, reset), now), 80 / 6, 6)
        # The same reset spelled another way still finds its entry
        with open(kt.BASELINE_PATH, "w", encoding="utf-8") as f:
            json.dump({reset.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z"):
                       {"day": "2026-11-20", "base": 20, "last": 20}}, f)
        want("reset spelled with milliseconds", day({}, win(23, reset), now), 80 / 6, 3)
        # An odd-hour reset: the second to last day spreads over the 37 hours left, the last day gets what is left
        odd = L(11, 21, 13)
        entries(odd, "2026-11-19", 4, 4)
        want("odd-hour reset, day 2", day({}, win(4, odd), now), 96 * 24 / 37)
        entries(odd, "2026-11-20", 10, 10)
        want("odd-hour reset, last day", day({}, win(10, odd), L(11, 21, 9).timestamp()), 90, 0, tol=1e-6)
        # No baseline entry, mid-week: the week's use so far is the base, so nothing is used today yet
        mid_week, now = L(11, 24), L(11, 19, 14).timestamp()
        os.remove(kt.BASELINE_PATH)
        got = day({}, win(30, mid_week), now)
        if not got or got["base"] != 30 or got["used"] != 0:
            errs.append(f"day_budget without a baseline, mid-week: {got}")
        # The spec's example: today 2026-11-19, base 4, reset 2026-11-21T01:00Z
        fixed, now = datetime(2026, 11, 21, 1, tzinfo=timezone.utc), L(11, 19, 14).timestamp()
        entries(fixed, "2026-11-19", 4, 4)
        mid, nxt = L(11, 19).timestamp(), L(11, 20).timestamp()
        want("spec example", day({}, win(4, fixed), now), 96 * (nxt - mid) / (fixed.timestamp() - mid), 0, tol=1e-9)
        # The cap is the week's own 100%, whatever an old key says; days counts today
        entries(reset, "2026-11-19", 10, 10)
        now = L(11, 19, 14).timestamp()
        got = day({"weekly_limit_percent": 50}, win(10, reset), now)
        if not got or got["cap"] != 100 or got["days"] != 7:
            errs.append(f"day_budget cap and days: {got}")
        # Over the allowance, and an allowance of zero
        got = day({}, win(99, reset), now)
        if not got or got["frac"] < 1:
            errs.append(f"day_budget overspent: {got}")
        entries(reset, "2026-11-19", 100, 100)
        got = day({}, win(100, reset), now)
        if not got or got["allowed"] != 0 or got["frac"] != 0:
            errs.append(f"day_budget with nothing left: {got}")
        got = day({}, win(101, reset), now)
        if not got or got["frac"] != 1.0:
            errs.append(f"day_budget with nothing left and use: {got}")
        # A figure whatever daily_budget holds; None: no weekly reading, a reset already past
        for cfg in ({}, {"daily_budget": {}}, {"daily_budget": False}):
            if kt.day_budget(cfg, win(1, reset), now) is None:
                errs.append(f"day_budget: a figure needs no switch, got none for {cfg}")
        if day({}, {}, now) is not None or day({}, {"five_hour": win(1, reset)["seven_day"]}, now) is not None:
            errs.append("day_budget: no weekly reading should be None")
        if day({}, win(1, L(11, 18)), now) is not None:
            errs.append("day_budget: a reset already past should be None")
        # Carry-over (beta): a seventh of the cap a day, plus what earlier days left, minus what they overspent
        CARRY = {"daily_budget": {"carry_over": True}}
        reset = L(11, 26)
        os.remove(kt.BASELINE_PATH)
        got = day(CARRY, win(0, reset), L(11, 19, 10).timestamp())
        want("carry-over, day 1", got, 100 / 7, 0)
        if got and (abs(got["daily"] - 100 / 7) > 0.05 or abs(got["carried"]) > 1e-9):
            errs.append(f"day_budget carry-over day 1: {got}")
        now = L(11, 20, 9).timestamp()
        entries(reset, "2026-11-20", 0, 0)
        got = day(CARRY, win(0, reset), now)
        want("carry-over, day 1 unused", got, 200 / 7, 0)
        if got and abs(got["carried"] - 100 / 7) > 0.05:
            errs.append(f"day_budget carry-over carried: {got}")
        entries(reset, "2026-11-20", 20, 20)
        got = day(CARRY, win(25, reset), now)
        want("carry-over, day 1 overspent", got, 200 / 7 - 20, 5)
        if got and got["carried"] > -5.7:
            errs.append(f"day_budget carry-over overspent: {got}")
        entries(reset, "2026-11-20", 40, 40)
        got = day(CARRY, win(41, reset), now)
        if not got or got["allowed"] != 0 or got["frac"] != 1.0 or got["carried"] >= -got["daily"]:
            errs.append(f"day_budget carry-over overspent past today's share: {got}")
        got = day(CARRY, win(40, reset), now)
        if not got or got["allowed"] != 0 or got["used"] != 0 or got["frac"] != 1.0:
            errs.append(f"day_budget carry-over overspent, nothing used yet today: {got}")
        entries(reset, "2026-11-20", 10, 10)
        if "carried" in (day({}, win(10, reset), now) or {}):
            errs.append("day_budget: carried without carry_over")
    finally:
        kt.BASELINE_PATH = saved
    # budget.py: strings and the file's version are not set here
    import budget
    for key, old, raw in (("other", "text", "1"), ("version", 2, "3")):
        try:
            budget.parse(key, old, raw)
            errs.append(f"budget.parse: {key}={raw} should be refused")
        except ValueError:
            pass
    # spend_less: a plain switch
    got = [budget.parse("spend_less", True, "on"), budget.parse("spend_less", True, "off"), budget.parse("spend_less", False, "on")]
    if got != [True, False, True]:
        errs.append(f"budget.parse: spend_less should take on and off: {got}")
    for key, old in (("spend_less", False), ("hard_stop", True), ("other", None)):
        try:
            budget.parse(key, old, "auto")
            errs.append(f"budget.parse: {key}=auto should be refused")
        except ValueError:
            pass
    # budget_type: each unit's own words; percent has no session, and there is no auto
    got = [budget.parse("budget_type.usd", "today", "Session"), budget.parse("budget_type.percent", "today", "five_hour")]
    if got != ["session", "five_hour"]:
        errs.append(f"budget.parse: budget_type should take its unit's types: {got}")
    for key, raw in (("budget_type.usd", "week"), ("budget_type.percent", "month"), ("budget_type.percent", "session"),
                     ("budget_type.usd", "auto")):
        try:
            budget.parse(key, None, raw)
            errs.append(f"budget.parse: {key}={raw} should be refused")
        except ValueError:
            pass
    # The type in effect: the pick, else today; no guessing from the limits set
    for cfg, unit, want in (({"budget_type": {"usd": "session"}}, "usd", "session"),
                            ({"budget_type": {"usd": "week"}}, "usd", "today"),
                            ({"session_limit_usd": 5, "daily_limit_usd": None}, "usd", "today"),
                            ({"budget_type": {"percent": "session"}}, "percent", "today"),
                            ({"budget_type": {"percent": "five_hour"}}, "percent", "five_hour"),
                            ({}, "percent", "today")):
        if kt.budget_type(cfg, unit) != want:
            errs.append(f"budget_type for {cfg} in {unit}: {kt.budget_type(cfg, unit)}, want {want}")
    # One budget at a time: only the type's limit is checked
    both = {"session_limit_usd": 2, "daily_limit_usd": 40}
    for cfg, want in (({**both, "budget_type": {"usd": "session"}}, [("session", 0.25)]),
                      (both, [("today", 0.075)]), ({**both, "daily_limit_usd": None, "budget_type": {"usd": "today"}}, [])):
        got = [(r["scope"], r["frac"]) for r in kt.limit_rows(cfg, {"session": 0.5, "today": 3}, {})[1]]
        if got != want:
            errs.append(f"limit_rows for {cfg}: {got}, want {want}")
    # Today's limit resets with the local day, so a session past midnight warns afresh; a session's never does
    noon = kt.datetime(2026, 3, 4, 12).timestamp()
    resets = [kt.limit_rows({**both, "budget_type": {"usd": typed}}, {"session": 0.5, "today": 3}, {}, noon)[1][0]["reset"]
              for typed in ("today", "session")]
    if resets != ["2026-03-04", None]:
        errs.append(f"limit_rows: today resets with the day, the session never: {resets}")
    five = {"five_hour": {"kind": "five_hour", "percentUsed": 40, "resetsAt": "2999-01-01T00:00:00Z"},
            "seven_day": {"kind": "seven_day", "percentUsed": 45, "resetsAt": "2999-01-01T00:00:00Z"}}
    # On a plan each window is watched against Anthropic's own 100%, an old file's limit keys ignored
    plan = {"five_hour_limit_percent": 80, "weekly_limit_percent": 90, "session_limit_usd": 2}
    for typed, want in (("five_hour", [("five_hour", 0.4)]), ("week", [("seven_day", 0.45)])):
        got = [(r["scope"], r["frac"]) for r in kt.limit_rows({**plan, "budget_type": {"percent": typed}}, {"session": 0.5, "today": 3}, five)[1]]
        if got != want:
            errs.append(f"limit_rows on a plan, type {typed}: {got}, want {want}")
    row = kt.limit_rows({"budget_type": {"percent": "five_hour"}}, {"session": 0.5, "today": 3}, five)[1]
    if not row or row[0]["parts"][0] != "40% used":
        errs.append(f"limit_rows: a window budget reads its use: {row}")
    # A file from before carry_over gains it inside daily_budget, the user's values and own keys kept
    up = budget.upgraded({"daily_budget": {"enabled": True}, "hard_stop": True, "mine": 1})
    if up.get("daily_budget") != {"enabled": True, "carry_over": False} or up.get("hard_stop") is not True \
            or up.get("mine") != 1 or list(up)[-1] != "mine":
        errs.append(f"budget.upgraded: nested key not added: {up}")
    up = budget.upgraded({"_daily_comment": "old", "_mine": "note"})
    with open(budget.SHIPPED, encoding="utf-8") as f:
        shipped_comment = json.load(f)["_daily_comment"]
    if up["_daily_comment"] != shipped_comment or up.get("_mine") != "note":
        errs.append(f"budget.upgraded: shipped comment not refreshed: {up}")
    return errs


def day_budget_pane_case(store, prices):
    """The pane's today window, the status line and the reopen button's text follow budget.json's headline."""
    from datetime import datetime, timedelta, timezone
    errs = []
    saved = (kt.BASELINE_PATH, kt.WINDOWS_DIR)
    tmp = tempfile.mkdtemp()
    kt.BASELINE_PATH, kt.WINDOWS_DIR = os.path.join(tmp, "week_baseline.json"), os.path.join(tmp, "windows")
    os.makedirs(kt.WINDOWS_DIR)
    try:
        # Whole hours, three days out, so the week began before today
        reset = (datetime.now(timezone.utc) + timedelta(days=3)).replace(minute=0, second=0, microsecond=0)
        r = reset.strftime("%Y-%m-%dT%H:%M:%SZ")
        windows = {"five_hour": {"kind": "five_hour", "percentUsed": 13, "resetsAt": "2999-01-01T00:00:00Z"},
                   "seven_day": {"kind": "seven_day", "percentUsed": 5, "resetsAt": r}}
        with open(kt.BASELINE_PATH, "w", encoding="utf-8") as f:
            json.dump({r: {"day": datetime.now().astimezone().date().isoformat(), "base": 2, "last": 5}}, f)
        with open(os.path.join(kt.WINDOWS_DIR, "fx.json"), "w", encoding="utf-8") as f:
            json.dump({"windows": list(windows.values())}, f)
        TODAY = {"budgets_on": True, "budget_type": {"percent": "today"}}
        day = kt.day_budget({}, windows)
        scopes = lambda s: [w["scope"] for w in s["windows"]]
        # Off, the pane has no today window and auto leads with the week
        s = kt.pane_report(store, "fx", prices, {}, windows=windows)["session"]
        if "today" in scopes(s) or s["headline"] != "seven_day":
            errs.append(f"pane today window: none while not asked for, auto leads with the week: {s['windows']} {s['headline']}")
        # As the budget it leads under auto and carries its limit, the day's allowance
        s = kt.pane_report(store, "fx", prices, TODAY, windows=windows)["session"]
        today = [w for w in s["windows"] if w["scope"] == "today"]
        if scopes(s) != ["five_hour", "seven_day", "today"] or not day:
            errs.append(f"pane today window: windows are {s['windows']}")
        elif (today[0]["limit"], today[0]["resets"], today[0]["used"]) != (day["allowed"], None, 3) \
                or today[0]["frac"] != day["frac"] or today[0]["allowed"] != day["allowed"] or today[0]["days"] != day["days"]:
            errs.append(f"pane today window: {today[0]}")
        if s["headline"] != "today" or s["frac"] != day["frac"] or "warn_at" in s:
            errs.append(f"pane headline: {s['headline']} {s['frac']}")
        # The budget off with today picked: the week leads, and today is not shown
        s = kt.pane_report(store, "fx", prices, {**TODAY, "budgets_on": False}, windows=windows)["session"]
        if "today" in scopes(s) or s["headline"] != "seven_day":
            errs.append(f"pane with the budget off: {s['windows']} {s['headline']}")
        # The headline follows the budget type while the budget is on, else the week; today in USD
        for budget, wins, want in ((TODAY, windows, "today"),
                                   ({"budgets_on": True, "budget_type": {"percent": "five_hour"}}, windows, "five_hour"),
                                   ({"budgets_on": True, "budget_type": {"percent": "week"}}, windows, "seven_day"),
                                   ({"budgets_on": True, "headline": "five_hour"}, windows, "today"),
                                   ({}, windows, "seven_day"),
                                   ({}, {"five_hour": windows["five_hour"]}, "five_hour"),
                                   ({}, {}, "today"),
                                   ({"budgets_on": True, "budget_type": {"usd": "session"}}, {}, "session"),
                                   ({"budgets_on": False, "budget_type": {"usd": "session"}}, {}, "today")):
            got = kt.pane_report(store, "fx", prices, budget, windows=wins)["session"]["headline"]
            if got != want:
                errs.append(f"headline for {budget} on {list(wins)}: {got}, want {want}")
        # The status line leads with the headline, counted as what is left, and shows at most two figures
        left = f"{(1 - day['frac']) * 100:.0f}%"
        cases = ((TODAY, f"today's budget {left} left · week 95% left", f"today's budget {left} left"),
                 ({}, "week 95% left · 5 hours 87% left", "week 95% left"),
                 # A window budget leads, the other window second
                 ({"budgets_on": True, "budget_type": {"percent": "five_hour"}}, "5 hours 87% left · week 95% left", "5-hour 87% left"))
        for budget, want_line, button in cases:
            line, got = kt.status_parts(store, "fx", prices, budget)
            if line != want_line or got != button or kt.status_line(store, "fx", prices, budget) != line:
                errs.append(f"status for {budget}: {line!r} {got!r}, want {want_line!r} {button!r}")
        # Carry-over names the budget's size in the status line and passes its parts to the pane
        CARRY = {"carry_over": True}
        carried = kt.day_budget({"daily_budget": CARRY}, windows)
        line = kt.status_line(store, "fx", prices, {**TODAY, "daily_budget": CARRY})
        want_item = f"today's budget {(1 - carried['frac']) * 100:.0f}% left ({carried['allowed']:.0f}% of week"
        if not line.startswith(want_item):
            errs.append(f"status line with carry-over: {line!r}, want {want_item!r}")
        # ...and what earlier days carried or overspent, with fixed figures since the real ones move with the clock
        real_day_budget = kt.day_budget
        try:
            for daily, carried_in, want_size in ((14.3, 14.3, "(29% of week, +14% carried)"),
                                                 (14.3, -5.7, "(9% of week, −6% overspent)"),
                                                 (14.3, 0.5, "(15% of week)"),
                                                 (14.3, 0.4, "(15% of week)")):
                allowed = daily + carried_in
                kt.day_budget = lambda *_args, **_kw: {"used": 0.0, "allowed": allowed, "frac": 0.0, "base": 0.0,
                                                       "cap": 100.0, "daily": daily, "carried": carried_in}
                line = kt.status_line(store, "fx", prices, {**TODAY, "daily_budget": CARRY})
                if not line.startswith(f"today's budget 100% left {want_size}"):
                    errs.append(f"status line carried {carried_in}: {line!r}, want {want_size!r}")
        finally:
            kt.day_budget = real_day_budget
        s = kt.pane_report(store, "fx", prices, {**TODAY, "daily_budget": CARRY}, windows=windows)["session"]
        today = next(w for w in s["windows"] if w["scope"] == "today")
        if (today.get("daily"), today.get("carried")) != (carried["daily"], carried["carried"]):
            errs.append(f"pane today window with carry-over: {today}")
        if "carried" in next(w for w in kt.pane_report(store, "fx", prices, TODAY, windows=windows)["session"]["windows"]
                             if w["scope"] == "today"):
            errs.append("pane today window: carried without carry_over")
        os.remove(os.path.join(kt.WINDOWS_DIR, "fx.json"))
        # In USD today's spend leads by default, then the session; a limit that is on shows what is left of it
        line, button = kt.status_parts(store, "fx", prices, TODAY)
        if not line.startswith("today $") or " · session $" not in line or "of week" in line or button != line.split(" · ")[0]:
            errs.append(f"status without a plan reading: {line!r} {button!r}")
        line, button = kt.status_parts(store, "fx", prices, {"budgets_on": True, "budget_type": {"usd": "session"}, "session_limit_usd": 1, "daily_limit_usd": None})
        if not (line.startswith("session ") and "% left of $1.00 · today $" in line) or button != line.split(" · ")[0]:
            errs.append(f"status with a USD session limit: {line!r} {button!r}")
    finally:
        kt.BASELINE_PATH, kt.WINDOWS_DIR = saved
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
    errs += day_budget_case()
    errs += day_budget_pane_case(store, prices)
    errs += breakdown_check(store, prices)
    errs += series_check(b, "transcript", store)
    errs += ledger_case(root, store, prices)
    errs += ledger_5m_case(prices)
    errs += backfill_case(prices)
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
    budget = {"budgets_on": True, "budget_type": {"usd": "session"}, "session_limit_usd": 0.0001}
    rep = json.loads(json.dumps(kt.pane_report(store, "fx", prices, budget, windows={})))
    s = rep["session"]
    if not s or s["unit"] != "usd" or not s["limit"].endswith("of $0.00") or not s["frac"] or s["frac"] < 1:
        errs.append(f"pane report: session against its limit is wrong: {s}")
    # A plan with usage windows counts the 5-hour window in percent
    windows = {"five_hour": {"kind": "five_hour", "percentUsed": 40, "resetsAt": "2999-01-01T00:00:00Z"},
               "seven_day": {"kind": "seven_day", "percentUsed": 12.5, "resetsAt": "2000-01-01T00:00:00Z"}}
    plan = {**budget, "budget_type": {"percent": "five_hour"}}
    s = kt.pane_report(store, "fx", prices, plan, windows=windows)["session"]
    if s["unit"] != "percent" or abs(s["frac"] - 0.4) > 1e-9 or s["limit"] != "5-hour window 40% used":
        errs.append(f"pane report: the 5-hour window against its limit is wrong: {s}")
    if not s["plan"] or [w["scope"] for w in s["windows"]] != ["five_hour", "seven_day"] or s["windows"][1]["used"] != 0:
        errs.append(f"pane report: both windows should be listed, the weekly one reset to 0%: {s}")
    # Limits off on a plan: the windows still lead the hero, in percent, with no limit or fraction
    s = kt.pane_report(store, "fx", prices, {**plan, "budgets_on": False}, windows=windows)["session"]
    if s["unit"] != "percent" or [(w["scope"], w["limit"], w["frac"]) for w in s["windows"]] != [("five_hour", None, None), ("seven_day", None, None)] or s["windows"][0]["used"] != 40:
        errs.append(f"pane report: limits off should still list the plan windows in percent: {s}")
    if kt.pane_report(store, "fx", prices, budget, windows={})["session"]["plan"]:
        errs.append("pane report: no plan windows means no subscription label")
    if kt.reset_text("2000-01-01T00:00:00Z") is not None or not kt.reset_text("2999-01-01T00:00:00Z"):
        errs.append("reset_text: a past reset should be None, a future one a time")
    if kt.pane_report(store, "fx", prices, {**plan, "limit_unit": "usd"}, windows=windows)["session"]["unit"] != "percent":
        errs.append("pane report: an old file's limit_unit should not override the account")
    # An account with no usage windows (Enterprise, API key) checks a USD limit, whatever an old file says
    unit, rows = kt.limit_rows({**plan, "limit_unit": "percent", "daily_limit_usd": 50},
                               {"session": 9, "today": 120}, {})
    if unit != "usd" or [(r["scope"], r["frac"] >= 1) for r in rows] != [("today", True)]:
        errs.append(f"limit_rows: no usage windows should check the USD limits: {unit} {rows}")
    unit, rows = kt.limit_rows({**plan, "budget_type": {"percent": "week"}}, {"session": 0, "today": 0}, windows)
    if [(r["scope"], r["frac"]) for r in rows] != [("seven_day", 0.0)]:
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
    # The budget, when the hero does not lead with it, is still reported, so the pane can judge it
    for usd_budget, want_lead, want_other in (
            (budget, "session", None),
            ({**budget, "daily_limit_usd": 1e6, "budget_type": {"usd": "today"}}, "today", None),
            ({**budget, "daily_limit_usd": 1e6, "budgets_on": False}, "today", None)):
        s = kt.pane_report(store, "fx", prices, usd_budget, windows={})["session"]
        other = s["other_limit"]
        if s["headline"] != want_lead or (other and other["scope"]) != want_other \
                or (other is not None and (not other["limit"].startswith("$") or other["frac"] is None)):
            errs.append(f"pane report other_limit for {usd_budget}: {s['headline']} {other}")
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


def backfill_case(prices):
    """A resumed session whose earlier calls have no ledger line (TokenTab off or not yet
    installed) shows them from the transcript, matched by request id: once, across saved
    runs; not a call too recent for its ledger line; not in a session whose ledger lines
    carry no id; a forked copy's under the session it came from; web searches priced."""
    from datetime import datetime, timedelta, timezone
    errs = []
    keep = kt.ROOT, kt.CFG
    root = tempfile.mkdtemp()
    kt.ROOT, kt.CFG = os.path.join(root, "projects"), os.path.join(root, "cfg")
    try:
        os.makedirs(os.path.join(kt.ROOT, "p"))
        os.makedirs(os.path.join(kt.CFG, "ledger", "2026-10-13"))
        start = datetime.now(timezone.utc) - timedelta(minutes=10)   # past BACKFILL_AFTER, and this month
        at = lambda n: (start + timedelta(seconds=n)).isoformat()
        now = datetime.now(timezone.utc).isoformat()
        plain = usage(i=10, r=1000, w1=500, o=100, th=20)
        searched = dict(plain, server_tool_use={"web_search_requests": 3})
        calls = {"none": [("n1", at(1), plain), ("n2", at(2), searched)],               # no ledger line at all
                 "part": [("p1", at(3), plain), ("p2", at(4), plain), ("p3", now, plain)],  # p1 in the ledger, p3 too recent
                 "blind": [("b1", at(5), plain), ("b2", at(6), plain)],                # its ledger line has no request id
                 "fork": [("n1", at(1), plain), ("f1", at(20), plain)]}                # repeats n1, then goes on
        for sid, reqs in calls.items():
            with open(os.path.join(kt.ROOT, "p", f"{sid}.jsonl"), "w", encoding="utf-8") as f:
                for rid, when, use in reqs:
                    for line in (dict(user("go"), sessionId=sid),
                                 dict(asst(rid, OPUS, use, {"type": "text", "text": "ok"}), sessionId=sid, timestamp=when)):
                        f.write(json.dumps(line) + "\n")
        led_line = lambda sid, rid: {"ts": at(0), "session_id": sid, "model": OPUS, "request_id": rid,
                                     "query_source": "repl_main_thread",
                                     "tokens": {"input": 10, "output": 100, "cache_read": 1000, "cache_write": 500}}
        ledger = os.path.join(kt.CFG, "ledger", "2026-10-13", "l.jsonl")
        with open(ledger, "w", encoding="utf-8") as f:
            for entry in (led_line("part", "p1"), led_line("blind", None)):
                f.write(json.dumps(entry) + "\n")

        probe = kt.Store(prices)
        probe.scan()
        one = probe.sessions["part"].cost / 3       # one call's price, thinking split as the transcript has it
        if kt.LedgerStore.owner(probe, "n1", "fork") != "none" or kt.LedgerStore.owner(probe, "n1", "none") != "none":
            errs.append("backfill: a forked copy's call belongs to the session it came from, whatever the read order")

        kt.PERF.clear()
        store, led = kt.scanned(prices)
        if kt.PERF.get("filled") != 4 or set(store.req_of) != {"p3", "b1", "b2"}:
            errs.append(f"backfill: n1, n2, p2 and f1 added, only the waiting calls left: "
                        f"{kt.PERF.get('filled')} {sorted(store.req_of)}")
        for _ in range(2):                          # a second report backfills nothing more
            rep = kt.pane_report(store, "none", prices, {}, led, windows={})
        session = rep["session"]
        if not session or abs(session["cost"] - (2 * one + 3 * kt.WEB_SEARCH_USD)) > 1e-9 \
                or session.get("backfilled", {}).get("calls") != 2:
            errs.append(f"backfill: a session with no ledger line shows both calls and the searches: {session}")
        fork = led.sessions.get("fork")
        if not fork or fork.msgs != 1 or abs(fork.cost - one) > 1e-9:
            errs.append(f"backfill: the fork keeps only its own call: {fork and (fork.msgs, fork.cost)}")
        part = kt.pane_report(store, "part", prices, {}, led, windows={})["session"]
        if abs(part["cost"] - led.sessions["part"].cost) > 1e-12 or part["msgs"] != 2 \
                or part.get("backfilled", {}).get("calls") != 1:
            errs.append(f"backfill: p1 from the ledger, p2 from the transcript, p3 waits: {part}")
        if kt.pane_report(store, "blind", prices, {}, led, windows={})["session"].get("backfilled"):
            errs.append("backfill: a session with an id-less ledger line cannot be matched and gets none")
        added = sum(bucket.by_source.get(kt.BACKFILL_SOURCE, [0, 0.0])[1] for bucket in led.sessions.values())
        local = [datetime.fromisoformat(when).astimezone() for when in (at(1), at(20))]
        if local[0].month == local[1].month == datetime.now().month and rep["month"] < added - 1e-9:
            errs.append(f"backfill: the month counts the backfilled calls: {rep['month']} < {added}")
        if abs(sum(row["cost"] for row in rep["sessions"]) - rep["month"]) > 1e-9:
            errs.append("backfill: the month's sessions do not sum to the month")
        today = datetime.now().date().isoformat()
        with open(os.path.join(kt.CFG, "state", kt.BACKFILL_DAYS), encoding="utf-8") as f:
            days = json.load(f)
        if abs(sum(led.filled_days.values()) - added) > 1e-9 or days != dict(sorted(led.filled_days.items())[-2:]) \
                or rep["today"] < days.get(today, 0.0) - 1e-9:
            errs.append(f"backfill: backfill.json holds what was added per day, in today too: {days} {led.filled_days}")
        if "2 calls it has no line" not in (kt.report(store, "none", prices, {}, False, led)["session"] or {}).get("source", ""):
            errs.append("backfill: the live view should say some calls come from the transcript")
        if "1 call it has no line" not in (kt.report(store, "part", prices, {}, False, led)["session"] or {}).get("source", ""):
            errs.append("backfill: the live view says '1 call', not '1 calls'")

        # The next run starts from the saved stores, backfill and all: nothing is added again
        cost = led.sessions["part"].cost
        kt.PERF.clear()
        store, led = kt.scanned(prices)
        if kt.PERF.get("kind") != "incremental" or kt.PERF.get("filled") != 0 \
                or led.sessions["none"].by_source[kt.BACKFILL_SOURCE][0] != 2:
            errs.append(f"backfill: a saved run adds nothing again: {kt.PERF}")
        # p2's ledger line arrives after the backfill: the transcript's figure stays, once
        with open(ledger, "a", encoding="utf-8") as f:
            f.write(json.dumps(led_line("part", "p2")) + "\n")
        store, led = kt.scanned(prices)
        if abs(led.sessions["part"].cost - cost) > 1e-12:
            errs.append("backfill: a ledger line for a backfilled call counted it twice")
        # A full read gives p2 to the ledger
        store, led = kt.Store(prices), kt.LedgerStore(prices)
        store.scan(); led.scan(); kt.source(store, led, "part")
        if "p2" in led.filled or led.sessions["part"].by_source.get(kt.BACKFILL_SOURCE, [0])[0] != 0:
            errs.append("backfill: p2 has a ledger line and should not be backfilled on a full read")
    finally:
        kt.ROOT, kt.CFG = keep
    print("[backfill] " + ("OK" if not errs else "FAIL"))
    for err in errs:
        print("   ", err)
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
    budget = {"budgets_on": True, "session_limit_usd": 0.0001, "budget_type": {"usd": "session", "percent": "five_hour"}}
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
    # Today's budget never leads the tile, even as the only window with a fraction; it gets its own line
    R = kt.report(store, "fx", prices, {"budgets_on": False}, True)
    R["pane"] = P = kt.pane_report(store, "fx", prices, {"budgets_on": False}, windows=windows)
    P["session"]["windows"].append({"scope": "today", "used": 3, "allowed": 10, "limit": None, "frac": 0.3, "resets": None})
    try:
        hero = sankey.parts(R)["hero"]
        if "<b>40%</b>" not in hero or "today&#x27;s budget 30% used · 10% of the week" not in hero:
            errs.append(f"page: today's budget beside the windows: {hero[:400]}")
    except KeyError as e:
        errs.append(f"page: today's budget breaks the first tile: KeyError {e}")
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
            store.scan(); led.scan(); led.backfill(store)
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
        errs += handoff_touch_case()
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


def handoff_touch_case():
    """guard.py --cold-check: the handoff note's fork read the cache, so a note written after
    the last call keeps the cache warm even when the ledger has no line for the fork."""
    import subprocess
    from datetime import datetime, timedelta, timezone
    errs, cfg = [], tempfile.mkdtemp()
    data = os.path.join(cfg, "plugins", "data", "tokentab-supertab")
    os.makedirs(os.path.join(cfg, "projects", "p"))
    os.makedirs(os.path.join(data, "handoff"))
    now = datetime.now(timezone.utc)
    iso = lambda moment: moment.isoformat(timespec="milliseconds").replace("+00:00", "Z")
    call = dict(asst("rH", OPUS, usage(i=10, r=200_000, o=20), {"type": "text", "text": "ok"}), sessionId="s")
    call["timestamp"] = iso(now - timedelta(minutes=70))
    with open(os.path.join(cfg, "projects", "p", "s.jsonl"), "w", encoding="utf-8") as f:
        f.write(json.dumps(call) + "\n")
    with open(os.path.join(data, "budget.json"), "w", encoding="utf-8") as f:
        json.dump({"cold_resume": {"min_context_tokens": 100000}}, f)
    env = dict(os.environ, CLAUDE_CONFIG_DIR=cfg)
    guard = os.path.join(PLUGIN, "tokentab", "guard.py")

    def check(note_minutes_ago, name="2026-11-24-fix-tests.md"):
        """A note `note_minutes_ago` old under `name`, indexed unless it is the pre-1.15.0 `s.md`."""
        if note_minutes_ago is not None:
            with open(os.path.join(data, "handoff", name), "w", encoding="utf-8") as f:
                f.write(f"<!-- TokenTab handoff, {iso(now - timedelta(minutes=note_minutes_ago))} -->\nnote\n")
            if name != "s.md":
                os.makedirs(os.path.join(data, "handoff", ".index"), exist_ok=True)
                with open(os.path.join(data, "handoff", ".index", "s"), "w", encoding="utf-8") as f:
                    f.write(name + "\n")
        r = subprocess.run([sys.executable, guard, "--cold-check", "s"], env=env, capture_output=True)
        return json.loads(r.stdout or b"null")

    if "cold" not in (check(None) or {}):
        errs.append("handoff touch: a 70-minute-old call with no note should be cold")
    if check(20, "s.md") != {}:
        errs.append("handoff touch: a pre-1.15.0 note written 20 minutes ago should keep the cache warm")
    os.remove(os.path.join(data, "handoff", "s.md"))
    if check(20) != {}:
        errs.append("handoff touch: an indexed note written 20 minutes ago should keep the cache warm")
    older = check(80) or {}
    if "cold" not in older or abs(older.get("started", 0) - (now - timedelta(minutes=70)).timestamp()) > 1:
        errs.append(f"handoff touch: a note older than the call changes nothing, got {older}")
    print("[handoff touch] " + ("OK" if not errs else "FAIL"))
    return errs


if __name__ == "__main__":
    sys.exit(1 if main() else 0)
