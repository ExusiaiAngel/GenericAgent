# System Monitor SOP

## 触发词

- "系统监控"
- "检查系统状态"
- "system monitor"
- "进程监控"
- "检查运行情况"
- "health check"

## 目的

对当前 Linux 系统进行全面监控检查，采集 uptime、内存、CPU、进程、磁盘、日志等关键指标，输出结构化的系统健康报告并给出健康评分。

## 前置条件

1. 已读取 `memory/sandbox_policy.md` — 确认写入边界
2. 已读取 `memory/env_audit_sop.md` — 了解环境审计基线
3. Linux bash 环境可用（Ubuntu 24.04，root 用户）

## 输入

无 — 使用默认 GenericAgent 工作区，自动检测系统状态。

## 步骤

### Step 1: 一次性数据采集（批量 bash）

将所有监控子检查合并为一个脚本执行，减少 round-trip：

```bash
echo "=== UPTIME ==="
uptime -p
echo "Boot time: $(who -b | awk '{print $3, $4}')"
echo "Logged in user: $(who | awk '{print $1}' | sort -u | tr '\n' ' ')"

echo ""
echo "=== MEMORY ==="
free -h

echo ""
echo "=== CPU ==="
lscpu | grep -E 'Model name|^CPU\(s\)|^Core|^Thread' || true
echo "Load avg: $(cat /proc/loadavg)"
echo "Processes: $(ps aux | wc -l)"

echo ""
echo "=== TOP5 MEMORY PROCESSES ==="
ps aux --sort=-%mem | awk 'NR<=6 {printf "%-12s %-8s %8.1f MB %s\n", $1, $2, $6/1024, $11}'

echo ""
echo "=== GENERICAGENT PROCESSES ==="
ps aux | grep -E 'agentmain|GenericAgent|task_runner' | grep -v grep ||
    echo "(no GenericAgent processes running)"
echo "--- all python processes ---"
ps aux | grep -E 'python' | grep -v grep ||
    echo "(no python processes)"

echo ""
echo "=== DISK ==="
df -h -x tmpfs -x devtmpfs -x overlay -x squashfs

echo ""
echo "=== RECENT SYSTEM EVENTS ==="
journalctl -p warning -n 10 --no-pager 2>/dev/null ||
    echo "(journald 不可用或当前用户无权限读取系统日志)"
```

### Step 2: 分析并生成报告

对 Step 1 的原始输出进行分析，按以下 7 个分区组织报告：

| 分区 | 内容 |
|------|------|
| 1. 系统概览 | uptime、用户、CPU 型号和核心数 |
| 2. 内存状态 | 总量、已用、可用、使用率 |
| 3. 内存占用 TOP5 进程 | 排序前 5（进程名、PID、内存使用量） |
| 4. GenericAgent 进程状态 | agentmain / python 进程树（如有），含 PID、内存、启动时间 |
| 5. 磁盘状态 | 各驱动器总量、已用、可用、使用率 |
| 6. 最近系统事件 | 错误/警告级别事件日志摘要 |
| 7. 健康评分与建议 | 综合评分（0-100），分维度评分（CPU/内存/磁盘/进程/日志），给出具体建议 |

#### 健康评分计算规则

| 维度 | 满分 | 扣分条件 |
|:----:|:----:|----------|
| CPU 负载 | 20 | CPU 使用率 > 70% 扣 20 分；> 40% 扣 10 分 |
| 内存压力 | 25 | 使用率 > 90% 扣 25 分；> 70% 扣 15 分 |
| 磁盘空间 | 25 | 任一驱动器使用率 > 90% 扣 25 分；> 80% 扣 15 分 |
| 进程健康 | 15 | 存在单个进程占用 > 50% 内存扣 10 分 |
| 系统日志 | 15 | 严重错误出现扣 15 分 |

每个检查项包含：原始命令输出摘要 + 简短健康评估。

### Step 3: 写入报告

将完整报告写入沙箱输出路径：

```
/opt/GenericAgent/sandbox/reports/system_monitor_YYYY-MM-DD.md
```

## 成功标准

- 报告覆盖全部 7 个分区：系统概览、内存状态、TOP5 进程、Agent 进程、磁盘状态、系统日志、健康评分
- 健康评分基于 5 个计算维度给出，分数范围 0-100
- 每个分区都有数据解读或评估标注
- 输出文件非空且超过 40 行

## 验证命令

```bash
# 检查输出文件存在且非空
REPORT=/opt/GenericAgent/sandbox/reports/system_monitor_$(date +%Y-%m-%d).md
wc -l < "$REPORT"

# 检查所有 7 个分区标题是否存在
grep -E '系统概览|内存状态|TOP5|Agent 进程|磁盘状态|系统日志|健康评分' "$REPORT"
# 期望: >= 7 个匹配

# 检查健康评分数值
grep -E '[0-9]+/100' "$REPORT"
# 期望: 输出格式如 "99/100"
```

## 失败恢复

| 尝试次数 | 操作 |
|---------|------|
| 第 1 次失败 | 检查单个命令是否因权限或工具缺失报错；对 `journalctl` 失败（无 journald）跳过该命令 |
| 第 2 次失败 | 跳过失败的命令，用 `|| echo` 兜底并继续；生成不完整报告 |
| 第 3 次失败 | 请求用户介入 |

## 人为确认点

无高风险操作。所有检查均为只读，不修改系统配置。

## 预期输出

报告写入：`/opt/GenericAgent/sandbox/reports/system_monitor_YYYY-MM-DD.md`

## 版本记录

| 版本 | 日期 | 变更 |
|------|------|------|
| v1 | 2026-06-09 | 基于 system monitor 任务第一遍成功运行结晶 |
| v2 | 2026-06-10 | 迁移至 Windows 原生 PowerShell 命令和 WMI/CIM 接口 |
| v3 | 2026-08-06 | 迁移至 Linux bash（Ubuntu 24.04，root）：ps/free/df/lscpu/journalctl 替代 PowerShell/WMI |

## Provenance

- First successful run: 2026-06-09
- Pass 1 friction: 5 rounds
- Pass 2 optimized: [date TBD]
- Pass 3 crystallized: [date TBD]
