## v1.1.3 更新内容（2026-09-25）

### 启动早期扫描修复
- 修复启动后立即扫描报 "no running event loop"、设备列表空白的问题
- 原因：bleak 2.1 新增的 COM 线程模型（MTA）检查依赖运行中的事件循环，启动早期事件循环尚未就绪时会失败；现自动跳过该检查（旧版 bleak 从无此检查，行为等价）

### 包含 v1.1.2 全部修复
- 旧版 Win10（2004 以下）扫描报 "property is not available in this version of Windows" 的兼容修复

### 说明
- 仅桌面端 EXE 更新；**安卓/鸿蒙接收端沿用 v1.1.1 无需更新**
