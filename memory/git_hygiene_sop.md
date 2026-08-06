# Git Workspace Hygiene SOP

## 触发词

- "整理 Git 工作区"
- "Git 卫生检查"
- "git hygiene"
- "检查 GenericAgent Git 状态"

## 目的

对 GenericAgent 仓库的 Git 工作区进行全面卫生检查：确认工作区是否干净、分支是否健康、stash 是否有遗留、.gitignore 覆盖是否完善、是否存在未跟踪的大文件风险，输出结构化的卫生报告和改进建议。

## 前置条件

1. 已读取 `memory/sandbox_policy.md` — 确认写入边界
2. 已读取 `memory/personal_bootstrap_profile.md` — 确认用户偏好
3. Linux bash 环境可用（Ubuntu 24.04，root），`git` 命令可用，工作区为 Git 仓库

## 输入

- `repo_path` (可选): 要检查的 Git 仓库路径，默认 `/opt/GenericAgent`

## 步骤

### Step 1: 一次性数据采集（批量 bash）

将所有检查子命令合并为单个脚本执行，减少 round-trip：

```bash
repo="/opt/GenericAgent"
cd "$repo"

echo "=== WORKSPACE STATUS ==="
git status --short 2>&1
echo "--- DIFF STAT ---"
git diff --stat 2>&1
echo "--- UNTRACKED SIZE ---"
git ls-files --others --exclude-standard 2>/dev/null | while read -r f; do
    stat -c '%s %n' "$f" 2>/dev/null
done

echo ""
echo "=== BRANCHES ==="
git branch -a 2>&1
echo "--- BRANCH TIMESTAMPS ---"
git for-each-ref --sort=-committerdate --format='%(refname:short)  %(committerdate:relative)' refs/heads/ 2>&1
echo "--- RECENT COMMITS ---"
git log --oneline -10 2>&1

echo ""
echo "=== STASH ==="
git stash list 2>&1

echo ""
echo "=== REMOTE ==="
git remote -v 2>&1

echo ""
echo "=== GITIGNORE ==="
cat .gitignore 2>/dev/null || echo "NO .gitignore FOUND"

echo ""
echo "=== LARGE UNTRACKED FILES (>1MB) ==="
git ls-files --others --exclude-standard 2>/dev/null | while read -r f; do
    size=$(stat -c %s "$f" 2>/dev/null)
    if [ -n "$size" ] && [ "$size" -gt 1048576 ]; then
        sizeMB=$(awk "BEGIN {printf \"%.2f\", $size/1048576}")
        echo "$f : ${sizeMB}MB"
    fi
done

echo ""
echo "=== UNTRACKED DIRECTORIES TOTAL SIZE ==="
git ls-files --others --exclude-standard --directory 2>/dev/null |
    sed 's|/[^/]*$||' | sort -u | while read -r dir; do
    if [ -d "$dir" ]; then
        sizeKB=$(du -sk "$dir" 2>/dev/null | cut -f1)
        sizeMB=$(awk "BEGIN {printf \"%.2f\", $sizeKB/1024}")
        echo "$dir : ${sizeMB}MB"
    fi
done
```

### Step 2: 分析并生成报告

对 Step 1 的原始输出进行分析，按以下分区组织报告：

| 分区 | 内容 |
|------|------|
| 1. 工作区状态 | `git status --short` 输出，标注 CLEAN / DIRTY；若有修改，列出 diff 量级 |
| 2. 分支卫生 | 本地/远程分支列表、最后活动时间、是否有过期分支 (>30天) 或 stray 分支 |
| 3. 提交历史 | 最近 10 条提交，检查是否 shallow clone（历史过浅） |
| 4. Stash 状态 | stash list 输出，标注是否有未恢复的暂存 |
| 5. .gitignore 覆盖 | 逐项检查已覆盖模式，列出缺失项（结合当前未跟踪目录判断） |
| 6. 大文件扫描 | 列出 >1MB 的未跟踪文件，标注风险等级 |
| 7. 远程配置 | remote URL、fetch/push 一致性 |
| 8. 总结与建议 | 整体卫生评级（🟢/🟡/🔴），按优先级列出改进建议 |

每条检查项包含：原始命令输出 + 简短健康评估（✅/⚠️/❌）。

### Step 3: 写入沙箱报告

将完整报告写入沙箱输出路径：

```
/opt/GenericAgent/sandbox/workspace/git_hygiene_task/output.txt
```

## 成功标准

- 报告覆盖全部 8 个分区：工作区状态、分支卫生、提交历史、Stash、.gitignore、大文件、远程、总结
- 工作区状态明确标注 CLEAN 或 DIRTY
- .gitignore 审计部分列出至少 3 个已覆盖模式 + 所有缺失项
- 大文件扫描结果包含文件路径和大小
- 总结部分给出按优先级排列的具体改进建议（禁止自动执行，仅建议）
- 输出文件非空且超过 80 行

## 验证命令

```bash
REPORT=/opt/GenericAgent/sandbox/workspace/git_hygiene_task/output.txt

# 检查输出文件存在且非空
wc -l < "$REPORT"

# 检查工作区状态标记
grep -E 'CLEAN|DIRTY' "$REPORT"
# 期望: >= 1 个匹配

# 检查 .gitignore 审计章节存在
grep -E 'gitignore|\.gitignore' "$REPORT"
# 期望: >= 3 个匹配

# 检查关键分区标题存在
grep -E '工作区|分支|Stash|gitignore|大文件|远程|总结|建议' "$REPORT"
# 期望: >= 5 个匹配
```

## 失败恢复

| 尝试次数 | 操作 |
|---------|------|
| 第 1 次失败 | 检查 `git` 命令是否可用；检查当前目录是否为 Git 仓库；若不在仓库内，提示用户指定正确路径 |
| 第 2 次失败 | 简化检查范围：跳过 `git log` 和 `git diff --stat`（可能因仓库损坏而失败）；继续用 `git status`, `git branch`, `.gitignore` 生成不完整报告；缺失项标记为 ⚠️ UNKNOWN |
| 第 3 次失败 | 请求用户介入：展示失败的具体命令和错误输出 |

## 人为确认点

- **`.gitignore` 修改**：本 SOP 仅建议添加 `.gitignore` 规则，**不自动修改文件**。
- **大文件处理**：发现超大未跟踪文件时，报告风险和建议，**不自动删除或移动文件**。
- 所有检查均为只读操作（`git status`, `cat`, `ls -la`, `git ls-files --others`），不执行 `git add`, `git commit`, `git stash`, `git rm` 等写操作。

## 预期输出

报告写入：`/opt/GenericAgent/sandbox/workspace/git_hygiene_task/output.txt`

## 版本记录

| 版本 | 日期 | 变更 |
|------|------|------|
| v1 | 2026-06-09 | 基于 git hygiene 任务 Pass 1 成功运行结晶（3 rounds） |
| v2 | 2026-06-10 | 迁移至 Windows 原生路径和 PowerShell 命令 |
| v3 | 2026-08-06 | 迁移至 Linux bash（Ubuntu 24.04，root）：采集脚本与验证命令 Linux 化，git 命令不变 |

## Provenance

- First successful run: 2026-06-09
- Pass 1 friction: 3 rounds
- Pass 2 optimized: [date TBD]
- Pass 3 crystallized: [date TBD]
