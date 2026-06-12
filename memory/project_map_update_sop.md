# Project Map Update SOP

## 触发条件
- 新模块/目录加入项目时
- 发现现有 project_map.md 信息不准确时
- 冷启动/重大重构后

## 更新流程

### 1. 数据采集（1 步替代多步）
```powershell
# 一次性采集：顶层文件 + 子目录 + 行数
Set-Location D:\GenericAgent
Write-Output "=== 顶层 .py/.pyw ==="
Get-ChildItem *.py, *.pyw | Select-Object Name
Write-Output "=== 子目录 ==="
foreach ($d in @('memory', 'reflect', 'plugins', 'frontends', 'docs', 'assets')) {
    Write-Output "--- $d/ ---"
    Get-ChildItem "$d/*.py", "$d/*.md" -ErrorAction SilentlyContinue | Select-Object -First 20 Name
}
Write-Output "=== 行数 ==="
Get-Content agentmain.py, agent_loop.py, ga.py, llmcore.py, TMWebDriver.py | Measure-Object -Line
# 检查遗漏
Get-ChildItem *.py | ForEach-Object {
    $name = $_.Name
    $lines = (Get-Content $name | Measure-Object -Line).Lines
    if (-not (Select-String -Path memory/project_map.md -Pattern [regex]::Escape($name) -Quiet)) {
        Write-Warning "未记录: $name ($lines 行)"
    }
}
```
### 2. 对比现有地图
- 读取 `memory/project_map.md`
- 标记缺失模块、错误行数、过时路径

### 3. 更新
- 优先 patch，避免 overwrite
- 验证表格格式：`|| **名称** | \`路径\` | 说明 |`
- 确认所有路径可访问

### 4. 验证
```powershell
# 路径可用性检查
Select-String -Path memory/project_map.md -Pattern '`[^`]+\.(py|md|pyw)`' -AllMatches | 
    ForEach-Object { $_.Matches } | ForEach-Object { $_.Value.Trim('`') } | ForEach-Object {
    if (Test-Path "D:\GenericAgent\$_") { Write-Output "✅ $_" }
    else { Write-Warning "⚠️ $_ not found" }
}
```
## 注意事项
- `mykey.py` 只引用不读取内容
- 行数标注在 `| 说明 |` 列，如 `(1063行)`
- 内存文件 `memory/*.py` 记录在 Specialized Utilities 章节

## 版本记录

| 版本 | 日期 | 变更 |
|------|------|------|
| v1 | 2026-06-11 | 自动生成版本记录 |

