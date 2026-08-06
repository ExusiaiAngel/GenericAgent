# computer_use

L3 memory: ljqCtrl.py ljqCtrl_sop.md ui_detect.py

## 1. 基础规则
枚举窗口总是可用：GUI任务先用 `wmctrl -l` / `xdotool search --name` 枚举窗口标题/几何（WM_CLASS/名称），确定目标窗口、前台状态和客户区原点后再操作。
优先使用 Python UIA（Linux 下为 AT-SPI/a11y 桥）；若UIA可用，探测和操作均可使用UIA；游戏不使用UIA；
一旦 UIA 对该窗口无效，立刻改用 ui_detect 定位；
ui_detect 不足时才用 vision，vision 只用于语义理解、确认界面状态和辅助判断目标，不可信其坐标，不要直接按 vision 坐标点击。
Linux 下窗口截图和操作使用 ljqCtrl（X11 桥）：严禁 pyautogui；记得先 Activate 到前台；
坑1-DPI/缩放：一律屏幕物理坐标；X11 下 `xdotool` 坐标即物理像素，HiDPI 缩放（xrandr scale）时需乘缩放因子换算；
坑2-遮盖/失焦：混乱先枚举窗口确认前台；
ui_detect 的 bbox 是截图内坐标，点击前必须用 `客户区原点 + bbox中心` 转屏幕物理坐标（客户区原点 = `xdotool getwindowgeometry` 的 X/Y，去掉标题栏/边框）
坐标转换禁用 `xdotool getwindowgeometry` 窗口矩形直接加截图坐标（含标题栏/边框/阴影会错位）
ljqCtrl.Click 后会返回像素/前台变化，0% 或近 0% 变化立即停下诊断，禁止盲目重试。
ljqCtrl 失效或目标为网络游戏时，必须使用硬件键鼠 Xbananakb / Arduino Leonardo（如有）
网络游戏除非用户明确允许，严禁普通键鼠事件，必须硬件执行。

## 2. GUI操作节奏建议
进入新界面时，建议先只探测不操作：枚举窗口 + UIA + ljqCtrl截图 + ui_detect，读完实际输出再决定下一步
明确一个操作后，可以在同一轮执行该动作，短暂等待，再立刻枚举窗口 + 截图/ui_detect 验证新状态；不要在未知状态下把多步决策写进大脚本
尽量不要预测关键词筛候选，应看 detect 输出、坐标、层级和上下文判断
若确定UIA可用则少用ui_detect/ljqCtrl；若UIA不可用，则后续不用UIA

## 版本记录

| 版本 | 日期 | 变更 |
|------|------|------|
| v1 | 2026-06-11 | 自动生成版本记录 |
| v3 | 2026-08-06 | Linux 化：win32gui/ClientToScreen/GetWindowRect/DWM 等 API 映射为 X11 等价（wmctrl/xdotool），环境说明迁移至 Linux bash（Ubuntu 24.04，root） |
