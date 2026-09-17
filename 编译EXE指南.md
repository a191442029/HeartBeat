# 编译EXE文件指南

## 准备工作

### 1. 安装PyInstaller

```bash
pip install pyinstaller
```

### 2. 确认所需依赖已安装

```bash
pip install pyqt5
pip install qasync
pip install bleak
pip install paho-mqtt
```

## 编译方法

项目提供了两种编译方式：

### 方法一：使用独立版本编译（推荐）

这是最简单的方法，适合大多数用户。

```bash
# 在 HeartBeat 目录下运行
build_standalone.bat
```

编译完成后，可执行文件位于：`dist\HRMLink.exe`

**特点：**
- ✅ 单文件，易于分发
- ✅ 包含所有必要组件
- ✅ 不需要额外配置
- ⚠️ 文件较大（约50-100MB）

### 方法二：使用标准编译

如果你在conda环境中开发，可以使用这个方法。

```bash
# 在 HeartBeat 目录下运行
build.bat
```

编译完成后，可执行文件位于：`./_dist/v1.0.0/HRMLink.exe`

**特点：**
- ✅ 可以自定义输出路径
- ⚠️ 需要确保DLL路径正确
- ⚠️ 可能需要修改 HRMLink.spec 中的DLL路径

### 方法三：手动编译

如果批处理文件无法运行，可以手动执行命令：

```bash
# 切换到 HeartBeat 目录
cd HeartBeat

# 使用独立版本spec文件编译
pyinstaller HRMLink_standalone.spec --clean

# 或者使用标准spec文件编译
pyinstaller HRMLink.spec --clean --distpath=./_dist/v1.0.0
```

## 编译后的文件结构

```
HeartBeat/
├── dist/
│   └── HRMLink.exe          # 编译后的可执行文件
└── build/                   # 编译过程中的临时文件（可以删除）
```

## 分发准备

编译完成后，建议创建一个分发包：

### 1. 创建分发目录

```
HRMLink_v1.0.0/
├── HRMLink.exe              # 主程序
├── config.ini               # 配置文件（可选，首次运行会自动创建）
├── README.txt               # 使用说明
└── log/                     # 日志目录（首次运行会自动创建）
```

### 2. 准备README文件

可以参考现有的 `README.txt` 或创建新的使用说明。

### 3. 打包分发

将整个文件夹压缩为ZIP文件，方便分发。

## 新增功能提醒

本版本新增了**心率日志记录功能**，编译时已自动包含：
- `heart_rate_logger.py` - 核心日志模块
- CSV支持 - 用于保存心率数据
- 日志会自动保存到 `log` 目录

用户无需额外配置，连接设备后会自动记录心率数据。

## 常见问题

### Q1: 编译失败，提示找不到模块

**解决方法：**
```bash
# 确保所有依赖都已安装
pip install -r requirements.txt

# 如果没有requirements.txt，手动安装：
pip install pyqt5 qasync bleak paho-mqtt pyinstaller
```

### Q2: 编译后运行出错，提示缺少DLL

**解决方法：**

方法一：使用独立版本编译（HRMLink_standalone.spec）

方法二：如果使用HRMLink.spec，需要修改DLL路径：
- 打开 `HRMLink.spec`
- 修改 `binaries` 部分的DLL路径为你的实际路径
- 或者注释掉 `binaries` 部分，让PyInstaller自动处理

### Q3: 编译后的EXE文件很大

这是正常的，因为包含了Python运行时和所有依赖库。

**优化方法：**
- 使用UPX压缩（已在spec中启用）
- 确保spec文件中的 `upx=True`

### Q4: 编译后的程序无法连接蓝牙设备

确保：
1. 使用的是Windows 10或更高版本
2. 以管理员权限运行编译后的程序
3. 系统蓝牙功能正常

### Q5: 如何更新版本号？

1. 打开 `__main__.py`
2. 修改 __main__.py 顶部的版本号：
   ```python
   VER2 = (1, 0, 0, 0)  # 修改这里
   ```
3. 重新编译

## 测试编译结果

编译完成后，建议进行以下测试：

1. ✅ 双击运行EXE文件
2. ✅ 检查主界面是否正常显示
3. ✅ 扫描蓝牙设备
4. ✅ 连接心率设备
5. ✅ 检查心率数据是否正常显示
6. ✅ 检查 `log` 目录是否自动创建
7. ✅ 检查心率数据是否正常记录到CSV文件
8. ✅ 测试MQTT功能（如果使用）
9. ✅ 测试浮动窗口
10. ✅ 测试开机自启动

## 发布检查清单

发布前确认：

- [ ] 版本号已更新
- [ ] 所有功能测试通过
- [ ] README.txt 已更新
- [ ] 编译无错误和警告
- [ ] 在干净的Windows系统上测试过
- [ ] 杀毒软件不报毒
- [ ] 文件大小合理
- [ ] 准备好更新日志

## 高级选项

### 修改程序图标

1. 准备一个 `.ico` 文件
2. 修改 spec 文件中的 `icon` 参数：
   ```python
   icon='path/to/your/icon.ico'
   ```
3. 重新编译

### 添加版本信息

程序已配置使用 `version.txt` 文件显示版本信息。

### 隐藏控制台窗口

spec文件中已设置 `console=False`，编译后不会显示控制台窗口。

如果需要调试，可以临时改为 `console=True`。

## 技术支持

如果编译过程中遇到问题：

1. 查看本文档的常见问题部分
2. 检查PyInstaller官方文档
3. 查看项目的GitHub Issues
4. 确保使用的是Python 3.13（推荐版本）

## 相关文件

- `HRMLink.spec` - PyInstaller配置文件（标准版本）
- `HRMLink_standalone.spec` - PyInstaller配置文件（独立版本，推荐）
- `build.bat` - 编译脚本（标准版本）
- `build_standalone.bat` - 编译脚本（独立版本，推荐）
- `version.txt` - 版本信息文件（构建时由 build_bat.py 自动生成，无需手动维护）

