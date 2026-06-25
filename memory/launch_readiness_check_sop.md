# Launch Readiness Check SOP

## 触发词

- "检查 GenericAgent 是否能启动"
- "Launch Readiness Check"
- "启动准备检查"
- "检查环境就绪"

## 目的

检查 GenericAgent 的运行环境是否就绪，识别必须修复的问题，输出明确的启动命令和修复步骤。

## 前置条件

1. 已读取 `memory/sandbox_policy.md` — 确认当前写入权限
2. 已读取 `memory/personal_bootstrap_profile.md` — 确认用户边界
3. 项目目录为 `/opt/GenericAgent`

## 输入

可选：如果系统存在多个 Python 版本，用户可指定使用的 Python 可执行路径。

默认：自动检测 Windows Python 3.13。

## 步骤

### Step 1: 检查 Python 环境

```powershell
# Windows Python
python --version
(Get-Command python).Source

# 检查是否可用
python -c "import sys; print(f'Python {sys.version}')"
```

### Step 2: 检查关键文件

| 文件 | 检查方法 |
|------|---------|
| `mykey.py` | `ls -la mykey.py` (存在即可，不打印 secret) |
| `agentmain.py` | `ls -la agentmain.py` |
| `llmcore.py` | `ls -la llmcore.py` |
| `TMWebDriver.py` | `ls -la TMWebDriver.py` |
| `ga.sh` (启动脚本) | `Get-Content ga.sh -TotalCount 5` |
| `env.sh` | `ls -la env.sh` |
| `pyproject.toml` | `ls -la pyproject.toml` |

### Step 3: 核心模块导入测试

```python
import sys
sys.path.insert(0, 'D:\\GenericAgent')
modules = ['mykey', 'llmcore', 'agent_loop', 'agentmain', 'ga', 'TMWebDriver']
for mod in modules:
    try:
        __import__(mod)
        print(f'✅ {mod}')
    except Exception as e:
        print(f'❌ {mod}: {e}')
```

### Step 4: 依赖审计

```powershell
pip list
```

检查关键依赖是否安装：`requests`, `beautifulsoup4`, `bottle`, `simple-websocket-server`, `aiohttp`

### Step 5: 生成报告

包含以下部分：
1. 环境概览 (OS, Python 版本)
2. 关键文件存在性表
3. 核心模块导入结果 (✅/❌)
4. 缺失依赖列表
5. 推荐启动命令
6. 必须修复项 (按优先级)

## 成功标准

- 报告明确指出哪条命令可以启动 GenericAgent
- 报告列出所有必须修复的问题及其修复命令
- 不打印任何密钥/secret 值

## 验证命令

```powershell
Get-Content memory/launch_readiness_report.md -TotalCount 20
Select-String -Path memory/launch_readiness_report.md -Pattern '启动命令|修复|✅|❌|🔴'
```

## 失败恢复

| 尝试次数 | 操作 |
|---------|------|
| 第 1 次失败 | 检查 Python 是否安装且在 PATH 中 |
| 第 2 次失败 | 检查 `pip list` 确认关键依赖 |
| 第 3 次失败 | 请求用户介入，展示 `python --version` 和 `$env:PATH` 输出 |

## 人为确认点

1. **读取 mykey.py**：只能检查存在性和大小，不能读取或打印 secret
2. **写入报告到 memory/**：需要用户批准
3. **安装依赖**：`pip install` 操作需要用户明确批准

## 预期输出

`memory/launch_readiness_report.md` — 完整的启动就绪报告。

## 版本记录

| 版本 | 日期 | 变更 |
|------|------|------|
| v1 | 2026-06-08 | 基于 Launch Readiness Check 任务第一遍经验结晶 |
| v2 | 2026-06-10 | 迁移至 Windows 原生路径和 PowerShell 命令 |
