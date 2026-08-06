# Environment Audit SOP

## 触发词

- "检查我的开发环境"
- "环境审计"
- "env audit"

## 目的

对当前 Linux 开发环境进行全面审计，检查 Python、Git、磁盘、开发工具链等关键组件状态，输出结构化的审计报告。

## 前置条件

1. 已读取 `memory/sandbox_policy.md` — 确认写入边界
2. 已读取 `memory/personal_bootstrap_profile.md` — 确认用户偏好
3. Linux bash 环境可用（Ubuntu 24.04，root 用户）

## 输入

无 — 使用默认 GenericAgent 工作区，自动检测环境。

## 步骤

### Step 1: 一次性数据采集（批量 bash）

将所有审计子检查合并为一个脚本执行，减少 round-trip：

```bash
echo "=== PYTHON ==="
python3 --version 2>&1
which python3
echo "--- pip ---"
pip3 --version 2>&1
echo "--- conda ---"
conda --version 2>&1 || echo "(not installed)"

echo ""
echo "=== GIT ==="
git --version 2>&1
git config --list --global 2>&1
echo "--- aliases ---"
git config --global --get-regexp alias 2>&1

echo ""
echo "=== SYSTEM ==="
uname -a
lsb_release -a 2>/dev/null
lscpu | grep -E 'Model name|^CPU\(s\)|^Core|^Thread' || true

echo ""
echo "=== DISK ==="
df -h -x tmpfs -x devtmpfs -x overlay -x squashfs

echo ""
echo "=== TOOLS ==="
echo "--- node ---"
node --version 2>&1 || echo "(not installed)"
echo "--- npm ---"
npm --version 2>&1 || echo "(not installed)"
echo "--- docker ---"
docker --version 2>&1 || echo "(not installed)"
echo "--- docker compose ---"
docker compose version 2>&1 || echo "(not installed)"

echo ""
echo "=== PATH ==="
echo "$PATH" | tr ':' '\n' | head -30
echo "... ($(echo "$PATH" | tr ':' '\n' | wc -l) total entries)"
```

### Step 2: 分析并生成报告

对 Step 1 的原始输出进行分析，按以下分区组织报告：

| 分区 | 内容 |
|------|------|
| 1. Python 版本 | 版本、路径、pip 版本、conda 状态 |
| 2. Git 配置 | 版本、user.name、user.email、proxy、alias |
| 3. 系统信息 | Ubuntu/Linux 版本、CPU、内核 |
| 4. 磁盘空间 | 各驱动器使用率 |
| 5. 关键开发工具 | node, npm, docker, docker compose 版本表 |
| 6. PATH 检查 | 前 30 个 PATH 条目，检查 Python/Git 目录是否在 PATH 中 |
| 7. 总结 | 整体健康度评级（✅ HEALTHY / ⚠️ ATTENTION / ❌ BROKEN） |

每条检查项包含：原始命令输出 + 简短健康评估（✅/⚠️/❌）。

### Step 3: 写入报告

将完整报告写入沙箱输出路径：

```
/opt/GenericAgent/sandbox/workspace/env_audit_task/output.txt
```

## 成功标准

- 报告覆盖全部 7 个分区
- 每个检查项都有原始输出和健康评估标记（✅/⚠️/❌）
- 总结部分给出整体评级和具体建议
- 输出文件非空且超过 200 行

## 验证命令

```bash
REPORT=/opt/GenericAgent/sandbox/workspace/env_audit_task/output.txt

# 检查输出文件存在且非空
wc -l < "$REPORT"

# 检查关键分区标题是否存在
grep -E 'PYTHON|GIT|SYSTEM|DISK|TOOLS|PATH|SUMMARY' "$REPORT"
# 期望: >= 7 个匹配
```

## 失败恢复

| 尝试次数 | 操作 |
|---------|------|
| 第 1 次失败 | 检查单个命令是否因权限或工具缺失报错 |
| 第 2 次失败 | 跳过失败的命令，用 `|| echo` 兜底并继续；生成不完整报告 |
| 第 3 次失败 | 请求用户介入 |

## 人为确认点

无高风险操作。所有检查均为只读，不修改系统配置。

## 预期输出

报告写入：`/opt/GenericAgent/sandbox/workspace/env_audit_task/output.txt`

## 版本记录

| 版本 | 日期 | 变更 |
|------|------|------|
| v1 | 2026-06-09 | 基于 env audit 任务第一遍成功运行结晶 |
| v2 | 2026-06-10 | 迁移至 Windows 原生路径和 PowerShell 命令，移除 WSL 专用检查 |
| v3 | 2026-08-06 | 迁移至 Linux bash（Ubuntu 24.04，root）：python3/df/lscpu/uname 替代 PowerShell/WMI |

## Provenance

- First successful run: 2026-06-09
- Pass 1 friction: 6 rounds
- Pass 2 optimized: [date TBD]
- Pass 3 crystallized: [date TBD]
