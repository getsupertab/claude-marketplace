#!/usr/bin/env python3
"""
budget.py — the one validator for TokenTab's budget.json, used by the pane's Settings tab
and the budget skill alike.

  budget.py get [KEY]       print the value under KEY (dotted: cold_resume.confirm_seconds), or the
                            whole file, as JSON
  budget.py set KEY VALUE   change one existing key and write the file back, its order and
                            its _comment keys kept. VALUE: a number; "off", "none" or "" for
                            a limit that is switched off (null); on/off/true/false/yes/no
                            for a switch;
                            budget_type.usd: today or session; budget_type.percent: today,
                            week or five_hour.
                            Prints the new value as JSON; a value that does not fit is
                            refused on stderr with exit code 1, nothing written.

A key that is not in the file is refused too, so a typo never adds one. Lists
(context_warn_tokens) and the word scale are edited in the file itself.
"""
import json, os, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tokentab as kt  # noqa: E402

PATH = os.path.join(kt.CFG, "budget.json")
SHIPPED = os.path.join(os.path.dirname(os.path.abspath(__file__)), "budget.json")
TRUE, FALSE = ("on", "true", "yes", "1"), ("off", "false", "no", "0")


def load():
    with open(PATH, encoding="utf-8") as f:
        return json.load(f)   # a dict keeps the file's key order


def upgraded(cfg):
    """cfg with the keys the shipped file has and cfg lacks, in the shipped order, one level into objects,
    and the shipped _comment texts. register.tsx's withNewKeys does the same."""
    with open(SHIPPED, encoding="utf-8") as f:
        shipped = json.load(f)
    def merged(key, shipped_value):
        if key.startswith("_") and isinstance(shipped_value, str):
            return shipped_value
        mine = cfg.get(key, shipped_value)
        if isinstance(shipped_value, dict) and isinstance(mine, dict):
            return {**shipped_value, **mine}
        return mine
    return {**{key: merged(key, value) for key, value in shipped.items()},
            **{key: value for key, value in cfg.items() if key not in shipped}}


def lookup(cfg, key):
    """The parent dict and the last key of a dotted path, or an error."""
    parts = key.split(".")
    node = cfg
    for p in parts[:-1]:
        node = node.get(p) if isinstance(node, dict) else None
        if not isinstance(node, dict):
            raise ValueError(f"no such key: {key}")
    if parts[-1] not in node or parts[-1].startswith("_"):
        raise ValueError(f"no such key: {key}")
    return node, parts[-1]


def parse(key, old, raw):
    """The new value for key from the text raw, by the key's name and the value it holds."""
    leaf = key.split(".")[-1]
    if key == "version":
        raise ValueError(f"{key}: the file's format, not a setting")
    text = str(raw).strip().lower().replace(",", "").replace("$", "").rstrip("%").strip()
    if key.startswith("budget_type."):
        if text not in kt.BUDGET_TYPES.get(leaf, ()):
            raise ValueError(f"{key}: one of {', '.join(kt.BUDGET_TYPES.get(leaf, ()))}, not {raw!r}")
        return text
    if isinstance(old, str):
        raise ValueError(f"{key}: edit this one in the file")
    if isinstance(old, bool):
        if text in TRUE: return True
        if text in FALSE: return False
        raise ValueError(f"{key}: on or off, not {raw!r}")
    if isinstance(old, (list, dict)):
        raise ValueError(f"{key}: edit this one in the file")
    if text in ("", "off", "none", "null"):
        if leaf.endswith("_usd"):
            return None
        raise ValueError(f"{key}: a number is needed")
    try:
        v = float(text)
    except ValueError:
        raise ValueError(f"{key}: a number is needed, not {raw!r}")
    if v < 0:
        raise ValueError(f"{key}: not below zero")
    if leaf.endswith(("_tokens", "_minutes", "_seconds")):
        return int(v)
    return v


def main():
    a = sys.argv[1:]
    if len(a) >= 1 and a[0] == "get":
        cfg = load()
        if len(a) == 1:
            print(json.dumps(cfg, indent=2)); return
        node, leaf = lookup(cfg, a[1])
        print(json.dumps(node[leaf])); return
    if len(a) == 3 and a[0] == "set":
        cfg = load()
        if cfg.get("version") != 2:
            raise ValueError("budget.json is from before TokenTab 1.14; a new session replaces it (the old one kept as budget.json.old)")
        try:
            node, leaf = lookup(cfg, a[1])
        except ValueError:
            cfg = upgraded(cfg)                    # a key a newer version ships
            node, leaf = lookup(cfg, a[1])
        node[leaf] = parse(a[1], node[leaf], a[2])
        tmp = PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2, ensure_ascii=False)
            f.write("\n")
        os.replace(tmp, PATH)
        print(json.dumps(node[leaf])); return
    sys.exit(__doc__.strip())


if __name__ == "__main__":
    kt.utf8_stdio()
    try:
        main()
    except (ValueError, OSError) as e:
        print(str(e), file=sys.stderr)
        sys.exit(1)
