# Memory Index Hygiene SOP

## 触发词

- "检查 L1 路由完整性"
- "Memory Index Hygiene"
- "同步 L1 索引"
- "更新 global_mem_insight"

## 目的

确保 `memory/global_mem_insight.txt`（L1）与实际 `memory/` 目录下的文件保持同步，使 L1 能够路由到所有关键工作流。

## 前置条件

1. 已读取 `memory/sandbox_policy.md` — 确认当前写入权限
2. 已读取 `memory/memory_management_sop.md`（META-SOP）— 确认 L1 约束
3. 已读取 `memory/personal_bootstrap_profile.md` — 确认用户边界

## 输入

无。自动读取 `memory/` 目录和 `memory/global_mem_insight.txt`。

## 步骤

### Step 1: 基准采集

```powershell
# List memory directory — use Python (cross-platform) or PowerShell on Windows
python -c "import os; print('\n'.join(f for f in os.listdir('memory') if os.path.isfile(os.path.join('memory', f))))"
file_read ../memory/global_mem_insight.txt
```

### Step 2: 差距分析

对比当前 L1 与 7 个必须路由目标：

| 路由目标 | 检查关键词 |
|---------|-----------|
| Bootstrap | `personal_bootstrap_profile.md` |
| Project map | `project_map.md` |
| SOPs | L3 列表中有代表性 SOP 名 |
| Browser | `tmwebdriver_sop` |
| Mobile | `adb_ui.py` |
| Scheduling | `scheduled_task_sop` |
| Review workflows | `review_sop` |

### Step 3: 提交 patch 方案

使用标准模板请求用户批准：

```text
我要修改: <path>/global_mem_insight.txt
动作: <file_patch>
原因: 缺失 XXX 路由 / 命名错误
验证方式: file_read 确认行内容
是否允许？
```

### Step 4: 执行 patch（用户批准后）

- 使用 `file_patch`，一次提交所有修改
- 不允许 `file_write` overwrite 整个文件

### Step 5: 验证

```python
routes = {
    'Bootstrap': 'personal_bootstrap_profile.md',
    'Project map': 'project_map.md',
    'Review': 'review_sop',
    'SOPs': 'memory_cleanup_sop',
    'Browser': 'tmwebdriver_sop',
    'Scheduling': 'scheduled_task_sop',
    'Mobile': 'adb_ui.py'
}
content = open('../memory/global_mem_insight.txt').read()
line_count = len(content.splitlines())
all_found = all(kw in content for kw in routes.values())
# 输出结果
```

## 成功标准

- L1 文件 ≤30 行
- 7 个路由目标全部可路由
- 文件路径引用与实际文件匹配（无 `ljqCtrl_sop+.py` 这类命名错误）

## 失败恢复

| 尝试次数 | 操作 |
|---------|------|
| 第 1 次失败 | 重新读取文件内容，确认 old_content 唯一匹配 |
| 第 2 次失败 | 检查 sandbox policy，确认写入权限 |
| 第 3 次失败 | 请求用户介入，展示当前文件状态和预期修改 |

## 人为确认点

1. **写操作前**：必须请求用户批准 patch 方案（sandbox policy §22）
2. **多文件操作时**：如果涉及创建新 SOP 文件到 `memory/`，需额外确认

## L1 约束速查（来自 memory_management_sop）

- L1 ≤30 行，<1k tokens
- 只写关键词/名称，禁搬细节
- 括号内只写反直觉场景触发词（2-4字），禁写机制/方法/步骤
- 名字已自解释时禁加描述
- 只能使用 file_patch 修改，不允许 overwrite 或 code_run

## 版本记录

| 版本 | 日期 | 变更 |
|------|------|------|
| v1 | 2026-06-08 | 基于 Memory Index Hygiene 任务第一/二遍经验结晶 |
