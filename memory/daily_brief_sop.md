# Daily Brief SOP

## 触发词

- "每日简报"
- "今天什么情况"
- "daily brief"
- "morning report"

## 目的

对当前 Windows 开发环境执行轻量级快速扫描，采集系统状态（uptime/memory/disk）、项目状态（git log/status）、安全检查（backup/agents）、待办提醒（recent reports）四项核心指标，输出结构化的每日简报并给出当日健康评分与建议。

本 SOP 定位为 **紧凑型日报**：每个分区不超过 3 行，总行数不超过 100 行，以 🟢🟡🔴 表情符号标注状态等级。

## 前置条件

1. 已读取 `memory/sandbox_policy.md` — 确认写入边界
2. 已读取 `memory/backup_verify_sop.md` — 了解备份验证基线
3. Windows PowerShell 环境可用

## 输入

无 — 使用默认 GenericAgent 工作区，自动检测系统状态。可选 `{date}` 参数指定简报日期。

## 步骤

### Step 1: 一次性数据采集（批量 PowerShell）

将四个分区的所有检查合并为一个脚本执行，减少 round-trip：

```powershell
Write-Output "=== SYSTEM ==="
$os = Get-CimInstance Win32_OperatingSystem
$bootTime = $os.LastBootUpTime
$uptime = (Get-Date) - $bootTime
Write-Output "Uptime: $($uptime.Days)d $($uptime.Hours)h $($uptime.Minutes)m"
$memTotal = [math]::Round($os.TotalVisibleMemorySize / 1MB, 1)
$memFree = [math]::Round($os.FreePhysicalMemory / 1MB, 1)
$memUsedPct = [math]::Round(($os.TotalVisibleMemorySize - $os.FreePhysicalMemory) / $os.TotalVisibleMemorySize * 100, 1)
Write-Output "Memory: ${memUsedPct}% used (${memFree}GB free / ${memTotal}GB total)"
Get-PSDrive C | Select-Object @{N='Used%';E={[math]::Round(($_.Used/$_.Used + $_.Free) * 100, 1)}}, Used, Free

Write-Output ""
Write-Output "=== PROJECTS ==="
Set-Location D:\GenericAgent
Write-Output "--- git log ---"
git log --oneline -5 2>&1
Write-Output "--- git status ---"
git status --short 2>&1
Write-Output "--- commit count today ---"
$today = Get-Date -Format 'yyyy-MM-dd'
git log --oneline --after="$today 00:00" 2>&1 | Measure-Object -Line | Select-Object -ExpandProperty Lines

Write-Output ""
Write-Output "=== SECURITY ==="
Write-Output "--- backup verification ---"
Get-Item D:\GenericAgent\mykey.py | Select-Object Length, LastWriteTime
Get-Item D:\GenericAgent\env.sh | Select-Object Length, LastWriteTime
Get-Item D:\GenericAgent\pyproject.toml | Select-Object Length, LastWriteTime
Get-Item D:\GenericAgent\.gitignore | Select-Object Length, LastWriteTime
Set-Location D:\GenericAgent; git remote -v 2>&1
Write-Output "--- agent processes ---"
Get-Process python* -ErrorAction SilentlyContinue | Where-Object { $_.CommandLine -match 'agentmain|task_runner' } 2>$null
Write-Output "--- memory dir stats ---"
$memFiles = Get-ChildItem D:\GenericAgent\memory\*.md -ErrorAction SilentlyContinue
Write-Output "file count: $($memFiles.Count)"
$memSizeKB = [math]::Round(($memFiles | Measure-Object Length -Sum).Sum / 1KB, 1)
Write-Output "total size: ${memSizeKB}KB"

Write-Output ""
Write-Output "=== TODOS ==="
Write-Output "--- recent reports ---"
Get-ChildItem D:\GenericAgent\sandbox\reports\ -ErrorAction SilentlyContinue | Sort-Object LastWriteTime -Descending | Select-Object -First 20 Name, LastWriteTime
Write-Output "--- today reports ---"
$todayStr = Get-Date -Format 'yyyy-MM-dd'
Get-ChildItem D:\GenericAgent\sandbox\reports\*$todayStr* -ErrorAction SilentlyContinue | Select-Object Name
if (-not $?) { Write-Output "(no reports dated today)" }
```

### Step 2: 分析并生成简报

对 Step 1 的原始输出进行分析，按以下 6 个分区组织简报（每分区最多 3 行总结）：

| 分区 | 内容 | 状态标记 |
|------|------|----------|
| 1. 系统状态 | uptime、负载、内存使用率、磁盘 C 盘使用率 | 🟢/🟡/🔴 |
| 2. 项目看板 | 最近 3 条 commit、未提交文件数、今日活跃度 | 🟢/🟡/🔴 |
| 3. 安全检查 | 备份状态（4 个关键文件是否存在）、Agent 进程数、远程仓库状态 | 🟢/🟡/🔴 |
| 4. 待办提醒 | 今日报告列表、发现的风险项 | 🟢/🟡/🔴 |
| 5. 健康评分 | 100 分制，按 4 维度量化 | 🟢/🟡/🔴 |
| 6. 今日建议 | 2-3 条可执行建议 | — |

#### 健康评分计算规则

| 维度 | 满分 | 扣分条件 |
|:----:|:----:|----------|
| 系统资源 | 30 | 内存使用率 > 90% 扣 30；> 70% 扣 15；磁盘使用率 > 90% 扣 15；> 80% 扣 10 |
| 项目活跃 | 30 | 今日 0 提交扣 15；未提交文件 > 10 扣 10；git status 有异常扣 5 |
| 安全合规 | 30 | 备份文件缺失扣 15；remote 不可达扣 10；agent 进程异常扣 5 |
| 待办清理 | 10 | 有 HIGH 风险项扣 5；未读报告 > 10 扣 5 |

评分档位：
- 🟢 **90-100**: 优秀，资源充裕，项目活跃，安全合规
- 🟡 **70-89**: 良好，有可改进项但无紧急问题
- 🔴 **< 70**: 需要关注，存在需立即处理的问题

### Step 3: 写入简报

将完整简报写入沙箱输出路径：

```
D:\GenericAgent\sandbox\reports\daily_brief_YYYY-MM-DD.md
```

简报格式约束：
- 每个分区摘要不超过 3 行
- 总文件行数不超过 100 行
- 使用 🟢🟡🔴 表情符号标注各分区状态
- 基本信息以表格呈现，总结以 `>` 引用块标注
- 底部标注：`*报告由物理级全能执行器自动生成 | 只读采集，未修改任何文件*`

## 成功标准

- 简报覆盖全部 6 个分区：系统状态、项目看板、安全检查、待办提醒、健康评分、今日建议
- 每个分区有 🟢🟡🔴 状态标记
- 健康评分基于 4 个维度量化给出，分数范围 0-100
- 今日建议为 2-3 条具体可执行建议（非泛泛而谈）
- 输出文件非空，行数不超过 100 行
- 所有数据均为只读采集，无任何写操作

## 验证命令

```powershell
# 检查输出文件存在且行数合理
$reportPath = "D:\GenericAgent\sandbox\reports\daily_brief_$(Get-Date -Format 'yyyy-MM-dd').md"
(Get-Content $reportPath | Measure-Object -Line).Lines
# 期望: 30-100 行

# 检查所有 6 个分区标题是否存在
Select-String -Path $reportPath -Pattern '系统状态|项目看板|安全检查|待办提醒|健康评分|今日建议'
# 期望: >= 6

# 检查健康评分数值
Select-String -Path $reportPath -Pattern '\d+/100'
# 期望: 输出格式如 "92/100"

# 检查状态标记存在
Select-String -Path $reportPath -Pattern '🟢|🟡|🔴'
# 期望: >= 4

# 确认只读声明存在
Select-String -Path $reportPath -Pattern '只读采集'
# 期望: 1
```

## 失败恢复

| 尝试次数 | 操作 |
|---------|------|
| 第 1 次失败 | 检查单个命令是否因路径或权限问题报错；确认 `D:\GenericAgent\` 存在且可读；`mykey.py` 仅用 `Get-Item` 获取元数据 |
| 第 2 次失败 | 跳过失败的命令，用 `try/catch` 兜底；继续生成不完整简报，缺失分区标记为 ⚠️ UNAVAILABLE |
| 第 3 次失败 | 请求用户介入，展示失败的具体命令和错误输出 |

## 人为确认点

| 操作 | 确认要求 |
|------|----------|
| `Get-Item mykey.py` | 只读元数据（大小、时间戳），**禁止** `Get-Content` 读取内容 |
| `git log` / `git status` | 只读 Git 信息，不执行 commit/push/pull 等修改操作 |
| `Get-Process` | 只读进程信息，不 kill 任何进程 |

**安全红线**：`mykey.py` 只能使用 `Get-Item` 命令，**绝对禁止**读取其内容。`env.sh` 同样只读元数据。

## 预期输出

简报写入：`D:\GenericAgent\sandbox\reports\daily_brief_YYYY-MM-DD.md`

## 版本记录

| 版本 | 日期 | 变更 |
|------|------|------|
| v1 | 2026-06-09 | 基于 daily brief 任务 Pass 1 成功运行结晶 |
| v2 | 2026-06-10 | 迁移至 Windows 原生路径和 PowerShell 命令 |

## Provenance

- First successful run: 2026-06-09
- Pass 1 friction: 5 rounds
- Health score: 92/100 (优秀)
- Pass 2 optimized: [date TBD]
- Pass 3 crystallized: [date TBD]
