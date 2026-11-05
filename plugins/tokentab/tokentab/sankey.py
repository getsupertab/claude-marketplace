#!/usr/bin/env python3
"""
sankey.py — the status page as a snapshot file: spending as a Sankey diagram,
Home-Assistant-energy style, the cost-over-time chart, and every figure of tokentab.py's
report underneath. Renderers only; nothing here serves HTTP.

  sankey.py                 write the snapshot for the most recently active session and
                            print its path (state/status.html in the data folder)
  sankey.py --session ID    pin it to one session
  sankey.py --out FILE      write it somewhere else
  sankey.py --push FILE     write the page's data file: the same parts as a script the open
                            page loads every few seconds and swaps in place (no reload, so
                            hover and scroll survive); the hooks module runs this while the
                            page was opened in the last 30 minutes
  sankey.py --flow-svg      print the Sankey alone, compact and without the hover layers,
                            for the pane beside the chat (an Svg there is 128 KB at most)

Flow, left to right:  session total → model → kind (context / generation / fees) → rate
→ tool → where it went, in the pane's terms: tool asks, answers into context, answers
re-read (a tool's three parts), then what no tool or hook accounts for: context re-reads,
prompts & replies; and TokenTab overhead. Every ribbon is built from the per-model
bookkeeping, so hovering a model lights exactly its slices downstream and hovering a tool
exactly the models that paid for it. Plain SVG computed here in Python. Every column but
the session can be hidden (--hide model,kind,...); its flows are summed over it. Colours follow the system's light or dark setting.
"""
import argparse, bisect, html, json, math, os, re, sys, time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tokentab as kt  # noqa: E402

W, H, PAD, NODE_W, GAP = 1200, 520, 150, 18, 14
HIT_H = 6        # minimum hover target height in px (invisible)
MIN_PX = 3       # every flow path is drawn at least this wide; labels still show the real value
DIR = "ltr"      # ltr: left→right; ttb: top→bottom; btt: bottom→top (set by --direction)
HIDE = frozenset()   # columns left out of the drawing (keys of COLUMNS; set by body())
MODELS = True        # the model column is drawn (also drops the per-model split in the tools table)
FONT_MAX = 13        # largest label size; the compact drawing for the pane caps it lower
LABEL_MAX = None     # longest label drawn (a longer one is cut with an ellipsis); the compact drawing sets it

# the columns that can be hidden, in flow order: key, toggle label, what it holds
COLUMNS = [
    ("model", "model", "which model paid"),
    ("kind", "kind", "context · generation · fees"),
    ("rate", "rate", "cache read and write, input, output, thinking"),
    ("tool", "tool", "the tool a slice went to"),
    ("sink", "where it went", "tool asks · answers into context · answers re-read · context re-reads · prompts & replies · TokenTab overhead"),
]
ROLES = ("session", "model", "kind", "rate", "tool", "sink")   # a path's nodes; non-tool paths skip "tool"

TOKENS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "hooks", "tokens.ts")


def load_tokens(path=TOKENS_PATH):
    """The pane's design tokens (hooks/tokens.ts): the object after `TOKENS ... =` is strict
    JSON, so the page and the pane draw from the same colours, grounds and fonts."""
    with open(path, encoding="utf-8") as f:
        text = f.read()
    start = text.index("= {", text.index("export const TOKENS")) + 2
    return json.loads(text[start:text.rindex("}") + 1])


TOKENS = load_tokens()
# The light value of each series: a mid tone that reads on both grounds, as the pane's dots
# take it. The Sankey and the chart colour inline, so they cannot switch with the scheme.
SERIES = {k: v[0] for k, v in TOKENS["series"].items()}
NEUTRAL = TOKENS["surface"]["dim"][0]

# One colour per leaf, as the pane colours its rates; the session and the kinds above the
# rates take a neutral tone, so the colour starts where the pane's colours do.
COLOR = {
    "session":        NEUTRAL,
    "context":        NEUTRAL,
    "generation":     NEUTRAL,
    "fees":           NEUTRAL,
    "cache_read":     SERIES["cache_read"],
    "cache_write_1h": SERIES["cache_write_1h"],
    "cache_write_5m": SERIES["cache_write_5m"],
    "input":          SERIES["input"],
    "output":         SERIES["output"],
    "thinking":       SERIES["thinking"],
    "fees_leaf":      SERIES["fees"],
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
    fs = max(7, min(FONT_MAX, h + 1))   # font follows the node height, down to a readable floor
    if LABEL_MAX and len(label) > LABEL_MAX:
        label = label[:LABEL_MAX - 1] + "…"
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


OVERHEAD, PROMPTS, REREADS = "TokenTab overhead", "prompts & replies", "context re-reads"
ASKING, INTO_CTX, ANSWERS_REREAD = "tool asks", "answers into context", "answers re-read"
# Where a tool's money went, in the pane's three parts (its Tools bars): asking for the call,
# the answer written into context, re-reading that answer later. What no tool accounts for
# goes past the tool column: re-reading the rest of the context, and prompts & replies. A
# sink takes paths through a tool or past it, never both, so its node is as tall as its ribbons.
TOOL_SINK = {"ask": ASKING, "think": ASKING, "result_1h": INTO_CTX, "result_5m": INTO_CTX, "read": ANSWERS_REREAD}
COLOR.update({ASKING: SERIES["ask"], INTO_CTX: SERIES["ctx"], ANSWERS_REREAD: SERIES["read"],
              REREADS: SERIES["read"], PROMPTS: SERIES["input"], OVERHEAD: SERIES["fees"]})
LABEL.update({k: k for k in (ASKING, INTO_CTX, ANSWERS_REREAD, REREADS, PROMPTS, OVERHEAD)})
OTHER_COLOR = SERIES["session_other"]

# tools and models take the pane's categorical series, costliest first
TOOL_COLORS = [SERIES[f"session_{i}"] for i in range(1, 11)]
MODEL_COLORS = [SERIES[f"model_{i}"] for i in range(1, 6)]
# fixed display order for the named nodes; models and tools sort by cost inside their column
RANK = {n: i for i, n in enumerate(["session"] + list(TREE) + [l for ls in TREE.values() for l in ls]
                                   + [ASKING, INTO_CTX, ANSWERS_REREAD, REREADS, PROMPTS, OVERHEAD])}


def slug(n):
    """An element id from a node name: letters, digits and underscores."""
    return re.sub(r"[^A-Za-z0-9]+", "_", str(n)).strip("_") or "x"


def fold_tools(paths, colors, labels, keep):
    """The tool paths beyond the `keep` costliest tools, folded into one "other tools" node,
    for the compact drawing. Paths with the same nodes are summed. Only for an unhidden
    drawing, where a tool path is the full length (its tool second to last)."""
    OTHER = "other tools"
    via_tool = lambda nodes: len(nodes) == len(ROLES)
    cost = {}
    for nodes, v in paths:
        if via_tool(nodes):
            cost[nodes[-2]] = cost.get(nodes[-2], 0.0) + v
    top = {n for n, _ in sorted(cost.items(), key=lambda kv: -kv[1])[:keep]}
    if len(cost) <= keep + 1:
        return paths, colors, labels
    merged = {}
    for nodes, v in paths:
        if via_tool(nodes) and nodes[-2] not in top:
            nodes = nodes[:-2] + (OTHER, nodes[-1])
        merged[nodes] = merged.get(nodes, 0.0) + v
    colors = dict(colors, **{OTHER: OTHER_COLOR})
    labels = dict(labels, **{OTHER: f"{len(cost) - len(top)} other tools"})
    return list(merged.items()), colors, labels


def short_model(mid):
    """claude-haiku-4-5-20251001 -> haiku 4.5, claude-fable-5-1 -> fable 5.1"""
    s = re.sub(r"^claude-", "", mid or "?")
    s = re.sub(r"-\d{8}$", "", s)
    s = re.sub(r"(?<=\d)-(?=\d)", ".", s)
    return s.replace("-", " ")


def flows(b, tb=None):
    """Root-to-sink paths [(nodes, value)] read straight from the per-model bookkeeping:
    cost_bm gives each model's spend per rate, tools[name][model] and overhead[model] what
    each tool (and TokenTab's own hook text) draws from it (TOOL_DRAWS). Links and columns
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
                            out.append((head + ((OVERHEAD,) if name == OVERHEAD else (name, TOOL_SINK[field])), v))
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
    for i, (name, _) in enumerate(tools):
        colors[name] = TOOL_COLORS[i % len(TOOL_COLORS)]; labels[name] = name
    for i, m in enumerate(models):   # any past the fifth share the last, as in the pane
        colors[m] = MODEL_COLORS[min(i, len(MODEL_COLORS) - 1)]; labels[m] = short_model(m)
    return out, colors, labels


def visible(nodes):
    """A path without the nodes of hidden columns. A path that runs past no tool keeps its
    sink, so hiding "where it went" only drops the sink behind a tool (the tool then ends
    the path); with the tool column hidden too, every path keeps its sink."""
    roles = ROLES[:len(nodes) - 1] + ("sink",)
    via_tool = "tool" in roles
    keep = []
    for n, r in zip(nodes, roles):
        if r == "sink" and not ("sink" in HIDE and via_tool and "tool" not in HIDE) \
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


def svg(b, tb=None, traces=True, keep_tools=None):
    """The Sankey. traces=False leaves out the hover tracing layers and hit boxes (the bulk
    of the markup); keep_tools folds the long tail of tools into one node."""
    all_paths, colors, labels = flows(b, tb)
    if keep_tools is not None:
        all_paths, colors, labels = fold_tools(all_paths, colors, labels, keep_tools)
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
    nid = {n: "n" + slug(n) for n in val}
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
        hit = rb.replace(f'fill="url(#g{i})" fill-opacity="0.45"', 'class="h" fill="none" stroke="#000" stroke-opacity="0" stroke-width="6" pointer-events="stroke"') if traces else ""
        parts.append(f'<g id="l{i}">{rb}{hit}</g>')
        out_off[src] += w[i]; in_off[dst] += w[i]
    # bars go under the trace layers, labels above everything; a label is a hover target too
    texts = []
    for ci, col in enumerate(cols):
        for n in col:
            u, v, h = pos[n]
            rect, text = node(u, v, h, colors[n], labels[n], true_val[n], right=ci > 0).split("<text", 1)
            hh = max(h, HIT_H)   # transparent hit box so hairline nodes can be hovered
            hit = bar(u, v + h / 2 - hh / 2, hh, "none", "h").replace('<rect class="h"', '<rect class="h" pointer-events="all"') if traces else ""
            parts.append(f'<g id="{nid[n]}">{rect}{hit}</g>')
            texts.append(f'<g id="k{nid[n][1:]}"><text{text}</g>')
    pw, ph = size()
    if not traces:
        return f'<svg viewBox="0 0 {pw} {ph}" width="100%" xmlns="http://www.w3.org/2000/svg"><defs>{defs}</defs>{"".join(parts + texts)}</svg>'

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
    return f'<svg viewBox="0 0 {pw} {ph}" width="100%" xmlns="http://www.w3.org/2000/svg">{"".join(parts + layers + texts)}</svg>'


# The pane's drawing is the page's Sankey in miniature: the same page size and columns,
# every tool but a long tail, labels small; the surface scales it to the pane's width, and
# where that is too small to read, "open in browser" shows the page.
FLOW_W, FLOW_H, FLOW_PAD, FLOW_FONT, FLOW_LABEL, FLOW_TOOLS = W, H, 110, 8, 30, 12
FLOW_HIDE = ()
_S = TOKENS["surface"]
FLOW_STYLE = (f"text{{font:8px {TOKENS['font']['sans']};fill:{_S['text'][1]}}}.v{{fill:{_S['dim'][1]}}}"
              f"@media(prefers-color-scheme:light){{text{{fill:{_S['text'][0]}}}.v{{fill:{_S['dim'][0]}}}}}")


def flow_svg(R, keep_tools=FLOW_TOOLS, hide=FLOW_HIDE):
    """The Sankey for the pane: small labels, the hover layers left out, a long tail of
    tools folded, and a <style> of its own so it follows the surface's light or dark scheme."""
    global W, H, PAD, FONT_MAX, LABEL_MAX, HIDE, MODELS, DIR
    b, tb = R["bucket"], R["transcript"]
    if not b or b.cost <= 0:
        return (f'<svg viewBox="0 0 {FLOW_W} 40" width="{FLOW_W}" height="40" xmlns="http://www.w3.org/2000/svg">'
                f'<style>{FLOW_STYLE}</style><text x="8" y="24">no usage for this session yet.</text></svg>')
    saved = W, H, PAD, FONT_MAX, LABEL_MAX, HIDE, MODELS, DIR
    W, H, PAD, FONT_MAX, LABEL_MAX, DIR = FLOW_W, FLOW_H, FLOW_PAD, FLOW_FONT, FLOW_LABEL, "ltr"
    HIDE = frozenset(hide) & {k for k, _, _ in COLUMNS}
    MODELS = "model" not in HIDE
    try:
        s = svg(b, tb, traces=False, keep_tools=keep_tools)
    finally:
        W, H, PAD, FONT_MAX, LABEL_MAX, HIDE, MODELS, DIR = saved
    s = s.replace('width="100%"', f'width="{FLOW_W}" height="{FLOW_H}"', 1)
    return s.replace("<defs>", f"<style>{FLOW_STYLE}</style><defs>", 1)


esc = html.escape

# The page's figures follow the pane's: dollars with 2 places for the big figures, 4 in the
# tables; shares as whole percents; plan windows to a tenth. Words follow the pane's too:
# "msgs" are API calls, "calls" are tool calls.
usd = lambda x, d=2: f"${x:,.{d}f}"
pct = lambda share: f"{round(share * 100)}%"
percent = lambda n: f"{round(n * 10) / 10:g}%"
big = lambda x: usd(x, 2 if x >= 0.01 or x == 0 else 4)     # a big figure, unless it would read $0.00
WINDOW_SHORT = {"five_hour": "5-hour", "seven_day": "week"}
SESSIONS_COLOURED = 10
TOP_TOOLS = 4                     # tools in the summary card; the table at the bottom has them all

# What each rate is, under its name in "Where the money went"
RATE_NOTE = {"input": "new input, not cached", "cache_write_1h": "saving context to the cache for 1 hour",
             "cache_write_5m": "saving context to the cache for 5 minutes", "cache_read": "re-reading cached context on each call",
             "output": "Claude's replies and tool inputs", "thinking": "extended thinking", "fees": "web searches"}
UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)


def tone(frac, warn_at):
    """How close a scope is to its limit, as the pane's toneOf: over at 100%, warning from warn_at."""
    return "error" if frac >= 1 else ("warning" if frac * 100 >= warn_at else "success")


def dot(series):
    return f'<i class="sd" style="background:var(--s-{series})"></i>'


def sbar(parts, cls="sbar", title=""):
    """A stacked bar like the pane's: [(series, fraction of the width)], left to right on a track."""
    segs = "".join(f'<i style="width:{max(0.0, min(f, 1)) * 100:.2f}%;background:var(--s-{s})"></i>'
                   for s, f in parts if f > 0)
    tip = f' title="{esc(title)}"' if title else ""
    return f'<div class="{cls}"{tip}>{segs}</div>'


def label(text, note=""):
    """A card's title, with a dim note beside it."""
    return f'<h2>{esc(text)}' + (f' <span>{esc(note)}</span>' if note else "") + "</h2>"


def source_tag(name):
    """Where a tool comes from, from its full name: an MCP server (mcp__<server>__<tool>) or
    Claude Code itself. A connector's server is an id, not a name, so it reads "MCP"."""
    if not name.startswith("mcp__"):
        return "built-in"
    server = name.split("__")[1] if name.count("__") >= 2 else ""
    return "MCP" if not server or UUID.match(server) else server.replace("_", " ")


def lead(P):
    """(scope row or None, its fraction, the worst fraction over every window): what the
    first tile leads with, as the pane's hero: under percent the first plan window."""
    s = P["session"]
    windows = s["windows"] if s["unit"] == "percent" else []
    first = windows[0] if windows else None
    frac = first["frac"] if first else s["frac"]
    worst = max(w["frac"] for w in windows) if first else frac
    return first, frac, worst


def spend_tile(P, R):
    """The first tile, the pane's Spend hero: plan usage under percent (dollars at API rates
    move below), else this session's dollars; the badge and the bar in the limit's tone, the
    other windows or limits, and how the limits behave."""
    s = P["session"]
    first, frac, worst = lead(P)
    api = " at API rates" if s["plan"] else ""
    badge = ""
    if worst is not None:
        t = tone(worst, s["warn_at"])
        badge = (f'<span class="chip {t}">' + ("Over · " if t == "error" else "Warning · " if t == "warning" else "")
                 + pct(worst) + ("" if t != "success" else " used") + "</span>")
    resets = lambda w: f' · resets {w["resets"]}' if w.get("resets") else ""
    lines = []
    if first:
        figure = percent(first["used"])
        lines.append(f'of the {kt.WINDOW_NAMES[first["scope"]]} · limit {percent(first["limit"])}{resets(first)}')
        lines += [f'{WINDOW_SHORT[w["scope"]]} {percent(w["used"])}, limit {percent(w["limit"])}{resets(w)}'
                  for w in s["windows"][1:]]
        lines.append(f'{big(s["cost"])}{api}')
    else:
        figure = big(s["cost"])
        if s["limit"]:
            lines.append(s["limit"] + api)
        elif api:
            lines.append(api.strip())
        bg = R.get("budget") or {}
        lines += [f'{r["name"]} {r["parts"][0]}' for r in bg.get("lines") or []
                  if r["scope"] != "session" and bg.get("unit") == "usd"]
    lines.append(f'{"hard stop" if s["hard_stop"] else "warn only"} · warns at {s["warn_at"]}%' if frac is not None
                 else "limits off" if not s["limits_on"]
                 else "no window limit set or no reading yet" if s["unit"] == "percent" else "no session limit set")
    meter = (f'<div class="sbar hbar"><i style="width:{min(frac, 1) * 100:.2f}%;background:var(--s-{tone(frac, s["warn_at"])})"></i></div>'
             if frac is not None else "")
    small = "".join(f'<div class="m">{esc(x)}</div>' for x in lines)
    return (f'<div class="kpi lead"><div class="kl"><span>{"plan usage" if first else "session spend"}</span>{badge}</div>'
            f'<b>{esc(figure)}</b>{meter}{small}</div>')


def tile(name, figure, note="", cls=""):
    return (f'<div class="kpi"><div class="kl"><span>{esc(name)}</span></div><b class="{cls}">{esc(figure)}</b>'
            + (f'<div class="m">{esc(note)}</div>' if note else "") + "</div>")


def kpis(P, R):
    """The summary: the first tile (the pane's hero), then tokens, msgs, a request's average
    cost and the context, and a banner for anything that needs attention now."""
    s = P["session"]
    if not s:
        return '<div class="card empty">No API call in this session yet; its spend shows from the first one.</div>'
    api = " at API rates" if s["plan"] else ""
    total = (P.get("total") or {}).get("tokens") or 0
    read = sum(r["tokens"] for r in P.get("rates") or [] if r["key"] == "cache_read")
    calls = sum(t["calls"] for t in P.get("tools") or [])
    q = R.get("requests")
    ctx = (R.get("budget") or {}).get("context")
    tiles = [spend_tile(P, R),
             tile("tokens", kt.num(total), f"{pct(read / total)} cache read" if total else ""),
             tile("msgs", kt.num(s["msgs"]), f"{calls} tool {'call' if calls == 1 else 'calls'}"),
             tile("avg per request", big(q["average"]) if q else "–",
                  f'of {q["count"]} {"request" if q["count"] == 1 else "requests"}{api}' if q else ""),
             tile("context", f'{kt.num(ctx["tokens"])} tok' if ctx else "–",
                  f'{usd(ctx["per_call"], 4)}/call to re-read{api}' if ctx else "", ctx["level"] if ctx else "")]
    alerts = []
    first, frac, worst = lead(P)
    if worst is not None and worst >= 1:
        what = f'the {kt.WINDOW_NAMES[max(s["windows"], key=lambda w: w["frac"])["scope"]]}' if first else "this session"
        alerts.append(("error", f"Over the limit of {what} ({pct(worst)}). "
                       + ("Hard stop is on: TokenTab blocks tool calls." if s["hard_stop"] else "TokenTab only warns: hard stop is off.")))
    if ctx and ctx["cold"]:
        alerts.append(("warning", f'Cache is cold. The next prompt re-writes the context ({kt.num(ctx["tokens"])} tok), '
                       f'about {big(ctx["rewrite"])}{api}.'))
    if not P.get("prices_verified", True):
        alerts.append(("warning", "Prices unverified: the figures are estimates at the default rates."))
    banners = "".join(f'<div class="alert {t}"><i></i>{esc(x)}</div>' for t, x in alerts)
    return f'<div class="kpis">{"".join(tiles)}</div>' + (f'<div class="alerts">{banners}</div>' if banners else "")


def rates_card(P):
    """Where the money went: this session by rate, a bar and a table, each rate saying what it is."""
    s, rates = P["session"], P.get("rates") or []
    series = lambda k: k if k in SERIES else "input"
    rows = "".join(f'<tr><td><div class="rl"><span class="dl">{dot(series(r["key"]))}</span><div><div>{esc(r["label"])}</div>'
                   f'<div class="m">{esc(RATE_NOTE.get(r["key"], ""))}</div></div></div></td><td class="n">{kt.num(r["tokens"])}</td>'
                   f'<td class="n">{usd(r["cost"], 4)}</td><td class="n m">{pct(r["share"])}</td></tr>' for r in rates)
    t = P.get("total") or {"tokens": 0, "cost": s["cost"]}
    rows += (f'<tr class="tot"><td>total{" at API rates" if s["plan"] else ""}</td><td class="n">{kt.num(t["tokens"])}</td>'
             f'<td class="n">{usd(t["cost"], 4)}</td><td></td></tr>')
    foot = (f'<p class="m">{kt.num(P["unmatched"])} output tok have no transcript line. Any thinking is counted in output.</p>'
            if P.get("unmatched") else "")
    return (f'<section class="card">{label("Where the money went", "by rate" + (" at API rates" if s["plan"] else ""))}'
            f'{sbar([(series(r["key"]), r["share"]) for r in rates], "sbar big")}<table class="rates">'
            f'<tr><th>rate</th><th class="n">tokens</th><th class="n">cost</th><th class="n">share</th></tr>{rows}</table>{foot}</section>')


def list_rows(rows):
    """A side card's rows: name on the left, a dim note and a figure on the right."""
    return "".join(f'<div class="li{" " + cls if cls else ""}"><span class="nm">{nm}</span><span class="m">{esc(note)}</span>'
                   f'<span class="n">{esc(fig)}</span></div>' for nm, note, fig, cls in rows)


def requests_card(R):
    q = R.get("requests")
    if not q:
        return ""
    rows = []
    for name in ("latest", "previous"):
        r = q[name]
        if r:
            at = time.strftime("%H:%M", time.localtime(r[0]))
            rows.append((name, f'{r[2]} {"msg" if r[2] == 1 else "msgs"} · {at}', usd(r[1], 4), ""))
    rows.append(("average", f'of {q["count"]} {"request" if q["count"] == 1 else "requests"}', usd(q["average"], 4), "dim"))
    return f'<section class="card">{label("Requests", "one prompt and everything it set off")}{list_rows(rows)}</section>'


def share_card(title, note, items, series):
    """A side card of shares (call source, models): a row each, then a bar of the shares."""
    if not items:
        return ""
    rows = [(esc(nm), f'{kt.num(msgs)} {"msg" if msgs == 1 else "msgs"} · {pct(share)}', usd(cost, 4), "")
            for nm, msgs, cost, share in items]
    return (f'<section class="card">{label(title, note)}{list_rows(rows)}'
            f'{sbar([(series(i), x[3]) for i, x in enumerate(items)], "sbar foot")}</section>')


def sessions_card(P):
    """The pane's ALL SESSIONS: today and this month over every session, then this month's
    sessions (this one in bold), opening like a tool row."""
    rows = [("today", "", usd(P["today"]), ""), ("this month", "", usd(P["month"]), "")]
    sessions = P.get("sessions") or []
    body = list_rows(rows)
    if sessions:
        series = lambda rank: f"session_{rank + 1}" if rank < SESSIONS_COLOURED else "session_other"
        others = sum(x["share"] for x in sessions[SESSIONS_COLOURED:])
        parts = [(series(i), x["share"]) for i, x in enumerate(sessions[:SESSIONS_COLOURED])] + ([("session_other", others)] if others else [])
        items = list_rows((f'{dot(series(i))}{esc(x["name"])}', pct(x["share"]), usd(x["cost"]),
                           "me" if x["sid"] == P["sid"] else "") for i, x in enumerate(sessions))
        n = len(sessions)
        body += (f'<details data-path="~sessions"><summary class="li"><span class="nm">{n} {"session" if n == 1 else "sessions"} this month</span>'
                 f'<span></span><span></span></summary>{sbar(parts, "sbar foot")}{items}</details>')
    return f'<section class="card sessions">{label("All sessions")}{body}</section>'


def top_tools_card(P, R):
    """The costliest few tools, a bar each against the costliest; the rest folded into one row.
    The full table is at the bottom of the page."""
    tools = P.get("tools") or []
    if not tools:
        return f'<section class="card">{label("Top tools")}<p class="m">No tool calls in this session yet.</p></section>'
    tags = [source_tag(nm) for nm, _ in R.get("tools") or []]
    total = sum(t["cost"] for t in tools)
    top = max(t["cost"] for t in tools) or 1e-9
    shown, rest = tools[:TOP_TOOLS], tools[TOP_TOOLS:]
    row = lambda name, tag, calls, cost, cls="": (
        f'<div class="tt-row{cls}"><div class="li"><span class="nm">{name}</span><span class="m">{esc(tag)}</span>'
        f'<span class="m n">{calls} {"call" if calls == 1 else "calls"}</span><span class="n">{usd(cost, 4)}</span></div>'
        f'<div class="sbar thin"><i style="width:{min(cost / top, 1) * 100:.2f}%"></i></div></div>')
    rows = "".join(row(f'<b>{esc(t["name"])}</b>', tags[i] if i < len(tags) else "", t["calls"], t["cost"]) for i, t in enumerate(shown))
    if rest:
        rows += row(f"{len(rest)} more", "", sum(t["calls"] for t in rest), sum(t["cost"] for t in rest), " more")
    over = f'+ TokenTab overhead {usd(P["overhead"], 4)} · ' if P.get("overhead") else ""
    return (f'<section class="card">{label("Top tools", f"{usd(total, 4)} total")}{rows}'
            f'<p class="m foot">{over}<a href="#tools">all tools ↓</a></p></section>')


def tool_rows(t, path, top, depth, tag=""):
    """One tool, or one layer of its breakdown, as the pane draws it: name, calls, cost and a
    bar of asking / answer into context / re-reading against the costliest sibling (the
    dollars on hover), and the tokens its answers put into context. A row with a breakdown is
    a <details> that opens its layers beneath it."""
    kids = t.get("children") or []
    tip = f'ask {usd(t["ask"], 4)} · into context {usd(t["ctx"], 4)} · re-read {usd(t["read"], 4)}'
    bar = sbar([("ask", t["ask"] / top), ("ctx", t["ctx"] / top), ("read", t["read"] / top)], "sbar tb", tip)
    chip = f'<em class="tag">{esc(tag)}</em>' if tag else ""
    cells = (f'<span class="tn" style="padding-left:{depth * 18}px"><span class="nm">{esc(t["name"])}</span>{chip}</span>'
             f'<span class="n m">{t["calls"]}</span><span class="n">{usd(t["cost"], 4)}</span>{bar}'
             f'<span class="n m">{kt.num(t["ctx_tokens"])}</span>')
    cls = "tr top" if depth == 0 else "tr"
    if not kids:
        return f'<div class="{cls} leaf">{cells}</div>'
    kid_top = max([k["cost"] for k in kids] + [1e-9])
    inner = "".join(tool_rows(k, f'{path}/{k["name"]}', kid_top, depth + 1) for k in kids)
    return f'<details data-path="{esc(path)}"><summary class="{cls}">{cells}</summary>{inner}</details>'


def tools_card(P, R):
    """Every tool (the pane shows the costliest few), each opening into its layers, and
    TokenTab's own overhead last."""
    tools = P.get("tools") or []
    if not tools and not P.get("overhead"):
        return ""
    tags = [source_tag(nm) for nm, _ in R.get("tools") or []]
    calls = sum(t["calls"] for t in tools)
    top = max([t["cost"] for t in tools] + [1e-9])
    head = ('<div class="tr th"><span>tool</span><span class="n">calls</span><span class="n">cost</span>'
            '<span>ask · into context · re-read</span><span class="n">tok</span></div>')
    body = "".join(tool_rows(t, t["name"], top, 0, tags[i] if i < len(tags) else "") for i, t in enumerate(tools))
    o = R.get("overhead") or {}
    if P.get("overhead"):
        ask, ctx, read = o.get("ask", 0) + o.get("think", 0), o.get("result", 0), o.get("read", 0)
        tip = f'into context {usd(ctx, 4)} · re-read {usd(read, 4)}'
        body += (f'<div class="tr top leaf over"><span class="tn"><span class="nm">TokenTab overhead</span></span><span></span>'
                 f'<span class="n">{usd(P["overhead"], 4)}</span>'
                 f'{sbar([("ask", ask / top), ("ctx", ctx / top), ("read", read / top)], "sbar tb", tip)}'
                 f'<span class="n m">{kt.num(o.get("rtokens", 0))}</span></div>')
    legend = "".join(f'<span>{dot(k)}{n}</span>' for k, n in (("ask", "ask"), ("ctx", "into context"), ("read", "re-read")))
    note = f'{len(tools)} {"tool" if len(tools) == 1 else "tools"} · {calls} {"call" if calls == 1 else "calls"} · cost = asking + answer into context + re-reading it (estimates)'
    return (f'<section class="card tools" id="tools">{label("Tools", note)}'
            f'<div class="scroll"><div class="tt">{head}{body}</div></div><div class="legend">{legend}</div></section>')


def status_html(R):
    """The cards under the charts, from the pane's report (`R["pane"]`, the same pane_report()
    the pane draws) and what the pane has no room for: where the money went beside the
    requests, the call sources, the models and all sessions; then every tool."""
    P = R["pane"]
    model_series = lambda rank: f"model_{min(rank + 1, 5)}"
    source_series = lambda rank: f"session_{rank + 1}" if rank < SESSIONS_COLOURED else "session_other"
    side = (requests_card(R)
            + share_card("By call source", "what in Claude Code made the call",
                         [(nm, calls, c, share) for nm, calls, c, share in R["by_source"]], source_series)
            + share_card("Models", f'{len(P.get("models") or [])} used',
                         [(re.sub("^claude-", "", m["name"]), m["msgs"], m["cost"], m["share"]) for m in P.get("models") or []],
                         model_series)
            + sessions_card(P))
    return f'<div class="row2">{rates_card(P)}<div class="side">{side}</div></div>{tools_card(P, R)}'


def report(live, sid=None):
    """kt.report() for one session from a Live's stores (rescanned first), with the pane's
    report of it (kt.pane_report(), what `tokentab.py --report` prints) as R["pane"]: the page
    draws its figures from the same data as the pane. Without sid: the most recently active
    session."""
    live.scan()
    sid = sid or live.store.newest_session()
    try:
        budget = json.load(open(kt.BUDGET_PATH, encoding="utf-8"))
    except (OSError, ValueError):
        budget = {}
    R = kt.report(live.store, sid, live.prices, budget, True, live.led)
    R["pane"] = kt.pane_report(live.store, sid, live.prices, budget, live.led)
    return R


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
    s = (R.get("pane") or {}).get("session") or {}
    api = " at API rates" if s.get("plan") else ""   # a subscription pays its plan, not these dollars
    return (f'<section class="card chart"><h2>Cost over time <span>cumulative by rate{api} · '
            f'dashed: your prompts</span>{seg}</h2>{draw("time")}{draw("calls")}</section>')


# Page order, "summary first" (Figma "TokenTab browser / A summary first v2"): the tiles and
# banners, the cost flow, cost over time beside the top tools, where the money went beside
# the smaller cards, then every tool.
PARTS = ("head", "hero", "flow", "chart", "status")


def parts(R, direction="ltr", models=True, hide=()):
    """Everything below the page title as named HTML fragments (PARTS, in page order). The
    page holds each in a div of its own and the live data file carries the same names, so
    an update swaps only the parts whose markup changed. hide: columns to leave out
    (models=False hides "model" too)."""
    global DIR, MODELS, HIDE
    DIR = direction
    HIDE = frozenset(hide) & {k for k, _, _ in COLUMNS} | (set() if models else {"model"})
    MODELS = "model" not in HIDE
    s, b, tb = R["session"], R["bucket"], R["transcript"]
    P = R.get("pane")
    if not s:
        head = f'<div class="sub">session {esc(R["sid"] or "?")} · waiting for activity…</div>'
        status = f'<div class="row2"><div></div><div class="side">{sessions_card(P)}</div></div>' if P else ""
        return {"head": head, "hero": kpis(P, R) if P else "", "flow": "", "chart": "", "status": status}
    since = s["since"].strftime("%H:%M") if s["since"] else "?"
    name = f'<b title="{esc(R["sid"])}">{esc(s["title"])}</b>' if s["title"] else f'session {esc(R["sid"][:8])}'
    head = (f'<div class="sub">{name} · {esc(", ".join(s["models"]))} · since {since}'
            f' · updated {R["now"].strftime("%H:%M:%S")}</div>'
            f'<div class="src">{esc(s["source"])}</div>')
    flow = (f'<section class="card flow">{label("Cost flow", "session → model → context / generation → rate → where it went")}'
            + (svg(b, tb) if b and b.cost > 0 else '<p class="m">No usage for this session yet.</p>') + "</section>")
    chart = timeline(R)
    chart = f'<div class="row2">{chart or "<div></div>"}{top_tools_card(P, R)}</div>'
    return {"head": head, "hero": kpis(P, R), "flow": flow, "chart": chart, "status": status_html(R)}


def body(R, direction="ltr", models=True, hide=()):
    """Everything below the page title, as one HTML fragment: the parts in their divs."""
    p = parts(R, direction, models, hide)
    return "".join(f'<div id="p-{k}">{p[k]}</div>' for k in PARTS)


def push_js(R, direction="ltr", models=True, hide=()):
    """The live data file: one call of the page's `__tokentab` with the parts. Loaded by
    the open page as a script (a page from a file: URL may load a sibling script, where it
    may not fetch one), so no server is needed to keep it current."""
    data = {"at": R["now"].strftime("%H:%M:%S"), "parts": parts(R, direction, models, hide)}
    return "window.__tokentab && window.__tokentab(" + json.dumps(data, ensure_ascii=False) + ");\n"


def css_vars():
    """The tokens as CSS custom properties, light on :root and dark under prefers-color-scheme:
    every surface token as --<name>, every series as --s-<name> (the HTML bars and dots, which
    switch with the scheme as the pane's Svg bars do), and the tones the page uses (--ok,
    --warn, --over, --track) from the series."""
    pairs = {k.replace("_", "-"): v for k, v in TOKENS["surface"].items()}
    s = TOKENS["series"]
    pairs.update({f"s-{k}": v for k, v in s.items()})
    pairs.update({"ok": s["success"], "warn": s["warning"], "over": s["error"], "track": s["track"]})
    side = lambda i: ";".join(f"--{k}:{v[i]}" for k, v in pairs.items())
    f = TOKENS["font"]
    return (f":root{{color-scheme:light dark;--sans:{f['sans']};--mono:{f['mono']};{side(0)}}}\n"
            f"@media (prefers-color-scheme:dark){{:root{{{side(1)}}}}}")


# The pane's design language (Figma "TokenTab UI") in the page's layout (Figma "TokenTab
# browser / A summary first v2"): Inter for words, mono for figures, white cards on a warm
# ground with hairline borders, a row of summary tiles, bars on a track.
CSS = css_vars() + """
*{box-sizing:border-box}
body{margin:0;padding:32px 40px 48px;background:var(--bg);color:var(--text);font:13px/1.45 var(--sans)}
#app{max-width:1360px}
header{max-width:1360px}
h1{font-size:20px;line-height:28px;font-weight:600;margin:0 0 4px;display:flex;align-items:center;gap:10px}
h2{font-size:15px;line-height:22px;margin:0 0 14px;font-weight:600;display:flex;align-items:baseline;gap:10px;flex-wrap:wrap}
h2 span{font-size:12px;font-weight:400;color:var(--dim)}
.sub{color:var(--dim);font-size:12px}.sub b{color:var(--secondary);font-weight:500}
.src{font-size:12px;color:var(--dim)}
.m{color:var(--dim);font-size:12px}.warn{color:var(--warn)}.over{color:var(--over)}
a{color:inherit}
#app>div{margin-bottom:20px}#app>div:empty{display:none}
.card{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:22px;min-width:0}
.card.empty{color:var(--dim)}
.row2{display:grid;grid-template-columns:minmax(0,2.2fr) minmax(0,1fr);gap:20px;align-items:stretch}
.side{display:flex;flex-direction:column;gap:20px}
/* the summary tiles: one card, split by hairlines */
.kpis{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));background:var(--panel);border:1px solid var(--line);border-radius:12px}
.kpi{padding:18px 22px;border-left:1px solid var(--line);display:flex;flex-direction:column;gap:2px;min-width:0}
.kpi:first-child{border-left:0}
.kl{display:flex;justify-content:space-between;align-items:center;gap:8px;min-height:20px}
.kl span{font-size:11px;line-height:16px;font-weight:500;color:var(--dim);text-transform:uppercase;letter-spacing:.06em}
.kpi b{font-size:20px;line-height:28px;font-weight:600;font-variant-numeric:tabular-nums}
.kpi.lead b{font-size:30px;line-height:40px}
.kpi b.warn{color:var(--warn)}.kpi b.over{color:var(--over)}
.kpi .hbar{margin:6px 0 4px}
.chip{font:600 11px/16px var(--sans);border-radius:10px;padding:2px 8px;white-space:nowrap}
.chip.error{background:var(--error-soft);color:var(--over)}.chip.warning{color:var(--warn)}.chip.success{color:var(--ok)}
.alert{display:flex;align-items:center;gap:12px;border-radius:12px;padding:14px 18px;margin-top:20px;font-size:13px;
 border:1px solid color-mix(in srgb,var(--warn) 35%,var(--panel));background:color-mix(in srgb,var(--warn) 10%,var(--panel))}
.alert i{flex:none;width:10px;height:10px;border-radius:50%;background:var(--warn)}
.alert.error{border-color:color-mix(in srgb,var(--over) 35%,var(--panel));background:var(--error-soft)}.alert.error i{background:var(--over)}
/* bars */
.sbar{display:flex;gap:2px;height:8px;border-radius:4px;background:var(--track);overflow:hidden}
.sbar i{display:block;height:100%}.hbar i{border-radius:4px}
.sbar.big{height:12px;border-radius:6px;margin-bottom:14px}
.sbar.foot{margin-top:12px}
.sbar.thin{height:5px;border-radius:3px;margin-top:6px}.sbar.thin i{background:var(--s-ctx);border-radius:3px}
.tt-row.more .sbar.thin i{background:var(--dim)}
.sbar.tb{height:8px;border-radius:2px;background:none;align-self:center}.sbar.tb i{border-radius:2px}
.sd{display:inline-block;width:8px;height:8px;border-radius:50%;flex:none}
/* side card rows: name, a dim note, a figure */
.li{display:grid;grid-template-columns:minmax(0,1fr) auto 76px;gap:8px;align-items:baseline;padding:6px 0}
.li .nm{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;display:flex;align-items:center;gap:8px}
.li .n,.n{text-align:right;font:13px var(--mono);font-variant-numeric:tabular-nums}
.li .m.n{font:12px var(--sans)}
th.n,.tr.th .n{font:11px var(--sans)}
.li.dim .nm,.li.dim .n{color:var(--dim)}.li.me .nm,.li.me .n{font-weight:700}
.sessions details>summary.li{cursor:pointer;list-style:none;color:var(--secondary)}
.sessions summary::-webkit-details-marker{display:none}
.sessions summary .nm::before{content:"▸";color:var(--dim)}.sessions details[open]>summary .nm::before{content:"▾"}
.sessions details .li .nm{font-size:12px}
.tt-row{padding:4px 0 8px}.tt-row .li{padding:0;grid-template-columns:auto minmax(0,1fr) auto 76px}
.tt-row .li .nm b{font:600 13px var(--mono)}
p.foot{margin:10px 0 0}
/* where the money went */
table{border-collapse:collapse;width:100%}
th{font-size:11px;font-weight:400;color:var(--dim);text-align:left;padding:0 0 6px}
td{padding:9px 0;border-top:1px solid var(--line);vertical-align:middle}
th.n,td.n{padding-left:16px;white-space:nowrap}
.rl{display:flex;gap:10px}.rl .m{font-size:12px}
/* the dot sits on the middle of the first line (the rate's name), not the top of the row */
.dl{display:flex;align-items:center;height:1.45em}
tr.tot td{font-weight:700;font-family:var(--sans)}tr.tot td.n{font-family:var(--mono)}
.card>p.m{margin:12px 0 0}
/* every tool */
.scroll{overflow-x:auto}.tt{min-width:720px}
.tr{display:grid;grid-template-columns:minmax(200px,1fr) 60px 90px 220px 80px;gap:0 16px;align-items:center;padding:8px 0;border-top:1px solid var(--line)}
.tr.th{border-top:0;padding:0 0 6px;font-size:11px;color:var(--dim)}
details details .tr,details .tr.leaf{border-top:0;padding:4px 0}
.tn{display:flex;align-items:center;gap:8px;min-width:0}
.tn .nm{font:13px var(--mono);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.tr.top>.tn .nm{font-weight:500}.tr.over .nm{color:var(--dim)}
.tag{font:400 11px/16px var(--sans);font-style:normal;color:var(--secondary);background:var(--bg);border-radius:6px;padding:1px 7px;white-space:nowrap}
summary.tr{cursor:pointer;list-style:none}summary.tr::-webkit-details-marker{display:none}
summary.tr .tn::before{content:"▸";color:var(--dim);width:10px;flex:none}details[open]>summary.tr .tn::before{content:"▾"}
.tr.leaf .tn::before{content:"";width:10px;flex:none}
summary.tr:hover,summary.li:hover{background:var(--hover)}
.legend{display:flex;gap:16px;margin-top:14px;font-size:12px;color:var(--dim)}.legend span{display:flex;align-items:center;gap:6px}
/* charts */
svg{display:block;width:100%;height:auto}svg text{fill:var(--text);font-family:var(--sans);font-size:13px}svg .v{fill:var(--dim)}
.flow svg{max-width:100%}
.dot{font-size:12px;font-weight:400;color:var(--dim);display:inline-flex;align-items:center;gap:6px}
.dot::before{content:"";width:8px;height:8px;border-radius:50%;background:var(--dim)}
.dot.on::before{background:var(--ok)}.dot.off::before{background:var(--over)}
html[data-state=stale] #p-hero{opacity:.55}
html[data-state=stale] #p-hero::after{content:"These figures stopped updating: the session may have ended.";display:block;margin-top:8px;font-size:12px;color:var(--dim)}
.chart h2{align-items:center}
.seg{margin-left:auto;display:inline-flex;gap:2px;padding:2px;border-radius:8px;background:var(--bg)}
.seg button{font:12px/17px var(--sans);color:var(--dim);background:none;border:0;border-radius:6px;padding:4px 10px;cursor:pointer}
.seg button:hover{color:var(--text)}
html:not([data-x=calls]) .seg [data-x=time],html[data-x=calls] .seg [data-x=calls]{background:var(--panel);color:var(--text);font-weight:500;
 box-shadow:0 0 0 1px var(--line)}
html:not([data-x=calls]) svg.xc,html[data-x=calls] svg.xt{display:none}
.chart svg text{font-size:12px}.chart .g{stroke:var(--line)}.chart .a{stroke:var(--dim);stroke-opacity:.5}
.chart .ln{fill:none;stroke-width:2;stroke-linejoin:round}.chart .ln:hover{stroke-width:3.5}
.pm line{stroke:var(--dim);stroke-opacity:.4;stroke-dasharray:3 4}.pm .hit{stroke:#000;stroke-opacity:0;stroke-width:9;stroke-dasharray:none}
.pm:hover line:first-child{stroke-opacity:.9}
/* narrower windows: tiles wrap, the side column moves under the main one */
@media (max-width:1200px){.kpis{grid-template-columns:repeat(3,minmax(0,1fr))}.kpi{border-top:1px solid var(--line)}
 .kpi:nth-child(-n+3){border-top:0}.kpi:nth-child(3n+1){border-left:0}}
@media (max-width:1000px){.row2{grid-template-columns:minmax(0,1fr)}}
@media (max-width:640px){body{padding:16px}.card{padding:16px}.kpis{grid-template-columns:repeat(2,minmax(0,1fr))}
 .kpis .kpi{border-left:0;border-top:1px solid var(--line)}.kpis .kpi.lead{grid-column:1/-1;border-top:0}
 .kpis .kpi:nth-child(odd):not(.lead){border-left:1px solid var(--line)}}
"""

# Which x-axis the cost-over-time chart shows. Kept on <html>, outside the body the live
# page swaps on every push, and remembered in this browser where storage is allowed.
AXIS = """<script>
function kx(m) { document.documentElement.dataset.x = m; try { localStorage.setItem("tokentab-x", m); } catch (e) {} }
try { const m = localStorage.getItem("tokentab-x"); if (m) document.documentElement.dataset.x = m; } catch (e) {}
</script>"""


# Keeps the page current from its data file (`--push`): every few seconds a script tag
# loads `<name>.js` beside the page (a cache-busting query on it), which calls
# `__tokentab` with the parts; only a part whose markup changed is swapped, in place, so
# the Sankey's hover, the scroll position, the chart's axis and the opened tool rows survive. The dot says
# whether a push arrived in the last 15 s; a stale page dims its tiles.
LIVE = """<script>
(function () {
  var src = SRC, last = {}, seen = 0, at = "", dot = document.getElementById("dot");
  window.__tokentab = function (d) {
    seen = Date.now();
    if (d.at === at) return;
    at = d.at;
    for (var k in d.parts) {
      if (last[k] === d.parts[k]) continue;
      var el = document.getElementById("p-" + k);
      if (el) {
        // a tool row the reader opened stays open across the swap
        var open = Array.prototype.map.call(el.querySelectorAll("details[open]"), function (x) { return x.dataset.path; });
        el.innerHTML = d.parts[k];
        open.forEach(function (p) {
          Array.prototype.forEach.call(el.querySelectorAll("details"), function (x) { if (x.dataset.path === p) x.open = true; });
        });
      }
      last[k] = d.parts[k];
    }
  };
  function poll() {
    var s = document.createElement("script");
    s.src = src + "?t=" + Date.now();
    s.onload = s.onerror = function () { s.remove(); };
    document.head.appendChild(s);
  }
  setInterval(poll, 3000);
  setInterval(function () {
    var ok = Date.now() - seen < 15000;
    dot.className = "dot " + (ok ? "on" : "off");
    dot.textContent = ok ? "live" : (seen ? "stale" : "snapshot");
    document.documentElement.dataset.state = ok ? "live" : (seen ? "stale" : "snapshot");
  }, 1000);
  poll();
})();
</script>"""


def page(content, data_src=None):
    """Full HTML document around one body(): the snapshot. data_src: the live data file's
    name beside the page (`--push` writes it); without one the page is a plain snapshot."""
    live = LIVE.replace("SRC", json.dumps(data_src)) if data_src else ""
    return f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>TokenTab status</title>
<style>{CSS.replace("SVGW", str(size()[0]))}</style></head><body>
<header><h1>TokenTab status <span class="dot" id="dot">snapshot</span></h1></header>
<div id="app">{content}</div>
{AXIS}{live}</body></html>"""


def main():
    ap = argparse.ArgumentParser(description="TokenTab status page, written as a snapshot file")
    ap.add_argument("--out", help="where to write the snapshot (default: state/status.html in the data "
                                  "folder; with --push or --flow-svg the page is written only when given)")
    ap.add_argument("--push", metavar="FILE", help="write the live data file for the open page (see above)")
    ap.add_argument("--flow-svg", action="store_true", help="print the compact Sankey for the pane")
    ap.add_argument("--keep-tools", type=int, default=FLOW_TOOLS, help=f"tools drawn on their own in the compact Sankey (default {FLOW_TOOLS})")
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
    R = report(live, sid)
    if a.flow_svg:
        sys.stdout.write(flow_svg(R, a.keep_tools))
        sys.stdout.flush()
    if a.push:
        write(a.push, push_js(R, a.direction, hide=hide))
    if a.out or not (a.flow_svg or a.push):
        out = a.out or os.path.join(kt.CFG, "state", "status.html")
        # the page's data file sits beside it under the same name
        data = os.path.splitext(out)[0] + ".js"
        write(data, push_js(R, a.direction, hide=hide))
        write(out, page(body(R, a.direction, hide=hide), os.path.basename(data)))
        if not a.flow_svg:
            print(out, flush=True)


REPLACE_TRIES, REPLACE_WAIT = 5, 0.05


def write(path, text):
    """Writes the whole file, through a temporary twin, so a reader never sees a cut file.
    A target Windows keeps locked (a browser reading it) skips this redraw; the next one writes it."""
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = path + ".tmp"
    # newline="" keeps the page byte-identical on every OS; Windows would write \r\n
    with open(tmp, "w", encoding="utf-8", newline="") as f:
        f.write(text)
    for attempt in range(REPLACE_TRIES):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if attempt + 1 < REPLACE_TRIES:
                time.sleep(REPLACE_WAIT)
    try:
        os.remove(tmp)
    except OSError:
        pass


if __name__ == "__main__":
    kt.utf8_stdio()
    main()
