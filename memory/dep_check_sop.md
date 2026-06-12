# Project Dependency Check SOP

## 触发词

- "检查依赖"
- "依赖审计"
- "检查 GenericAgent 依赖状态"
- "dependency check"

## 目的

对 GenericAgent 项目的 Python 依赖进行全面审计：读取 `pyproject.toml` 声明的依赖，与当前环境中已安装的包进行交叉比对，识别缺失、版本不满足、以及未声明的包，输出结构化的依赖状态报告。

## 前置条件

1. 已读取 `memory/sandbox_policy.md` — 确认写入边界
2. 已读取 `memory/personal_bootstrap_profile.md` — 确认用户偏好
3. Windows Python 可用，`packaging` 库已安装

## 输入

无 — 默认使用 GenericAgent 工作区 (`D:\GenericAgent`)。

## 步骤

### Step 1: 读取声明文件并采集已安装包（单次并行调用）

同时读取 `pyproject.toml` 并列出当前环境中所有已安装的包：

```powershell
# 并行执行两个操作：
# 1. 读取 pyproject.toml
Get-Content D:\GenericAgent\pyproject.toml

# 2. 列出已安装的包（JSON 格式）
pip list --format=json 2>$null
```

### Step 2: 交叉比对并生成报告

使用 Python 脚本进行自动化交叉比对（依赖 `packaging.specifiers` 和 `packaging.version`）：

1. **Python 版本检查** — 验证当前 Python 版本是否满足 `>=3.10,<3.14`
2. **Core 依赖检查** — 遍历 `[project] dependencies`，逐项比对已安装版本是否满足约束
3. **UI 可选依赖检查** — 遍历 `[project.optional-dependencies] ui`，逐项比对
4. **All-frontends 可选依赖检查** — 遍历 `[project.optional-dependencies] all-frontends`，逐项比对
5. **未声明包扫描** — 找出所有已安装但未在 `pyproject.toml` 中声明的包（排除 `pip`, `setuptools`, `wheel`, `packaging`, `genericagent`），区分传递依赖与可疑遗留包

每条检查项输出状态标记：
- `SATISFIED` — 已安装且版本满足约束
- `MISSING` — 未安装
- `BELOW_MINIMUM` — 已安装但版本低于要求
- `UNDECLARED` — 已安装但未在 `pyproject.toml` 中声明

### Step 3: 写入沙箱报告

将完整报告写入沙箱输出路径：

```
D:\GenericAgent\sandbox\workspace\dep_check_task\output.txt
```

报告分区：
| 分区 | 内容 |
|------|------|
| 1. Python 版本 | 当前版本 vs 要求版本，SATISFIED/NOT SATISFIED |
| 2. Core 依赖 | 逐项比对表：包名、约束、状态、已安装版本 |
| 3. UI 可选依赖 | 逐项比对表（同上） |
| 4. All-frontends 可选依赖 | 逐项比对表（同上） |
| 5. 未声明包列表 | 完整列表 + 类别标注（传递依赖 / 可疑遗留） |
| 6. 汇总表 | core/ui/frontends 满足率、未声明包数量、整体结论 |

## 成功标准

- 报告覆盖全部 6 个分区：Python 版本、Core、UI、All-frontends、未声明包、汇总
- 每个声明的依赖都有状态标记（SATISFIED/MISSING/BELOW_MINIMUM）
- 汇总表给出各分组的满足率百分比
- Core 核心依赖满足率必须达到 100%，否则项目无法正常运行
- 输出文件非空且超过 100 行

## 验证命令

```powershell
# 检查输出文件存在且非空
(Get-Content D:\GenericAgent\sandbox\workspace\dep_check_task\output.txt | Measure-Object -Line).Lines

# 检查关键状态标记是否存在
Select-String -Path D:\GenericAgent\sandbox\workspace\dep_check_task\output.txt -Pattern 'SATISFIED|MISSING|UNDECLARED'
# 期望: >= 10 匹配

# 检查汇总行存在
Select-String -Path D:\GenericAgent\sandbox\workspace\dep_check_task\output.txt -Pattern '汇总'
# 期望: >= 1

# 检查分区标题
Select-String -Path D:\GenericAgent\sandbox\workspace\dep_check_task\output.txt -Pattern '\[project\]|core|ui|all-frontends|未声明'
# 期望: >= 4
```

## 失败恢复

| 尝试次数 | 操作 |
|---------|------|
| 第 1 次失败 | 检查 `pip list` 是否可用；若 `packaging` 库缺失，先 `pip install packaging` |
| 第 2 次失败 | 放弃 `packaging` 库的 `SpecifierSet` 自动比对；改为手动逐项比对版本号字符串，用 `import pkg_resources` 获取已安装版本；标注手动比对可能不精确 |
| 第 3 次失败 | 请求用户介入：展示 `pyproject.toml` 内容 + `pip list` 原始输出，让用户手动判定 |

## 人为确认点

无高风险操作。所有检查均为只读（读取 `pyproject.toml`、`pip list`、Python 版本检查），不安装或卸载任何包，不修改配置文件。

## 预期输出

报告写入：`D:\GenericAgent\sandbox\workspace\dep_check_task\output.txt`

## 版本记录

| 版本 | 日期 | 变更 |
|------|------|------|
| v1 | 2026-06-09 | 基于 dep check 任务 Pass 1 成功运行结晶（3 rounds） |
| v2 | 2026-06-10 | 迁移至 Windows 原生路径和 PowerShell 命令 |

## Provenance

- First successful run: 2026-06-09
- Pass 1 friction: 3 rounds — Round 1 并行读取 pyproject.toml + pip list，Round 2 Python 脚本交叉比对（含 packaging 库版本约束检查），Round 3 格式化报告 + 汇总。
- Pass 2 optimized: [date TBD]
- Pass 3 crystallized: [date TBD]
