# Backup Verify SOP

## 触发词

- "验证备份"
- "检查备份状态"
- "backup check"
- "备份审计"
- "检查备份完整性"
- "backup audit"

## 目的

对 GenericAgent 项目的备份状态进行全面审计检查，包括关键配置文件元数据、Git 远程仓库状态、SOP 文档体系、本地备份文件扫描，以及综合风险等级评估，输出结构化的备份审计报告。

## 前置条件

1. 已读取 `memory/sandbox_policy.md` — 确认写入边界，确认 `mykey.py` 属敏感文件
2. 已读取 `memory/env_audit_sop.md` — 了解环境审计基线
3. Windows PowerShell 环境可用
4. **关键安全约束：`mykey.py` 只能 Get-Item（获取元数据），禁止读其内容**

## 输入

无 — 使用默认 GenericAgent 工作区 `/opt/GenericAgent`。

## 步骤

### Step 1: 一次性数据采集（批量 PowerShell）

将所有备份检查合并为一个脚本执行，减少 round-trip：

```powershell
echo "=== CONFIG FILES STAT ==="
echo "--- mykey.py (stat only, NO cat) ---"
ls -la /opt/GenericAgent/mykey.py, LastWriteTime
echo "--- env.sh ---"
ls -la /opt/GenericAgent/env.sh, LastWriteTime
echo "--- pyproject.toml ---"
ls -la /opt/GenericAgent/pyproject.toml, LastWriteTime
echo "--- .gitignore ---"
ls -la /opt/GenericAgent/.gitignore, LastWriteTime

echo ""
echo "=== GIT REMOTE ==="
cd /opt/GenericAgent && git remote -v 2>&1

echo ""
echo "=== GIT RECENT LOG ==="
cd /opt/GenericAgent && git log --oneline -5 2>&1

echo ""
echo "=== MEMORY DIRECTORY ==="
$memFiles = ls /opt/GenericAgent/memory\*.md 2>/dev/null
echo "--- file count: $($memFiles.Count) ---"
$memSize = ($memFiles | Measure-Object Length -Sum).Sum
$memSizeKB = [math]::Round($memSize / 1KB, 1)
echo "--- total size: ${memSizeKB}KB ---"

echo ""
echo "=== BACKUP FILES SCAN ==="
echo "--- bundles ---"
ls /opt/GenericAgent/sandbox\*.bundle 2>/dev/null | Select-Object Name, Length
echo "--- backup files (excluding cache/workspace) ---"
ls /opt/GenericAgent/sandbox\*backup* 2>/dev/null | 
    Where-Object { $_.DirectoryName -notmatch 'cache|workspace' } | Select-Object Name, Length, LastWriteTime
echo "--- zip archives ---"
ls /opt/GenericAgent/sandbox\*.zip 2>/dev/null | Select-Object Name, Length
```

### Step 2: 分析并生成报告

对 Step 1 的原始输出进行分析，按以下 5 个分区组织报告：

| 分区 | 内容 |
|------|------|
| 1. 关键配置文件元数据 | 4 个文件（mykey.py, env.sh, pyproject.toml, .gitignore）的元数据：大小、最后修改时间、存在状态。**mykey.py 行必须明确标注 "内容未读取"** |
| 2. Git 远程仓库状态 | remote -v 列表、最近 5 条 commit hash + message + 日期 |
| 3. memory/ 目录 SOP 文件 | `.md` 文件数量、总大小（KB） |
| 4. 本地备份文件扫描 | 扫描结果：bundles、backup 文件、zip 归档（排除 cache 和 workspace） |
| 5. 备份风险等级评估 | 综合判定风险等级（低/中/高） |

#### 风险等级判定规则

| 等级 | 条件 | 颜色 |
|:----:|------|:----:|
| **低 (LOW)** | 代码已推送到远程 **且** 存在本地备份文件（bundle/zip） | 🟢 |
| **中 (MEDIUM)** | 代码已推送 **但** 无本地备份，或本地有备份但未推送 | 🟡 |
| **高 (HIGH)** | 代码未推送到远程 **且** 无本地备份 | 🔴 |

每个检查项包含：原始命令输出摘要 + 简短健康评估。

### Step 3: 写入报告

将完整报告写入沙箱输出路径：

```
/opt/GenericAgent/sandbox/reports/backup_verify_YYYY-MM-DD.md
```

## 成功标准

- 报告覆盖全部 5 个分区：配置文件元数据、Git 远程、memory/ 统计、备份文件扫描、风险等级评估
- `mykey.py` 行明确标注 "内容未读取" 或等效表述，无文件内容暴露
- 所有文件路径为绝对路径
- 风险等级已评估并给出具体判定依据
- 输出文件非空且超过 30 行

## 验证命令

```powershell
# 检查输出文件存在且非空
(Get-Content /opt/GenericAgent/sandbox/reports/backup_verify_$(Get-Date -Format 'yyyy-MM-dd').md | Measure-Object -Line).Lines

# 确认 mykey.py 未读内容
Select-String -Path /opt/GenericAgent/sandbox/reports/backup_verify_$(Get-Date -Format 'yyyy-MM-dd').md -Pattern '内容未读取'
# 期望: >= 1

# 确认风险等级已评估
Select-String -Path /opt/GenericAgent/sandbox/reports/backup_verify_$(Get-Date -Format 'yyyy-MM-dd').md -Pattern '低|中|高|风险等级'
# 期望: 至少一个匹配

# 确认所有路径为绝对路径（以 D: 开头）
Select-String -Path /opt/GenericAgent/sandbox/reports/backup_verify_$(Get-Date -Format 'yyyy-MM-dd').md -Pattern 'disk\'
# 期望: >= 5
```

## 失败恢复

| 尝试次数 | 操作 |
|---------|------|
| 第 1 次失败 | 检查单个命令是否因路径错误或权限问题报错；确认 `/opt/GenericAgent/` 存在且可读 |
| 第 2 次失败 | 跳过失败的命令，用 `try/catch` 兜底；继续生成不完整报告，缺失项标记为 ⚠️ UNKNOWN |
| 第 3 次失败 | 请求用户介入，展示失败的具体命令和错误输出 |

## 人为确认点

| 操作 | 确认要求 |
|------|----------|
| `ls -la mykey.py` | 只读元数据（大小、时间戳），不读内容 |
| `git log` | 只读最近 commit 信息，不修改 Git 状态 |
| `Get-ChildItem` 扫描 | 排除 `cache/` 和 `workspace/` 避免大量无关输出 |
| 报告包含路径 | 所有路径必须为绝对路径，避免歧义 |

**安全红线**：任何步骤中 `mykey.py` 都只能使用 `Get-Item` 命令，**绝对禁止** `Get-Content`、`cat` 或任何其他读取该文件内容的操作。

## 预期输出

报告写入：`/opt/GenericAgent/sandbox/reports/backup_verify_YYYY-MM-DD.md`

## 版本记录

| 版本 | 日期 | 变更 |
|------|------|------|
| v1 | 2026-06-09 | 基于 backup verify 任务第一遍成功运行结晶 |
| v2 | 2026-06-10 | 迁移至 Windows 原生路径和 PowerShell 命令 |

## Provenance

- First successful run: 2026-06-09
- Pass 1 friction: 4 rounds
- Risk assessment: MEDIUM
- Security: mykey.py protected (stat only, no content read)
- Pass 2 optimized: 2026-06-11 (单次PS批量采集 + .git大小检查 + bundle深度扫描 + 3轮完成)
- Pass 3 crystallized: [date TBD]
