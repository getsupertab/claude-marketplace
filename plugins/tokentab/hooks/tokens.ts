// TokenTab's design tokens, one source for the pane (register.tsx) and the browser status
// page (tokentab/sankey.py). From the Figma section "TokenTab UI" (Playground); the dark
// values follow the share page. Every token is a pair, [light, dark].
//
// series:  what a figure is (a rate, a tool's split, a model, a session, a tone); the pane's
//          Svg bars pick a value through prefers-color-scheme, its dots take the light one,
//          a mid tone that reads on both grounds.
// surface: the page around the figures (grounds, text, lines, controls).
// font:    the type families; the pane takes the surface's own.
//
// sankey.py reads this file as JSON: everything from the brace after `TOKENS ... =` to the
// last brace must stay strict JSON (double quotes, no comments, no trailing commas). The
// hooks module can only import code files, so the tokens live in a .ts file, not a .json.
type Pair = [string, string]
export const TOKENS: { series: Record<string, Pair>; surface: Record<string, Pair>; font: { sans: string; mono: string } } = {
  "series": {
    "input": ["#9a99a6", "#a9a8b4"],
    "cache_write_1h": ["#6b6fc5", "#8d90e0"],
    "cache_write_5m": ["#9a9ee0", "#b3b6ee"],
    "cache_read": ["#5ba899", "#6cc2b1"],
    "output": ["#e0973a", "#f0ab55"],
    "thinking": ["#a874c9", "#c08fe0"],
    "fees": ["#c9655b", "#e07e74"],
    "ask": ["#7f80cf", "#9a9be6"],
    "ctx": ["#5ba899", "#6cc2b1"],
    "read": ["#d9a23e", "#ebb757"],
    "success": ["#2e7d32", "#5fd75f"],
    "warning": ["#c18f00", "#e5c07b"],
    "error": ["#c0392b", "#ff6b6b"],
    "track": ["#ecebe6", "#3a3a3a"],
    "model_1": ["#c26b4a", "#e08a68"],
    "model_2": ["#4f86c6", "#6fa3e0"],
    "model_3": ["#5ba899", "#6cc2b1"],
    "model_4": ["#c9a227", "#e5c04a"],
    "model_5": ["#9a99a6", "#a9a8b4"],
    "session_1": ["#c26b4a", "#e08a68"],
    "session_2": ["#4f86c6", "#6fa3e0"],
    "session_3": ["#5ba899", "#6cc2b1"],
    "session_4": ["#c9a227", "#e5c04a"],
    "session_5": ["#a874c9", "#c08fe0"],
    "session_6": ["#c75b8f", "#e07aaa"],
    "session_7": ["#6a9a4a", "#86b865"],
    "session_8": ["#6b6fc5", "#8d90e0"],
    "session_9": ["#e0973a", "#f0ab55"],
    "session_10": ["#3f9fbf", "#5cb8d8"],
    "session_other": ["#9a99a6", "#a9a8b4"]
  },
  "surface": {
    "bg": ["#f4f3ee", "#141413"],
    "panel": ["#ffffff", "#1f1f1d"],
    "text": ["#1a1a1a", "#f2f1ec"],
    "secondary": ["#5e5e5e", "#c4c3bc"],
    "dim": ["#8a8a8a", "#a3a29b"],
    "line": ["#e8e8e6", "#34342f"],
    "control": ["#dededb", "#3d3d38"],
    "hover": ["#f1f0ea", "#2a2a27"],
    "primary": ["#111111", "#f2f1ec"],
    "on_primary": ["#ffffff", "#1a1a1a"],
    "error_soft": ["#fbedeb", "#3a2220"]
  },
  "font": {
    "sans": "Inter, -apple-system, BlinkMacSystemFont, \"Segoe UI\", system-ui, sans-serif",
    "mono": "\"JetBrains Mono\", ui-monospace, SFMono-Regular, Menlo, Consolas, monospace"
  }
}
