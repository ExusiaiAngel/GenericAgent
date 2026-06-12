"""GenericAgent TUI v2 — Widgets.
ChatMessage, AgentSession, ChoiceList, InputArea, bars.
"""
from __future__ import annotations
import os, sys, json, re, time, queue, tempfile, subprocess, shutil, threading
from dataclasses import dataclass, field
from itertools import count
from typing import Any, Callable, Optional
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll, Container
from textual.widgets import Static, TextArea, OptionList, SelectionList, Label, Button, Header, Markdown
from textual.widgets.option_list import Option
from textual.binding import Binding
from textual import work
from textual.message import Message
from rich.text import Text
from rich.style import Style as RichStyle
from rich.table import Table as RichTable
from rich.console import RenderableType

_PROJECT_ROOT3 = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
for _p in (_PROJECT_ROOT3, os.path.join(_PROJECT_ROOT3, "frontends", "shared")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from keysym import fmt_key, fmt_keys
from agentmain import GenericAgent as _GA
import tui_base as _tb
from tui_base import (
    _TIPS, _THEME_CYCLE, fold_turns, render_folded_text,
    _md_line_has_box_drawing, _md_run_has_box_drawing, _build_passthrough_source,
    _align_md_renders, _rmdir_if_empty, _sweep_stale_task_dirs,
    _copy_to_clipboard, _sanitize_candidates, _extract_user_text,
    _load_settings, _save_settings, _render_tool_use_block,
    HardBreakMarkdown, _patch_markdown_table_overflow,
    _cjk_divide_line, _cjk_compute_wrap_offsets, _install_cjk_wrap,
    _markdown_rich_theme, _palette_from_resolved_vars,
    _DEFAULT_PALETTE, _MAIN_CSS,
    _hex_rgb, _rgb_hex, _mix, _strip_quote_deco,
    _MdRender, _hint_terminal_capabilities, _random_tip, _tip_line,
)

@dataclass
class ChatMessage:
    role: str            # 'user' | 'assistant' | 'system'
    content: str
    task_id: Optional[int] = None
    done: bool = True
    # Interactive choice support
    kind: str = "text"   # "text" | "choice"
    choices: list = field(default_factory=list)   # [(label, value), ...]
    on_select: Optional[Callable] = field(default=None, repr=False)
    # Optional Esc/cancel hook for choice cards. When set, _cancel_choice
    # invokes this *after* removing the card (used by /scheduler's submit-
    # confirm card to re-show the picker, mirroring ask_user's free-text
    # "Esc rolls back to the previous picker" UX).
    on_cancel: Optional[Callable] = field(default=None, repr=False)
    selected_label: Optional[str] = None
    # Indices into `choices` that should render pre-ticked when the card first
    # mounts (multi_choice only). Used by /scheduler so already-running
    # services show up checked, making "untick = stop" discoverable (bug#4).
    preselected_indices: list[int] = field(default_factory=list)
    # Optional lazy-render hints for choice pickers with huge option counts
    # (e.g. /continue across thousands of sessions). Default is empty / 0,
    # so every existing call site keeps the eager-mount behavior bit-for-bit.
    lazy_choice_items: Optional[list] = field(default=None, repr=False)
    lazy_choice_batch: int = 0
    # `/continue` picker opt-in: when True, _mount_message wraps the picker
    # with an Input filter; `all_choices` is the unfiltered baseline so empty
    # queries restore the full list. Other call sites keep searchable=False
    # (default) and the existing eager/lazy paths run untouched.
    searchable: bool = False
    search_query: str = ""
    all_choices: Optional[list] = field(default=None, repr=False)
    image_paths: list[str] = field(default_factory=list)
    _role_widget: Any = field(default=None, repr=False)
    _hint_widget: Any = field(default=None, repr=False)
    _body_widget: Any = field(default=None, repr=False)
    _cached_body: Any = field(default=None, repr=False)
    _cache_key: tuple = field(default=(), repr=False)
    # Fold indices the user has manually toggled away from the global default.
    # Effective expansion = (default ⊕ in this set), where default = not fold_mode.
    _toggled_folds: set = field(default_factory=set, repr=False)
    _segment_widgets: list = field(default_factory=list, repr=False)
    _segment_sig: tuple = field(default=(), repr=False)
    _spinner_widget: Any = field(default=None, repr=False)
    # Stream start + token baselines so the spinner shows *this turn's* deltas.
    _stream_started_at: Optional[float] = field(default=None, repr=False)
    _stream_baseline_input: int = field(default=0, repr=False)
    _stream_baseline_output: int = field(default=0, repr=False)
    # Frozen `(elapsed, last_in, last_out)` at done→True; keeps the post-turn
    # card from ticking when the next turn shifts cost_tracker deltas.
    _done_summary: Optional[tuple] = field(default=None, repr=False)
    # Frozen `(elapsed, last_in, last_out)` stamped the instant the user aborts
    # (Ctrl+C / `/stop`). Flips the live spinner to a settled "Stopping…" line so
    # elapsed stops climbing while the LLM stream unwinds in the background.
    _stop_summary: Optional[tuple] = field(default=None, repr=False)
    # Per-(seg_hash, width) Text cache; survives fold-toggle re-mounts.
    _seg_render_cache: dict = field(default_factory=dict, repr=False)


@dataclass
class AgentSession:
    agent_id: int
    name: str
    agent: Any
    thread: Optional[threading.Thread] = None
    status: str = "idle"
    messages: list[ChatMessage] = field(default_factory=list)
    task_seq: int = 0
    current_task_id: Optional[int] = None
    current_display_queue: Optional[queue.Queue] = None
    # Per-session input box state. Restored into the shared InputArea on session switch.
    input_text: str = ""
    input_history: list[str] = field(default_factory=list)
    input_pastes: dict[int, str] = field(default_factory=dict)
    input_paste_counter: int = 0
    buffer: str = ""
    # Drives topbar heat-color ramp + elapsed label; set on first running tick.
    _busy_since: Optional[float] = None
    # Stamps running→idle; topbar dot flashes green for ~5s after.
    _done_at: Optional[float] = None
    # ask_user INTERRUPT events; drained by display thread on turn done.
    ask_user_events: Any = field(default_factory=lambda: queue.Queue())
    # Pending `{question:str}` after the user picks free-text in an ask_user
    # picker; next submission becomes a 2-step "Ready to submit?" confirm.
    free_text_pending: Optional[dict] = None
    # Plan state: items + grace-period timers (3s farewell, 1.5s lost-grace).
    plan_items: list = field(default_factory=list)
    plan_complete_since: Optional[float] = None
    plan_lost_since: Optional[float] = None
    # Boundary between restored history (≤ idx) and this run (> idx);
    # `/continue` bumps to `len(messages)` so old plan cards don't resurrect.
    plan_scan_baseline: int = 0
    # `pending`: raw user text for UI display ([queued #N] chip).
    # `pending_wrapped`: same entries wrapped with the "complete current
    # task first" supplementary phrasing, in the form actually appended
    # to `_intervene`.  Replay uses these so the exit-turn put_task
    # carries the wrap context.
    pending: list[str] = field(default_factory=list)
    pending_wrapped: list[str] = field(default_factory=list)
    pending_lk: threading.Lock = field(default_factory=threading.Lock)


def default_agent_factory() -> Any:
    from agentmain import GenericAgent
    agent = GenericAgent()
    agent.inc_out = True
    return agent


# ---------- commands ----------
COMMANDS = [
    ("/help",     "",                 "显示帮助"),
    ("/status",   "",                 "查看会话状态"),
    ("/sessions", "",                 "列出所有会话"),
    ("/new",      "[name]",           "新建并切换到新会话"),
    ("/switch",   "<id|name>",        "切换到指定会话"),
    ("/close",    "",                 "关闭当前会话"),
    ("/rename",   "<name>",           "重命名当前会话（持久化）"),
    ("/branch",   "[name]",           "从当前会话分支"),
    ("/rewind",   "[n]",              "回退最近 n 轮"),
    ("/clear",    "",                 "清空显示（不动 LLM 历史）"),
    ("/stop",     "",                 "中止当前任务"),
    ("/llm",      "[n]",              "查看 / 切换模型"),
    ("/btw",      "<question>",       "side question — 不打断主 agent"),
    ("/review",   "[request]",         "in-session 代码审查（直接输出报告）"),
    # ── slash_cmds bundle (prompt-injection + /scheduler picker).  Kept in
    # the same table so /-completion + the palette pick them up for free.
    ("/update",    "[note]",           "git pull 更新 GA 仓库并报告影响面"),
    ("/autorun",   "[seed]",           "进入 autonomous_operation 自主模式"),
    ("/morphling", "[target]",         "启用 Morphling 蒸馏 / 吞噬外部技能"),
    ("/goal",      "[goal]",           "进入 Goal 模式（需 condition 约束）"),
    ("/hive",      "[target]",         "进入 Hive 多 worker 协作模式"),
    ("/conductor", "[task]",           "调用 frontends/conductor.py 多 subagent 编排"),
    ("/scheduler", "",                 "多选启动/停止 reflect 任务（cron 由 reflect/scheduler.py 驱动）"),
    ("/continue", "[n|name]",         "列出 / 恢复历史会话"),
    ("/resume",   "",                 "列出最近会话并恢复其中一个"),
    ("/cost",     "[all]",            "显示当前会话 token 用量（all = 所有会话）"),
    ("/export",   "clip|<file>|all",  "导出最后回复"),
    ("/restore",  "",                 "恢复上次模型响应日志"),
    ("/reload-keys", "",              "重新加载mykey.py（不重启）"),
    ("/quit",     "",                 "退出"),
]


# ---------- widgets ----------
# Picker sentinels — opaque values routed through `_collapse_choice` so any
# kind of picker can hand off to the same handlers.
#   FREE_TEXT — user wants to type a free-form answer instead of picking
#   EDIT_ANSWER — back from the submit-confirmation, go re-edit the draft
FREE_TEXT_CHOICE = "\x00__free_text__"
FREE_TEXT_LABEL = "Type something"
EDIT_ANSWER_CHOICE = "\x00__edit_answer__"


class ChoiceList(OptionList):
    BINDINGS = [*OptionList.BINDINGS,
                Binding("right", "select", "Select", show=False),
                # `left` mirrors Esc — pickers spawned with an on_cancel
                # (e.g. /scheduler's submit-confirm card → rollback to
                # picker) get a directional way to back out without
                # reaching for Esc.  Choices without an on_cancel just
                # dismiss, same as Esc.
                Binding("left",  "cancel", "Back",   show=False),
                Binding("escape", "cancel", "Cancel", show=False)]

    def __init__(self, msg: "ChatMessage", *options, **kwargs):
        super().__init__(*options, **kwargs)
        self.msg = msg

    def action_cancel(self) -> None:
        try:
            self.app._cancel_choice(self.msg)
        except Exception:
            pass

    def on_key(self, event) -> None:
        # Inside `/continue`'s SearchablePicker, Up on the first row returns
        # focus to the search box (mirrors Down going search → list), closing
        # the navigation loop. No-op for ChoiceLists mounted outside a
        # SearchablePicker (other pickers have no `_search_input` parent), so
        # this stays scoped to `/continue`.
        if event.key != "up":
            return
        search = getattr(self.parent, "_search_input", None)
        if search is None:
            return
        if self.highlighted not in (None, 0):
            return
        try:
            # Clear the highlight on the way out so the search box doesn't show
            # row 0 as still-selected, and the next Down re-enters at the first
            # row (cursor_down from None → 0) instead of skipping to the second.
            self.highlighted = None
            search.focus()
        except Exception:
            pass
        event.stop(); event.prevent_default()


class LazyChoiceList(ChoiceList):
    """ChoiceList that materializes options in bounded batches.

    Why: `/continue` can list thousands of historical sessions; mounting every
    `Option` up-front stalls Textual's render pipeline for ~hundreds of ms and
    inflates the row cache. We mount the first `batch` rows immediately so the
    picker is interactive on first paint, then extend the mounted set as the
    cursor approaches the loaded tail (Down/PageDown/End) or as the user asks
    for the last row from the top via Up — see `action_cursor_up`.

    Back-end contract: ChoiceList already accepts whatever the picker's
    `highlighted` Option's prompt is — the consumer code uses the index via
    `msg.choices`. Lazy only changes *when* rows enter the DOM, not the value
    contract. Falls back to eager super() behaviour for empty / tiny lists.
    """

    def __init__(self, msg: "ChatMessage", labels: list, batch: int = 50, **kwargs):
        self._lazy_labels = list(labels or [])
        self._lazy_loaded = 0
        self._lazy_batch = max(1, int(batch or 50))
        super().__init__(msg, **kwargs)
        # Mount the first batch synchronously so the picker is usable on the
        # very first frame; remaining rows stream in on demand.
        self._load_more(self._lazy_batch)

    @property
    def _has_more(self) -> bool:
        return self._lazy_loaded < len(self._lazy_labels)

    def _load_more(self, count: Optional[int] = None) -> bool:
        if not self._has_more:
            return False
        take = (len(self._lazy_labels) - self._lazy_loaded) if count is None else max(1, int(count))
        end = min(len(self._lazy_labels), self._lazy_loaded + take)
        try:
            self.add_options([Option(self._lazy_labels[i]) for i in range(self._lazy_loaded, end)])
        except Exception:
            # If the list isn't mounted yet (very early call), fall back to
            # buffering via _options if available; otherwise silently bail so
            # the eager half still works.
            return False
        self._lazy_loaded = end
        return True

    def _ensure_window(self) -> None:
        """Extend the loaded window when the cursor nears the tail."""
        hi = self.highlighted
        if hi is None or not self._has_more:
            return
        if hi >= max(0, self._lazy_loaded - 5):
            self._load_more(self._lazy_batch)

    def action_cursor_down(self) -> None:
        before = self.highlighted
        super().action_cursor_down()
        # If Down had no effect (cursor was at the last loaded row), extend.
        if self.highlighted == before and self._has_more:
            if self._load_more(self._lazy_batch):
                super().action_cursor_down()
        self._ensure_window()

    def action_page_down(self) -> None:
        # PageDown can leap ~10 rows at once; pre-extend by a full batch so the
        # visible window doesn't get capped by the load horizon.
        if self._has_more:
            self._load_more(self._lazy_batch)
        super().action_page_down()
        self._ensure_window()

    def action_last(self) -> None:
        # End/Last must reveal the genuine last session, not the last *loaded*
        # row. Load everything (one-shot, no batching loop) then defer to super.
        if self._has_more:
            self._load_more(None)
        super().action_last()

    def action_cursor_up(self) -> None:
        # OptionList wraps Up-at-row-0 to the last *mounted* row. With lazy
        # loading that would land on row 99, not on the actual most-recent
        # session. Detect the wrap intent and redirect to the real tail.
        cur = self.highlighted
        if (cur in (None, 0)) and self._has_more:
            self._load_more(None)
            try:
                self.highlighted = len(self._lazy_labels) - 1
                return
            except Exception:
                pass
        super().action_cursor_up()


def _filter_choices(all_choices: list, query: str) -> list:
    """Case-insensitive multi-term filter for `/continue` style pickers.

    `all_choices` is `[(label, value), ...]`. Each whitespace-separated token
    in `query` must hit somewhere in either:
      * the label text (cheap, always tried first), or
      * the basename of `value` when it looks like a path, or
      * the **content** of the session file at `value` (first ~1MB), so users
        who remember a phrase from inside a session ("Conductor", "subB diff",
        a file path they pasted) can find it back.

    Empty/whitespace query short-circuits to the full list. Lives at module
    scope so the smoke test can exercise it without booting the TUI.
    """
    q = (query or "").strip().lower()
    if not q:
        return list(all_choices or [])
    terms = [t for t in q.split() if t]
    if not terms:
        return list(all_choices or [])

    # Lazy import: continue_cmd already lives next to this module and provides
    # the bounded-window file grep. We keep the import inside the function so
    # other (non-/continue) pickers don't pay for it on app startup.
    try:
        from . import continue_cmd as _cc
    except Exception:
        try:
            import continue_cmd as _cc  # type: ignore
        except Exception:
            _cc = None

    out = []
    for item in (all_choices or []):
        try:
            label, value = item[0], item[1]
        except (TypeError, IndexError):
            continue
        meta = str(label).lower()
        if isinstance(value, str) and value:
            meta = meta + "\n" + os.path.basename(value).lower()
        if all(t in meta for t in terms):
            out.append(item)
            continue
        # Fall back to session-file content grep so phrases that only appear
        # inside the conversation (not in the one-line preview label) still
        # surface. Path-shaped string values only — non-path values skip.
        if (
            _cc is not None
            and isinstance(value, str)
            and value
            and os.path.isfile(value)
            and _cc.file_contains_all(value, terms)
        ):
            out.append(item)
    return out


class SearchableChoiceList(Vertical):
    """Picker wrapper: an Input filter on top of an inner ChoiceList.

    Only used when `ChatMessage.searchable=True` (today: `/continue`). Other
    pickers keep mounting `ChoiceList` / `LazyChoiceList` / `MultiChoiceList`
    directly so this code path has zero blast radius outside `/continue`.

    The inner picker is rebuilt on every query change because OptionList
    doesn't expose a stable "replace all options" primitive that plays nice
    with the lazy-loading subclass. Rebuilds are cheap relative to the user's
    typing cadence and use the same eager/lazy threshold as the original
    `_mount_message` (≤50 eager, >50 lazy).
    """

    LAZY_THRESHOLD = 50

    def __init__(self, msg: "ChatMessage", initial_picker: Optional[OptionList] = None, **kwargs):
        super().__init__(**kwargs)
        self.msg = msg
        self._search_input: Optional[Input] = None
        # `initial_picker` is the eager/lazy widget that `_mount_message`
        # already built from the unfiltered choices. We reuse it on first
        # mount so the eager/lazy decision stays in one place.
        self.picker: Optional[OptionList] = initial_picker

    def compose(self):
        self._search_input = Input(
            value=self.msg.search_query or "",
            placeholder="Search sessions: type to filter, Esc to cancel",
            id="continue-search",
        )
        yield self._search_input
        if self.picker is None:
            self.picker = self._build_picker(self.msg.choices)
        yield self.picker

    def on_mount(self) -> None:
        # First paint: the inner picker was just yielded from compose, but a
        # LazyChoiceList populates its rows across later refresh passes. Defer
        # a scroll so we pin the *settled* wrapper height into view rather than
        # racing the lazy fill (see _rescroll_into_view).
        self._rescroll_into_view()

    def _rescroll_into_view(self) -> None:
        """Pin this picker into the viewport after its inner list (re)mounts.

        The inner LazyChoiceList fills its option rows across refresh passes,
        so the wrapper's final height isn't known until after the next layout.
        Scrolling synchronously here — or relying solely on the single
        deferred scroll_end in `_mount_message` — can fire before those rows
        land, leaving the options below the fold (the `/continue` bug seen
        with a populated history). Deferring our own `scroll_visible()` to
        after the next refresh guarantees we scroll against the settled
        height. Covers both first mount and every query rebuild. Guarded: a
        harmless no-op if the widget is already detached.
        """
        def _do():
            try:
                self.scroll_visible(animate=False)
            except Exception:
                pass
        try:
            self.call_after_refresh(_do)
        except Exception:
            _do()

    def _build_picker(self, choices: list) -> ChoiceList:
        labels = [lbl for lbl, _ in choices]
        # `classes="picker"` is what lets the OptionList.picker CSS rule
        # (`max-height: 12`) clamp the inner list's physical height. Without
        # it the inner ChoiceList falls back to OptionList's default
        # `max-height: 100%`, which — combined with this wrapper being a
        # plain Vertical (height: 1fr inside a VerticalScroll → content-sized)
        # — lets the picker grow to ≈50 rows and push the head / role / search
        # input above the viewport fold on `/continue`. The outer wrapper
        # already carries `classes="picker"` from `_mount_message`, but that
        # selector is type-qualified (`OptionList.picker, SelectionList.picker`)
        # so it does NOT match the Vertical wrapper — only the inner list it
        # builds can claim the height cap. (Root-cause fix 2026-05-27.)
        if len(choices) > self.LAZY_THRESHOLD:
            return LazyChoiceList(self.msg, labels, batch=self.LAZY_THRESHOLD, classes="picker")
        return ChoiceList(self.msg, *labels, classes="picker")

    # Debounce window for incremental filtering. Content-grep across ~270
    # session files costs ~0.2s; running it per keystroke makes the Input
    # feel laggy. Wait until the user pauses for this many seconds before
    # rebuilding the picker. Empty query still applies immediately so a
    # Ctrl+U / backspace-to-empty restores the full list with no perceptible
    # delay. Tuned 2026-05-27 on user feedback ("每输入一个 char 都会立马搜索").
    DEBOUNCE_SEC = 0.22

    def on_input_changed(self, event) -> None:
        if event.input is not self._search_input:
            return
        query = event.value or ""
        self.msg.search_query = query
        # Cancel any pending rebuild from a previous keystroke — last input
        # wins, so we never grep for an intermediate prefix the user has
        # already moved past.
        prev = getattr(self, "_debounce_timer", None)
        if prev is not None:
            try:
                prev.stop()
            except Exception:
                pass
            self._debounce_timer = None
        # Empty query: clearing the box should feel instant, no debounce.
        if not query.strip():
            self._apply_filter(query)
            return
        # Otherwise schedule a single deferred rebuild.
        try:
            self._debounce_timer = self.set_timer(
                self.DEBOUNCE_SEC,
                lambda q=query: self._apply_filter(q),
            )
        except Exception:
            # Fallback: if set_timer is unavailable for any reason, apply
            # synchronously so search at least still works.
            self._apply_filter(query)

    def _apply_filter(self, query: str) -> None:
        """Rebuild the picker for `query`. Called from the debounce timer or
        directly for the empty-query fast path. Safe to call after the widget
        has been unmounted (guards every DOM op)."""
        self._debounce_timer = None
        # If the input value has moved on while we were waiting, skip this
        # stale rebuild — a fresher timer will land shortly with the latest
        # text. This keeps fast typing snappy without queueing grep work.
        try:
            current = self._search_input.value if self._search_input else query
        except Exception:
            current = query
        if (current or "") != (query or ""):
            return
        filtered = _filter_choices(self.msg.all_choices or [], query)
        self.msg.choices = filtered
        # Remove the old picker before mounting a new one. `remove()` is sync
        # enough for our needs — Textual flushes the DOM before the next paint.
        if self.picker is not None:
            try:
                self.picker.remove()
            except Exception:
                pass
            self.picker = None
        if not filtered:
            # Show a disabled hint row so Enter on an empty result set is a
            # no-op rather than a crash inside _collapse_choice.
            empty = ChoiceList(self.msg, "(no matches)", classes="picker")
            try:
                empty.disabled = True
            except Exception:
                pass
            self.picker = empty
        else:
            self.picker = self._build_picker(filtered)
        try:
            self.mount(self.picker)
        except Exception:
            # Widget likely unmounted between the timer firing and now (e.g.
            # user pressed Esc). Drop silently — nothing to render into.
            return
        # A rebuilt result set changes the wrapper height; re-pin it into view
        # so a query that shrinks/grows the list never leaves the picker (or
        # the search Input) stranded below the fold. Same deferred-scroll
        # rationale as first mount.
        self._rescroll_into_view()

    def on_key(self, event) -> None:
        # While the Input has focus, redirect navigation keys to the picker so
        # the user can keep typing yet still drive selection. Enter/Right on
        # the Input commits the current highlight.
        if self._search_input is None or self.picker is None:
            return
        if not self._search_input.has_focus:
            return
        key = event.key
        if key == "up":
            # Up from the search box wraps around to the BOTTOM of the list, so
            # the loop is search ↓→ list top ... list top ↑→ search ↑→ list
            # bottom. Land on the last row directly.
            try:
                self.picker.focus()
                last = getattr(self.picker, "action_last", None)
                if last is not None:
                    last()
                else:
                    n = getattr(self.picker, "option_count", 0)
                    if n:
                        self.picker.highlighted = n - 1
            except Exception:
                pass
            event.stop(); event.prevent_default()
            return
        if key in ("down", "pageup", "pagedown", "home", "end"):
            try:
                self.picker.focus()
                # Replay one step so the very first arrow doesn't get swallowed
                # by the focus change. Subsequent arrows go straight to the picker.
                action = {
                    "down": self.picker.action_cursor_down,
                    "pagedown": getattr(self.picker, "action_page_down", None),
                    "pageup": getattr(self.picker, "action_page_up", None),
                    "home": getattr(self.picker, "action_first", None),
                    "end": getattr(self.picker, "action_last", None),
                }.get(key)
                if action is not None:
                    action()
            except Exception:
                pass
            event.stop(); event.prevent_default()
            return
        if key == "right":
            # Right commits the highlight ONLY when the caret is already at the
            # end of the query — otherwise let the Input consume it so Right
            # still moves the caret within the search text (the box must stay
            # editable). Without this guard Right was always swallowed and the
            # cursor could never move right inside `/continue`'s search box.
            try:
                at_end = self._search_input.cursor_position >= len(self._search_input.value or "")
            except Exception:
                at_end = True
            if not at_end:
                return
        if key in ("enter", "right"):
            try:
                self.picker.action_select()
            except Exception:
                pass
            event.stop(); event.prevent_default()
            return


class MultiChoiceList(SelectionList):
    """Multi-select variant of ChoiceList. Space toggles, Enter submits all
    checked items joined by `; `. Esc cancels back to free-text input.

    SelectionList expects `Selection` objects as positional args, so we
    forward `*selections` through. The `msg` kwarg is ours.
    """
    BINDINGS = [*SelectionList.BINDINGS,
                Binding("enter", "submit", "Submit", show=True),
                Binding("escape", "cancel", "Cancel", show=False)]

    def __init__(self, msg: "ChatMessage", *selections, **kwargs):
        super().__init__(*selections, **kwargs)
        self.msg = msg

    def action_submit(self) -> None:
        try:
            self.app._finalize_multi_choice(self.msg, list(self.selected))
        except Exception:
            pass

    def action_cancel(self) -> None:
        try:
            self.app._cancel_choice(self.msg)
        except Exception:
            pass


class SelectableStatic(Static):
    # PR #461: a SelectableStatic that gets removed from the DOM but whose
    # reference still lingers (e.g. cached in a closure) was firing mouse
    # selection on stale screen coordinates.  has_valid_selection_parent
    # is the cheap "am I still in the tree?" probe used by the screen-
    # level mouse-event filter (`_is_stale_selectable_mouse_event`).
    def has_valid_selection_parent(self) -> bool:
        return isinstance(self.parent, Widget)

    # Widget.get_selection returns None for non-Text/Content visuals; fall back to render_line.
    def get_selection(self, selection):
        render = getattr(self, "_ga_render", None)
        if render is not None:
            return _extract_md_render(render, selection), "\n"
        result = super().get_selection(selection)
        if result is not None:
            return result
        height = self.size.height
        if height <= 0:
            return None
        lines = []
        for y in range(height):
            try:
                strip = self.render_line(y)
            except Exception:
                lines.append("")
                continue
            lines.append("".join(seg.text for seg in strip))
        if not lines:
            return None
        return selection.extract("\n".join(lines)), "\n"


def _extract_md_render(render, selection) -> str:
    starts = render.line_starts
    indents = render.line_indents
    lens = render.line_lengths
    n = len(starts)
    if n == 0:
        return ""

    if selection.start is None:
        s_y, s_x = 0, 0
    else:
        s_y, s_x = selection.start.y, selection.start.x
    if selection.end is None:
        e_y, e_x = n - 1, lens[n - 1]
    else:
        e_y, e_x = selection.end.y, selection.end.x

    s_y = max(0, min(s_y, n - 1))
    e_y = max(0, min(e_y, n - 1))

    def col(y, x):
        ind = indents[y]
        total = lens[y]
        content_len = max(0, total - ind)
        if x <= ind:
            return 0
        return min(x - ind, content_len)

    return render.source[starts[s_y] + col(s_y, s_x): starts[e_y] + col(e_y, e_x)]


class FoldHeader(SelectableStatic):
    # Clickable collapsed/expanded turn header. App.on_click reads .msg/.fold_idx
    # to toggle msg._toggled_folds and remount the segments around this widget.
    def __init__(self, body, msg, fold_idx, **kwargs):
        super().__init__(body, **kwargs)
        self.msg = msg
        self.fold_idx = fold_idx


# User-message display elision: pastes get expanded to full content before send
# (agent needs the whole thing) but the user-visible message echo collapses the
# middle so the chat log doesn't get buried under a 1000-line dump.
_USER_DISPLAY_HEAD_LINES = 10
_USER_DISPLAY_TAIL_LINES = 5
_USER_DISPLAY_MAX_LINES = _USER_DISPLAY_HEAD_LINES + _USER_DISPLAY_TAIL_LINES + 4


def _elide_user_display(text: str) -> str:
    """Collapse middle of long user messages: keep head + tail, summarize gap."""
    lines = text.split("\n")
    n = len(lines)
    if n <= _USER_DISPLAY_MAX_LINES:
        return text
    omitted = n - _USER_DISPLAY_HEAD_LINES - _USER_DISPLAY_TAIL_LINES
    head = lines[:_USER_DISPLAY_HEAD_LINES]
    tail = lines[-_USER_DISPLAY_TAIL_LINES:]
    return "\n".join(head + [f"⋯ 省略 {omitted} 行 ⋯"] + tail)


def _read_clipboard_text() -> str:
    try:
        import tkinter as tk
        r = tk.Tk(); r.withdraw()
        try:
            return r.clipboard_get() or ""
        finally:
            r.destroy()
    except Exception:
        return ""


_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".tiff", ".tif", ".ico"}


def _grab_clipboard_file() -> Optional[tuple[str, bool]]:
    """Return (path, is_image) from clipboard. is_image distinguishes image files
    (rendered inline as `[Image #N]`) from any other file (folded as `[File #N]`)."""
    try:
        from PIL import ImageGrab, Image
        data = ImageGrab.grabclipboard()
    except Exception:
        return None
    if isinstance(data, list):
        for item in data:
            if isinstance(item, str) and os.path.isfile(item):
                is_img = os.path.splitext(item)[1].lower() in _IMAGE_EXTS
                return (item, is_img)
        return None
    if isinstance(data, Image.Image):
        try:
            out_dir = os.path.join(tempfile.gettempdir(), "genericagent_tui_clipboard")
            os.makedirs(out_dir, exist_ok=True)
            path = os.path.join(out_dir, f"clipboard_{int(time.time() * 1000)}.png")
            data.save(path, "PNG")
            return (path, True)
        except Exception:
            return None
    return None


class InputArea(TextArea):
    _PASTE_RE = re.compile(r'\[Pasted text #(\d+) \+\d+ lines\]')
    # `[Image #N]` is the folded form; expand_placeholders restores the raw path at submit time.
    # The longer `[Image #N: ...]` form is tolerated for backward compatibility only.
    _IMAGE_RE = re.compile(r'\[Image #(\d+)(?::[^\]]*)?\]')
    _FILE_RE = re.compile(r'\[File #(\d+)\]')
    _PLACEHOLDER_RES = (_PASTE_RE, _IMAGE_RE, _FILE_RE)

    BINDINGS = [
        Binding("ctrl+j",      "newline", "Newline", show=False),
        Binding("ctrl+enter",  "newline", "Newline", show=False),
        Binding("shift+enter", "newline", "Newline", show=False),
        Binding("ctrl+v",      "paste", "Paste", show=False),
        # macOS muscle-memory alias: most terminals swallow Cmd+V (forward via bracketed
        # paste → _on_paste); this only hits if the terminal forwards Cmd as a key.
        Binding("cmd+v",       "paste", "Paste", show=False),
        # Ctrl+U: readline-style kill-line, repurposed here to clear the whole input.
        Binding("ctrl+u",      "clear_input", "ClearInput", show=False),
        # Ctrl+S: toggle-stash the current draft.  First press → stash
        # text + clear input; second press on empty input → restore the
        # stashed draft.  Independent of Up/Down history so a queued
        # draft survives sending the previous one.  reset() uses
        # TextArea.clear() to avoid the document-rebuild path that
        # blocked the UI for seconds on long sessions.
        Binding("ctrl+s",      "stash", "Stash", show=False),
        Binding("cmd+s",       "stash", "Stash", show=False),
    ]

    def action_noop(self) -> None:
        pass

    def action_stash(self) -> None:
        """Stash/restore the input draft.  reset()/text restore both defer
        to `call_after_refresh` so the layout cascade runs off the
        keystroke event, leaving Ctrl+S itself snappy on long sessions."""
        current = self.text
        if current:
            self._draft_stash = current
            self._history_index = -1
            self._history_stash = ""
            try:
                self.app.call_after_refresh(self._stash_cleanup_clear)
            except Exception:
                # Last-resort synchronous fallback (re-introduces the freeze
                # window but at least keeps the function correct).
                self._stash_cleanup_clear()
        elif self._draft_stash:
            stashed = self._draft_stash
            self._draft_stash = ""
            self._history_index = -1
            self._history_stash = ""
            try:
                self.app.call_after_refresh(self._stash_cleanup_restore, stashed)
            except Exception:
                self._stash_cleanup_restore(stashed)

    def _stash_cleanup_clear(self) -> None:
        """Deferred companion to action_stash (clear path).  The Changed
        event posted by `clear()` is async-queued — set the flag and let
        `on_text_area_changed` self-clear it when the event lands.  A
        try/finally here clears the flag too early and lets the handler
        re-run the heavy resize + palette path."""
        self._skip_change_next = True
        self.reset()
        try: self.app._hide_palette()
        except Exception: pass
        try: self.app._resize_input(self)
        except Exception: pass

    def _stash_cleanup_restore(self, stashed: str) -> None:
        """Deferred companion to action_stash (restore path)."""
        try: self._suppress_palette_next_change()
        except Exception: pass
        self.text = stashed
        try:
            self.cursor_location = self.document.end
        except Exception:
            pass
        try: self.app._resize_input(self)
        except Exception: pass

    def action_clear_input(self) -> None:
        self.reset()
        self._history_index = -1
        self._history_stash = ""
        try:
            self.app._hide_palette()
        except Exception:
            pass
        try:
            self.app._resize_input(self)
        except Exception:
            pass

    def _insert_via_keyboard(self, text: str) -> None:
        result = self._replace_via_keyboard(text, *self.selection)
        if result:
            self.move_cursor(result.end_location)
            self.focus()
            try:
                self.app._resize_input(self)
            except Exception:
                pass

    def _paste_file_from_clipboard(self) -> bool:
        result = _grab_clipboard_file()
        if not result:
            return False
        path, is_image = result
        self._paste_counter += 1
        sid = self._paste_counter
        self._pastes[sid] = path
        marker = f"[Image #{sid}]" if is_image else f"[File #{sid}]"
        self._insert_via_keyboard(marker)
        return True

    def _insert_paste_text(self, text: str) -> None:
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        line_count = len(text.splitlines()) or 1
        if line_count > 2:
            self._paste_counter += 1
            sid = self._paste_counter
            self._pastes[sid] = text
            text = f"[Pasted text #{sid} +{line_count} lines]"
        self._insert_via_keyboard(text)

    def action_paste(self) -> None:
        if self.read_only or self._paste_file_from_clipboard():
            return
        text = _read_clipboard_text() or getattr(self.app, "clipboard", "")
        if text:
            self._insert_paste_text(text)

    def action_paste_file(self) -> None:
        self._paste_file_from_clipboard()

    def _placeholder_adjacent(self, side: str) -> Optional[tuple[int, int, int, int]]:
        """Return (row, start_col, end_col, sid) if a placeholder is flush against
        the caret on the given side ('left' = backspace target, 'right' = delete target)."""
        if self.selection.start != self.selection.end:
            return None
        row, col = self.cursor_location
        try:
            line = self.text.split("\n")[row]
        except IndexError:
            return None
        for pat in self._PLACEHOLDER_RES:
            for m in pat.finditer(line):
                edge = m.end() if side == "left" else m.start()
                if edge == col:
                    return (row, m.start(), m.end(), int(m.group(1)))
        return None

    def _delete_placeholder(self, side: str) -> bool:
        hit = self._placeholder_adjacent(side)
        if not hit:
            return False
        row, start, end, sid = hit
        self.delete((row, start), (row, end))
        self._pastes.pop(sid, None)
        try:
            self.app._resize_input(self)
        except Exception:
            pass
        return True

    def action_delete_left(self) -> None:
        if not self._delete_placeholder("left"):
            super().action_delete_left()

    def action_delete_right(self) -> None:
        if not self._delete_placeholder("right"):
            super().action_delete_right()

    async def _on_mouse_down(self, event: events.MouseDown) -> None:
        # Right-button: short-circuit TextArea's default cursor-move so
        # paste lands at the user's existing caret, not where their mouse
        # happened to be — matches every native text-box right-click.
        if getattr(event, "button", 0) == 3:
            event.stop(); event.prevent_default()
            return
        await super()._on_mouse_down(event)

    async def _on_click(self, event: events.Click) -> None:
        if getattr(event, "button", 0) == 3 and not self.read_only:
            self.action_paste()
            event.stop(); event.prevent_default()

    class Submitted(Message):
        def __init__(self, input_area: "InputArea", value: str) -> None:
            super().__init__()
            self.input_area = input_area
            self.value = value

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._pastes: dict[int, str] = {}
        self._paste_counter = 0
        self._input_history: list[str] = []
        self._history_index: int = -1         # -1 means not browsing
        self._history_stash: str = ""
        # Ctrl+S scratch draft (PR#479 semantics). Distinct from
        # `_history_stash`, which is the Up/Down-arrow working buffer.
        self._draft_stash: str = ""
        # Set by `action_stash` to make on_input_area_changed bail out on
        # the synchronous Changed event from `reset()` — the layout work
        # is rescheduled via `call_after_refresh` so the keystroke handler
        # returns immediately even when streaming has the reactive queue
        # saturated.  Cleared by `_stash_cleanup_clear`.
        self._skip_change_next: bool = False
        self._HISTORY_MAX = 200

    def expand_placeholders(self, text: str) -> str:
        def repl(m):
            sid = int(m.group(1))
            return self._pastes.get(sid, m.group(0))
        for pat in self._PLACEHOLDER_RES:
            text = pat.sub(repl, text)
        return text

    # ---- history public API ----
    def record_history(self, raw_text: str) -> None:
        stripped = raw_text.strip()
        if not stripped:
            return
        if not (self._input_history and self._input_history[-1] == stripped):
            self._input_history.append(stripped)
            if len(self._input_history) > self._HISTORY_MAX:
                self._input_history = self._input_history[-self._HISTORY_MAX:]
        self._history_index = -1
        self._history_stash = ""

    def _suppress_palette_next_change(self) -> None:
        # Single-shot guard against re-opening the palette during programmatic text changes.
        self.app._suppress_palette_open = True

    def _history_up(self) -> bool:
        if not self._input_history:
            return False
        if self._history_index == -1:
            self._history_stash = self.text
            self._history_index = len(self._input_history) - 1
        elif self._history_index > 0:
            self._history_index -= 1
        else:
            return True  # already at oldest — absorb the key
        self._suppress_palette_next_change()
        self.text = self._input_history[self._history_index]
        return True

    def _history_down(self) -> bool:
        if self._history_index == -1:
            return False
        if self._history_index < len(self._input_history) - 1:
            self._history_index += 1
            new_text = self._input_history[self._history_index]
        else:
            self._history_index = -1
            new_text = self._history_stash
        self._suppress_palette_next_change()
        self.text = new_text
        return True

    def reset(self) -> None:
        # `self.text = ""` rebuilds Document + WrappedDocument and triggers
        # a full re-wrap + `_refresh_size` layout cascade.  On long
        # sessions (100+ message widgets in the scroll), that cascade
        # blocks the UI for seconds — perceived as freeze on Ctrl+S.
        # `clear()` deletes in place via the edit pipeline and only
        # re-wraps the affected range, so empty-out is O(content-len)
        # without rebuilding the document object.
        if self.document.text:
            self.clear()
        self._pastes.clear()
        self._paste_counter = 0
        self._history_index = -1
        self._history_stash = ""

    def action_newline(self) -> None:
        self._insert_via_keyboard("\n")

    def _shift_is_physically_down(self) -> bool:
        """Best-effort fallback for terminals/Textual builds that report Shift+Enter as plain Enter."""
        if os.name != "nt":
            return False
        try:
            import ctypes
            # VK_SHIFT = 0x10.  High bit means the key is currently down.
            return bool(ctypes.windll.user32.GetAsyncKeyState(0x10) & 0x8000)
        except Exception:
            return False

    async def _on_paste(self, event: events.Paste) -> None:
        # Terminal Ctrl+V in bracketed-paste mode lands here, bypassing action_paste.
        if self.read_only:
            return
        if self._paste_file_from_clipboard():
            event.stop(); event.prevent_default(); return
        # Git-bash / mintty fallback: PIL.ImageGrab can't return Image objects
        # in that TTY env, but the OS clipboard does hold the file path the
        # screenshot tool wrote. Treat a single-line, on-disk path as if the
        # file grab had succeeded — same placeholder + `_pastes` entry.
        if self._paste_file_from_text(event.text):
            event.stop(); event.prevent_default(); return
        self._insert_paste_text(event.text)
        event.stop(); event.prevent_default()

    def _paste_file_from_text(self, raw: str) -> bool:
        if not raw: return False
        path = raw.strip().strip('"').strip("'")
        if "\n" in path or "\r" in path: return False
        if len(path) > 1024: return False
        if not os.path.isfile(path): return False
        is_image = os.path.splitext(path)[1].lower() in _IMAGE_EXTS
        self._paste_counter += 1
        sid = self._paste_counter
        self._pastes[sid] = path
        marker = f"[Image #{sid}]" if is_image else f"[File #{sid}]"
        self._insert_via_keyboard(marker)
        return True

    async def _on_key(self, event: events.Key) -> None:
        # 1) command palette routing
        try:
            palette = self.app.query_one("#palette", OptionList)
        except Exception:
            palette = None
        if palette is not None and palette.has_class("-visible"):
            routes = {"up": palette.action_cursor_up, "down": palette.action_cursor_down}
            if event.key in {"enter", "right"} and palette.highlighted is not None:
                routes[event.key] = palette.action_select
            elif event.key == "left":
                routes["left"] = self.app._hide_palette
            fn = routes.get(event.key)
            if fn:
                fn(); event.stop(); event.prevent_default(); return
        # 2) inline ChoiceList routing — borrow arrow keys without moving focus.
        choice = getattr(self.app, "_active_choice", lambda: None)()
        if choice is not None:
            if event.key == "up":
                choice.action_cursor_up(); event.stop(); event.prevent_default(); return
            if event.key == "down":
                choice.action_cursor_down(); event.stop(); event.prevent_default(); return
            if event.key in ("enter", "right") and choice.highlighted is not None:
                choice.action_select(); event.stop(); event.prevent_default(); return
            if event.key == "escape":
                self.app._cancel_choice(choice.msg); event.stop(); event.prevent_default(); return
        # 3) history browse: only at (0,0) for up / end-of-text for down, so in-line
        #    cursor movement is preserved.
        if event.key == "up" and self.cursor_location == (0, 0):
            # Pending-queue recall removed: each Enter while running writes
            # to `_intervene` immediately; popping back would leave a stale
            # entry in the file.  Up just walks input history; Esc clears.
            if self._history_up():
                event.stop(); event.prevent_default(); return
        if event.key == "down":
            row, col = self.cursor_location
            lines = self.text.split("\n")
            if row == len(lines) - 1 and col == len(lines[-1]):
                if self._history_down():
                    event.stop(); event.prevent_default(); return
        if event.key == "enter":  # plain Enter submits; physical Shift+Enter inserts newline
            if self._shift_is_physically_down():
                event.stop(); event.prevent_default()
                self.action_newline()
                return
            event.stop(); event.prevent_default()
            self.post_message(self.Submitted(self, self.text))
            return
        if self._history_index != -1 and event.key not in ("up", "down", "left", "right"):
            self._history_index = -1
        await super()._on_key(event)


# ---------- top bar ----------
def _fmt_elapsed(secs: int) -> str:
    if secs < 60: return f"{secs}s"
    if secs < 3600: return f"{secs // 60}m {secs % 60:02d}s"
    h, rem = divmod(secs, 3600); m, s = divmod(rem, 60)
    return f"{h}h {m:02d}m {s:02d}s"


_TITLE_SPINNER_FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

# Done-flash window: dot stays green this many seconds after a run finishes.
_DONE_FLASH_SECS = 5

# Heat ramp for the running dot. Pale green → amber → deep orange → vivid red.
# The thresholds are deliberately non-linear: short runs stay cool, only past
# ~3min do we paint it red to signal "this is taking unusually long".
_HEAT_RAMP = (
    (20,  "#aae8aa"),       # <20s   pale mint
    (60,  "#d4a72c"),       # <60s   amber
    (180, "#dc6b1f"),       # <3min  deep orange
    (None, "bold #ff2c2c"), # ≥3min  vivid red — "stuck?" warning
)


def _heat_color(elapsed: int) -> str:
    """Map a busy-elapsed in seconds to a Rich style for the running dot."""
    for threshold, color in _HEAT_RAMP:
        if threshold is None or elapsed < threshold:
            return color
    return _HEAT_RAMP[-1][1]


# Gerund (`Reticulating…`) easter-egg color ramp. Drives a two-axis heat:
# elapsed seconds + accumulated tokens. Cool blue → cyan → mint → amber → red.
# Each tier returns a Rich style string. Keep bands wide so the color rarely
# strobes between adjacent ticks.
_GERUND_RAMP = (
    "#5e9fd6",          # cool blue   — fresh, < ~10s and < ~1k tokens
    "#56d4d4",          # cyan        — warming up
    "#7ec27e",          # mint        — settled cruise
    "#d4a72c",          # amber       — taking a while
    "#dc6b1f",          # deep orange — long wait
    "bold #ff2c2c",     # vivid red   — really stuck
)


def _gerund_color(elapsed: int, tokens: int) -> str:
    """Compose a tier index from elapsed (sec) + tokens, then index the ramp.

    The two axes contribute additively so a tokenless 3-minute hang and a
    fast-but-token-heavy run both walk up the ramp. Tiers are integer-clamped
    to len(ramp)-1 so the worst case caps at the red band.
    """
    t_tier = 0 if elapsed < 10 else 1 if elapsed < 30 else 2 if elapsed < 90 else 3 if elapsed < 180 else 4
    k_tier = 0 if tokens < 1_000 else 1 if tokens < 10_000 else 2 if tokens < 50_000 else 3
    tier = min(len(_GERUND_RAMP) - 1, t_tier + k_tier)
    return _GERUND_RAMP[tier]


def render_status_chip(busy: bool, elapsed: int = 0) -> Text:
    """`✦ GenericAgent` identity chip. Brightens green when any session is busy.

    The `elapsed` kwarg is kept for API stability but intentionally unrendered:
    the per-session dot now carries the elapsed counter, which is more accurate
    than an app-wide tally when multiple sessions run concurrently.
    """
    chip = Text()
    chip.append("✦ ", style=C_GREEN if busy else C_DIM)
    chip.append("GenericAgent", style=f"bold {C_GREEN}" if busy else f"bold {C_FG}")
    return chip


def render_topbar(session_name: str, status: str, model: str, tasks_running: int,
                  fold_mode: bool = True, busy_elapsed: int = 0,
                  effort: str = "", sess_elapsed: int = 0,
                  just_done: bool = False, term_width: int = 0) -> Table:
    # Layout: identity-chip + session + status + fold packed LEFT; model + effort
    # + tasks CENTERED; clock RIGHT. The 2:2:1 ratio keeps the centered model
    # chip visually anchored even when the left column has the long status pill.
    # The OS terminal tab title carries the session name separately — see
    # GenericAgentTUI._update_terminal_title.
    t = Table.grid(expand=True)
    # Equal column widths so the middle column's geometric center sits at the
    # window center. Uneven ratios shift the centered band off-axis.
    t.add_column(ratio=1, justify="left", no_wrap=True, overflow="ellipsis")
    t.add_column(ratio=1, justify="center", no_wrap=True, overflow="ellipsis")
    t.add_column(ratio=1, justify="right", no_wrap=True)

    short_name = session_name if len(session_name) <= 20 else session_name[:19] + "…"

    # LEFT: identity chip · session · status
    left = Text()
    left.append_text(render_status_chip(busy=tasks_running > 0, elapsed=busy_elapsed))
    left.append("  ·  ", style=C_DIM)
    left.append("session: ", style=C_MUTED); left.append(short_name, style=f"bold {C_CHIP_NAME}")
    left.append("  ·  ", style=C_DIM)
    if status == "running":
        dot_color = _heat_color(sess_elapsed)
        left.append("● ", style=dot_color)
        left.append(f"running {_fmt_elapsed(sess_elapsed)}", style=f"bold {dot_color}")
    elif just_done:
        left.append("● ", style=f"bold {C_GREEN}")
        left.append("done", style=f"bold {C_GREEN}")
    else:
        left.append("● ", style=C_DIM); left.append(status, style=C_MUTED)

    # CENTER: model · effort · tasks — dropped right-to-left on narrow terminals
    # so the chip column never wraps under the left half.
    budget = max(20, (term_width * 2 // 5) - 6) if term_width else 999
    def chip_w(label: str, value: str) -> int:
        return len(label) + len(value) + 5
    used = chip_w("model: ", model or "?")
    show_effort = bool(effort) and used + chip_w("effort: ", effort) <= budget
    if show_effort: used += chip_w("effort: ", effort)
    show_tasks = used + chip_w("tasks: ", str(tasks_running)) <= budget
    mid = Text()
    mid.append("model: ", style=C_MUTED); mid.append(model or "?", style=C_CHIP_MODEL)
    if show_effort:
        mid.append("  ·  ", style=C_DIM)
        mid.append("effort: ", style=C_MUTED); mid.append(effort, style=f"bold {C_CHIP_EFFORT}")
    if show_tasks:
        mid.append("  ·  ", style=C_DIM)
        mid.append("tasks: ", style=C_MUTED); mid.append(str(tasks_running), style=C_CHIP_TASKS)

    # RIGHT: fold indicator + clock. Moved here from the LEFT column to keep the
    # narrow `▾ fold` glyph from being eaten by the left's ellipsis when the
    # running status pill fills the column budget.
    right = Text()
    if fold_mode:
        right.append("▾ fold", style=C_DIM)
        right.append("  ·  ", style=C_DIM)
    right.append(time.strftime("%H:%M:%S"), style=C_CHIP_TIME)

    t.add_row(left, mid, right)
    return t


def render_bottombar(quit_armed: bool = False, rewind_armed: bool = False) -> Table:
    t = Table.grid(expand=True)
    t.add_column(justify="left")
    left = Text()
    if quit_armed:
        left.append(f"再按 {fmt_key('ctrl+c')} 退出", style=f"bold {C_GREEN}")
    elif rewind_armed:
        left.append("再按 Esc 回退", style=f"bold {C_GREEN}")
    else:
        pairs = [("enter", "发送"), ("ctrl+n", "新会话"),
                 ("ctrl+b", "侧栏"), ("ctrl+c", "停止/退出"),
                 ("/", "命令面板"), ("ctrl+/", "快捷键帮助")]
        for i, (combo, d) in enumerate(pairs):
            if i: left.append("    ")
            k = "/" if combo == "/" else fmt_key(combo)
            left.append(k, style=C_GREEN if combo in ("/", "ctrl+/") else C_FG)
            left.append(" ")
            left.append(d, style=C_MUTED)
    t.add_row(left)
    return t


