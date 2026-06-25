# System Monitor SOP

## 触发词

- "系统监控"
- "检查系统状态"
- "system monitor"
- "进程监控"
- "检查运行情况"
- "health check"

## 目的

对当前 Windows 系统进行全面监控检查，采集 uptime、内存、CPU、进程、磁盘、日志等关键指标，输出结构化的系统健康报告并给出健康评分。

## 前置条件

1. 已读取 `memory/sandbox_policy.md` — 确认写入边界
2. 已读取 `memory/env_audit_sop.md` — 了解环境审计基线
3. Windows PowerShell 环境可用

## 输入

无 — 使用默认 GenericAgent 工作区，自动检测系统状态。

## 步骤

### Step 1: 一次性数据采集（批量 PowerShell）

将所有监控子检查合并为一个脚本执行，减少 round-trip：

```powershell
echo "=== UPTIME ==="
$os = Get-CimInstance Win32_OperatingSystem
$bootTime = $os.LastBootUpTime
$uptime = (Get-Date) - $bootTime
echo "Uptime: $($uptime.Days)d $($uptime.Hours)h $($uptime.Minutes)m $($uptime.Seconds)s"
echo "Boot time: $($bootTime.ToString('yyyy-MM-dd HH:mm:ss'))"
$sessions = (Get-CimInstance Win32_ComputerSystem).UserName
echo "Logged in user: $sessions"

echo ""
echo "=== MEMORY ==="
$memTotal = [math]::Round($os.TotalVisibleMemorySize / 1MB, 1)
$memFree = [math]::Round($os.FreePhysicalMemory / 1MB, 1)
$memUsed = [math]::Round(($os.TotalVisibleMemorySize - $os.FreePhysicalMemory) / 1MB, 1)
$memPct = [math]::Round(($os.TotalVisibleMemorySize - $os.FreePhysicalMemory) / $os.TotalVisibleMemorySize * 100, 1)
echo "Total: ${memTotal}GB"
echo "Used: ${memUsed}GB"
echo "Free: ${memFree}GB"
echo "Usage: ${memPct}%"

echo ""
echo "=== CPU ==="
$cpu = ps auxor
echo "CPU: $($cpu.Name)"
echo "Cores: $($cpu.NumberOfCores) / Logical: $($cpu.NumberOfLogicalProcessors)"
$cpuLoad = $cpu.LoadPercentage
echo "Load: ${cpuLoad}%"
# Get current CPU usage via counter
$cpuSample = ps auxor | Measure-Object -Property LoadPercentage -Average
echo "Average Load: $([math]::Round($cpuSample.Average))%"
echo "Processes: $((ps aux).Count)"

echo ""
echo "=== TOP5 MEMORY PROCESSES ==="
ps aux | grep | Sort-Object WorkingSet64 -Descending | Select-Object -First 6 |
    Select-Object Name, Id, @{N='WorkingSet(MB)';E={[math]::Round($_.WorkingSet64/1MB,1)}},
    @{N='CPU(s)';E={[math]::Round($_.TotalProcessorTime.TotalSeconds,1)}}

echo ""
echo "=== GENERICAGENT PROCESSES ==="
$gaProcesses = ps aux | grep -Name python* 2>/dev/null | 
    Where-Object { $_.CommandLine -match 'agentmain|GenericAgent|task_runner' } 2>$null
if ($gaProcesses) {
    $gaProcesses | Select-Object Name, Id, @{N='WorkingSet(MB)';E={[math]::Round($_.WorkingSet64/1MB,1)}}, StartTime
} else {
    echo "(no GenericAgent processes running)"
}
echo "--- all python processes ---"
ps aux | grep -Name python* 2>/dev/null | Select-Object Name, Id, @{N='WS(MB)';E={[math]::Round($_.WorkingSet64/1MB,1)}} | Format-Table -AutoSize
if (-not $?) { echo "(no python processes)" }

echo ""
echo "=== DISK ==="
Get-PSDrive -PSProvider FileSystem | Where-Object Used -gt 0 |
    Select-Object Name, Root,
    @{N='Total(GB)';E={[math]::Round(($_.Used+$_.Free)/1GB,1)}},
    @{N='Used(GB)';E={[math]::Round($_.Used/1GB,1)}},
    @{N='Free(GB)';E={[math]::Round($_.Free/1GB,1)}},
    @{N='Used%';E={[math]::Round($_.Used/($_.Used+$_.Free)*100,1)}} | Format-Table -AutoSize

echo ""
echo "=== RECENT SYSTEM EVENTS ==="
try {
    Get-WinEvent -LogName System -MaxEvents 10 2>/dev/null | 
        Where-Object { $_.LevelDisplayName -match 'Error|Warning|Critical' } |
        Select-Object TimeCreated, Id, LevelDisplayName, @{N='Message(truncated)';E={$_.Message.Substring(0, [Math]::Min(100, $_.Message.Length))}} |
        Format-Table -AutoSize
} catch {
    echo "(WinEvent access requires admin or limited logs available)"
    echo "--- Application errors ---"
    try {
        Get-WinEvent -LogName Application -MaxEvents 5 2>/dev/null |
            Where-Object { $_.LevelDisplayName -match 'Error|Critical' } |
            Select-Object TimeCreated, Id, LevelDisplayName | Format-Table -AutoSize
    } catch { echo "(application log not available)" }
}
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
/opt/GenericAgent/sandbox/reports\system_monitor_YYYY-MM-DD.md
```

## 成功标准

- 报告覆盖全部 7 个分区：系统概览、内存状态、TOP5 进程、Agent 进程、磁盘状态、系统日志、健康评分
- 健康评分基于 5 个计算维度给出，分数范围 0-100
- 每个分区都有数据解读或评估标注
- 输出文件非空且超过 40 行

## 验证命令

```powershell
# 检查输出文件存在且非空
$reportPath = "/opt/GenericAgent/sandbox/reports\system_monitor_$(Get-Date -Format 'yyyy-MM-dd').md"
(Get-Content $reportPath | Measure-Object -Line).Lines

# 检查所有 7 个分区标题是否存在
Select-String -Path $reportPath -Pattern '系统概览|内存状态|TOP5|Agent 进程|磁盘状态|系统日志|健康评分'
# 期望: >= 7

# 检查健康评分数值
Select-String -Path $reportPath -Pattern '\d+/100'
# 期望: 输出格式如 "99/100"
```

## 失败恢复

| 尝试次数 | 操作 |
|---------|------|
| 第 1 次失败 | 检查单个命令是否因权限或工具缺失报错；对 `Get-WinEvent` 失败跳过该命令 |
| 第 2 次失败 | 跳过失败的命令，用 `try/catch` 兜底；继续生成不完整报告 |
| 第 3 次失败 | 请求用户介入 |

## 人为确认点

无高风险操作。所有检查均为只读，不修改系统配置。

## 预期输出

报告写入：`/opt/GenericAgent/sandbox/reports\system_monitor_YYYY-MM-DD.md`

## 版本记录

| 版本 | 日期 | 变更 |
|------|------|------|
| v1 | 2026-06-09 | 基于 system monitor 任务第一遍成功运行结晶 |
| v2 | 2026-06-10 | 迁移至 Windows 原生 PowerShell 命令和 WMI/CIM 接口 |

## Provenance

- First successful run: 2026-06-09
- Pass 1 friction: 5 rounds
- Pass 2 optimized: [date TBD]
- Pass 3 crystallized: [date TBD]
