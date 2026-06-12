# Frontends Refactoring Execution Plan

**Date**: 2026-06-10
**Scope**: `/home/exusiai/GenericAgent/frontends/` (38 files, flat directory)
**Strategy**: Phase-by-phase, each self-contained and independently reversible via `git checkout -- frontends/`

---

## Pre-Flight Audit Results (completed 2026-06-10)

### Entry Point References
| Entry Point | References `tui_v3` | References `stapp2` | Direct `import frontends` |
|-------------|---------------------|---------------------|---------------------------|
| `ga.py` (CLI) | No | No | No |
| `agentmain.py` | No | No | No |
| `launch.pyw` (desktop) | No | No | No (uses `os.path.join`) |
| `hub.pyw` (service hub) | No (no "app" in name) | Yes (auto-discovered: "app" in name) | No |

### Auto-Discovery Rules (hub.pyw)
- Scans `frontends/` for files: `'app' in f and f.endswith('.py') and f not in EXCLUDES`
- `EXCLUDES = {'goal_mode.py', 'chatapp_common.py', 'tuiapp.py'}`
- `tui_v3.py` is excluded because "app" is NOT in its name
- `stapp2.py` IS auto-discovered (has "app", is .py, not excluded)

### Internal frontends Imports (who imports from the `frontends` package)
Only 3 files use `from frontends.xxx` style imports:
1. `fsapp.py` — `from frontends.chatapp_common import ...`
2. `tuiapp_v2.py` — `from frontends import slash_cmds` (lazy)
3. `tui_v3.py` — `from frontends.slash_cmds import ...` (lazy, 20+ locations)

All other files import via relative paths (no package imports).

### Existing Subdirectories
- `frontends/desktop/` — Tauri project (src-tauri, package.json), NOT Python files
- `frontends/skins/` — UI theme assets (10 subdirs)
- `frontends/conductor_im_plugins/` — Conductor IM plugin modules
- `frontends/temp/` — temp artifacts
- `frontends/__pycache__/` — compiled bytecache

---

## Phase 1: Deprecation Marking (LOW risk, atomic)

**What changes**: Add comment headers to 2 files + create 1 new README file. NO file moves, NO import changes, NO git mv.

**User approval required for**: Which files to deprecate.

### Files Proposed for Deprecation

#### 1. `frontends/tui_v3.py` (266,652 lines, 5 MB)
- **Evidence**: NOT wired into any entry point. No "app" in name so hub.pyw excludes it. Only referenced in README changelog.
- **Status**: Implemented but orphaned — no code path reaches it.
- **Recommendation**: Mark deprecated with 30-day safe-delete date.

#### 2. `frontends/stapp2.py` (38,073 lines, 1 MB)
- **Evidence**: NOT referenced by launch.pyw (primary launcher). IS auto-discovered by hub.pyw. `stapp.py` (17,457 lines) is the active Streamlit frontend.
- **Status**: Secondary/backup Streamlit version, auto-discoverable but not primary.
- **Recommendation**: Flag for user decision — deprecate or keep.

### Phase 1 Steps

**Step 1.1**: Create `frontends/_DEPRECATED.md`:

```markdown
# Deprecated Frontends

This file tracks frontend modules that have been deprecated.
Deprecated files remain in-tree for reference but are no longer
wired into any entry point or actively maintained.

---

## tui_v3.py (deprecated 2026-06-10)
- **Reason**: Not wired into any entry point (ga.py, agentmain.py, launch.pyw, hub.pyw).
  Replaced by tuiapp_v2.py for active TUI use.
- **Recommendation**: Use `python -m frontends.tuiapp_v2` or `ga tui2`.
- **Safe to delete after**: 2026-07-10

## stapp2.py (deprecated 2026-06-10)
- **Reason**: Secondary Streamlit version. Primary launcher (launch.pyw) uses stapp.py.
  Auto-discovered by hub.pyw but not the canonical Streamlit frontend.
- **Recommendation**: Use `stapp.py` for Streamlit access.
- **Safe to delete after**: TBD (awaiting user decision)
```

**Step 1.2**: Add deprecation header to `frontends/tui_v3.py` (line 1):

```python
# DEPRECATED — see frontends/_DEPRECATED.md
# This file is no longer wired into any entry point (ga.py, agentmain.py,
# launch.pyw, hub.pyw). Use tuiapp_v2.py instead. Safe to delete after 2026-07-10.
```

**Step 1.3**: Add deprecation header to `frontends/stapp2.py` (line 1):

```python
# DEPRECATED — see frontends/_DEPRECATED.md
# Secondary Streamlit version. Primary launcher uses stapp.py.
# Safe to delete after: TBD (awaiting user confirmation).
```

**Step 1.4**: Commit as single atomic change.

### Rollback (Phase 1)
```bash
cd /home/exusiai/GenericAgent && git checkout -- frontends/_DEPRECATED.md  # if created
cd /home/exusiai/GenericAgent && git checkout -- frontends/tui_v3.py frontends/stapp2.py
```

### User Confirmation Checklist (Phase 1)
- [ ] Confirm `tui_v3.py` should be marked deprecated
- [ ] Confirm `stapp2.py` should be marked deprecated (or keep as secondary)
- [ ] Approve creation of `frontends/_DEPRECATED.md`

---

## Phase 2: Subdirectory Reorganization (MEDIUM risk)

**What changes**: git mv files into logical subdirectories. Update internal imports in 3 files.

**User approval required for**: Target directory structure.

### Proposed Structure

```
frontends/
├── _DEPRECATED.md
├── shared/              # Shared utilities, no app logic
│   ├── chatapp_common.py
│   ├── slash_cmds.py
│   ├── session_names.py
│   ├── cost_tracker.py
│   ├── keysym.py
│   ├── btw_cmd.py
│   ├── continue_cmd.py
│   ├── review_cmd.py
│   └── export_cmd.py
├── tui/                 # Terminal UI frontends
│   ├── tuiapp.py        (v1, default CLI TUI)
│   ├── tuiapp_v2.py     (v2, alternative CLI TUI)
│   └── tui_v3.py        (v3, DEPRECATED)
├── chat/                # Chat bot frontends (IM platforms)
│   ├── tgapp.py         (Telegram)
│   ├── wechatapp.py     (WeChat)
│   ├── dingtalkapp.py   (DingTalk)
│   ├── wecomapp.py      (WeCom)
│   ├── qqapp.py         (QQ)
│   ├── fsapp.py         (Feishu/Lark)
│   └── dcapp.py         (Discord)
├── streamlit/           # Streamlit web frontends
│   ├── stapp.py         (primary)
│   └── stapp2.py        (DEPRECATED)
├── desktop/             # Desktop GUI and bridge (existing dir, Tauri + Python)
│   ├── desktop_pet.pyw
│   ├── desktop_pet_v2.pyw
│   ├── desktop_bridge.py
│   ├── qtapp.py
│   └── genericagent_acp_bridge.py
├── conductor/           # Service conductor
│   ├── conductor.py
│   ├── conductor.html
│   ├── conductor_im_plugins/
│   └── plan_state.py
├── skins/               # UI themes (existing)
├── assets/              # Move chat_bubble.png, pet.gif, DESKTOP_PET_README.md here
│   ├── chat_bubble.png
│   ├── pet.gif
│   └── DESKTOP_PET_README.md
├── __pycache__/
└── temp/
```

### Import Impact Analysis

Files that will need import path updates:

| File | Current Import | New Import |
|------|---------------|------------|
| `fsapp.py` | `from frontends.chatapp_common import ...` | `from frontends.shared.chatapp_common import ...` |
| `tuiapp_v2.py` | `from frontends import slash_cmds` | `from frontends.shared import slash_cmds` |
| `tui_v3.py` | `from frontends.slash_cmds import ...` (20+ locations) | `from frontends.shared.slash_cmds import ...` |

Entry point files that need path updates:

| File | Current Path | New Path |
|------|-------------|----------|
| `launch.pyw` | `frontends/stapp.py` | `frontends/streamlit/stapp.py` |
| `launch.pyw` | `frontends/tgapp.py` | `frontends/chat/tgapp.py` |
| `launch.pyw` | `frontends/qqapp.py` | `frontends/chat/qqapp.py` |
| `launch.pyw` | `frontends/fsapp.py` | `frontends/chat/fsapp.py` |
| `launch.pyw` | `frontends/wechatapp.py` | `frontends/chat/wechatapp.py` |
| `launch.pyw` | `frontends/wecomapp.py` | `frontends/chat/wecomapp.py` |
| `launch.pyw` | `frontends/dingtalkapp.py` | `frontends/chat/dingtalkapp.py` |
| `hub.pyw` | `frontends/` scan dir + cmd construction | Update `frontends_dir` subpath resolution |
| `ga_cli/cli.py` | TUI launcher references | TBD (needs verification) |

### Phase 2 Prerequisites
1. Complete Phase 1 (deprecation markers in place)
2. Run full import audit (see commands below)
3. Verify `ga_cli/cli.py` references (not yet checked)
4. Verify `assets/configure_mykey.py` reference (line 976 imports `frontends.wechatapp`)

### Phase 2 Steps

**Step 2.1**: Run comprehensive import audit:
```bash
# All imports from frontends package
grep -rn 'from frontends\|import frontends' /home/exusiai/GenericAgent --include='*.py' --include='*.pyw' | grep -v __pycache__ | grep -v _DEPRECATED

# All file-path references to frontends/ (used by launch.pyw, hub.pyw)
grep -rn 'frontends/' /home/exusiai/GenericAgent --include='*.py' --include='*.pyw' | grep -v __pycache__ | grep -v '\.git'
```

**Step 2.2**: Create target directories:
```bash
mkdir -p /home/exusiai/GenericAgent/frontends/{shared,tui,chat,streamlit,assets}
```

**Step 2.3**: git mv files into subdirectories (preserves git history):
```bash
cd /home/exusiai/GenericAgent

# Shared utilities
git mv frontends/chatapp_common.py frontends/shared/
git mv frontends/slash_cmds.py frontends/shared/
git mv frontends/session_names.py frontends/shared/
git mv frontends/cost_tracker.py frontends/shared/
git mv frontends/keysym.py frontends/shared/
git mv frontends/btw_cmd.py frontends/shared/
git mv frontends/continue_cmd.py frontends/shared/
git mv frontends/review_cmd.py frontends/shared/
git mv frontends/export_cmd.py frontends/shared/

# TUI frontends
git mv frontends/tuiapp.py frontends/tui/
git mv frontends/tui/tuiapp_v2.py frontends/tui/
git mv frontends/tui_v3.py frontends/tui/

# Chat bot frontends
git mv frontends/tgapp.py frontends/chat/
git mv frontends/wechatapp.py frontends/chat/
git mv frontends/dingtalkapp.py frontends/chat/
git mv frontends/wecomapp.py frontends/chat/
git mv frontends/qqapp.py frontends/chat/
git mv frontends/fsapp.py frontends/chat/
git mv frontends/dcapp.py frontends/chat/

# Streamlit frontends
git mv frontends/stapp.py frontends/streamlit/
git mv frontends/stapp2.py frontends/streamlit/

# Desktop files (move into existing desktop/ subdir)
git mv frontends/desktop_pet.pyw frontends/desktop/
git mv frontends/desktop_pet_v2.pyw frontends/desktop/
git mv frontends/desktop_bridge.py frontends/desktop/
git mv frontends/qtapp.py frontends/desktop/
git mv frontends/genericagent_acp_bridge.py frontends/desktop/

# Conductor
git mv frontends/conductor.py frontends/conductor/
git mv frontends/conductor.html frontends/conductor/
git mv frontends/plan_state.py frontends/conductor/
# conductor_im_plugins/ already inside conductor/ target? Move if needed.

# Assets (non-code)
git mv frontends/chat_bubble.png frontends/assets/
git mv frontends/pet.gif frontends/assets/
git mv frontends/DESKTOP_PET_README.md frontends/assets/
```

**Step 2.4**: Create `frontends/__init__.py` if needed for package imports to continue working. Or use namespace packages.

**Step 2.5**: Update all imports (see import impact table above).

**Step 2.6**: Update entry points (launch.pyw, hub.pyw, assets/configure_mykey.py).

**Step 2.7**: Verify no runtime breakage — test `ga tui`, `ga tui2`, and streamlit launch.

### Rollback (Phase 2)
```bash
cd /home/exusiai/GenericAgent && git checkout -- frontends/ ga.py agentmain.py launch.pyw hub.pyw assets/configure_mykey.py
```

### User Confirmation Checklist (Phase 2)
- [ ] Approve target subdirectory structure
- [ ] Confirm `frontends/desktop/` subdir to also contain Python files (currently Tauri-only)
- [ ] Confirm `tuiapp.py` (v1, 731 lines) stays active (it IS the default CLI TUI via `ga tui`)
- [ ] Approve git mv approach (preserves git blame/history)

---

## Phase 3: temp/ Cleanup (LOW risk)

**What changes**: Add `.gitignore` entries. No file deletion (user does that manually).

### Current temp/ Status (from report)
- 68 files, 5.6 MB total
- `temp/model_responses/` — 5.5 MB (98%), 23 LLM response log files
- `temp/user_prompt_*.md` — 16 files, ~116 KB cumulative
- `temp/code_quality_task/` — 20 KB
- `temp/sandbox/reports/` — 12 KB
- Various other analysis artifacts

### Phase 3 Steps

**Step 3.1**: Add to `.gitignore`:
```
temp/model_responses/
temp/*.txt
temp/user_prompt_*.md
temp/code_quality_*
temp/*_analysis.md
```

**Step 3.2**: Commit .gitignore change. User manually deletes temp files if desired.

### Rollback (Phase 3)
```bash
cd /home/exusiai/GenericAgent && git checkout -- .gitignore
```

### User Confirmation Checklist (Phase 3)
- [ ] Confirm which temp/ patterns to gitignore
- [ ] Confirm user will manually delete temp files (not automated)

---

## Global Rollback Plan

Any phase can be independently reverted:
```bash
# Phase 1 only
cd /home/exusiai/GenericAgent && git checkout -- frontends/tui_v3.py frontends/stapp2.py frontends/_DEPRECATED.md

# Phase 2 only
cd /home/exusiai/GenericAgent && git checkout -- frontends/ launch.pyw hub.pyw ga.py agentmain.py assets/configure_mykey.py ga_cli/cli.py

# Phase 3 only
cd /home/exusiai/GenericAgent && git checkout -- .gitignore
```

---

## Execution Order

1. **User reviews and approves Phase 1 scope** (which files to deprecate)
2. Execute Phase 1 (atomic commit)
3. **User reviews import audit results and approves Phase 2 structure**
4. Execute Phase 2 (atomic commit)
5. **User reviews Phase 3 patterns**
6. Execute Phase 3 (atomic commit)
