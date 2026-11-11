#!/usr/bin/env python3
"""TokenTab - live token and cost monitor for Claude Code sessions.

Totals come from the ledger the plugin's hooks module writes, one line per API
request (ledger/YYYY-MM-DD/<session-id>.jsonl in the data folder): every call, helpers
and Claude Code's own background calls included. The JSONL transcripts under
projects/ in Claude Code's config folder are read for what the ledger does not have: thinking tokens and the
per-tool attribution. No dependencies.

  tokentab.py                 live view of the most recently active session
  tokentab.py --session ID    pin to one session
  tokentab.py --once          print one snapshot and exit
  tokentab.py --json          machine-readable snapshot (for hooks)
  tokentab.py --status        one plain line for a status bar (for the hooks module)
  tokentab.py --all           live view across every project
"""
import argparse, bisect, glob, json, os, re, shlex, sys, time
from datetime import datetime, timezone
HERE = os.path.dirname(os.path.abspath(__file__))
_T0 = time.perf_counter()     # the run's start, for the timing log

# plugin: Claude Code's config folder, CLAUDE_CONFIG_DIR when set, else .claude in USERPROFILE
# (Windows), else in HOME: the order the hooks module uses (dataDir in hooks/register.tsx)
_HOME = os.environ.get("USERPROFILE") or os.environ.get("HOME") or os.path.expanduser("~")
CLAUDE_DIR = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(_HOME, ".claude")
ROOT = os.path.join(CLAUDE_DIR, "projects")
def data_dir():
    """The plugin's data folder: one fixed path under the config folder, the same the hooks
    module builds (dataDir in hooks/register.tsx) and the shell gate uses. Not ${CLAUDE_PLUGIN_DATA}: that
    one is named after the marketplace the plugin was installed from (tokentab-supertab,
    tokentab-inline under --plugin-dir), the module cannot read it, and the ledger, the
    guard and the skills must all look in the same place."""
    return os.path.join(CLAUDE_DIR, "plugins", "data", "tokentab-supertab")

CFG  = data_dir()  # plugin: data in the plugin's data folder, shipped files next to this script
PRICES_PATH = os.path.join(HERE, "prices.json")
BUDGET_PATH = os.path.join(CFG, "budget.json")
# The plan's usage windows as the hooks module read them (session.measure), one file per session
WINDOWS_DIR = os.path.join(CFG, "state", "windows")

def utf8_stdio():
    """The hooks module and Claude Code speak UTF-8 on the pipes; Windows would use the ANSI code page."""
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass

# ---------- pricing ----------

def load_prices():
    with open(PRICES_PATH, encoding="utf-8") as fh:
        return json.load(fh)

def rate(prices, model, key):
    m = prices.get("models", {}).get(model)
    if m is None:
        m = prices.get("default", {})
    return float(m.get(key, 0.0))

# ---------- accumulation ----------

RATES = ("input", "cache_write_5m", "cache_write_1h", "cache_read", "output", "thinking")
PRICE_KEY = {"thinking": "output"}   # thinking is billed as output; kept apart for the split
# Per-tool fields, per model. ask: output text that emits the call. think: the turn's
# thinking, if THINKING_TO_TOOLS. result_1h/5m: the tool result's share of the next
# call's cache write. rtokens: tokens of that write. read: re-reading the result on
# every later call until a compaction. Each draws on one rate of one model.
TOOL_DRAWS = {"ask": "output", "think": "thinking", "result_1h": "cache_write_1h",
              "result_5m": "cache_write_5m", "read": "cache_read"}
COMPACT_DROP = 0.5         # context falling below this fraction of the last call = compacted

# Thinking happens once per call, before its tool calls; nothing says which tool it was
# for, so it stays unattributed (tools get output text only). True shares it among the
# call's tools by tool_use size instead, a convention with no data behind it.
THINKING_TO_TOOLS = False

# ---------- estimates: the API reports usage per call, not per content block ----------

CHARS_PER_TOKEN = 3.5      # code/JSON is ~3, prose ~4
IMAGE_CHARS = 1600 * CHARS_PER_TOKEN   # an image block costs ~1.6k tokens, whatever its base64 size
TOKENTAB_TEXT = re.compile(r"^(JOB|BUDGET|CONTEXT|SESSION) (CHECK|NOTICE|HINT|BUDGET)\b|TokenTab", re.I | re.M)

def block_chars(x):
    """Rough size in characters of a content block, tool result or attachment, as it
    sits in context. Only used to share out a call's tokens (size_shares)."""
    if isinstance(x, str):
        return len(x)
    if isinstance(x, list):
        return sum(block_chars(y) for y in x)
    if not isinstance(x, dict):
        return 0
    t = x.get("type")
    if t == "image":
        return IMAGE_CHARS
    if t in ("text", "thinking"):
        return len(x.get(t) or "")
    if t == "tool_use":
        return len(x.get("name") or "") + len(json.dumps(x.get("input"), ensure_ascii=False))
    if "content" in x:                      # tool_result, attachments
        return block_chars(x["content"])
    return len(json.dumps(x, ensure_ascii=False))

def size_shares(sizes, rest, tokens=0):
    """ESTIMATE. The one place a call's tokens are split among its parts: by the
    characters each part put into context ({key: chars}; rest = everything else).
    When the call reports more tokens than the visible characters explain (a cache
    miss re-writing the whole context, the system prompt), the excess stays
    unattributed rather than inflating the parts. Shares sum to at most 1."""
    tot = max(sum(sizes.values()) + rest, tokens * CHARS_PER_TOKEN)
    return {k: v / tot for k, v in sizes.items()} if tot > 0 else {}

def thread_of(o):
    """Which conversation a transcript line belongs to: helpers (subagents) share the
    parent's sessionId but have their own context, so they get their own attribution."""
    return o.get("agentId") or ("sidechain" if o.get("isSidechain") else "")

def resident_shares(res, ctx):
    """ESTIMATE. Share of a call's cache read that re-reads each resident part ({key:
    tokens}, as written by size_shares), out of the call's whole context. Assumes the
    parts are still in the cached prefix; a part dropped from context without a
    compaction (Claude Code clearing old tool results) keeps collecting rent."""
    tot = max(sum(res.values()), ctx)
    return {k: v / tot for k, v in res.items() if v > 0} if tot > 0 else {}

def overhead_total(b):
    """TokenTab's own hook text (budget hints, job checks), summed over models."""
    return tool_totals({None: b.overhead} if b and b.overhead else {None: {}})[0][1]

# A tool's breakdown, the layers under it in the pane: Bash by program, then subcommand
# for these; file tools by file; Agent by subagent; Skill by skill; WebFetch by host.
SUBCOMMANDS = {"git", "gh", "npm", "npx", "pnpm", "yarn", "bun", "cargo", "go", "docker", "kubectl",
               "brew", "pip", "pip3", "uv", "claude", "make", "terraform", "aws", "gcloud", "tmux", "deno"}
SHELL_PREFIX = {"sudo", "time", "env", "nohup", "exec", "command"}
SHELL_SETUP = {"cd", "export", "source", ".", "set", "pushd", "unset"}

def shell_layers(cmd):
    """("git", "status") for `cd repo && FOO=1 git status -s`: the first segment that is
    not setup, its program and, for SUBCOMMANDS, the subcommand."""
    for seg in re.split(r"&&|\|\||;|\||\n", cmd or ""):
        try:
            words = shlex.split(seg, comments=True)
        except ValueError:
            words = seg.split()
        words = [w for w in words if not re.match(r"^[A-Za-z_]\w*=", w)]
        while words and words[0] in SHELL_PREFIX:
            words = words[1:]
        if not words or words[0] in SHELL_SETUP:
            continue
        prog = words[0].rsplit("/", 1)[-1]
        sub_ = words[1] if len(words) > 1 and prog in SUBCOMMANDS else ""
        return (prog, sub_) if re.match(r"^[\w:.-]+$", sub_) and not sub_.startswith("-") else (prog,)
    return ()

def tool_layers(name, inp):
    """The breakdown key of one tool_use under its tool, () where there is none."""
    inp = inp if isinstance(inp, dict) else {}
    if name == "Bash":
        return shell_layers(inp.get("command"))
    path = inp.get("file_path") or inp.get("notebook_path")
    if name in ("Read", "Edit", "Write", "MultiEdit", "NotebookEdit") and isinstance(path, str):
        return (path.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1],)
    pick = {"Agent": "subagent_type", "Task": "subagent_type", "Skill": "skill"}.get(name)
    if pick and isinstance(inp.get(pick), str):
        return (inp[pick],)
    if name == "WebFetch" and isinstance(inp.get("url"), str):
        host = re.match(r"^\w+://([^/:?#]+)", inp["url"])
        return (host.group(1),) if host else ()
    return ()

def tool_tree(name, parts, cap=12):
    """One tool's breakdown as nested rows, costliest first: parts maps (name, *layers)
    to its fields. Past `cap` children, the rest fold into one "N more" row."""
    def node(label, items, depth):
        rows = {}
        for key, f in items:
            if len(key) > depth:
                rows.setdefault(key[depth], []).append((key, f))
        out = []
        for lab, sub_items in rows.items():
            t = {k: sum(f.get(k, 0) for _, f in sub_items) for k in ("calls", "rtokens", *TOOL_DRAWS)}
            t["cost"] = sum(t[k] for k in TOOL_DRAWS)
            t["result"] = t["result_1h"] + t["result_5m"]
            out.append((lab, t, node(lab, sub_items, depth + 1)))
        out.sort(key=lambda r: -r[1]["cost"])
        if len(out) > cap:
            rest = out[cap:]
            t = {k: sum(r[1][k] for r in rest) for k in out[0][1]}
            out = out[:cap] + [(f"{len(rest)} more", t, [])]
        return out
    return node(name, [(k, f) for k, f in parts.items() if k[0] == name], 1)

def tool_totals(tools):
    """{name: {model: fields}} -> [(name, fields summed over models)], costliest first.
    Adds result (1h + 5m) and cost (everything the tool draws)."""
    out = []
    for name, by_model in tools.items():
        t = {}
        for f in by_model.values():
            for k, v in f.items():
                t[k] = t.get(k, 0) + v
        t["result"] = t.get("result_1h", 0.0) + t.get("result_5m", 0.0)
        t["cost"] = sum(t.get(k, 0.0) for k in TOOL_DRAWS)
        out.append((name, t))
    return sorted(out, key=lambda kv: -kv[1]["cost"])

class Bucket:
    __slots__ = ("inp","cw5","cw1h","cr","out","think","cost","msgs","models","first","last",
                 "tools","parts","overhead","threads","cost_by","cost_bm","by_source","ctx","ctx_model","ttl","calls",
                 "series","_open")
    def __init__(self):
        self.inp = self.cw5 = self.cw1h = self.cr = self.out = self.think = 0
        self.ctx, self.ctx_model, self.ttl = 0, None, "1h"   # the latest call
        self.cost = 0.0
        self.msgs = 0
        self.models = {}
        self.first = None
        self.last = None
        self.tools = {}       # name -> model -> {calls, ask, think, result_1h, result_5m, rtokens}
        self.parts = {}       # (name, *layers) -> the tools fields summed over models (tool_layers)
        self.overhead = {}    # model -> {result_1h, result_5m, read, rtokens}: TokenTab's own hook text
        self.threads = {}     # transcripts only: per conversation (main, each helper) attribution state
        self.cost_by = {}     # rate key -> USD, summed over models (output = text only)
        self.cost_bm = {}     # model -> rate key -> USD, priced where the model is known
        self.by_source = {}   # ledger only: query_source -> [calls, USD]
        self.calls = []       # (epoch, USD) per call, for request totals
        self.series = []      # [epoch, {rate key: USD}] per call, for the cost-over-time chart
        self._open = None     # the series entry _charge adds to: the call being priced

    def _tool(self, name, model):
        if name is None:                      # None: TokenTab's own hook text, not a tool
            return self.overhead.setdefault(model, {"result_1h": 0.0, "result_5m": 0.0, "read": 0.0, "rtokens": 0})
        return self.tools.setdefault(name, {}).setdefault(model, {
            "calls": 0, "ask": 0.0, "think": 0.0, "result_1h": 0.0, "result_5m": 0.0, "read": 0.0, "rtokens": 0})

    def _add(self, key, model, field, v):
        """Charge a draw to a tool (key: (name, *layers)) or, key None, to TokenTab."""
        self._tool(key and key[0], model)[field] += v
        if key and len(key) > 1:
            p = self.parts.setdefault(key, {"calls": 0, "ask": 0.0, "think": 0.0, "result_1h": 0.0,
                                            "result_5m": 0.0, "read": 0.0, "rtokens": 0})
            p[field] += v

    def _thread(self, agent):
        # since: what entered context after the last call, sized; it is in the next call's write.
        # res: tokens each tool (None: TokenTab) has resident in context. ctx: the last call's.
        # ids: tool_use id -> (name, *layers); tools and res are keyed the same way. req: the open call, whose lines may still be coming.
        return self.threads.setdefault(agent or "", {"since": {"tools": {}, "other": 0, "kt": 0},
                                                     "res": {}, "ctx": 0, "ids": {}, "req": None})

    def note(self, o):
        """A transcript line that is not an API call: tool results, prompts, hook
        injections. Sized here; charged when the next call writes them to the cache."""
        t = self._thread(thread_of(o))
        s = t["since"]
        if o.get("isCompactSummary") or o.get("subtype") == "compact_boundary":
            t["res"] = {}                     # compacted: old results are no longer re-read
        if o.get("type") == "user":
            c = (o.get("message") or {}).get("content")
            for blk in (c if isinstance(c, list) else [c]):
                nm = t["ids"].get(blk.get("tool_use_id")) if isinstance(blk, dict) else None
                if nm:
                    s["tools"][nm] = s["tools"].get(nm, 0) + block_chars(blk)
                else:
                    s["other"] += block_chars(blk)
        elif o.get("type") == "attachment":
            a = o.get("attachment") or {}
            ours = a.get("type") == "hook_additional_context" and TOKENTAB_TEXT.search(
                " ".join(x for x in (a.get("content") or []) if isinstance(x, str)))
            s["kt" if ours else "other"] += block_chars(a)

    def _attribute(self, t, key, model, prices, i, r, w1, w5, o, th, content, dup):
        """Tool attribution for one log line of a call (transcripts only)."""
        req = t["req"]
        if not dup or req is None or req["key"] != key:
            # A new call. It re-reads what is resident (cache read, by resident share),
            # then writes what entered context since the last call: tools get their
            # tool_result share, TokenTab its hook text, the rest stays unattributed.
            ctx = i + r + w1 + w5
            if ctx < t["ctx"] * COMPACT_DROP:  # compacted without a marker line we saw
                t["res"] = {}
            t["ctx"] = ctx
            cr = r * rate(prices, model, "cache_read") / 1_000_000.0
            for nm, f in resident_shares(t["res"], ctx).items():
                self._add(nm, model, "read", cr * f)
            s = t["since"]
            if w1 + w5:
                c1 = w1 * rate(prices, model, "cache_write_1h") / 1_000_000.0
                c5 = w5 * rate(prices, model, "cache_write_5m") / 1_000_000.0
                for nm, f in size_shares({**s["tools"], None: s["kt"]}, s["other"], w1 + w5).items():
                    for field, v in (("result_1h", c1 * f), ("result_5m", c5 * f), ("rtokens", round((w1 + w5) * f))):
                        self._add(nm, model, field, v)
                    t["res"][nm] = t["res"].get(nm, 0) + (w1 + w5) * f
            t["since"] = {"tools": {}, "other": 0, "kt": 0}
            req = t["req"] = {"key": key, "model": model, "o": 0, "th": 0, "uses": {},
                              "text": 0, "think": 0, "applied": []}
        req["o"] += o; req["th"] += th
        for blk in content:
            if not isinstance(blk, dict):
                continue
            n = block_chars(blk)
            t["since"]["other"] += n          # in context, so in the next call's write
            if blk.get("type") == "tool_use" and blk.get("name"):
                k = (blk["name"], *tool_layers(blk["name"], blk.get("input")))
                t["ids"][blk.get("id")] = k
                req["uses"][blk.get("id")] = (k, n)
                self._add(k, model, "calls", 1)
            elif blk.get("type") == "thinking":
                req["think"] += n
            else:
                req["text"] += n
        # The call's output, re-split over its tools whenever a later line adds blocks:
        # output text by tool_use size (thinking blocks count as text when the call
        # reports no thinking tokens), thinking per THINKING_TO_TOOLS.
        m = req["model"]
        for nm, f, v in req["applied"]:
            self._add(nm, m, f, -v)
        req["applied"] = []
        if req["uses"]:
            per = rate(prices, m, "output") / 1_000_000.0
            sizes = {i: n for i, (_, n) in req["uses"].items()}
            ask = size_shares(sizes, req["text"] + (0 if req["th"] else req["think"]))
            think = size_shares(sizes, 0) if THINKING_TO_TOOLS else {}
            for i, (nm, _) in req["uses"].items():
                for f, v in (("ask", (req["o"] - req["th"]) * per * ask.get(i, 0)),
                             ("think", req["th"] * per * think.get(i, 0))):
                    if v:
                        self._add(nm, m, f, v); req["applied"].append((nm, f, v))

    def _charge(self, model, key, usd):
        if self._open is not None:
            self._open[1][key] = self._open[1].get(key, 0.0) + usd
        bm = self.cost_bm.setdefault(model, {})
        bm[key] = bm.get(key, 0.0) + usd
        self.cost_by[key] = self.cost_by.get(key, 0.0) + usd
        self.cost += usd

    def move(self, model, src, dst, usd, entry=None):
        """Re-file dollars of one model from one rate to another (same total). entry: the
        call's series entry, so the chart re-files them at the time they were spent."""
        for d in (self.cost_bm.setdefault(model, {}), self.cost_by) + ((entry[1],) if entry else ()):
            d[src] = d.get(src, 0.0) - usd
            d[dst] = d.get(dst, 0.0) + usd

    def fee(self, model, usd):
        """Charges that are not tokens (web search), ledger only."""
        self._charge(model, "fees", usd)

    def add(self, u, model, prices, ts, content=None, dup=False, source=None, key=None, agent=None):
        # dup: a later log line for a request already counted. u then holds
        # only the usage delta, and the message is not counted twice.
        cc = u.get("cache_creation") or {}
        w1 = int(cc.get("ephemeral_1h_input_tokens") or 0)
        w5 = int(cc.get("ephemeral_5m_input_tokens") or 0)
        if not (w1 or w5):
            w5 = int(u.get("cache_creation_input_tokens") or 0)
        i  = int(u.get("input_tokens") or 0)
        r  = int(u.get("cache_read_input_tokens") or 0)
        o  = int(u.get("output_tokens") or 0)
        th = min(o, int(((u.get("output_tokens_details") or {}).get("thinking_tokens")) or 0))

        self.inp += i; self.cw5 += w5; self.cw1h += w1; self.cr += r
        self.out += o; self.think += th; self.msgs += 0 if dup else 1
        # One series entry per call: a later log line of the same call (dup) adds to the
        # entry its first line opened, so the chart counts calls, not log lines. Helpers'
        # files are read one after another, so that entry need not be the latest one.
        hit = next((x for x in reversed(self.series[-50:]) if x[2] == key), None) if dup and key else None
        if hit is None:
            hit = [(ts or datetime.now(timezone.utc)).timestamp(), {}, key]
            self.series.append(hit)
        self._open = hit
        before = self.cost
        # Priced per call, where the model is known: exact for mixed models, and
        # thinking vs output text needs no session-wide ratio.
        for key, n in (("input", i), ("cache_write_5m", w5), ("cache_write_1h", w1),
                       ("cache_read", r), ("output", o - th), ("thinking", th)):
            self._charge(model, key, n * rate(prices, model, PRICE_KEY.get(key, key)) / 1_000_000.0)
        if source is not None:
            e = self.by_source.setdefault(source, [0, 0.0])
            e[0] += 1; e[1] += self.cost - before
        if not dup:
            self.models[model] = self.models.get(model, 0) + 1

        # Attribution. Asking for a tool costs the output tokens that emit the
        # call. The answer costs whatever it adds to context, which shows up as
        # the cache write on the NEXT call. That second part is usually the
        # larger of the two and is the one people are surprised by.
        if content is not None and key is not None:     # transcripts only: the ledger has no content
            self._attribute(self._thread(agent), key, model, prices, i, r, w1, w5, o, th, content, dup)

        if ts:
            if self.first is None or ts < self.first: self.first = ts
            if self.last  is None or ts > self.last:  self.last  = ts

    @property
    def tokens(self):
        return self.inp + self.cw5 + self.cw1h + self.cr + self.out


def usage_delta(prev, cur):
    """Usage in cur beyond prev, never negative."""
    def g(d, k): return int(d.get(k) or 0)
    out = {k: max(0, g(cur, k) - g(prev, k)) for k in
           ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens", "output_tokens")}
    pc, cc = prev.get("cache_creation") or {}, cur.get("cache_creation") or {}
    out["cache_creation"] = {k: max(0, g(cc, k) - g(pc, k)) for k in
                             ("ephemeral_1h_input_tokens", "ephemeral_5m_input_tokens")}
    pt = (prev.get("output_tokens_details") or {}); ct = (cur.get("output_tokens_details") or {})
    out["output_tokens_details"] = {"thinking_tokens": max(0, g(ct, "thinking_tokens") - g(pt, "thinking_tokens"))}
    return out


class Store:
    """Incrementally tails every transcript and keeps per-session buckets."""
    def __init__(self, prices):
        self.prices = prices
        self.offsets = {}          # path -> byte offset already consumed
        self.sessions = {}         # session_id -> Bucket
        self.proj_of = {}          # session_id -> project slug
        self.seen = {}             # dedupe key -> usage already counted
        self.events = []           # (epoch_seconds, cost) for rate windows
        self.prompts = {}          # session_id -> epochs of the user's prompts
        self.think = {}            # requestId -> thinking tokens (telemetry lacks them; see LedgerStore.reconcile)
        self.titles = {}           # session_id -> (rank, title): the user's own title (2) beats the generated one (1)
        self.cwd_of = {}           # session_id -> working directory its transcript names
        self.proj_cwd = {}         # project slug -> a working directory seen in it
        self.present = set()       # session ids with a transcript on disk at the last scan
        self.released = {}         # session_id -> project slug of a Desktop session deleted there
        self.read_bytes = self.read_files = 0   # what the last scan read (for the timing log)
        self.rewound, self.gone = [], []        # logs that shrank, logs that vanished, at the last scan

    def scan(self):
        self.read_bytes = self.read_files = 0
        self.rewound, self.gone = [], []
        if not os.path.isdir(ROOT):
            self.gone = list(self.offsets)
            return
        present, released, paths = set(), {}, set()
        for proj in os.listdir(ROOT):
            pdir = os.path.join(ROOT, proj)
            if not os.path.isdir(pdir):
                continue
            for name in os.listdir(pdir):
                if name.endswith(".jsonl"):
                    present.add(name[:-len(".jsonl")])
                elif name.endswith(".desktop-released.json"):
                    released[name[:-len(".desktop-released.json")]] = proj
            # Helpers' transcripts sit under <session>/subagents and carry the
            # parent's sessionId, so they land in the parent's bucket.
            for name in os.listdir(pdir) + [os.path.relpath(x, pdir) for x in
                                            glob.glob(os.path.join(pdir, "*", "subagents", "*.jsonl"))]:
                if not name.endswith(".jsonl"):
                    continue
                path = os.path.join(pdir, name)
                try:
                    size = os.path.getsize(path)
                except OSError:
                    continue
                paths.add(path)
                off = self.offsets.get(path, 0)
                if size < off:            # file was truncated or rotated
                    self.rewound.append(path)
                    off = 0
                if size == off:
                    continue
                try:
                    with open(path, "r", encoding="utf-8", errors="replace") as fh:
                        fh.seek(off)
                        chunk = fh.read()
                        self.offsets[path] = fh.tell()
                except OSError:
                    continue
                self.read_files += 1
                self.read_bytes += size - off
                # a partial trailing line is left for the next pass
                if chunk and not chunk.endswith("\n"):
                    cut = chunk.rfind("\n")
                    if cut == -1:
                        self.offsets[path] = off
                        continue
                    self.offsets[path] = off + len(chunk[:cut+1].encode("utf-8", "replace"))
                    chunk = chunk[:cut+1]
                self._consume(chunk, proj)
        self.present, self.released = present, released
        self.gone = [p for p in self.offsets if p not in paths]

    def title(self, sid):
        """The session's title as Claude Code shows it, or None."""
        return self.titles.get(sid, (0, None))[1]

    def deleted(self, sid):
        """True when the session's transcript is gone (e.g. deleted in Claude Desktop)."""
        return os.path.isdir(ROOT) and sid not in self.present

    def project(self, sid):
        """The folder the session ran in, by name, or None."""
        proj = self.proj_of.get(sid) or self.released.get(sid)
        cwd = self.cwd_of.get(sid) or self.proj_cwd.get(proj)
        if cwd:
            return os.path.basename(cwd.rstrip("/\\")) or cwd
        if not proj:
            return None
        # The slug is the path with every other character as "-", so only the home part can go.
        home = re.sub(r"[^A-Za-z0-9]", "-", _HOME)
        return (proj[len(home):].lstrip("-") or proj) if proj.startswith(home) else proj

    def _consume(self, chunk, proj):
        for line in chunk.splitlines():
            if '"type":"custom-title"' in line or '"type":"ai-title"' in line:
                try:
                    o = json.loads(line)
                except Exception:
                    continue
                rank, t = (2, o.get("customTitle")) if o.get("type") == "custom-title" else (1, o.get("aiTitle"))
                sid = o.get("sessionId") or "unknown"
                if t and rank >= self.titles.get(sid, (0, None))[0]:
                    self.titles[sid] = (rank, t.strip())
                continue
            if '"type":"user"' in line or '"type":"attachment"' in line or '"compact_boundary"' in line:
                try:
                    o = json.loads(line)
                except Exception:
                    continue
                if o.get("type") == "user":
                    self._prompt(o)
                if o.get("type") in ("user", "attachment", "system"):
                    sid = o.get("sessionId") or "unknown"
                    self.sessions.setdefault(sid, Bucket()).note(o)
                    self.proj_of.setdefault(sid, proj)
                    if o.get("cwd"):
                        self.cwd_of.setdefault(sid, o["cwd"])
                        self.proj_cwd.setdefault(proj, o["cwd"])
                continue
            if '"usage"' not in line:
                continue
            try:
                o = json.loads(line)
            except Exception:
                continue
            if o.get("type") != "assistant":
                continue
            msg = o.get("message") or {}
            u = msg.get("usage")
            if not isinstance(u, dict) or msg.get("model") == "<synthetic>":
                continue                  # placeholder replies are not API calls
            # One request can span several log lines. Count it once, and let
            # later lines add only what grew (usually output), so the final
            # count wins instead of the first partial one.
            key = o.get("requestId") or o.get("uuid")
            full = u
            if o.get("requestId"):
                self.think[o["requestId"]] = int((u.get("output_tokens_details") or {}).get("thinking_tokens") or 0)
            dup = False
            if key:
                prev = self.seen.get(key)
                self.seen[key] = u
                if prev is not None:
                    dup = True
                    u = usage_delta(prev, u)
            sid = o.get("sessionId") or "unknown"
            ts = parse_ts(o.get("timestamp"))
            b = self.sessions.setdefault(sid, Bucket())
            before, tbefore = b.cost, b.tokens
            b.add(u, msg.get("model") or "unknown", self.prices, ts, msg.get("content") or [], dup,
                  key=key, agent=thread_of(o))
            if ts and ts >= b.last:
                cc = full.get("cache_creation") or {}
                w1 = int(cc.get("ephemeral_1h_input_tokens") or 0)
                b.ctx = (int(full.get("input_tokens") or 0) + int(full.get("cache_read_input_tokens") or 0)
                         + int(full.get("cache_creation_input_tokens") or 0))
                b.ctx_model = msg.get("model") or "unknown"
                b.ttl = "1h" if w1 or not full.get("cache_creation_input_tokens") else "5m"
            self.proj_of[sid] = proj
            t = (ts or datetime.now(timezone.utc)).timestamp()
            self.events.append((t, b.cost - before, b.tokens - tbefore))
            b.calls.append((t, b.cost - before))

    def _prompt(self, o):
        """Record when the user sent a prompt. Tool results and meta lines are
        also type "user" in the transcript, so only real text counts."""
        if o.get("type") != "user" or o.get("isMeta") or o.get("isSidechain"):
            return
        c = (o.get("message") or {}).get("content")
        if isinstance(c, list):
            if any(isinstance(x, dict) and x.get("type") == "tool_result" for x in c):
                return
            c = " ".join(x.get("text", "") for x in c if isinstance(x, dict) and x.get("type") == "text")
        if not isinstance(c, str) or not c.strip() or c.lstrip().startswith("<local-command"):
            return
        ts = parse_ts(o.get("timestamp"))
        if ts:
            self.prompts.setdefault(o.get("sessionId") or "unknown", []).append(ts.timestamp())

    def window_cost(self, seconds):
        cutoff = time.time() - seconds
        return sum(c for t, c, _ in self.events if t >= cutoff)

    def day_total(self):
        return day_total(self.events)

    def newest_session(self):
        best, best_ts = None, None
        for sid, b in self.sessions.items():
            if b.last and (best_ts is None or b.last > best_ts):
                best, best_ts = sid, b.last
        return best


MATCH_USD = 0.0005   # within this, Claude Code's figure and ours count as the same price

def write_ttl(e, prices):
    """The rate a ledger line's cache write was billed at, "1h" or "5m". Telemetry does
    not say; Claude Code's main loop writes 1h, helper agents, SDK calls and compaction
    write 5m. Claude Code's own figure (cost_usd_cc) tells them apart: fees only add to
    it, so a figure below the 1h price (and not below the 5m one) means a 5m write, and
    what it has above the 5m price is a fee. Without a figure, 1h. Worked out on read,
    so lines written before this are filed too."""
    t = e.get("tokens") or {}
    w, cc = int(t.get("cache_write") or 0), e.get("cost_usd_cc")
    if not w or cc is None:
        return "1h"
    m = e.get("model") or "unknown"
    rest = (int(t.get("input") or 0) * rate(prices, m, "input") + int(t.get("output") or 0) * rate(prices, m, "output")
            + int(t.get("cache_read") or 0) * rate(prices, m, "cache_read")) / 1_000_000.0
    p1 = rest + w * rate(prices, m, "cache_write_1h") / 1_000_000.0
    p5 = rest + w * rate(prices, m, "cache_write_5m") / 1_000_000.0
    return "5m" if p5 - MATCH_USD < float(cc) < p1 - MATCH_USD else "1h"

def fee(e, tokens_usd=None):
    """Charges that are not tokens (web search is billed per search): whatever Claude
    Code's own cost figure has on top of the token price. Worked out on read, against
    the tokens repriced at the current rates when the caller has them (tokens_usd), so a
    price table corrected after a line was written leaves no stale fee on top. The
    stored fees_usd is only the collector's figure at write time and is used when the
    line has no cost figure at all."""
    cc = e.get("cost_usd_cc")
    if cc is None:
        return float(e.get("fees_usd") or 0.0)
    d = float(cc) - (float(e.get("cost_usd") or 0.0) if tokens_usd is None else tokens_usd)
    return d if d > MATCH_USD else 0.0

class LedgerStore:
    """Same shape as Store, built from the ledger the hooks module writes: one line per
    API request, in ledger/<day>/<session-id>.jsonl. Covers every call, including the
    background ones transcripts miss. The source of every total."""
    def __init__(self, prices):
        self.prices = prices
        self.offsets = {}
        self.sessions = {}
        self.events = []
        self.reqs = {}        # request_id -> [session, model, output tokens, thinking tokens filed, series entry]
        self.read_bytes = self.read_files = 0
        self.rewound, self.gone = [], []

    def reconcile(self, store):
        """Telemetry reports output tokens but not how many were thinking, so a ledger
        bucket would file all of it as output text. The transcript has the same call
        (same request id) with the thinking count: re-file that many tokens from
        output to thinking. Exact, not an estimate: both are billed at the output rate,
        so cost does not change. Safe on every refresh; a call whose transcript line
        is not written yet is picked up on a later pass."""
        for rid, r in self.reqs.items():
            th = min(store.think.get(rid, 0), r[2])
            if th > r[3]:
                b = self.sessions[r[0]]
                b.move(r[1], "output", "thinking", (th - r[3]) * rate(self.prices, r[1], "output") / 1_000_000.0, r[4])
                b.think += th - r[3]; r[3] = th

    def unmatched(self, sid, store):
        """Output tokens of this session's calls with no transcript line (background
        calls): their thinking, if any, stays in output text."""
        return sum(r[2] for rid, r in self.reqs.items() if r[0] == sid and rid not in store.think)

    def scan(self):
        self.read_bytes = self.read_files = 0
        self.rewound, self.gone = [], []
        paths = sorted(glob.glob(os.path.join(CFG, "ledger", "*", "*.jsonl")))
        known = set(paths)
        self.gone = [p for p in self.offsets if p not in known]
        for p in paths:
            try:
                if os.path.getsize(p) < self.offsets.get(p, 0):
                    self.rewound.append(p)
                    continue
                with open(p, "rb") as f:
                    f.seek(self.offsets.get(p, 0))
                    chunk = f.read()
            except OSError:
                continue
            if chunk:
                self.read_files += 1
                self.read_bytes += len(chunk)
            end = chunk.rfind(b"\n") + 1          # only whole lines
            self.offsets[p] = self.offsets.get(p, 0) + end
            for line in chunk[:end].splitlines():
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                t = e.get("tokens") or {}
                u = {"input_tokens": t.get("input"), "output_tokens": t.get("output"),
                     "cache_read_input_tokens": t.get("cache_read"),
                     "cache_creation": {f"ephemeral_{write_ttl(e, self.prices)}_input_tokens": t.get("cache_write")}}
                ts = parse_ts(e.get("ts"))
                b = self.sessions.setdefault(e.get("session_id") or "unknown", Bucket())
                before, tbefore = b.cost, b.tokens
                src = e.get("query_source") or "unknown"
                b.add(u, e.get("model") or "unknown", self.prices, ts, source=src)
                if e.get("request_id"):
                    self.reqs[e["request_id"]] = [e.get("session_id") or "unknown", e.get("model") or "unknown",
                                                  int(t.get("output") or 0), 0, b._open]
                f = fee(e, b.cost - before)      # tokens repriced at today's rates
                if f:
                    b.fee(e.get("model") or "unknown", f)
                    b.by_source[src][1] += f
                t = (ts or datetime.now(timezone.utc)).timestamp()
                self.events.append((t, b.cost - before, b.tokens - tbefore))
                b.calls.append((t, b.cost - before))

    def window_cost(self, seconds):
        cut = time.time() - seconds
        return sum(c for t, c, _ in self.events if t >= cut)

    def day_total(self):
        return day_total(self.events)


# What the scan has read, kept between runs. Every pane refresh, status line, page push and
# skill is a new Python process; without this each one re-reads every transcript in projects/
# from the start (over a GB after a busy month, longer than the module's timeout). The state
# file holds both stores as they stand after a scan, so a run only reads what was appended
# since. It is thrown away and rebuilt from a full read when it cannot be trusted: a different
# tokentab.py or price table (key), a log that shrank or vanished (Claude Code's cleanup, a
# session deleted in Desktop), or a file that does not load. A lock lets one process at a
# time read and write it, so sessions starting together share one full read.
def _state(name):
    return os.path.join(CFG, "state", name)   # read at call time: the tests point CFG elsewhere


PERF_MAX = 1_000_000          # bytes; past this the timing log keeps its newer half
SCAN_FORMAT = 1               # raise when the stores' fields change shape
PERF = {}                     # this run's timings, for the timing log


def scan_key(prices):
    """What the saved stores depend on: this file's code, the prices, the folders read."""
    import hashlib
    h = hashlib.sha256()
    try:
        with open(os.path.abspath(__file__), "rb") as f:
            h.update(f.read())
    except OSError:
        h.update(b"?")
    h.update(json.dumps([SCAN_FORMAT, list(sys.version_info[:2]), ROOT, CFG, prices], sort_keys=True,
                        default=str).encode("utf-8"))
    return h.hexdigest()


class ScanLock:
    """An exclusive lock on state/scan.lock for the length of a with block. The OS drops it when the
    process ends, a killed one included. A lock that cannot be taken is skipped, not waited on."""
    def __enter__(self):
        t = time.perf_counter()
        self.f = None
        try:
            path = _state("scan.lock")
            os.makedirs(os.path.dirname(path), exist_ok=True)
            self.f = open(path, "a+b")
            if os.name == "nt":
                import msvcrt
                while True:
                    try:
                        self.f.seek(0)
                        msvcrt.locking(self.f.fileno(), msvcrt.LK_NBLCK, 1)
                        break
                    except OSError:
                        time.sleep(0.05)
            else:
                import fcntl
                fcntl.flock(self.f.fileno(), fcntl.LOCK_EX)
        except OSError:
            if self.f:
                self.f.close()
            self.f = None
        self.waited = time.perf_counter() - t
        return self

    def __exit__(self, *exc):
        if self.f:
            try:
                if os.name == "nt":
                    import msvcrt
                    self.f.seek(0)
                    msvcrt.locking(self.f.fileno(), msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
            self.f.close()


def scanned(prices):
    """(Store, LedgerStore), both scanned up to now: the saved ones caught up on what was
    appended since they were saved, or new ones from a full read when there are none or
    they cannot be trusted. Saves them again when the scan read anything."""
    import pickle
    clock, path = time.perf_counter, _state("scan.pickle")
    with ScanLock() as lock:
        PERF["lock_s"] = lock.waited
        t = clock()
        key, store, led, reason = scan_key(prices), None, None, None
        try:
            with open(path, "rb") as f:
                saved = pickle.load(f)
            if saved.get("key") == key:
                store, led = saved["store"], saved["led"]
            else:
                reason = "changed"
        except FileNotFoundError:
            reason = "none"
        except Exception:
            reason = "unreadable"
        PERF["load_s"] = clock() - t
        t = clock()
        if store is not None:
            store.prices = led.prices = prices
            store.scan(); led.scan()
            if store.rewound or led.rewound:
                reason, store = "shrank", None
            elif store.gone or led.gone:
                reason, store = "vanished", None
        PERF["kind"] = "incremental" if store is not None else "full"
        if reason:
            PERF["reason"] = reason
        if store is None:
            store, led = Store(prices), LedgerStore(prices)
            store.scan(); led.scan()
        PERF["scan_s"] = clock() - t
        PERF["read_bytes"] = store.read_bytes + led.read_bytes
        PERF["read_files"] = store.read_files + led.read_files
        PERF["tracked_files"] = len(store.offsets) + len(led.offsets)
        t = clock()
        if PERF["kind"] == "full" or PERF["read_bytes"]:
            tmp = f"{path}.{os.getpid()}.tmp"
            try:
                with open(tmp, "wb") as f:
                    pickle.dump({"key": key, "store": store, "led": led}, f, protocol=4)
                os.replace(tmp, path)
            except Exception:
                try:
                    os.remove(tmp)
                except OSError:
                    pass
        PERF["save_s"] = clock() - t
        try:
            PERF["state_bytes"] = os.path.getsize(path)
        except OSError:
            pass
    return store, led


def perf_log(entry):
    """Appends one line to the timing log (state/perf.jsonl). Never fails the run."""
    PERF_LOG = _state("perf.jsonl")
    try:
        os.makedirs(os.path.dirname(PERF_LOG), exist_ok=True)
        line = json.dumps(dict({"ts": datetime.now(timezone.utc).isoformat(timespec="seconds")}, **entry)) + "\n"
        with open(PERF_LOG, "a", encoding="utf-8") as f:
            f.write(line)
        if os.path.getsize(PERF_LOG) > PERF_MAX:
            with open(PERF_LOG, encoding="utf-8") as f:
                lines = f.readlines()
            tmp = f"{PERF_LOG}.{os.getpid()}.tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                f.writelines(lines[len(lines) // 2:])
            os.replace(tmp, PERF_LOG)
    except Exception:
        pass


def perf_done(script, mode, sid=None, error=None):
    """Logs this run: what ran, how the scan went, and the time each phase took (seconds)."""
    e = {"script": script, "mode": mode, "version": plugin_version()}
    if sid:
        e["session"] = sid
    e.update({k: round(v, 3) if isinstance(v, float) else v for k, v in PERF.items()})
    e["total_s"] = round(time.perf_counter() - _T0, 3)
    e["output_s"] = round(max(0.0, e["total_s"] - sum(PERF.get(k, 0.0) for k in ("lock_s", "load_s", "scan_s", "save_s"))), 3)
    if error:
        e["error"] = error
    perf_log(e)


def plugin_version():
    try:
        with open(os.path.join(HERE, "..", ".claude-plugin", "plugin.json"), encoding="utf-8") as f:
            return json.load(f).get("version")
    except Exception:
        return None


def requests(calls, prompts):
    """Cost of each request: one prompt and every call until the next prompt.
    That includes the prompt suggestion Claude Code makes after the reply.
    Returns [(start_epoch, USD, calls)], oldest first, one per prompt (the chart marks
    every prompt), so a prompt that never reached the API is there with 0 calls: see
    made_calls()."""
    starts = sorted(prompts)
    out = [[t, 0.0, 0] for t in starts]
    for t, c in calls:
        i = bisect.bisect_right(starts, t) - 1
        if i >= 0:
            out[i][1] += c
            out[i][2] += 1
    return [tuple(x) for x in out]


def made_calls(reqs):
    """The requests that reached the API. A local slash command (/tokentab:status) or a
    prompt the expired-cache check held back is a prompt with no call: not a request for
    latest, previous, the average or the count."""
    return [r for r in reqs if r[2]]


def day_total(events):
    """(USD, tokens) spent today, local time, across every session."""
    today = datetime.now().astimezone().date()
    c = t = 0
    for ts, dc, dt in events:
        if datetime.fromtimestamp(ts).date() == today:
            c += dc; t += dt
    return c, t


def month_total(events):
    """USD spent this calendar month, local time, across every session."""
    now = datetime.now().astimezone()
    return sum(dc for ts, dc, _ in events
               if (d := datetime.fromtimestamp(ts)).year == now.year and d.month == now.month)


def month_sessions(store, src):
    """This month's spend per session, costliest first: [{sid, name, cost, share}]."""
    now = datetime.now().astimezone()
    spent = {sid: c for sid, b in src.sessions.items()
             if (c := sum(dc for ts, dc in b.calls
                          if (d := datetime.fromtimestamp(ts)).year == now.year and d.month == now.month)) > 0}
    total = sum(spent.values())
    return [session_row(store, sid, c, total) for sid, c in sorted(spent.items(), key=lambda kv: -kv[1])]


def session_row(store, sid, cost, total):
    """One row of the sessions list: its title, else its project, else its id; `detail` is the
    dimmed rest: the short id when untitled, so two sessions in one project stay apart, and
    whether the transcript is gone."""
    title = store.title(sid)
    name = title or store.project(sid) or sid[:8]
    detail = [] if title or name == sid[:8] else [sid[:8]]
    if store.deleted(sid):
        detail.append("deleted")
    return {"sid": sid, "name": name, "detail": " · ".join(detail), "cost": cost, "share": cost / total if total else 0}


def parse_ts(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None

# ---------- rendering ----------

def money(x):
    # Telemetry reports cost to the micro-dollar, so show 4 places, not cents.
    return f"${x:,.4f}"

def num(x):
    return f"{x:,}"

DIM, BOLD, RESET = "\033[2m", "\033[1m", "\033[0m"
CYAN, YELLOW, RED, GREEN = "\033[36m", "\033[33m", "\033[31m", "\033[32m"

def bar(frac, width=28):
    frac = max(0.0, min(1.0, frac))
    filled = int(round(frac * width))
    color = GREEN if frac < 0.7 else (YELLOW if frac < 1.0 else RED)
    return color + "█" * filled + DIM + "·" * (width - filled) + RESET

# ---------- limits: USD, or percent of the plan's usage windows ----------

# The limits of each unit, one field per scope: (scope, budget.json key, label)
LIMITS = {"usd": (("session", "session_limit_usd", "session"), ("today", "daily_limit_usd", "today")),
          "percent": (("five_hour", "five_hour_limit_percent", "5 hours"),
                      ("seven_day", "weekly_limit_percent", "week"))}

WINDOW_NAMES = {"five_hour": "5-hour window", "seven_day": "weekly window"}

def _read_json(path):
    try:
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
    except (OSError, ValueError):
        return None
    return doc if isinstance(doc, dict) else None

def read_windows(sid=None):
    """{kind: window} of the session's 5-hour and weekly windows; {} off a subscription or before
    the session's first reading. Only its own: another session's may be another login's."""
    doc = _read_json(os.path.join(WINDOWS_DIR, sid + ".json")) if sid else None
    if doc is None:
        return {}
    ws = doc.get("windows") or []
    return {w["kind"]: w for w in ws if isinstance(w, dict) and w.get("kind") in ("five_hour", "seven_day")}

def limit_unit(budget, windows):
    """"percent" where the account reports plan usage windows (Pro, Max), else "usd". Not a setting:
    the account decides. An old budget.json's limit_unit is ignored."""
    return "percent" if windows else "usd"

def window_used(w, now=None):
    """Percent of a window used; 0 once it has reset since the reading."""
    try:
        reset = datetime.fromisoformat(str(w.get("resetsAt")).replace("Z", "+00:00")).timestamp()
        if reset <= (now or time.time()):
            return 0.0
    except ValueError:
        pass
    return float(w.get("percentUsed") or 0)

def reset_text(iso, now=None):
    """When a window resets, local time: `16:00` today, else `Thu 09:00`; None once past."""
    try:
        at = datetime.fromisoformat(str(iso).replace("Z", "+00:00")).astimezone()
    except ValueError:
        return None
    now = datetime.fromtimestamp(now or time.time()).astimezone()
    if at <= now:
        return None
    return at.strftime("%H:%M" if at.date() == now.date() else "%a %H:%M")

def limit_rows(budget, spent, windows):
    """(unit, rows): each limit set in the unit in effect, {scope, name, frac, used, limit, parts, reset}.
    spent is {"session": usd, "today": usd}; a percent limit with no reading yet is left out."""
    unit = limit_unit(budget, windows)
    rows = []
    for scope, key, name in LIMITS[unit]:
        lim = budget.get(key)
        if not lim:
            continue
        lim = float(lim)
        if unit == "usd":
            used, detail, reset = spent[scope], f"{money(spent[scope])} of ${lim:,.2f}", None
        elif scope in windows:
            w = windows[scope]
            used, reset = window_used(w), w.get("resetsAt")
            detail = f"{used:g}% used, limit {lim:g}%"
        else:
            continue
        resets = reset_text(reset) if reset else None
        rows.append({"scope": scope, "name": name, "frac": used / lim, "used": used, "limit": lim,
                     "parts": [detail] + ([f"resets {resets}"] if resets else []), "reset": reset, "resets": resets})
    return unit, rows


def budget_text(r):
    frac = r["frac"]
    over = f"  {RED}{BOLD}OVER{RESET}" if frac >= 1.0 else ""
    return (f"    {r['name']:<9} {bar(frac, 20)} {frac*100:5.0f}%   "
            f"{DIM}{' · '.join(r['parts'])}{RESET}{over}")


SOURCE_LINE = "data: ledger (every API call, helpers and background calls included)"

def source(store, led, sid):
    """(bucket, store) for this session: the ledger's, reconciled with the transcripts
    for the thinking split. The ledger is complete for every session the plugin loaded
    in, so transcripts are never a fallback for totals; a session with no ledger lines
    yet shows as waiting. Without a ledger store (library use, the tests) the transcript
    bucket is reported instead."""
    if led is None:
        return store.sessions.get(sid), store
    led.reconcile(store)              # thinking counts come from the transcripts
    return led.sessions.get(sid), led


def report(store, sid, prices, budget, show_all, led=None):
    """Everything the live view shows, as data: render() prints it in the terminal and
    sankey.py draws it on the page, so the two cannot drift apart."""
    tb = store.sessions.get(sid)          # transcripts: only source of tool detail
    b, src = source(store, led, sid)
    R = {"sid": sid, "now": datetime.now(), "prices_verified": bool(prices.get("_verified")), "session": None,
         "bucket": b, "transcript": tb}   # the buckets themselves, for the Sankey
    if not b:
        return R
    R["session"] = {"title": store.title(sid), "models": sorted(b.models), "since": b.first.astimezone() if b.first else None,
                    "source": SOURCE_LINE}
    rows = [
        ("input",            b.inp,  "input"),
        ("cache write 1h",   b.cw1h, "cache_write_1h"),
        ("cache write 5m",   b.cw5,  "cache_write_5m"),
        ("cache read",       b.cr,   "cache_read"),
        ("output text",      b.out - b.think, "output"),
        ("thinking",         b.think, "thinking"),
        ("fees (web search)", 0,     "fees"),
    ]
    R["rates"] = []
    for label, toks, key in rows:
        if toks == 0 and (key.startswith("cache_write") or key == "thinking"
                          or (key == "fees" and not b.cost_by.get("fees"))):
            continue
        c = b.cost_by.get(key, 0.0)
        R["rates"].append({"label": label, "key": key, "tokens": toks, "cost": c,
                           "share": (c / b.cost) if b.cost else 0})
    R["unmatched"] = led.unmatched(sid, store) if led else 0
    R["total"] = {"tokens": b.tokens, "cost": b.cost, "msgs": b.msgs}
    R["prompts"] = store.prompts.get(sid, [])
    reqs = made_calls(requests(b.calls, R["prompts"]))
    R["requests"] = reqs and {"latest": reqs[-1], "previous": reqs[-2] if len(reqs) > 1 else None,
                              "average": sum(r[1] for r in reqs) / len(reqs), "count": len(reqs)}
    R["by_source"] = [(nm, calls, c, (c / b.cost) if b.cost else 0)
                      for nm, (calls, c) in sorted(b.by_source.items(), key=lambda kv: -kv[1][1])]
    R["window"] = {"5m": src.window_cost(300), "1h": src.window_cost(3600)}
    windows = read_windows(sid)
    # A subscription reports plan windows; its dollars are API-rate equivalents, not a bill
    R["plan"] = bool(windows)

    dc = src.day_total()[0]               # today, every session

    R["budget"] = None
    if budget:
        cold = budget.get("cold_resume") or {}
        on = budget.get("budgets_on") is True
        unit, lines = limit_rows(budget, {"session": b.cost, "today": dc}, windows)
        lines = lines if on else []
        ctx = None
        if tb and tb.ctx:
            # Same rules as guard.py: re-read cost per call, and whether the
            # next prompt would hit an expired cache.
            idle = time.time() - max(x.last for x in (tb, b) if x.last).timestamp()
            lim = 60 * float(cold.get("idle_minutes_" + tb.ttl, 55 if tb.ttl == "1h" else 4))
            marks = sorted(int(x) for x in (budget.get("context_warn_tokens") or []))
            ctx = {"tokens": tb.ctx,
                   "level": "over" if marks and tb.ctx >= marks[-1] else ("warn" if marks and tb.ctx >= marks[0] else ""),
                   "per_call": tb.ctx * rate(prices, tb.ctx_model, "cache_read") / 1e6,
                   "cold": idle >= lim, "minutes_left": int((lim - idle) // 60),
                   "rewrite": tb.ctx * rate(prices, tb.ctx_model, "cache_write_" + tb.ttl) / 1e6}
        R["budget"] = {"on": on, "unit": unit, "hard_stop": bool(budget.get("hard_stop")), "warn_at": budget.get("warn_at_percent", 70),
                       "lines": lines, "context": ctx}

    R["tools"] = tool_totals(tb.tools) if tb and tb.tools else []
    R["overhead"] = overhead_total(tb) if tb else {"cost": 0}
    R["totals"] = None
    if show_all:                          # the ledger knows sessions, not projects
        R["totals"] = {"today": dc, "all": sum(x.cost for x in src.sessions.values()), "sessions": len(src.sessions)}
    return R


def render(store, sid, prices, budget, show_all, led=None):
    R = report(store, sid, prices, budget, show_all, led)
    L = []
    warn = "" if R["prices_verified"] else f"  {YELLOW}prices unverified{RESET}"
    L.append(f"{BOLD}TokenTab{RESET} {DIM}· live{RESET}{warn}"
             f"{DIM}{R['now'].strftime('%H:%M:%S').rjust(max(1, 62 - 22 - len(warn)))}{RESET}")
    L.append("")

    s = R["session"]
    if not s:
        L.append(f"  {DIM}waiting for activity...{RESET}")
        return "\n".join(L)

    started = s["since"].strftime("%H:%M") if s["since"] else "?"
    title = f"{s['title']} {DIM}· " if s["title"] else DIM
    L.append(f"  {BOLD}SESSION{RESET} {title}{sid[:8]} · {', '.join(s['models'])} · since {started}{RESET}")
    L.append(f"  {DIM}{s['source']}{RESET}")
    L.append("")
    for r in R["rates"]:
        L.append(f"    {r['label']:<16}{num(r['tokens']):>14}{money(r['cost']):>12}   {DIM}{r['share']*100:4.0f}%{RESET}")
    if R["unmatched"]:
        L.append(f"    {DIM}{'':<16}{num(R['unmatched']):>14} output tok without a transcript line: any thinking is in output text{RESET}")
    t = R["total"]
    L.append(f"    {DIM}{'-'*44}{RESET}")
    L.append(f"    {BOLD}{'total':<16}{num(t['tokens']):>14}{money(t['cost']):>12}{RESET}"
             f"   {DIM}{t['msgs']} msgs{' · at API rates' if R['plan'] else ''}{RESET}")
    q = R["requests"]
    if q:
        L.append("")
        L.append(f"  {BOLD}REQUESTS{RESET}  {DIM}one prompt and everything it set off{RESET}")
        for label in ("latest", "previous"):
            r = q[label]
            if r:
                at = datetime.fromtimestamp(r[0]).strftime("%H:%M")
                L.append(f"    {label:<16}{money(r[1]):>26}   {DIM}{r[2]} calls · {at}{RESET}")
        L.append(f"    {DIM}{'average':<16}{money(q['average']):>26}   of {q['count']} requests{RESET}")
    L.append("")
    if R["by_source"]:
        L.append("")
        L.append(f"  {BOLD}BY CALL SOURCE{RESET}  {DIM}what in Claude Code made the call{RESET}")
        for nm, calls, c, share in R["by_source"][:8]:
            L.append(f"    {nm[:24]:<24}{calls:>5} {DIM}calls{RESET}{money(c):>11}"
                     f"   {DIM}{share * 100:4.0f}%{RESET}")
    L.append("")
    L.append(f"    {DIM}all sessions · last 5 min{RESET} {money(R['window']['5m']):>10}"
             f"    {DIM}last hour{RESET} {money(R['window']['1h']):>10}")

    bg = R["budget"]
    if bg:
        lines = [budget_text(r) for r in bg["lines"]]
        if lines or bg["context"]:
            L.append("")
            mode = ((f"{'hard stop' if bg['hard_stop'] else 'warn only'} · warns at {bg['warn_at']}%"
                     f"{' of plan usage' if bg['unit'] == 'percent' else ''}")
                    if bg["on"] else "off · /tokentab:budget turns budgets on")
            L.append(f"  {BOLD}BUDGET{RESET}  {DIM}{mode}{RESET}")
            L.extend(lines)
            c = bg["context"]
            if c:
                color = {"over": RED, "warn": YELLOW}.get(c["level"], DIM)
                if c["cold"]:
                    cache = (f"{RED}cache COLD{RESET}{DIM}: next prompt re-writes it, ~{money(c['rewrite'])}")
                else:
                    cache = f"cache warm, {c['minutes_left']}m left"
                L.append(f"    {'context':<9} {color}{num(c['tokens'])} tok{RESET}{DIM} · "
                         f"{money(c['per_call'])}/call to re-read · {cache}{RESET}")

    if R["tools"]:
        rows = R["tools"]
        L.append("")
        L.append(f"  {BOLD}TOOLS{RESET}  {DIM}asking + answer into context + re-reading it (estimates){RESET}")
        for nm, e in rows[:8]:
            short = nm.split("__")[-1][:22]
            L.append(f"    {short:<24}{e['calls']:>5} {DIM}calls{RESET}"
                     f"{money(e['cost']):>11}   {DIM}ask {money(e['ask'] + e['think'])} · ctx {money(e['result'])}"
                     f" ({num(e['rtokens'])} tok) · re-read {money(e['read'])}{RESET}")
        if len(rows) > 8:
            L.append(f"    {DIM}+{len(rows)-8} more{RESET}")
        kt_ = R["overhead"]
        if kt_["cost"]:
            L.append(f"    {DIM}{'TokenTab overhead':<30}{money(kt_['cost']):>11}   its hook text in context"
                     f" ({num(kt_['rtokens'])} tok) · re-read {money(kt_['read'])}{RESET}")

    tt = R["totals"]
    if tt:
        L.append("")
        L.append(f"  {BOLD}TOTALS{RESET}")
        L.append(f"    {'today, all sessions':<26}{money(tt['today']):>12}")
        L.append(f"    {'all sessions, all time':<26}{money(tt['all']):>12}   {DIM}{tt['sessions']} sessions{RESET}")

    L.append("")
    L.append(f"  {DIM}ctrl-c to quit{RESET}")
    return "\n".join(L)


def snapshot(store, sid, prices, led=None):
    b, src = source(store, led, sid)
    tb = store.sessions.get(sid)          # transcripts: only source of tool detail
    if not b:
        return {"session": sid, "cost_usd": 0.0, "tokens": 0, "messages": 0}
    return {
        "session": sid,
        "cost_usd": round(b.cost, 6),
        "tokens": b.tokens,
        "messages": b.msgs,
        "breakdown": {"input": b.inp, "cache_write_1h": b.cw1h, "cache_write_5m": b.cw5,
                      "cache_read": b.cr, "output": b.out, "output_text": b.out - b.think,
                      "thinking": b.think},
        # USD per rate (output = text only), summed and per model; both sum to cost_usd
        "cost_by": {k: round(v, 6) for k, v in b.cost_by.items()},
        "cost_by_model": {m: {k: round(v, 6) for k, v in d.items()} for m, d in b.cost_bm.items()},
        "tools": {k: {"calls": v["calls"], "cost_usd": round(v["cost"], 6),
                      **{f + "_usd": round(v[f], 6) for f in TOOL_DRAWS},
                      "context_tokens": v["rtokens"],
                      "by_model": {m: {f: (round(x, 6) if isinstance(x, float) else x) for f, x in d.items()}
                                   for m, d in tb.tools[k].items()}}
                  for k, v in tool_totals(tb.tools if tb else {})},
        "tokentab_overhead_usd": round(overhead_total(tb)["cost"], 6),
        "by_source": {k: {"calls": n, "cost_usd": round(c, 6)}
                      for k, (n, c) in sorted(b.by_source.items(), key=lambda kv: -kv[1][1])},
        "prices_verified": bool(prices.get("_verified")),
        "all_sessions_cost_usd": round(sum(x.cost for x in src.sessions.values()), 6),
        "requests": [{"start": datetime.fromtimestamp(t, timezone.utc).isoformat(),
                      "cost_usd": round(c, 6), "calls": n}
                     for t, c, n in made_calls(requests(b.calls, store.prompts.get(sid, [])))[-5:]],
    }


def pane_report(store, sid, prices, budget, led=None, windows=None):
    """The pane's Spend tab as data (`--report`), from the same report() as the live view
    and the status line, so the figures agree: spend (the session against its limit, the
    rate breakdown, the models, today and this month over every session) and tools."""
    R = report(store, sid, prices, budget, True, led)
    b, src = R["bucket"], source(store, led, sid)[1]
    out = {"sid": sid, "session": None, "prices_verified": R["prices_verified"],
           "today": src.day_total()[0], "month": month_total(src.events), "tools": [], "overhead": 0.0,
           "sessions": month_sessions(store, src)}
    if not b:
        return out
    on = budget.get("budgets_on") is True
    windows = read_windows(sid) if windows is None else windows
    unit, rows = limit_rows(budget, {"session": b.cost, "today": out["today"]}, windows)
    # The hero's bar: this session in USD; under percent the first window with a limit and a reading
    row = next((r for r in rows if r["scope"] in ("session", "five_hour", "seven_day")), None) if on else None
    plan_windows = [{"scope": r["scope"], "used": r["used"], "limit": r["limit"], "frac": r["frac"], "resets": r["resets"]}
                    for r in rows if r["scope"] in windows] if on and unit == "percent" else []
    out["session"] = {
        "title": R["session"]["title"], "cost": b.cost, "tokens": b.tokens, "msgs": b.msgs,
        "minutes": round((b.last - b.first).total_seconds() / 60) if b.first and b.last else 0,
        "limits_on": on, "unit": unit, "plan": bool(windows), "windows": plan_windows,
        "limit": None if not row else row["parts"][0] if unit == "usd"
                 else f"{WINDOW_NAMES[row['scope']]} {row['parts'][0]}",
        "frac": row["frac"] if row else None,
        "warn_at": budget.get("warn_at_percent", 70), "hard_stop": bool(budget.get("hard_stop")),
    }
    out["rates"] = [{"label": r["label"], "key": r["key"], "tokens": r["tokens"], "cost": r["cost"], "share": r["share"]}
                    for r in R["rates"]]
    out["total"] = {"tokens": R["total"]["tokens"], "cost": R["total"]["cost"]}
    out["unmatched"] = R["unmatched"]
    spent = {m: sum(d.values()) for m, d in b.cost_bm.items()}
    out["models"] = [{"name": m, "msgs": b.models.get(m, 0), "cost": c, "share": (c / b.cost) if b.cost else 0}
                     for m, c in sorted(spent.items(), key=lambda kv: -kv[1]) if c > 0 or b.models.get(m)]
    row = lambda name, e, kids: {"name": name, "calls": e["calls"], "cost": e["cost"], "ask": e["ask"] + e["think"],
                                 "ctx": e["result"], "read": e["read"], "ctx_tokens": e["rtokens"],
                                 "children": [row(*kid) for kid in kids]}
    parts = R["transcript"].parts if R["transcript"] else {}
    out["tools"] = [row(nm.split("__")[-1], e, tool_tree(nm, parts)) for nm, e in R["tools"]]
    out["overhead"] = R["overhead"]["cost"]
    return out


def status_line(store, sid, prices, budget, led=None):
    """One plain line for Claude Code's status bar while the pane is not placed (the hooks
    module runs `--status`): this session's cost and today's over every session. The same
    report() as the live view, so the figures agree."""
    R = report(store, sid, prices, budget, True, led)
    if R.get("total"):
        sess, today = R["total"]["cost"], R["totals"]["today"]
    else:
        sess, today = 0.0, source(store, led, sid)[1].day_total()[0]
    windows = read_windows(sid)
    dollars = f"session {money(sess)} · today {money(today)}{' at API rates' if windows else ''}"
    plan = [f"{name} {window_used(windows[scope]):g}%" for scope, _, name in LIMITS["percent"] if scope in windows]
    if plan and limit_unit(budget, windows) == "percent":
        return " · ".join(plan + [dollars])                           # the budget's unit leads
    return dollars                                                    # the engine draws the plugin name before the line


def main():
    ap = argparse.ArgumentParser(description="Live token and cost monitor for Claude Code.")
    ap.add_argument("--session", help="session id to pin to (default: most recently active)")
    ap.add_argument("--once", action="store_true", help="print one snapshot and exit")
    ap.add_argument("--json", action="store_true", help="machine-readable snapshot, implies --once")
    ap.add_argument("--status", action="store_true",
                    help="one plain line (session, today) for a status bar, implies --once")
    ap.add_argument("--report", action="store_true",
                    help="the pane's tabs as JSON (spend, tools), for the hooks module, implies --once")
    ap.add_argument("--all", action="store_true", help="include cross-project totals")
    ap.add_argument("--interval", type=float, default=2.0, help="refresh seconds (default 2)")
    ap.add_argument("--perf-note", metavar="JSON",
                    help="append this object to the timing log and exit (the hooks module's timeouts)")
    a = ap.parse_args()
    if a.perf_note:
        try:
            note = json.loads(a.perf_note)
        except ValueError:
            note = None
        perf_log(note if isinstance(note, dict) else {"note": a.perf_note})
        return
    mode = "report" if a.report else "json" if a.json else "status" if a.status else "once" if a.once else "live"
    try:
        run(a)
    except BaseException as e:
        if mode != "live" or not isinstance(e, KeyboardInterrupt):
            perf_done("tokentab.py", mode, a.session, error=type(e).__name__)
        raise
    if mode != "live":
        perf_done("tokentab.py", mode, a.session)


def run(a):

    prices = load_prices()
    budget = {}
    if os.path.exists(BUDGET_PATH):
        try:
            budget = json.load(open(BUDGET_PATH, encoding="utf-8"))
        except Exception:
            budget = {}

    store, led = scanned(prices)
    sid = a.session or store.newest_session()

    if a.json or a.status or a.once or a.report:
        if a.report:
            print(json.dumps(pane_report(store, sid, prices, budget, led)))
        elif a.json:
            print(json.dumps(snapshot(store, sid, prices, led), indent=2))
        elif a.status:
            print(status_line(store, sid, prices, budget, led))
        else:
            print(render(store, sid, prices, budget, True, led))
        return

    sys.stdout.write("\033[?25l")   # hide cursor
    try:
        while True:
            store.scan()
            led.scan()
            if not a.session:
                sid = store.newest_session() or sid
            out = render(store, sid, prices, budget, a.all or True, led)
            sys.stdout.write("\033[H\033[J" + out + "\n")
            sys.stdout.flush()
            time.sleep(a.interval)
    except KeyboardInterrupt:
        pass
    finally:
        sys.stdout.write("\033[?25h\n")   # show cursor


if __name__ == "__main__":
    # Run as the module sankey.py imports, not as __main__: the saved stores name their
    # classes by module, and both scripts must read the same file (scanned()).
    import tokentab
    tokentab.utf8_stdio()
    tokentab.main()
