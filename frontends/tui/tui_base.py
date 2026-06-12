"""GenericAgent TUI v2 — Textual app with refined visual style.

Run from project root:
    python frontends/tui/tuiapp_v2.py

Visual design carried from temp/GA_tui 设计/tui_demo.py;
functionality migrated from frontends/tuiapp.py plus new commands:
- /btw       — side question (subagent, doesn't interrupt main)
- /continue  — list / restore historical sessions
- /export    — export last reply (clip / file / all)
- /restore   — restore last model_responses log
"""
from __future__ import annotations

import argparse
import json
import os
import queue
import re
import sys
import tempfile
import threading
import time
import subprocess
import shutil

# Local: cross-platform shortcut-label formatter (Win/Linux "Ctrl+B" vs mac "⌃B").
# Imported early because _TIPS at module load time uses fmt_key().

# Make project root + shared dir importable so `from agentmain import ...`
# and `from keysym import ...` work regardless of how this file is run.
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
for _p in (_PROJECT_ROOT, os.path.join(_PROJECT_ROOT, "frontends", "shared")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from keysym import fmt_key, fmt_keys  # noqa: E402
from dataclasses import dataclass, field
from itertools import count
from typing import Any, Callable, Optional

def _ensure_tui_deps() -> None:
    """Try the imports; on first miss, pip-install the wheel and retry once.
    Keeps `ga-cli` working on a fresh Python (Windows / macOS / Linux) where
    Textual or Rich hasn't been installed yet. Bails with a clear message if
    pip itself is unavailable or the install fails — never silently."""
    import importlib.util, subprocess
    needed = ("rich", "textual")
    missing = [m for m in needed if importlib.util.find_spec(m) is None]
    if not missing: return
    print(f"[ga-tui] installing {' '.join(missing)} into {sys.executable} ...", file=sys.stderr)
    try:
        subprocess.check_call([sys.executable, "-m", "pip", "install", "--quiet", *missing])
    except Exception as e:
        print(f"[ga-tui] auto-install failed: {e}\n    fix: {sys.executable} -m pip install {' '.join(missing)}",
              file=sys.stderr)
        raise SystemExit(2)
    for m in missing: importlib.invalidate_caches()


_ensure_tui_deps()
try:
    from rich.markdown import Markdown
    from rich.table import Table
    from rich.text import Text
    from textual import events
    from textual.app import App, ComposeResult
    from textual.binding import Binding
    from textual.containers import Horizontal, Vertical, VerticalScroll
    from textual.geometry import Region
    from textual.message import Message
    from textual.screen import ModalScreen
    from textual.widget import Widget
    from textual.widgets import Input, OptionList, SelectionList, Static, TextArea
    from textual.widgets.option_list import Option
    from textual.widgets.selection_list import Selection
except ModuleNotFoundError as exc:
    print(f"[ga-tui] still missing: {exc.name}. Run: {sys.executable} -m pip install rich textual",
          file=sys.stderr)
    raise SystemExit(2) from exc


def _hint_terminal_capabilities() -> None:
    """Warn once at startup if we detect a terminal known to render Textual
    poorly (e.g. bare mintty/git-bash). The UI still works, but visuals like
    truecolor chips and unicode glyphs may degrade. Heuristic-only — never
    blocks startup, just prints a hint to stderr.
    """
    if os.name != "nt": return
    if os.environ.get("WT_SESSION") or os.environ.get("TERM_PROGRAM"):
        return  # Windows Terminal / iTerm2 / VSCode / Hyper — all fine
    if os.environ.get("TERM", "").startswith("xterm"):
        # mintty exports TERM=xterm-256color. Textual still renders, but
        # mouse + truecolor handling is patchy. Point at the better option.
        print("[ga-tui] hint: best rendering on Windows Terminal (`wt`) — "
              "the mintty/git-bash console may clip colors or mouse events.",
              file=sys.stderr)


_hint_terminal_capabilities()


# Strip terminal control sequences from subprocess stdout but keep SGR color codes,
# otherwise Text.from_ansi loses color downstream.
_ANSI_CONTROL_RE = re.compile(
    r"\x1b\[\?[\d;]*[hl]"
    r"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"
    r"|\x1b[=>]"
)

# Strip SGR-only codes — used when we need plain text for downstream parsing
# (e.g. mapping narrow rendered output to source positions for selection).
_ANSI_SGR_RE = re.compile(r"\x1b\[[0-9;]*m")

# Strip the leading turn marker that agent_loop yields per turn — covers
# both the default `**LLM Running (Turn N) ...**` and the task-mode short
# `**Turn N ...**` (agent_loop.py:52 switches when handler.parent.task_dir
# is set; v2 sets task_dir for the `_stop` / `_keyinfo` consume paths).
# fold_turns still needs the marker in source content to split turns, so we only strip at
# render time. Applies to the live (last) text segment, since folded turns don't include it.
_TURN_MARKER_RE = re.compile(r"^\s*\**(?:LLM Running \()?Turn \d+\)?[^\n]*\**\s*", re.MULTILINE)

# Commonmark task-list patterns: `- [ ] foo` / `* [x] foo` / `+ [X] foo`.
# Group 1 keeps the bullet + leading space so we can substitute the [ ] / [x]
# portion only and let the Markdown renderer still treat the line as a list item.
_TASKLIST_OPEN_RE = re.compile(r"^(\s*[-*+] )\[ \] ", re.MULTILINE)
_TASKLIST_DONE_RE = re.compile(r"^(\s*[-*+] )\[[xX]\] ", re.MULTILINE)

# `<tool_use>{...}</tool_use>` envelope emitted by the streaming layer in
# llmcore. Agents emit one per tool call; the wrapped object always has
# {"name": ..., "arguments": ...}. We replace the whole envelope so the raw
# JSON braces/quotes never leak into the markdown render.
_TOOL_USE_RE = re.compile(r"<tool_use>\s*(\{.*?\})\s*</tool_use>", re.DOTALL)

# Agent-internal metadata tags. The sidebar's `S:` and the fold title already
# surface the summary; the chat body should not show the raw tag. Stripping is
# also required because `<summary>X</summary>\n<body>` (no blank line) is parsed
# as a CommonMark HTML block that swallows the following body line, so the
# model's actual reply disappears from the rendered output.
# Only the start-of-line form triggers the CommonMark HTML-block swallow; mid-line
# occurrences are inline HTML that Rich renders as text, and tags inside backticks
# / fenced / indented code must stay verbatim. Anchoring sidesteps all of those.
_META_TAG_RE = re.compile(
    r"^[ ]{0,3}<(summary|thinking)>.*?</\1>\s*",
    re.DOTALL | re.IGNORECASE | re.MULTILINE,
)


# Rotating usage tips, picked once per launch.
_TIPS = (
    "Tip: 按 / 唤起命令面板；任何命令都能用方向键选择。",
    "Tip: /rename <name> 持久化会话名；/continue <name> 跨次重开同名会话。",
    "Tip: /cost 查看 token 用量；/cost all 列出所有会话的累计。",
    "Tip: /continue 列出最近 20 个历史会话，按 Enter 进入。",
    "Tip: /btw <问题> 让 side-agent 回答而不打断主任务。",
    f"Tip: {fmt_key('ctrl+b')} 折叠侧栏；{fmt_key('ctrl+o')} 切换长输出折叠；{fmt_key('ctrl+/')} 查看快捷键。",
    f"Tip: {fmt_key('ctrl+n')} 新建会话；{fmt_keys('ctrl+up','ctrl+down')} 在多个会话间切换。",
    "Tip: 粘贴图片 / 文件后会自动折叠成 [Image #N] / [File #N] 占位符。",
    f"Tip: 多行输入用 {fmt_key('ctrl+j')} 换行；Enter 直接发送。",
    "Tip: /rewind <n> 回退最近 n 轮对话；/stop 中止当前任务。",
    "Tip: /export clip 把上一条回复复制到剪贴板；/export all 给出完整日志路径。",
    "Tip: /branch [name] 从当前历史分裂出新会话，互不污染。",
    "Tip: ask_user 题目里写 [多选] 自动切到 SelectionList；任何 picker 都有 \"Type something\" 走自由输入。",
    "Tip: plan 模式下的 todo 会渲染在消息区与输入框之间的 📋 Plan 卡片，完成后自动消失。",
    "Tip: /update 让主 agent 自动 git pull 并核查影响面；/autorun 进入 autonomous 自主模式。",
    "Tip: /morphling <目标> 启用蒸馏吞噬外部技能。",
    "Tip: /goal <目标> 进入 Goal 模式（缺 condition 时会回头问你预算 / worker 上限）。",
    "Tip: /hive <目标> 进入 Hive 多 worker 协作；/scheduler 调出 reflect 任务多选启动器。",
    "Tip: /conductor <任务> 直接交给 frontends/conductor.py 做多 subagent 编排。",
    "Tip: /update 是双分支 upstream 同步 —— 先 diff 预演，再分别快进。",
    "Tip: /scheduler 里再点一下已勾选的任务可以 stop —— 取消勾选 = 停止。",
    f"Tip: {fmt_key('ctrl+s')} 把当前输入 stash 起来，下次 / 打开 picker 时还在。",
)


def _random_tip(exclude: str = "") -> str:
    """Pick a tip distinct from `exclude` so rotation doesn't repeat."""
    import random
    pool = [t for t in _TIPS if t != exclude] or list(_TIPS)
    return random.choice(pool)


def _tip_line(text: str = ""):
    """`└ Tip: …` as styled Rich Text; empty `text` → blank pulse line."""
    from rich.text import Text as _T
    t = _T()
    if not text:
        return t
    t.append("└ ", style="#6e7681")
    t.append("Tip: ", style="bold #6e7681")
    t.append(text.removeprefix("Tip: "), style="#6e7681")
    return t

# Defensive cleaners for ask_user candidates. The model occasionally smuggles
# JSON envelope debris (`"}`, `]`, `\`) in or out of a candidate string, or
# mashes several options together with `\n`. Both arrive as opaque strings
# from `_install_ask_user_hook` — we sanitize at the boundary so the picker
# never has to render broken text.
_CAND_LEFT_TRIM = re.compile(r'^[",\[\]{}\\\s]+')
_CAND_RIGHT_TRIM = re.compile(r'[",\[\]{}\\\s]+$')
_CAND_NUMBER_PFX = re.compile(r'^\d+\s*[.)、：:）．]\s*')


def _sanitize_candidates(raw) -> list[str]:
    """Normalize whatever the agent passes as `candidates` into a clean,
    deduped list of human-facing strings. Handles a `list[str]` of clean
    options (no-op), as well as the failure modes we've seen in the wild:
    JSON debris glued to one entry, a single string with embedded `\\n` that
    really meant N entries, numbered prefixes (`3. foo`) the picker would
    re-number, and pathologically long entries.
    """
    out: list[str] = []
    items = raw if isinstance(raw, list) else [raw] if raw else []
    for item in items:
        s = str(item) if item is not None else ""
        # An entry with literal `\n` or real newlines is N entries mashed together.
        for line in s.replace("\\n", "\n").splitlines() or [s]:
            line = _CAND_LEFT_TRIM.sub("", line)
            line = _CAND_RIGHT_TRIM.sub("", line)
            line = _CAND_NUMBER_PFX.sub("", line)
            line = line.strip()
            if not line: continue
            if len(line) > 200: line = line[:200] + "…"
            if line not in out: out.append(line)
    return out


def _render_tool_use_block(match) -> str:
    """Render a `<tool_use>{...}</tool_use>` envelope as readable markdown.

    For `ask_user` with candidates we deliberately render only the question —
    the interactive picker (drained in `_drain_ask_user_events`) shows the
    actual choices and owns the user input. Rendering candidates here too
    would double up the visible card.

    For `ask_user` without candidates (pure free-text prompt) the markdown
    stays the source of truth, so we still emit `> 💬 question`.

    All other tools collapse to a single `tool: <name>` line — the full fold
    machinery still hides the raw turn body when fold-mode is on.
    """
    try:
        obj = json.loads(match.group(1))
    except Exception:
        return match.group(0)
    name = obj.get("name", "")
    args = obj.get("arguments") or {}
    if name == "ask_user":
        question = (args.get("question") or "").strip()
        if not question:
            return ""
        return f"\n> 💬 **{question}**\n"
    return f"\n*tool: {name}*\n"


def _extract_user_text(entry: dict) -> str:
    c = entry.get("content") if isinstance(entry, dict) else None
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        parts = [b.get("text", "") for b in c
                 if isinstance(b, dict) and b.get("type") == "text"]
        return "\n".join(p for p in parts if p)
    return ""


def fold_turns(text: str) -> list[dict]:
    placeholders: list[str] = []
    def stash(m):
        placeholders.append(m.group(0))
        return f"\x00PH{len(placeholders) - 1}\x00"
    # Line-anchored so backticks embedded in tool output (e.g. `N|\`\`\`\``
    # gutter from file_read) don't pair with later real fences.
    safe = re.sub(r"^`{4,}.*?^`{4,}\n?", stash, text, flags=re.DOTALL | re.MULTILINE)
    parts = re.split(r"(\**(?:LLM Running \()?Turn \d+\)? \.\.\.\**)", safe)
    parts = [re.sub(r"\x00PH(\d+)\x00", lambda m: placeholders[int(m.group(1))], p) for p in parts]
    if len(parts) < 4:
        return [{"type": "text", "content": text}]
    segs: list[dict] = []
    if parts[0].strip():
        segs.append({"type": "text", "content": parts[0]})
    turns = [(parts[i], parts[i + 1] if i + 1 < len(parts) else "")
             for i in range(1, len(parts), 2)]
    for idx, (marker, content) in enumerate(turns):
        if idx == len(turns) - 1:
            segs.append({"type": "text", "content": marker + content})
            continue
        cleaned = re.sub(r"`{3,}.*?`{3,}|<thinking>.*?</thinking>", "", content, flags=re.DOTALL)
        ms = re.findall(r"<summary>\s*((?:(?!<summary>).)*?)\s*</summary>", cleaned, re.DOTALL)
        title = (ms[0].strip().split("\n", 1)[0] if ms
                 else re.sub(r",?\s*args:.*$", "", cleaned.strip().split("\n", 1)[0] or marker.strip("*")))
        if len(title) > 72: title = title[:72] + "..."
        segs.append({"type": "fold", "title": title, "content": content})
    return segs


def render_folded_text(text: str) -> str:
    out = []
    for seg in fold_turns(text):
        out.append(f"\n▸ {seg.get('title') or 'completed turn'}\n\n"
                   if seg["type"] == "fold" else seg.get("content", ""))
    return "".join(out)


class HardBreakMarkdown(Markdown):
    # softbreak → hardbreak so multi-line agent logs aren't collapsed into one line.
    def __init__(self, markup, **kwargs):
        super().__init__(markup, **kwargs)
        self._soft_to_hard(self.parsed)

    @staticmethod
    def _soft_to_hard(tokens):
        for tok in tokens:
            if tok.type == "softbreak":
                tok.type = "hardbreak"
            if tok.children:
                HardBreakMarkdown._soft_to_hard(tok.children)


# Rich's Markdown.TableElement adds columns without specifying `overflow`,
# so Rich Table falls back to "ellipsis" — long cell contents get truncated
# with `…` in narrow terminals. Patch to use "fold" instead so cells wrap
# across multiple lines and full content stays visible.
def _patch_markdown_table_overflow():
    import rich.markdown as _rmd
    from rich.table import Table as _RichTable
    from rich import box as _rich_box

    def _table_render(self, console, options):
        # `markdown.table.border` / `markdown.table.header` were Rich default
        # styles in older releases but have been dropped from DEFAULT_STYLES;
        # resolving the bare names now raises MissingStyle. Resolve with a
        # fallback so a table never aborts the whole Markdown render — which
        # would drop the entire message to raw, unrendered text.
        table = _RichTable(
            box=_rich_box.SIMPLE,
            pad_edge=False,
            style=console.get_style("markdown.table.border", default="none"),
            show_edge=True,
            collapse_padding=True,
        )
        if self.header is not None and self.header.row is not None:
            header_style = console.get_style("markdown.table.header", default="bold")
            for column in self.header.row.cells:
                heading = column.content.copy()
                heading.stylize(header_style)
                table.add_column(heading, overflow="fold")
        if self.body is not None:
            for row in self.body.rows:
                row_content = [element.content for element in row.cells]
                table.add_row(*row_content)
        yield table

    _rmd.TableElement.__rich_console__ = _table_render


_patch_markdown_table_overflow()


# Rich/Textual wrap treats a continuous CJK run as one indivisible word and
# bumps it whole to the next line when it doesn't fit the remaining space,
# leaving the line tail padded and producing wraps like "AI ↩ 助手...". We patch
# every binding of divide_line/compute_wrap_offsets so CJK-bearing chunks pack
# leading chars into the remainder then fold the rest at full width.
# Covers CJK Unified Ideographs, Hangul Syllables, fullwidth/halfwidth forms.
_CJK_WRAP_RE = re.compile(
    r"[　-鿿"   # CJK punctuation through Unified Ideographs
    r"가-힯"    # Hangul Syllables
    r"＀-￯]"   # Halfwidth / Fullwidth Forms
)


def _fold_chunk_cells(chunk, width, char_width_fn, line_offset=0):
    """Walk chunk char-by-char; return (breaks_relative_to_chunk, final_offset).

    A break at index i means a newline lands before chunk[i]. line_offset is the
    column where chunk[0] starts. char_width_fn must be called in order — it may
    carry state (e.g. tab section index).
    """
    breaks: list[int] = []
    for i, ch in enumerate(chunk):
        cw = char_width_fn(ch)
        if line_offset > 0 and line_offset + cw > width:
            breaks.append(i)
            line_offset = cw
        else:
            line_offset += cw
    return breaks, line_offset


def _cjk_divide_line(text: str, width: int, fold: bool = True) -> list[int]:
    from rich._wrap import words as _words
    from rich.cells import cell_len as _clen

    breaks: list[int] = []
    cell_offset = 0
    for start, _end, word in _words(text):
        word_length = _clen(word.rstrip())
        if width - cell_offset >= word_length:
            cell_offset += _clen(word)
            continue
        if not fold:
            if cell_offset:
                breaks.append(start)
            cell_offset = _clen(word)
            continue

        has_cjk = bool(_CJK_WRAP_RE.search(word))
        if not has_cjk and word_length <= width:
            if cell_offset:
                breaks.append(start)
            cell_offset = _clen(word)
            continue

        if has_cjk:
            line_offset = cell_offset
        else:
            if cell_offset:
                breaks.append(start)
            line_offset = 0
        sub_breaks, cell_offset = _fold_chunk_cells(
            word, width, _clen, line_offset
        )
        breaks.extend(start + b for b in sub_breaks)
    return breaks


def _cjk_compute_wrap_offsets(text, width, tab_size, fold=True,
                              precomputed_tab_sections=None):
    from rich.cells import get_character_cell_size
    from textual._cells import cell_len as _clen
    from textual._loop import loop_last
    from textual.expand_tabs import get_tab_widths

    tab_size = min(tab_size, width)
    tab_sections = precomputed_tab_sections or get_tab_widths(text, tab_size)

    cumulative_widths: list[int] = []
    cumulative_width = 0
    for last, (tab_section, tab_width) in loop_last(tab_sections):
        cumulative_widths.extend([cumulative_width] * (len(tab_section) + int(bool(tab_width))))
        cumulative_width += tab_width
        if last:
            cumulative_widths.append(cumulative_width)

    tab_idx = [0]
    def char_width(ch):
        if ch == "\t":
            cw = tab_sections[tab_idx[0]][1]
            tab_idx[0] += 1
            return cw
        return get_character_cell_size(ch)

    breaks: list[int] = []
    cell_offset = 0
    pos = 0
    chunk_re = re.compile(r"\S+\s*|\s+")
    while pos < len(text):
        m = chunk_re.match(text, pos)
        if m is None:
            break
        start, end = m.span()
        chunk = m.group(0)
        pos = end
        chunk_width = _clen(chunk) + (cumulative_widths[end] - cumulative_widths[start])

        if width - cell_offset >= chunk_width:
            cell_offset += chunk_width
            continue
        if not fold:
            if cell_offset:
                breaks.append(start)
            cell_offset = chunk_width
            continue

        has_cjk = bool(_CJK_WRAP_RE.search(chunk))
        if not has_cjk and chunk_width <= width:
            if cell_offset:
                breaks.append(start)
            cell_offset = chunk_width
            continue

        if has_cjk:
            line_offset = cell_offset
        else:
            if cell_offset:
                breaks.append(start)
            line_offset = 0
        sub_breaks, cell_offset = _fold_chunk_cells(chunk, width, char_width, line_offset)
        breaks.extend(start + b for b in sub_breaks)
    return breaks


def _install_cjk_wrap() -> None:
    # `from X import fn` copies the binding into the importer's namespace, so a
    # rebind on the source module misses every holder. Patch each one explicitly.
    import rich._wrap as _rw
    import rich.text as _rt
    import textual.content as _tc
    import textual._wrap as _tw
    import textual.document._wrapped_document as _twd
    if getattr(_cjk_divide_line, "_cjk_patched", False):
        return
    _cjk_divide_line._cjk_patched = True
    _rw.divide_line = _cjk_divide_line
    _rt.divide_line = _cjk_divide_line
    _tc.divide_line = _cjk_divide_line
    _tw.compute_wrap_offsets = _cjk_compute_wrap_offsets
    _twd.compute_wrap_offsets = _cjk_compute_wrap_offsets


_install_cjk_wrap()


# Markdown render result that supports clean copy. We render twice: once at the
# display width (wraps to ANSI for selectability) and once at a wide width (one
# logical line per block, no wrap newlines). The narrow render goes into the
# Text widget for display; the wide render becomes the "source" string that
# get_selection extracts from, with per-visual-line offsets mapping cursor
# positions back into source — wrap continuations skip the wide-side whitespace
# eaten at the break, and hanging indent on wrap lines maps to the same source
# position as the start of the wrapped content.
@dataclass
class _MdRender:
    text: Text
    source: str
    line_starts: list  # source offset for the content start of each visual line
    line_indents: list  # leading whitespace count to skip when mapping x
    line_lengths: list  # total length of each visual line (incl. indent)


_CENTER_LEAD_MIN = 4


def _strip_quote_deco(s: str) -> tuple:
    """Rich Markdown re-emits the `▌ ` blockquote marker on every wrapped visual
    line in narrow, but the wide single-line render contains it only once at the
    block start. Treat the re-prefix on continuation lines as visual indent that
    doesn't consume wide chars. Returns (content_without_deco, deco_width)."""
    if not s.startswith("▌"):  # `▌`
        return s, 0
    rest = s[1:]
    if rest.startswith(" "):
        return rest[1:], 2
    return rest, 1


def _md_line_has_box_drawing(line: str) -> bool:
    """Return True for Rich table / box-art glyphs, not for normal dashes.

    The previous table workaround keyed on the literal `─` at the whole-widget
    level.  That was too broad: one table anywhere in a message made ordinary
    paragraphs copy from the wrapped/narrow render, reintroducing visual
    newlines.  Use the Unicode Box Drawing block so SIMPLE/ROUNDED/HEAVY/etc.
    table styles are covered while em-dashes (`—`) and ASCII/Unicode hyphens are
    not mistaken for tables.
    """
    return any("\u2500" <= ch <= "\u257f" for ch in line)


def _md_run_has_box_drawing(lines: list[str]) -> bool:
    return any(_md_line_has_box_drawing(line) for line in lines)


def _build_passthrough_source(narrow_plain: str):
    """Fallback aligner: treat narrow render as the copy source verbatim.

    Used when the wide/narrow line-by-line correspondence assumed by
    `_align_md_renders` breaks down — most notably for Rich tables, where
    the wide render keeps each logical row on one line with `│` separators
    while the narrow render lays cells vertically. In that case we can't
    map (y, x) selection coordinates back to the wide source, so we just
    copy whatever is visually on screen and accept the cosmetic cost of
    leaving the table's `─`/`│` box characters in the clipboard output.

    Returns the same 4-tuple shape as `_align_md_renders`:
        (source, line_starts, line_indents, line_lengths)
    """
    lines = narrow_plain.split("\n")
    line_starts = [0] * len(lines)
    line_indents = [0] * len(lines)
    line_lengths = [0] * len(lines)
    parts = []
    pos = 0
    for i, raw in enumerate(lines):
        # Strip the `▌` user-message side bar the same way the aligner does,
        # so selections inside user echoes still copy clean text.
        body, deco = _strip_quote_deco(raw)
        line_starts[i] = pos
        line_indents[i] = deco
        line_lengths[i] = len(body)
        parts.append(body)
        pos += len(body)
        if i != len(lines) - 1:
            parts.append("\n")
            pos += 1
    return "".join(parts), line_starts, line_indents, line_lengths


def _align_md_renders(narrow_raw: str, wide_raw: str):
    """Walk narrow + wide line-by-line; return (source, line_starts, line_indents, line_lengths)."""
    narrow = [l.rstrip() for l in narrow_raw.split("\n")]
    wide = [l.rstrip() for l in wide_raw.split("\n")]

    wrap_groups: list = []
    ni = 0
    wi = 0
    while ni < len(narrow):
        if narrow[ni] == "":
            ni += 1
            while wi < len(wide) and wide[wi] == "":
                wi += 1
            continue
        run_start = ni
        while ni < len(narrow) and narrow[ni] != "":
            ni += 1
        run_lines = narrow[run_start:ni]

        wide_start = wi
        while wi < len(wide) and wide[wi] != "":
            wi += 1
        wide_lines = wide[wide_start:wi]

        K, W = len(run_lines), len(wide_lines)
        if _md_run_has_box_drawing(run_lines):
            # Rich tables are inherently two-dimensional: a single logical row in
            # the wide render may become several visual rows in the narrow render.
            # Treat only this *run* as visual/passthrough.  Do not poison the
            # rest of the widget, otherwise paragraphs before/after the table
            # start copying their wrapped visual newlines again.
            for k in range(K):
                wrap_groups.append(((run_start + k, run_start + k + 1), run_lines[k], True))
        elif W == 0:
            for k in range(K):
                wrap_groups.append(((run_start + k, run_start + k + 1), run_lines[k], False))
        elif K == W:
            for k in range(K):
                wrap_groups.append(((run_start + k, run_start + k + 1), wide_lines[k], False))
        else:
            j = 0
            for w_idx, w_line in enumerate(wide_lines):
                g_start = run_start + j
                accumulated = 0
                target = len(w_line)
                is_last = (w_idx == W - 1)
                while j < K and (accumulated < target or is_last):
                    nt = run_lines[j]
                    if j > g_start - run_start:
                        content, _ = _strip_quote_deco(nt.lstrip())
                    else:
                        content = nt
                    accumulated += len(content)
                    j += 1
                    # Each wrap boundary eats one space from the wide line, so
                    # the narrow side's accumulated content runs (consumed - 1)
                    # chars short of target at the natural wrap point.
                    consumed = j - (g_start - run_start)
                    if not is_last and accumulated + max(0, consumed - 1) >= target:
                        break
                wrap_groups.append(((g_start, run_start + j), w_line, False))

    source_parts: list = []
    line_starts = [0] * len(narrow)
    line_indents = [0] * len(narrow)
    line_lengths = [len(nt) for nt in narrow]
    src_pos = 0
    last_was_content = False
    group_idx = 0

    ni = 0
    while ni < len(narrow):
        if narrow[ni] == "":
            line_starts[ni] = src_pos
            if last_was_content:
                source_parts.append("\n")
                src_pos += 1
            source_parts.append("\n")
            src_pos += 1
            last_was_content = False
            ni += 1
            continue

        while group_idx < len(wrap_groups) and ni >= wrap_groups[group_idx][0][1]:
            group_idx += 1
        if group_idx >= len(wrap_groups):
            line_starts[ni] = src_pos
            source_parts.append(narrow[ni])
            src_pos += len(narrow[ni])
            ni += 1
            last_was_content = True
            continue

        (g_start, g_end), wide_line, passthrough = wrap_groups[group_idx]
        single_line = (g_end - g_start == 1)

        nt0 = narrow[g_start]
        nt0_lead = len(nt0) - len(nt0.lstrip())
        wide_lead = len(wide_line) - len(wide_line.lstrip())
        # Rich centers H1 against the available width, so wide_lead grows with the
        # console width (≈ 5000 at width=10000) while nt0_lead reflects narrow's
        # half-padding. Code lines, list/blockquote markers, etc. have wide_lead
        # ≈ nt0_lead — without the >=2× guard the heuristic would strip indent
        # from any code line with ≥5 leading spaces (e.g. `    print("hi")`),
        # causing the visible selection and the copied text to disagree.
        is_centered = (single_line and wide_lead > _CENTER_LEAD_MIN and nt0_lead > 0
                       and wide_lead >= 2 * nt0_lead)

        if last_was_content:
            source_parts.append("\n")
            src_pos += 1

        if passthrough:
            # Visual/source mapping for table rows: keep exactly what the user
            # sees on this line (minus quote decoration) so x offsets remain
            # valid.  Each table visual line is its own group, so no wrapped
            # paragraph outside the table inherits this behavior.
            body, deco = _strip_quote_deco(narrow[g_start])
            source_parts.append(body)
            line_starts[g_start] = src_pos
            line_indents[g_start] = deco
            src_pos += len(body)
        elif is_centered:
            content = wide_line.lstrip()
            source_parts.append(content)
            line_starts[g_start] = src_pos
            line_indents[g_start] = nt0_lead
            src_pos += len(content)
        else:
            block_start = src_pos
            source_parts.append(wide_line)
            src_pos += len(wide_line)
            pointer = 0
            for k in range(g_start, g_end):
                nt = narrow[k]
                if k == g_start:
                    content = nt
                    indent = 0
                else:
                    indent = len(nt) - len(nt.lstrip())
                    content = nt.lstrip()
                    content, deco = _strip_quote_deco(content)
                    indent += deco
                    while pointer < len(wide_line) and wide_line[pointer].isspace():
                        pointer += 1
                line_starts[k] = block_start + pointer
                line_indents[k] = indent
                pointer += len(content)
        ni = g_end
        last_was_content = True

    return "".join(source_parts).rstrip("\n"), line_starts, line_indents, line_lengths


ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)
FRONTENDS_DIR = os.path.dirname(os.path.abspath(__file__))
if FRONTENDS_DIR not in sys.path:
    sys.path.insert(0, FRONTENDS_DIR)

_TASK_DIR_GLOB = os.path.join(FRONTENDS_DIR, '..', 'temp', '_tui_v2_*')


def _rmdir_if_empty(path: Optional[str]) -> None:
    """Best-effort remove a signal task_dir once it holds no in-flight files.
    `os.rmdir` only succeeds on an empty dir, so a stray `_intervene` still
    pending consumption is never clobbered."""
    if not path:
        return
    try: os.rmdir(path)
    except OSError: pass


def _sweep_stale_task_dirs() -> None:
    """Delete empty `temp/_tui_v2_*` signal dirs left by prior runs (incl.
    crashes).  Empty == no pending signal, so removal is safe even while
    another live instance owns one — its writer re-creates lazily on the
    next inject."""
    import glob as _glob
    for d in _glob.glob(_TASK_DIR_GLOB):
        if os.path.isdir(d):
            _rmdir_if_empty(d)

# Side-effect imports activate /btw + /continue monkey-patches.
import frontends.shared.chatapp_common as chatapp_common  # noqa: F401
from frontends.shared.chatapp_common import format_restore
from frontends.cmd.btw_cmd import handle_frontend_command as btw_handle
from frontends.cmd.review_cmd import handle as review_handle
from frontends.cmd.continue_cmd import list_sessions as continue_list, extract_ui_messages as continue_extract
from frontends.cmd.export_cmd import last_assistant_text, export_to_temp, wrap_for_clipboard

# Cross-platform clipboard copy for /export clip. Uses native-tool strategy
# so the Textual frontend has no dependency on raw terminal frontend modules.
_HAS_WAYLAND = bool(os.environ.get("WAYLAND_DISPLAY"))


def _clipboard_run(cmd: list[str], input: bytes | None = None, timeout: float = 3.0) -> bytes | None:
    try:
        r = subprocess.run(cmd, input=input, capture_output=True, timeout=timeout)
        return r.stdout if r.returncode == 0 else None
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None


def _copy_to_clipboard_win32(text: str) -> bool:
    """Copy Unicode text on Windows without going through console code pages."""
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        GMEM_MOVEABLE = 0x0002
        CF_UNICODETEXT = 13

        kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
        kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
        kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
        kernel32.GlobalLock.restype = wintypes.LPVOID
        kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
        kernel32.GlobalUnlock.restype = wintypes.BOOL
        kernel32.GlobalFree.argtypes = [wintypes.HGLOBAL]
        kernel32.GlobalFree.restype = wintypes.HGLOBAL
        user32.OpenClipboard.argtypes = [wintypes.HWND]
        user32.OpenClipboard.restype = wintypes.BOOL
        user32.EmptyClipboard.restype = wintypes.BOOL
        user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
        user32.SetClipboardData.restype = wintypes.HANDLE
        user32.CloseClipboard.restype = wintypes.BOOL

        data = text.encode("utf-16-le") + b"\x00\x00"
        handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
        if not handle:
            return False
        locked = kernel32.GlobalLock(handle)
        if not locked:
            kernel32.GlobalFree(handle)
            return False
        ctypes.memmove(locked, data, len(data))
        kernel32.GlobalUnlock(handle)

        if not user32.OpenClipboard(None):
            kernel32.GlobalFree(handle)
            return False
        try:
            user32.EmptyClipboard()
            if not user32.SetClipboardData(CF_UNICODETEXT, handle):
                kernel32.GlobalFree(handle)
                return False
            # Ownership transferred to the clipboard; do not free `handle`.
            return True
        finally:
            user32.CloseClipboard()
    except Exception:
        return False


def _copy_to_clipboard(text: str) -> bool:
    data = text.encode("utf-8")
    if sys.platform == "darwin":
        return _clipboard_run(["pbcopy"], input=data) is not None
    if sys.platform == "win32":
        return _copy_to_clipboard_win32(text)
    if _HAS_WAYLAND and shutil.which("wl-copy"):
        return _clipboard_run(["wl-copy"], input=data) is not None
    if shutil.which("xclip"):
        return _clipboard_run(["xclip", "-selection", "clipboard"], input=data) is not None
    if shutil.which("xsel"):
        return _clipboard_run(["xsel", "--clipboard", "--input"], input=data) is not None
    return False

AgentFactory = Callable[[], Any]

# ---------- themes ----------
# Our `ga-default` palette is registered as a Textual Theme; the other themes in
# `_THEME_CYCLE` are Textual built-ins, whose ga-* slots are derived in
# get_css_variables. C_* globals are kept in sync via watch_theme so Rich Text
# styles (which take plain hex strings) update on theme switch.
_DEFAULT_PALETTE: dict[str, str] = {
    "fg": "#c9d1d9", "muted": "#8b949e", "dim": "#6e7681",
    "bg": "#0d1117", "alt_bg": "#21262d", "sel_bg": "#161b22",
    "border": "#30363d", "border_hi": "#484f58",
    "green": "#7ec27e", "blue": "#82adcf", "purple": "#b596d8",
    # Topbar info-segment chips — distinct hues for at-a-glance scanability.
    # Values are from the github-dark palette; built-in Textual themes derive
    # these from primary/secondary/warning/accent/success in get_css_variables.
    "chip_name":   "#79c0ff",  # session name — cyan-blue
    "chip_model":  "#a5d6ff",  # model id     — pale blue
    "chip_effort": "#f0883e",  # effort       — amber (heat)
    "chip_tasks":  "#d2a8ff",  # task count   — lavender
    "chip_time":   "#7ec27e",  # clock        — same muted green as the sidebar's active-session marker
}

_THEME_CYCLE = ["ga-default", "nord", "gruvbox", "dracula", "tokyo-night", "textual-light"]


# ---------- persisted settings ----------
# Lightweight JSON dropbox for cross-run UI state (theme, future toggles).
# Lives under temp/ alongside model logs so it tracks the workspace.
_SETTINGS_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "temp", "tui_settings.json"
)

def _load_settings() -> dict:
    try:
        with open(_SETTINGS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}

def _save_settings(patch: dict) -> None:
    cur = _load_settings()
    cur.update(patch)
    try:
        os.makedirs(os.path.dirname(_SETTINGS_PATH), exist_ok=True)
        with open(_SETTINGS_PATH, "w", encoding="utf-8") as f:
            json.dump(cur, f, ensure_ascii=False, indent=2)
    except Exception:
        pass

_palette: dict[str, str] = dict(_DEFAULT_PALETTE)
C_FG     = _palette["fg"]
C_MUTED  = _palette["muted"]
C_DIM    = _palette["dim"]
C_SEL_BG = _palette["sel_bg"]
C_GREEN  = _palette["green"]
C_BLUE   = _palette["blue"]
C_PURPLE = _palette["purple"]
C_CHIP_NAME   = _palette["chip_name"]
C_CHIP_MODEL  = _palette["chip_model"]
C_CHIP_EFFORT = _palette["chip_effort"]
C_CHIP_TASKS  = _palette["chip_tasks"]
C_CHIP_TIME   = _palette["chip_time"]


def _hex_rgb(h: str) -> tuple[int, int, int]:
    h = (h or "#000000").lstrip("#")
    if len(h) == 3: h = "".join(c * 2 for c in h)
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def _rgb_hex(rgb) -> str:
    return "#{:02x}{:02x}{:02x}".format(*(max(0, min(255, int(c))) for c in rgb))


def _mix(a: str, b: str, t: float) -> str:
    ra, rb = _hex_rgb(a), _hex_rgb(b)
    return _rgb_hex(tuple(ra[i] * (1 - t) + rb[i] * t for i in range(3)))


def _markdown_rich_theme(p: dict[str, str], minimal: bool = False):
    """Map our palette to Rich Markdown's named styles so code/links/headings
    follow the active theme instead of Rich's frozen defaults.

    `minimal=True` collapses everything to fg/muted so non-default themes don't
    fight Rich's frozen accent colors — each theme can be re-colorised case by
    case later."""
    from rich.theme import Theme as _RichTheme
    if minimal:
        fg, muted, dim, border = p["fg"], p["muted"], p["dim"], p["border"]
        return _RichTheme({
            "markdown.h1":          f"bold {fg}",
            "markdown.h2":          f"bold {fg}",
            "markdown.h3":          f"bold {fg}",
            "markdown.h4":          f"bold {fg}",
            "markdown.h5":          f"bold {fg}",
            "markdown.h6":          f"bold {fg}",
            "markdown.code":        f"bold {fg}",
            "markdown.code_block":  fg,
            "markdown.link":        f"underline {fg}",
            "markdown.link_url":    f"underline {dim}",
            "markdown.block_quote": muted,
            "markdown.item":        fg,
            "markdown.list":        fg,
            "markdown.item.bullet": f"bold {fg}",
            "markdown.item.number": fg,
            "markdown.hr":          border,
            "markdown.strong":      f"bold {fg}",
            "markdown.em":          f"italic {fg}",
            "markdown.s":           f"strike {dim}",
            "markdown.table.border": border,
            "markdown.table.header": f"bold {fg}",
        })
    return _RichTheme({
        "markdown.h1":          f"bold {p['green']}",
        "markdown.h2":          f"bold {p['blue']}",
        "markdown.h3":          f"bold {p['purple']}",
        "markdown.h4":          f"bold {p['fg']}",
        "markdown.h5":          f"bold {p['fg']}",
        "markdown.h6":          f"bold {p['fg']}",
        "markdown.code":        f"bold {p['fg']}",
        "markdown.code_block":  f"{p['fg']} on {p['sel_bg']}",
        "markdown.link":        p["blue"],
        "markdown.link_url":    f"underline {p['dim']}",
        "markdown.block_quote": p["muted"],
        "markdown.item":        p["fg"],
        "markdown.list":        p["blue"],
        "markdown.item.bullet": f"bold {p['blue']}",
        "markdown.item.number": p["blue"],
        "markdown.hr":          p["border"],
        "markdown.strong":      f"bold {p['fg']}",
        "markdown.em":          f"italic {p['fg']}",
        "markdown.s":           f"strike {p['dim']}",
        "markdown.table.border": p["border"],
        "markdown.table.header": f"bold {p['fg']}",
    })


def _palette_from_resolved_vars(v: dict[str, str], dark: bool) -> dict[str, str]:
    """Derive our 11-slot palette from Textual's *resolved* CSS variables (i.e.
    after super().get_css_variables()). Textual auto-fills foreground / surface /
    panel when the Theme leaves them None, so we read those rather than raw
    Theme attributes."""
    bg = v.get("background") or ("#1a1a1a" if dark else "#ffffff")
    fg = v.get("foreground") or ("#e6e6e6" if dark else "#1a1a1a")
    surface = v.get("surface") or _mix(bg, fg, 0.08)
    panel = v.get("panel") or _mix(bg, fg, 0.14)
    primary = v.get("primary") or fg
    return {
        "fg": fg, "bg": bg,
        "alt_bg": surface, "sel_bg": panel,
        # text-muted / text-disabled in Textual resolve to "auto NN%" — a Textual-only
        # syntax Rich can't parse. Always derive from bg/fg blend so the strings we
        # hand to Rich Text are plain hex.
        "muted": _mix(bg, fg, 0.55),
        "dim":   _mix(bg, fg, 0.35),
        "border":    _mix(bg, fg, 0.20),
        "border_hi": _mix(bg, fg, 0.35),
        "green":  v.get("success") or primary,
        "blue":   v.get("secondary") or primary,
        "purple": v.get("accent") or primary,
        # Topbar chips — five distinguishable Textual roles so each segment keeps
        # its own hue across themes. Fall back to primary if a slot is missing.
        "chip_name":   v.get("primary") or primary,
        "chip_model":  v.get("secondary") or primary,
        "chip_effort": v.get("warning") or v.get("accent") or primary,
        "chip_tasks":  v.get("accent") or primary,
        "chip_time":   v.get("success") or primary,
    }


_MAIN_CSS = """
Screen { background: $ga-bg; color: $ga-fg; }

#topbar, #bottombar {
    height: 1;
    background: $ga-bg;
    padding: 0 2;
}

#body { height: 1fr; }

/* Outer scroll container owns the geometry (width/height/border) and the
   scrolling; the inner #sidebar Static keeps the padding so the click
   hit-test math in on_click (event.y - 3) is unchanged. */
#sidebar-scroll {
    width: 34;
    height: 100%;
    background: $ga-bg;
    border-right: solid $ga-alt-bg;
    overflow-y: auto;
    overflow-x: hidden;
    scrollbar-size: 0 1;
    /* Reserve the 1-col scrollbar gutter up front so overflowing the window
       doesn't suddenly squeeze the session rows narrower. */
    scrollbar-gutter: stable;
}
#sidebar-scroll.-hidden, #sidebar-scroll.-narrow { display: none; }

#sidebar {
    width: 1fr;
    height: auto;
    padding: 1 2;
}

#main {
    height: 100%;
    padding: 1 6;
    background: $ga-bg;
}

#messages {
    height: 1fr;
    background: $ga-bg;
    /* horizontal hidden, 1-col vertical bar on right. */
    scrollbar-size: 0 1;
    scrollbar-background: $ga-bg;
    scrollbar-background-hover: $ga-bg;
    scrollbar-background-active: $ga-bg;
    scrollbar-color: $ga-border;
    scrollbar-color-hover: $ga-border-hi;
    scrollbar-color-active: $ga-dim;
}

/* Plan/todo panel — fixed 5-row card between messages and composer.
   `display: none` default so the empty post-compose frame doesn't flash;
   renderer flips it on once items materialize. Fixed height (no scroll)
   keeps layout stable; body truncates to 4 items + "+N more" footer. */
#planbar {
    display: none;
    height: 5;
    max-height: 5;
    background: $ga-sel-bg;
    padding: 0 1;
    margin: 0 0 1 0;
    border-left: thick $ga-green;
}

/* `└ Tip:` footer — one dim row, never grows. */
#tipbar {
    height: 1;
    background: $ga-bg;
    padding: 0;
    color: $ga-dim;
}

/* Pickers — used by both ChoiceList (OptionList) and MultiChoiceList
   (SelectionList). Same flat single-column look as the rest of the chat,
   with a thin green left edge so the picker reads as an actionable card. */
OptionList.picker, SelectionList.picker {
    height: auto;
    max-height: 12;
    margin: 0 0 1 0;
    padding: 0 1;
    background: $ga-bg;
    border: none;
    border-left: thick $ga-green;
    scrollbar-size: 0 1;
}
OptionList.picker > .option-list--option-hover,
SelectionList.picker > .option-list--option-hover { background: $ga-sel-bg; }
OptionList.picker > .option-list--option-highlighted,
SelectionList.picker > .option-list--option-highlighted {
    background: $ga-blue 20%;
    color: $ga-fg;
    text-style: none;
}
SelectionList.picker > .selection-list--button { color: $ga-dim; }
SelectionList.picker > .selection-list--button-selected { color: $ga-green; }
SelectionList.picker > .selection-list--button-highlighted { background: transparent; }

/* Searchable `/continue` picker wrapper. Textual's Vertical container defaults
   to a flex-like height in this scroll layout; if left implicit, scroll_end can
   align only the wrapper's tail and leave the search box / options hidden under
   the composer. Keep the wrapper content-sized; the inner OptionList.picker
   remains the only scrollable/clamped part (12 rows). */
SearchableChoiceList.picker {
    height: auto;
    margin: 0 0 1 0;
}

/* `/continue` search box: one-row gap above (to separate the input from the
   "选择要恢复的会话 …" prompt header) and one-row gap below (to separate it
   from the result list), so the input is visually distinct on both sides
   (user feedback 2026-05-27). */
#continue-search { margin: 1 0 1 0; }

.role {
    height: 1;
    margin-top: 1;
    margin-bottom: 0;
}
.msg {
    height: auto;
    margin-bottom: 0;
}
.fold-header:hover { background: $ga-sel-bg; }
.spinner {
    height: 1;
    margin-top: 1;
}

#palette {
    height: auto;
    max-height: 8;
    background: $ga-bg;
    border: none;
    padding: 0;
    display: none;
    margin-bottom: 1;
    scrollbar-size: 0 0;
}
#palette.-visible { display: block; }
OptionList {
    background: $ga-bg;
    border: none;
    padding: 0;
}
OptionList > .option-list--option {
    padding: 0 2;
    background: $ga-bg;
    color: $ga-fg;
}
OptionList > .option-list--option-highlighted {
    background: $ga-fg;
    color: $ga-bg;
    text-style: bold;
}

ChoiceList {
    height: auto;
    max-height: 12;
    background: $ga-bg;
    border: none;
    padding: 0;
    margin-bottom: 1;
    scrollbar-size: 0 0;
}

#input {
    height: 3;
    min-height: 3;
    max-height: 5;
    /* min-width guards TextArea.render_lines against `range() arg 3 must not be zero`
       when the content region collapses to <= 0 cols (narrow window + sidebar shown). */
    min-width: 10;
    background: $ga-sel-bg;
    border: none;
    margin-bottom: 1;
    padding: 1 2;
    color: $ga-fg;
    scrollbar-size: 0 0;
}
#input:focus { border: none; }
"""


