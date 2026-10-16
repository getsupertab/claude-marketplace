#!/usr/bin/env python3
"""
sankey.py — the status page as a snapshot file: spending as a Sankey diagram,
Home-Assistant-energy style, the cost-over-time chart, and every figure of keeptabs.py's
report underneath. Renderers only; nothing here serves HTTP.

  sankey.py                 write the snapshot for the most recently active session and
                            print its path (state/status.html in the data folder)
  sankey.py --session ID    pin it to one session
  sankey.py --out FILE      write it somewhere else

Flow, left to right:  session total → model → kind (context / generation / fees) → rate
→ tool → where it went: tools, keeptabs overhead, prompts & replies, context re-reads (the
last two are what no tool or hook accounts for). Every ribbon is built from the per-model
bookkeeping, so hovering a model lights exactly its slices downstream and hovering a tool
exactly the models that paid for it. Plain SVG computed here in Python. Every column but
the session can be hidden (--hide model,kind,...); its flows are summed over it. Colours follow the system's light or dark setting.
"""
import argparse, bisect, html, json, math, os, re, sys, time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import keeptabs as kt  # noqa: E402

W, H, PAD, NODE_W, GAP = 1200, 520, 150, 18, 14
HIT_H = 6        # minimum hover target height in px (invisible)
MIN_PX = 3       # every flow path is drawn at least this wide; labels still show the real value
DIR = "ltr"      # ltr: left→right; ttb: top→bottom; btt: bottom→top (set by --direction)
HIDE = frozenset()   # columns left out of the drawing (keys of COLUMNS; set by body())
MODELS = True        # the model column is drawn (also drops the per-model split in the tools table)

# the columns that can be hidden, in flow order: key, toggle label, what it holds
COLUMNS = [
    ("model", "model", "which model paid"),
    ("kind", "kind", "context · generation · fees"),
    ("rate", "rate", "cache read and write, input, output, thinking"),
    ("tool", "tool", "the tool a slice went to"),
    ("sink", "where it went", "tools · keeptabs overhead · prompts & replies · context re-reads"),
]
ROLES = ("session", "model", "kind", "rate", "tool", "sink")   # a path's nodes; non-tool paths skip "tool"

# HA energy palette: one colour per leaf, parents take a neutral tone.
COLOR = {
    "session":        "#8a94a6",
    "context":        "#4f8fdb",
    "generation":     "#f0a040",
    "fees":           "#9e9e9e",
    "cache_read":     "#22b8a8",
    "cache_write_1h": "#5c6bc0",
    "cache_write_5m": "#7e57c2",
    "input":          "#43a047",
    "output":         "#fb8c00",
    "thinking":       "#ab47bc",
    "fees_leaf":      "#bdbdbd",
}
LABEL = {
    "session": "session", "context": "context", "generation": "generation", "fees": "fees",
    "cache_read": "cache read", "cache_write_1h": "cache write 1h", "cache_write_5m": "cache write 5m",
    "input": "input", "output": "output text", "thinking": "thinking", "fees_leaf": "fees",
}
TREE = {  # parent -> leaves, in display order
    "context": ["cache_read", "cache_write_1h", "cache_write_5m", "input"],
    "generation": ["output", "thinking"],
    "fees": ["fees_leaf"],
}


def size():
    return (W, H) if DIR == "ltr" else (H, W)   # page size for the chosen direction


def flip(u):
    return W - NODE_W - u if DIR == "btt" else u


def P(u, v):
    """flow coords -> page coords"""
    return (u, v) if DIR == "ltr" else (v, u)


def ribbon(u0, v0, h0, u1, v1, h1, color, bend_to=None):
    """Curve from the source to the target. A ribbon that skips columns curves as if its
    target sat in the next column (bend_to = that column's u) and then runs straight on,
    the way Home Assistant draws its 'not measured' band. color may be a url(#gradient)."""
    u0, u1 = flip(u0), flip(u1)
    ub = flip(bend_to) if bend_to is not None else u1
    um = (u0 + ub) / 2
    f = lambda q: f"{q[0]:.1f},{q[1]:.1f}"
    top = f"M{f(P(u0, v0))} C{f(P(um, v0))} {f(P(um, v1))} {f(P(ub, v1))} L{f(P(u1, v1))}"
    bottom = (f"L{f(P(u1, v1 + h1))} L{f(P(ub, v1 + h1))} C{f(P(um, v1 + h1))} {f(P(um, v0 + h0))} "
              f"{f(P(u0, v0 + h0))} Z")
    return f'<path d="{top} {bottom}" fill="{color}" fill-opacity="0.45"/>'


def node(u, v, h, color, label, value, right):
    u = flip(u)
    fs = max(7, min(13, h + 1))   # font follows the node height, down to a readable floor
    text = f'<tspan font-size="{fs:.0f}">{html.escape(label)} <tspan class="v">{kt.money(value)}</tspan></tspan>'
    if DIR == "ltr":
        tx = u + NODE_W + 8 if right else u - 8
        return (f'<rect x="{u}" y="{v:.1f}" width="{NODE_W}" height="{h:.1f}" fill="{color}"/>'
                f'<text x="{tx}" y="{v + h / 2:.1f}" dominant-baseline="middle" text-anchor="{"start" if right else "end"}">{text}</text>')
    # vertical: bars are horizontal; label centred under the bar, above it for the first/last row
    after = right if DIR == "ttb" else not right
    ty = u + NODE_W + 14 if after else u - 8
    return (f'<rect x="{v:.1f}" y="{u}" width="{h:.1f}" height="{NODE_W}" fill="{color}"/>'
            f'<text x="{v + h / 2:.1f}" y="{ty:.1f}" text-anchor="middle">{text}</text>')


OVERHEAD, PROMPTS, REREADS = "keeptabs overhead", "prompts & replies", "context re-reads"
COLOR.update({OVERHEAD: "#e57373", PROMPTS: "#5c6f82", REREADS: "#3f7f7a"})
LABEL.update({k: k for k in (OVERHEAD, PROMPTS, REREADS)})

TOOL_COLORS = ["#26a69a", "#ec407a", "#ffca28", "#42a5f5", "#8d6e63", "#66bb6a", "#ff7043", "#78909c"]
MODEL_COLORS = ["#d4a63a", "#4db6ac", "#ba68c8", "#7cb342", "#f06292", "#90a4ae"]
# fixed display order for the named nodes; models and tools sort by cost inside their column
RANK = {n: i for i, n in enumerate(["session"] + list(TREE) + [l for ls in TREE.values() for l in ls]
                                   + ["tools", OVERHEAD, PROMPTS, REREADS])}


def short_model(mid):
    """claude-haiku-4-5-20251001 -> haiku 4.5, claude-fable-5-1 -> fable 5.1"""
    s = re.sub(r"^claude-", "", mid or "?")
    s = re.sub(r"-\d{8}$", "", s)
    s = re.sub(r"(?<=\d)-(?=\d)", ".", s)
    return s.replace("-", " ")


def flows(b, tb=None):
    """Root-to-sink paths [(nodes, value)] read straight from the per-model bookkeeping:
    cost_bm gives each model's spend per rate, tools[name][model] and overhead[model] what
    each tool (and keeptabs' own hook text) draws from it (TOOL_DRAWS). Links and columns
    are sums of these paths, so no mixing is assumed anywhere. What no tool or hook accounts
    for goes straight past the tool column: cache read to "context re-reads", the rest to
    "prompts & replies". The min() only guards a transcript bucket against a ledger one
    with fewer calls; it runs per model and rate, and the residual per model goes to the
    sink. tb: the transcript bucket, which holds the tool detail when b comes from the ledger."""
    src = tb or b
    parent = {l: p for p, ls in TREE.items() for l in ls}
    tools = kt.tool_totals(src.tools)
    draws = [(name, m, f) for name, _ in tools for m, f in src.tools[name].items()]
    draws += [(OVERHEAD, m, f) for m, f in src.overhead.items()]
    models = sorted(b.cost_bm, key=lambda m: -sum(b.cost_bm[m].values()))
    out = []
    for m in models:
        for rate, leaf in b.cost_bm[m].items():
            if leaf <= 0:
                continue
            l = "fees_leaf" if rate == "fees" else rate
            head = ("session", m, parent[l], l)
            taken = 0.0
            for name, mm, f in draws:
                if mm != m:
                    continue
                for field, r in kt.TOOL_DRAWS.items():
                    if r == rate and f.get(field, 0) > 0:
                        v = min(f[field], leaf - taken)
                        if v > 0:   # keeps the per-tool split by rate visible
                            out.append((head + ((OVERHEAD,) if name == OVERHEAD else (name, "tools")), v))
                            taken += v
            if leaf - taken > 0:
                out.append((head + (REREADS if rate == "cache_read" else PROMPTS,), leaf - taken))
    if HIDE:   # sum the paths over the hidden columns' nodes
        merged = {}
        for nodes, v in out:
            k = visible(nodes)
            merged[k] = merged.get(k, 0.0) + v
        out = list(merged.items())
    colors, labels = dict(COLOR), dict(LABEL)
    colors["tools"], labels["tools"] = "#f5b041", "tools"
    for i, (name, _) in enumerate(tools):
        colors[name] = TOOL_COLORS[i % len(TOOL_COLORS)]; labels[name] = name
    for i, m in enumerate(models):
        colors[m] = MODEL_COLORS[i % len(MODEL_COLORS)]; labels[m] = short_model(m)
    return out, colors, labels


def visible(nodes):
    """A path without the nodes of hidden columns. Every path keeps its sink, so hiding
    "where it went" only drops the "tools" node behind the tools; with the tool column
    hidden too, "tools" stays as the tool paths' sink."""
    roles = ROLES[:len(nodes) - 1] + ("sink",)
    keep = []
    for n, r in zip(nodes, roles):
        if r == "sink" and not (n == "tools" and "sink" in HIDE and "tool" not in HIDE) \
                or r != "sink" and r not in HIDE:
            keep.append(n)
    return tuple(keep)


def columns(paths):
    """Columns of {node: value} and links (src, dst, value) as sums of the paths. The last
    node of a path is a sink and sits in the last column, whatever columns it skipped."""
    depth = max(len(nodes) for nodes, _ in paths)
    cols = [{} for _ in range(depth)]
    links = {}
    for nodes, v in paths:
        for j, n in enumerate(nodes):
            c = cols[depth - 1 if j == len(nodes) - 1 else j]
            c[n] = c.get(n, 0.0) + v
        for a, d in zip(nodes, nodes[1:]):
            links[(a, d)] = links.get((a, d), 0.0) + v
    cols = [dict(sorted(c.items(), key=lambda kv: (RANK.get(kv[0], 0), -kv[1]))) for c in cols if c]
    return cols, [(a, d, v) for (a, d), v in links.items()]


def graph(b, tb=None):
    """Columns of {node: value} plus links (src, dst, value); every column sums to the total."""
    paths, colors, labels = flows(b, tb)
    cols, links = columns(paths)
    return cols, links, colors, labels


def layout(cols, links, rpad=PAD):
    """Home-Assistant style: node height is strictly value * scale, so every column's nodes
    add up to the same pixel height as the session node; gaps are added on top and each
    column is centred, so columns with many nodes overhang the others. Returns link widths
    and name -> (u, start, length) in flow coords. rpad: room after the last column."""
    across = H if DIR == "ltr" else W
    usable = across - 2 * 40
    total = sum(cols[0].values()) or 1
    colidx = {n: ci for ci, col in enumerate(cols) for n in col}
    slots = [len(c) + len({d for s_, d, _ in links if colidx[s_] < ci < colidx[d]}) for ci, c in enumerate(cols)]
    scale = (usable - GAP * (max(slots) - 1)) / total   # pass-through slots need gaps too
    w = [v * scale for _, _, v in links]
    xs = [PAD + i * (W - PAD - rpad - NODE_W) / (len(cols) - 1) for i in range(len(cols))]
    pos = {}
    for ci, (col, x) in enumerate(zip(cols, xs)):
        # a node whose ribbon skips this column gets a pass-through slot at the bottom, as if it
        # sat here (HA's "not measured" band); it then keeps that height in its own column
        through = {}
        for src, dst, v in links:
            if colidx[src] < ci < colidx[dst]:
                through[dst] = through.get(dst, 0) + v
        items = list(col.items()) + list(through.items())
        fixed = {n: pos[n] for n, _ in items if n in pos}
        free = [(n, v) for n, v in items if n not in pos]
        extent = sum(v for _, v in free) * scale + GAP * (len(free) - 1)
        if sum(v for _, v in items) >= total * 0.999 and not fixed:
            y = 40 + (usable - extent) / 2
        else:
            # partial column: centre on what it feeds, else what feeds it; stay clear of fixed slots
            near = [n for n in {d for s_, d, _ in links if s_ in col} if n in pos and n not in fixed] \
                or [n for n in {s_ for s_, d, _ in links if d in col} if n in pos]
            mid = sum(pos[n][1] + pos[n][2] / 2 for n in near) / len(near) if near else 40 + usable / 2
            top = min((p[1] for p in fixed.values()), default=40 + usable + GAP) - GAP
            y = min(max(mid - extent / 2, 40), top - extent)
        for n, v in free:
            pos[n] = (x, y, v * scale); y += v * scale + GAP
        for n, (_, fy, fh) in fixed.items():
            pos[n] = (x, fy, fh)
    return w, pos


def path_order(all_paths, links, val, pos):
    """Static order of the paths through every node: nodes with several outgoing ribbons
    sort by where the path goes next (so outgoing slices stay contiguous), the others by
    where it came from (so incoming slices do). Returns node -> [(path index, offset)]."""
    ypos = lambda n: pos[n][1]
    nouts = {n: sum(1 for s_, _, _ in links if s_ == n) for n in val}
    order = {}
    for x in val:
        through = [(k, p[0].index(x)) for k, p in enumerate(all_paths) if x in p[0]]

        def key(kp):
            k, j = kp
            nodes = all_paths[k][0]
            after = tuple(ypos(n) for n in nodes[j + 1:])
            before = tuple(ypos(n) for n in reversed(nodes[:j]))
            return (after, before) if nouts[x] > 1 else (before, after)
        off, rows = 0.0, []
        for k, _ in sorted(through, key=key):
            rows.append((k, off)); off += all_paths[k][2]
        order[x] = rows
    return order


def runs(seq, hit):
    """Group consecutive hit items of seq into (first item, total) runs. An item is an
    offset or a tuple of offsets (one per ribbon end); a run only continues while the
    next item starts exactly where the run ends, at every end, so slices that other links'
    paths sit between stay separate instead of being drawn as one band over them."""
    out, cur, end = [], None, None
    for item, v, is_hit in seq:
        offs = item if isinstance(item, tuple) else (item,)
        if is_hit:
            if cur is not None and any(abs(o - e) > 1e-9 for o, e in zip(offs, end)):
                out.append(tuple(cur)); cur = None
            if cur is None:
                cur = [item, 0.0]; end = offs
            cur[1] += v; end = tuple(o + v for o in end)
        elif cur:
            out.append(tuple(cur)); cur = None
    if cur:
        out.append(tuple(cur))
    return out


def bar(u, v, h, color, cls=""):
    u = flip(u)
    if DIR == "ltr":
        return f'<rect class="{cls}" x="{u}" y="{v:.1f}" width="{NODE_W}" height="{h:.1f}" fill="{color}"/>'
    return f'<rect class="{cls}" x="{v:.1f}" y="{u}" width="{h:.1f}" height="{NODE_W}" fill="{color}"/>'


def svg(b, tb=None):
    all_paths, colors, labels = flows(b, tb)
    cols, links = columns(all_paths)
    true_val = {n: v for col in cols for n, v in col.items()}   # labels show these
    # drawing values: every path is at least MIN_PX wide, so hairlines stay visible; node and
    # ribbon sizes are the sums of their paths, so the bump carries all the way back to the
    # session node and every slice still adds up
    # room for the last column's labels (left to right only: elsewhere they sit under the
    # bars), estimated at 0.6em a character at the largest label size, so none is cut off
    rpad = max(PAD, 12 + 13 * 0.6 * max(len(f"{labels[n]} {kt.money(v)}") for n, v in cols[-1].items())) \
        if DIR == "ltr" else PAD
    w0, _ = layout(cols, links, rpad)
    floor = MIN_PX / (w0[0] / links[0][2])
    lid = {(s_, d): i for i, (s_, d, _) in enumerate(links)}
    all_paths = [(n, tuple(lid[e] for e in zip(n, n[1:])), max(v, floor)) for n, v in all_paths]
    links = [(s_, d, sum(v for _, idx, v in all_paths if i in idx)) for i, (s_, d, _) in enumerate(links)]
    cols = [{n: sum(v for nodes, _, v in all_paths if n in nodes) for n in col} for col in cols]
    w, pos = layout(cols, links, rpad)
    val = {n: v for col in cols for n, v in col.items()}
    nid = {n: f"n{k}" for k, n in enumerate(val)}
    scale = w[0] / links[0][2]

    # ribbons: slice each node in link order, sorted by the other end's position to limit crossings
    out_off = {n: 0.0 for n in pos}; in_off = {n: 0.0 for n in pos}
    order = sorted(range(len(links)), key=lambda i: (pos[links[i][0]][1], pos[links[i][1]][1]))
    axis = 'x2="1"' if DIR == "ltr" else ('y2="1"' if DIR == "ttb" else 'y1="1" y2="0"')
    defs = "".join(f'<linearGradient id="g{i}" {axis}><stop offset="0" stop-color="{colors[s]}"/>'
                   f'<stop offset="1" stop-color="{colors[d]}"/></linearGradient>'
                   for i, (s, d, _) in enumerate(links))
    colidx = {n: ci for ci, col in enumerate(cols) for n in col}
    xs = sorted({pos[n][0] for n in pos})
    geom = {}
    parts = []
    for i in order:
        src, dst, _ = links[i]
        su, sv, _ = pos[src]; du, dv, _ = pos[dst]
        skips = colidx[dst] > colidx[src] + 1
        # tuck 1px under each node so no background seam shows at the joins
        geom[i] = (su + NODE_W - 1, sv + out_off[src], du + 1, dv + in_off[dst], xs[colidx[src] + 1] if skips else None)
        # an invisible 6px-stroked copy makes hairline ribbons hoverable without changing the drawing
        rb = ribbon(geom[i][0], geom[i][1], w[i], geom[i][2], geom[i][3], w[i], f"url(#g{i})", bend_to=geom[i][4])
        hit = rb.replace(f'fill="url(#g{i})" fill-opacity="0.45"', 'class="h" fill="none" stroke="#000" stroke-opacity="0" stroke-width="6" pointer-events="stroke"')
        parts.append(f'<g id="l{i}">{rb}{hit}</g>')
        out_off[src] += w[i]; in_off[dst] += w[i]
    # bars go under the trace layers, labels above everything; a label is a hover target too
    texts = []
    for ci, col in enumerate(cols):
        for n in col:
            u, v, h = pos[n]
            rect, text = node(u, v, h, colors[n], labels[n], true_val[n], right=ci > 0).split("<text", 1)
            hh = max(h, HIT_H)   # transparent hit box so hairline nodes can be hovered
            hit = bar(u, v + h / 2 - hh / 2, hh, "none", "h").replace('<rect class="h"', '<rect class="h" pointer-events="all"')
            parts.append(f'<g id="{nid[n]}">{rect}{hit}</g>')
            texts.append(f'<g id="k{nid[n][1:]}"><text{text}</g>')

    # hover tracing without JS: the flow is split into source-to-sink paths with a fixed
    # position inside every node, so a hover lights the same places every time. For each
    # target a hidden layer holds the runs of paths through it: bars inside the nodes and
    # sub-ribbons between them, ordered the same at both ends so they stay connected.
    order = path_order(all_paths, links, val, pos)
    targets = {nid[n]: (lambda n: lambda k: n in all_paths[k][0])(n) for n in val}
    targets.update({f"l{i}": (lambda i: lambda k: i in all_paths[k][1])(i) for i in range(len(links))})
    layers = []
    for h, hit in targets.items():
        items = []
        for x in val:
            u, top, _ = pos[x]
            for off, v in runs([(o, all_paths[k][2], hit(k)) for k, o in order[x]], hit):
                items.append(bar(u, top + off * scale, v * scale, colors[x], "t"))
        for i, (src, dst, _) in enumerate(links):
            soff = dict(order[src]); doff = dict(order[dst])
            seq = [((soff[k], doff[k]), all_paths[k][2], hit(k)) for k, _ in order[dst] if i in all_paths[k][1]]
            for (so, do), v in runs(seq, hit):
                items.append(ribbon(geom[i][0], pos[src][1] + so * scale, v * scale, geom[i][2], pos[dst][1] + do * scale,
                                    v * scale, f"url(#g{i})", bend_to=geom[i][4]).replace("<path ", '<path class="t" ', 1))
        layers.append(f'<g id="t{h}" class="tl">{"".join(items)}</g>')
    all_traces = targets
    style = ("path{transition:fill-opacity .15s}rect{transition:opacity .15s}"
             ".tl{display:none;pointer-events:none}.t{fill-opacity:.9}"
             + "".join(f"svg:has({sel}) path:not(.t){{fill-opacity:.12}}svg:has({sel}) rect:not(.t){{opacity:.3}}"
                       f"svg:has({sel}) #t{h}{{display:block}}"
                       for h in all_traces for sel in [f"#{h}:hover" + (f",#k{h[1:]}:hover" if h.startswith("n") else "")]))
    parts.insert(0, f"<style>{style}</style><defs>{defs}</defs>")
    pw, ph = size()
    return f'<svg viewBox="0 0 {pw} {ph}" width="100%" xmlns="http://www.w3.org/2000/svg">{"".join(parts + layers + texts)}</svg>'


def tools_table(R):
    """Every tool, with the same split the terminal shows (ask · answer into context ·
    re-reads) and, with several models, what each model paid for it."""
    b = R["transcript"]
    rows = R["tools"]
    if not rows:
        return ""
    models = sorted({m for by in b.tools.values() for m in by}, key=lambda m: -sum(b.cost_bm.get(m, {}).values()))
    if len(models) < 2 or not MODELS:
        models = []   # a single model adds nothing to the cost column
    per = lambda name, m: sum(b.tools[name].get(m, {}).get(f, 0.0) for f in kt.TOOL_DRAWS)
    th = "".join(f'<th class="n">{esc(short_model(m))}</th>' for m in models)
    tr = "".join(f'<tr><td>{esc(k)}</td><td class="n">{v["calls"]}</td><td class="n">{kt.money(v["cost"])}</td>'
                 + "".join(f'<td class="n">{kt.money(per(k, m))}</td>' for m in models)
                 + f'<td class="n">{kt.money(v["ask"] + v["think"])}</td><td class="n">{kt.money(v["result"])}</td>'
                   f'<td class="n m">{kt.num(v["rtokens"])}</td><td class="n">{kt.money(v["read"])}</td></tr>'
                 for k, v in rows)
    o = R["overhead"]
    if o["cost"]:
        tr += (f'<tr class="m"><td>keeptabs overhead</td><td></td><td class="n">{kt.money(o["cost"])}</td>'
               + "<td></td>" * len(models) + f'<td></td><td class="n">{kt.money(o["cost"] - o["read"])}</td>'
               f'<td class="n">{kt.num(o["rtokens"])}</td><td class="n">{kt.money(o["read"])}</td></tr>')
    return (f'<section class="wide"><h2>tools <span>asking + answer into context + re-reading it (estimates)</span></h2>'
            f'<table><tr><th>tool</th><th class="n">calls</th><th class="n">cost</th>{th}<th class="n">ask</th>'
            f'<th class="n">into context</th><th class="n">tok</th><th class="n">re-read</th></tr>{tr}</table></section>')


esc = html.escape


def meter(frac):
    level = "ok" if frac < 0.7 else ("warn" if frac < 1.0 else "over")
    return (f'<span class="meter {level}"><i style="width:{min(frac, 1) * 100:.1f}%"></i></span>'
            f'<span class="pct {level}">{frac * 100:.0f}%</span>')


def status_html(R):
    """The terminal's live view (keeptabs.py render()) as panels: same report, same numbers."""
    out = []
    t = R["total"]
    rows = "".join(f'<tr><td>{esc(r["label"])}</td><td class="n">{kt.num(r["tokens"])}</td>'
                   f'<td class="n">{kt.money(r["cost"])}</td><td class="n m">{r["share"] * 100:.0f}%</td></tr>'
                   for r in R["rates"])
    if R["unmatched"]:
        rows += (f'<tr class="m"><td colspan="4">{kt.num(R["unmatched"])} output tok without a transcript line: '
                 'any thinking is in output text</td></tr>')
    rows += (f'<tr class="tot"><td>total</td><td class="n">{kt.num(t["tokens"])}</td>'
             f'<td class="n">{kt.money(t["cost"])}</td><td class="n m">{t["msgs"]} msgs</td></tr>')
    out.append(f'<section><h2>by rate</h2><table>{rows}</table></section>')

    q = R["requests"]
    if q:
        rows = ""
        for label in ("latest", "previous"):
            r = q[label]
            if r:
                at = time.strftime("%H:%M", time.localtime(r[0]))
                rows += (f'<tr><td>{label}</td><td class="n">{kt.money(r[1])}</td>'
                         f'<td class="m">{r[2]} calls · {at}</td></tr>')
        rows += (f'<tr class="m"><td>average</td><td class="n">{kt.money(q["average"])}</td>'
                 f'<td>of {q["count"]} requests</td></tr>')
        out.append(f'<section><h2>requests <span>one prompt and everything it set off</span></h2><table>{rows}</table></section>')

    if R["by_source"]:
        rows = "".join(f'<tr><td>{esc(nm)}</td><td class="n">{calls}</td><td class="m">calls</td>'
                       f'<td class="n">{kt.money(c)}</td><td class="n m">{share * 100:.0f}%</td></tr>'
                       for nm, calls, c, share in R["by_source"])
        out.append(f'<section><h2>by call source <span>what in Claude Code made the call</span></h2><table>{rows}</table></section>')

    bg = R["budget"]
    if bg and (bg["lines"] or bg["request"] or bg["check_in"]):
        rows = "".join(f'<tr><td>{esc(r["name"])}</td><td>{meter(r["frac"])}</td>'
                       f'<td class="m">{esc(" · ".join(r["parts"]))}'
                       + (' <b class="over">OVER</b>' if r["frac"] >= 1 else "")
                       + (f' · {esc(r["note"])}' if r["note"] else "") + "</td></tr>"
                       for r in bg["lines"])
        if bg["check_in"]:
            rows += '<tr><td>check-in</td><td colspan="2" class="warn">paused: waiting for your budget reply</td></tr>'
        c = bg["context"]
        if c:
            cache = (f'<span class="over">cache COLD</span>: next prompt re-writes it, ~{kt.money(c["rewrite"])}'
                     if c["cold"] else f'cache warm, {c["minutes_left"]}m left')
            rows += (f'<tr><td>context</td><td class="{c["level"]}">{kt.num(c["tokens"])} tok</td>'
                     f'<td class="m">{kt.money(c["per_call"])}/call to re-read · {cache}</td></tr>')
        if bg["request"]:
            rows += f'<tr class="m"><td>request</td><td colspan="2">{esc(bg["request"])}, warned in the chat by guard.py</td></tr>'
        mode = "hard stop" if bg["hard_stop"] else "warn only"
        out.append(f'<section class="wrap"><h2>budget <span>{mode} · warns at {bg["warn_at"]}%</span></h2><table>{rows}</table></section>')

    w, tt = R["window"], R["totals"]
    rows = (f'<tr><td>all sessions · last 5 min</td><td class="n">{kt.money(w["5m"])}</td></tr>'
            f'<tr><td>all sessions · last hour</td><td class="n">{kt.money(w["1h"])}</td></tr>')
    if tt:
        rows += (f'<tr><td>today, all sessions</td><td class="n">{kt.money(tt["today"])}</td></tr>'
                 f'<tr><td>all sessions, all time</td><td class="n">{kt.money(tt["all"])}</td>'
                 f'<td class="m">{tt["sessions"]} sessions</td></tr>')
    out.append(f'<section><h2>totals</h2><table>{rows}</table></section>')
    return f'<div class="grid">{"".join(out)}{tools_table(R)}</div>'


def report(live, sid=None):
    """kt.report() for one session from a Live's stores (rescanned first). Without sid:
    the most recently active session."""
    live.scan()
    sid = sid or live.store.newest_session()
    try:
        budget = json.load(open(kt.BUDGET_PATH))
    except (OSError, ValueError):
        budget = {}
    return kt.report(live.store, sid, live.prices, budget, True, live.led)


class Live:
    """Transcript and ledger stores kept between renders: they tail their files, so a
    caller writing snapshots in a loop only reads what was appended since the last one."""
    def __init__(self):
        self.prices = kt.load_prices()
        self.store, self.led = kt.Store(self.prices), kt.LedgerStore(self.prices)

    def scan(self):
        self.store.scan(); self.led.scan()


# Cost over time: one cumulative line per rate, start of the session on the left. Two
# x-axes, both drawn and switched on the page (no round trip): wall-clock time, where
# idle stretches show as flat runs, and call number, where every call gets equal width.
CW, CH = 1200, 300                     # viewBox
CL, CR, CT, CB = 70, 190, 14, 30       # margins: y labels, line-end labels, top, x labels
LIVE_EDGE = 6 * 3600                   # stretch the time axis to now if the session is this fresh


def nice(top, ticks=4):
    """A round step and axis maximum covering top with about `ticks` gridlines."""
    if top <= 0:
        return 1.0, 1.0
    step = 10 ** math.floor(math.log10(top / ticks))
    for m in (1, 2, 2.5, 5, 10):
        if step * m * ticks >= top:
            step *= m
            break
    return step, step * math.ceil(top / step - 1e-9)


def axis_money(v, step):
    return f"${v:,.0f}" if step >= 1 else (f"${v:,.2f}" if step >= 0.01 else f"${v:,.4f}")


def time_ticks(t0, t1):
    span = t1 - t0
    step = next((s for s in (60, 120, 300, 600, 900, 1800, 3600, 7200, 10800, 21600, 43200, 86400)
                 if span / s <= 6), 86400 * math.ceil(span / 86400 / 6))
    off = time.localtime(t0).tm_gmtoff
    t = t0 - (t0 + off) % step + step
    fmt = "%d %b %H:%M" if span > 86400 else "%H:%M"
    out = []
    while t < t1:
        out.append((t, time.strftime(fmt, time.localtime(t))))
        t += step
    return out


def timeline(R):
    """The cost-over-time panel: both x-axes, the toggle, prompt markers."""
    b = R["bucket"]
    pts = sorted(b.series, key=lambda e: e[0])
    keys = [r["key"] for r in R["rates"]]
    if not pts or not keys:
        return ""
    n = len(pts)
    cum, run = {k: [] for k in keys}, dict.fromkeys(keys, 0.0)
    for e in pts:
        for k in keys:
            run[k] += e[1].get(k, 0.0)
            cum[k].append(run[k])
    step, ymax = nice(max(run.values()))
    pw, ph = CW - CL - CR, CH - CT - CB
    Y = lambda v: CT + ph * (1 - v / ymax)

    prompts = sorted(p for p in R.get("prompts", []) if p >= pts[0][0] - 600)
    reqs = kt.requests(b.calls, prompts)
    t0 = min([pts[0][0]] + prompts[:1])
    t1 = pts[-1][0]
    if time.time() - t1 < LIVE_EDGE:
        t1 = time.time()                 # right edge is now: flat lines show the idle time
    t1 = max(t1, t0 + 60)
    times = [e[0] for e in pts]

    def draw(mode):
        if mode == "time":
            X = lambda t: CL + pw * (t - t0) / (t1 - t0)
            xs = [X(t) for t in times]
            xticks = [(X(t), lab) for t, lab in time_ticks(t0, t1)]
            marks = [(X(p), r) for p, r in zip(prompts, reqs)]
        else:
            X = lambda i: CL + pw * i / n
            xs = [X(i + 1) for i in range(n)]
            cstep, _ = nice(n, 6)
            cstep = max(1, int(cstep))
            xticks = [(X(i), f"{i}") for i in range(cstep, n + 1, cstep)]
            marks = [(X(bisect.bisect_left(times, p)), r) for p, r in zip(prompts, reqs)]
        parts = []
        for i in range(1, round(ymax / step) + 1):     # gridlines and money labels
            y = Y(i * step)
            parts.append(f'<line class="g" x1="{CL}" x2="{CL + pw}" y1="{y:.1f}" y2="{y:.1f}"/>'
                         f'<text class="v" x="{CL - 8}" y="{y + 4:.1f}" text-anchor="end">{axis_money(i * step, step)}</text>')
        parts.append(f'<line class="a" x1="{CL}" x2="{CL + pw}" y1="{CT + ph}" y2="{CT + ph}"/>'
                     f'<text class="v" x="{CL - 8}" y="{CT + ph + 4}" text-anchor="end">$0</text>')
        for x, lab in xticks:
            parts.append(f'<line class="a" x1="{x:.1f}" x2="{x:.1f}" y1="{CT + ph}" y2="{CT + ph + 4}"/>'
                         f'<text class="v" x="{x:.1f}" y="{CH - 8}" text-anchor="middle">{esc(lab)}</text>')
        if mode == "calls":
            parts.append(f'<text class="v" x="{CL + pw}" y="{CH - 8}" text-anchor="start" dx="8">calls</text>')
        for x, (p, usd, nc) in marks:     # one marker per prompt, the request's cost on hover
            tip = f"prompt {time.strftime('%H:%M', time.localtime(p))} · {kt.money(usd)} · {nc} calls"
            parts.append(f'<g class="pm"><line x1="{x:.1f}" x2="{x:.1f}" y1="{CT}" y2="{CT + ph}"/>'
                         f'<line class="hit" x1="{x:.1f}" x2="{x:.1f}" y1="{CT}" y2="{CT + ph}"><title>{esc(tip)}</title></line></g>')
        ends = []
        for k in keys:
            # One vertex per pixel column at most (its last value): a long session stays a
            # small page. Time steps (flat until the call, then up); calls slope.
            cols = {}
            for x, v in zip(xs, cum[k]):
                cols.setdefault(round(x), [x, v])[1] = v
            seg_ = "H{:.1f}V{:.1f}" if mode == "time" else "L{:.1f},{:.1f}"
            d = [f"M{CL},{Y(0):.1f}"] + [seg_.format(x, Y(v)) for x, v in cols.values()]
            if mode == "time":
                d.append(f"H{CL + pw}")
            c = COLOR["fees_leaf" if k == "fees" else k]
            tip = f'{LABEL.get(k, k) if k != "fees" else "fees (web search)"} · {kt.money(run[k])}'
            parts.append(f'<path class="ln" d="{"".join(d)}" stroke="{c}" fill="none" stroke-width="2"><title>{esc(tip)}</title></path>')
            ends.append([Y(run[k]), k, c])
        ends.sort()                       # end labels, pushed apart so none overlap,
        for i in range(1, len(ends)):     # then back up from the axis: only a crowded
            ends[i][0] = max(ends[i][0], ends[i - 1][0] + 16)   # bottom moves, not the top
        ends[-1][0] = min(ends[-1][0], CT + ph)
        for i in range(len(ends) - 2, -1, -1):
            ends[i][0] = min(ends[i][0], ends[i + 1][0] - 16)
        for y, k, c in ends:
            lab = "fees" if k == "fees" else LABEL.get(k, k)
            parts.append(f'<circle cx="{CL + pw + 8}" cy="{y - 4:.1f}" r="4" fill="{c}"/>'
                         f'<text x="{CL + pw + 18}" y="{y:.1f}">{esc(lab)} <tspan class="v">{kt.money(run[k])}</tspan></text>')
        return (f'<svg class="x{mode[0]}" viewBox="0 0 {CW} {CH}" width="100%" '
                f'xmlns="http://www.w3.org/2000/svg">{"".join(parts)}</svg>')

    seg = ('<span class="seg"><button data-x="time" onclick="kx(\'time\')">time</button>'
           '<button data-x="calls" onclick="kx(\'calls\')">calls</button></span>')
    return (f'<section class="chart"><h2>cost over time <span>cumulative by rate · '
            f'dashed: your prompts</span>{seg}</h2>{draw("time")}{draw("calls")}</section>')


def body(R, direction="ltr", models=True, hide=()):
    """Everything below the page title, as one HTML fragment. hide: columns to leave out
    (models=False hides "model" too)."""
    global DIR, MODELS, HIDE
    DIR = direction
    HIDE = frozenset(hide) & {k for k, _, _ in COLUMNS} | (set() if models else {"model"})
    MODELS = "model" not in HIDE
    s, b, tb = R["session"], R["bucket"], R["transcript"]
    if not s:
        head = f'<div class="sub">session {esc(R["sid"] or "?")} · waiting for activity…</div>'
        return head
    since = s["since"].strftime("%H:%M") if s["since"] else "?"
    name = f'<b title="{esc(R["sid"])}">{esc(s["title"])}</b>' if s["title"] else f'session {esc(R["sid"][:8])}'
    head = (f'<div class="sub">{name} · {esc(", ".join(s["models"]))} · since {since}'
            f' · updated {R["now"].strftime("%H:%M:%S")}</div>'
            f'<div class="src">{esc(s["source"])}</div>'
            + ("" if R["prices_verified"] else '<div class="src warn">prices unverified</div>'))
    t = R["total"]
    kpi = (f'<div class="kpi"><div><b>{kt.money(t["cost"])}</b><span>this session</span></div>'
           f'<div><b>{kt.num(t["tokens"])}</b><span>tokens</span></div>'
           f'<div><b>{t["msgs"]}</b><span>messages</span></div></div>')
    flow = svg(b, tb) if b and b.cost > 0 else "<p>no usage for this session yet.</p>"
    return head + kpi + flow + timeline(R) + status_html(R)


CSS = """
:root{color-scheme:light dark;--bg:#f6f7f9;--fg:#1d2330;--muted:#5d6676;--line:#dde1e7;--panel:#fff;
 --ok:#2e7d32;--warn:#a86b00;--over:#c62828;--track:#e6e9ee}
@media (prefers-color-scheme:dark){:root{--bg:#111318;--fg:#e3e6ec;--muted:#9aa3b2;--line:#262a33;--panel:#181b22;
 --ok:#66bb6a;--warn:#ffca28;--over:#ef5350;--track:#2a2f39}}
body{margin:0;padding:24px;background:var(--bg);color:var(--fg);font:14px/1.4 -apple-system,system-ui,sans-serif}
h1{font-size:18px;font-weight:600;margin:0 0 4px;display:flex;align-items:center;gap:10px}
h2{font-size:13px;margin:0 0 8px;color:var(--muted);font-weight:600;text-transform:uppercase;letter-spacing:.04em}
h2 span{text-transform:none;letter-spacing:0;font-weight:400}
.sub{color:var(--muted);margin-bottom:6px}.sub b{color:var(--fg);font-weight:600}
.src{font-size:13px;margin-bottom:4px}.src.ok{color:var(--ok)}.src.warn,.warn{color:var(--warn)}.over{color:var(--over)}
.kpi{display:flex;gap:28px;margin:14px 0 4px}.kpi b{font-size:22px;font-weight:600;font-variant-numeric:tabular-nums;display:block}
.kpi span{color:var(--muted);font-size:12px}
svg{max-width:SVGWpx;display:block}svg text{fill:var(--fg);font-size:13px}svg .v{fill:var(--muted)}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(340px,1fr));gap:16px;margin-top:20px;max-width:SVGWpx}
section{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:12px 14px;overflow-x:auto}
section.wide{grid-column:1/-1}.wrap td:last-child{white-space:normal}
table{border-collapse:collapse;width:100%}td,th{text-align:left;padding:3px 12px 3px 0;white-space:nowrap}
th{color:var(--muted);font-weight:500}.n{text-align:right;font-variant-numeric:tabular-nums}.m,.m td{color:var(--muted)}
tr.tot td{border-top:1px solid var(--line);font-weight:600;padding-top:6px}
.meter{display:inline-block;width:120px;height:8px;border-radius:4px;background:var(--track);vertical-align:middle;overflow:hidden}
.meter i{display:block;height:100%;background:var(--ok)}.meter.warn i{background:var(--warn)}.meter.over i{background:var(--over)}
.pct{display:inline-block;width:44px;text-align:right;font-variant-numeric:tabular-nums}.pct.ok{color:var(--fg)}
.dot{font-size:12px;font-weight:400;color:var(--muted);display:inline-flex;align-items:center;gap:6px}
.dot::before{content:"";width:8px;height:8px;border-radius:50%;background:var(--muted)}
#pick{font:inherit;font-size:13px;color:var(--fg);background:var(--panel);border:1px solid var(--line);
 border-radius:6px;padding:3px 6px;margin:4px 0 6px;max-width:100%}
.dot.on::before{background:var(--ok)}.dot.off::before{background:var(--over)}
section.chart{margin-top:20px;max-width:SVGWpx}section.chart h2{display:flex;align-items:center;gap:8px}
.seg{margin-left:auto;display:inline-flex;border:1px solid var(--line);border-radius:6px;overflow:hidden}
.seg button{font:inherit;font-size:12px;text-transform:none;letter-spacing:0;color:var(--muted);background:none;
 border:0;padding:2px 10px;cursor:pointer}
html:not([data-x=calls]) .seg [data-x=time],html[data-x=calls] .seg [data-x=calls]{background:var(--track);color:var(--fg)}
html:not([data-x=calls]) svg.xc,html[data-x=calls] svg.xt{display:none}
.chart .g{stroke:var(--line)}.chart .a{stroke:var(--muted);stroke-opacity:.5}
.chart .ln{fill:none;stroke-width:2;stroke-linejoin:round}.chart .ln:hover{stroke-width:3.5}
.pm line{stroke:var(--muted);stroke-opacity:.35;stroke-dasharray:3 4}.pm .hit{stroke:#000;stroke-opacity:0;stroke-width:9;stroke-dasharray:none}
.pm:hover line:first-child{stroke-opacity:.9}
.cols{display:flex;flex-wrap:wrap;align-items:center;gap:6px;margin:14px 0 4px;font-size:12px;color:var(--muted)}
.cols>span{text-transform:uppercase;letter-spacing:.04em;font-weight:600;margin-right:4px}
.cols button{font:inherit;color:var(--fg);background:var(--track);border:1px solid var(--line);border-radius:999px;
 padding:2px 11px;cursor:pointer;transition:background .15s,color .15s}
.cols button[aria-pressed=false]{background:none;color:var(--muted);text-decoration:line-through;border-style:dashed}
.cols button.all{background:none;border-color:transparent;color:var(--muted);text-decoration:underline}
@media (max-width:600px){body{padding:16px}}
"""

# Which x-axis the cost-over-time chart shows. Kept on <html>, outside the body the live
# page swaps on every push, and remembered in this browser where storage is allowed.
AXIS = """<script>
function kx(m) { document.documentElement.dataset.x = m; try { localStorage.setItem("keeptabs-x", m); } catch (e) {} }
try { const m = localStorage.getItem("keeptabs-x"); if (m) document.documentElement.dataset.x = m; } catch (e) {}
</script>"""


def page(content):
    """Full HTML document around one body(): the snapshot."""
    return f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>keeptabs status</title>
<style>{CSS.replace("SVGW", str(size()[0]))}</style></head><body>
<h1>keeptabs · status <span class="dot">snapshot</span></h1>
<div id="app">{content}</div>
{AXIS}</body></html>"""


def main():
    ap = argparse.ArgumentParser(description="keeptabs status page, written as a snapshot file")
    ap.add_argument("--out", default=os.path.join(kt.CFG, "state", "status.html"),
                    help="where to write the snapshot (default: state/status.html in the data folder)")
    ap.add_argument("--session", help="the session to show (default: the most recently active one)")
    ap.add_argument("--direction", choices=["ltr", "ttb", "btt"], default="ltr",
                    help="flow direction: left→right (default), top→bottom, bottom→top")
    ap.add_argument("--no-models", action="store_true", help="drop the model column (same as --hide model)")
    ap.add_argument("--hide", default="", help="columns to leave out, comma-separated: "
                                               + ",".join(k for k, _, _ in COLUMNS))
    a = ap.parse_args()
    hide = [c for c in a.hide.split(",") if c] + (["model"] if a.no_models else [])
    live = Live()
    live.scan()
    sid = a.session or live.store.newest_session()
    os.makedirs(os.path.dirname(a.out), exist_ok=True)
    tmp = a.out + ".tmp"
    with open(tmp, "w") as f:
        f.write(page(body(report(live, sid), a.direction, hide=hide)))
    os.replace(tmp, a.out)
    print(a.out, flush=True)


if __name__ == "__main__":
    main()
