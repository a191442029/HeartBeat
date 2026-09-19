import sys
import os
import json
import asyncio
import argparse

# 固定工作目录为 exe/脚本所在目录:
# config.ini、log/、version.json、upd.exe 等均以相对路径解析,
# 若经开机自启等途径启动, CWD 可能是 System32, 导致配置与日志写错位置
try:
    os.chdir(os.path.dirname(os.path.abspath(sys.argv[0])))
except Exception:
    pass

# Use ASCII version of system_utils to avoid encoding issues
import system_utils_ascii as system_utils
system_utils.IS_FROZEN = IS_FROZEN = getattr(sys, 'frozen', False) or hasattr(sys, "_MEIPASS") or ("__compiled__" in globals())

VER2 = (1, 0, 2, 0)
BINARY_BUILD = 1
v1      = "v" + ".".join(map(str, VER2[0:3]))
F_      = ""   # v1.0.1 起为正式版发布(无 -beta 后缀); Fvname 同时用作 release tag
vname   = v1 + (F_ if IS_FROZEN else f"+code.{VER2[3]}")
Fvname  = v1 +  F_
__version__ = vname

system_utils.VER2  = VER2
system_utils.vname = vname
IS_NUITKA = IS_FROZEN and "__compiled__" in globals()

from system_utils import (check_run,AppisRunning,
     getlogger, upmod_logger, add_errorfunc, handle_exception
    ,init_config, pip_install_models
    ,handle_update_mode,handle_end_update, try_except
)

# 同步版本信息到真实 system_utils 模块:
# UI/checkupdate/read_data/add_to_startup 都从真实 system_utils 导入,
# 此前只设置了 system_utils_ascii 的属性, 导致 read_data 中 VER2 NameError、
# 窗口标题显示 no version name、打包版 IS_FROZEN 恒为 False 自启功能损坏
import system_utils
system_utils.VER2 = VER2
system_utils.vname = vname
system_utils.IS_FROZEN = IS_FROZEN

# 解析命令行参数
parser = argparse.ArgumentParser()
parser.add_argument('-updatemode', action='store_true', help='更新模式标志')
parser.add_argument('-endup', action='store_true', help='更新结束标志')
parser.add_argument('-startup', action='store_true', help='用于测试应用能否通过start.bat脚本正常启动')
parser.add_argument('-start_', action='store_true', help='开机启动标志')
args = parser.parse_args()

if args.start_:
    system_utils.SLEEP_TIME = 10

if args.startup:
    print("Success!")
    sys.exit(0)

# 调用已修改的check_run函数
# 现在这个函数已经被替换为一个空函数，不会执行任何可能导致编码问题的代码
try:
    check_run()  # 这个函数现在只会打印一条消息并返回False
except Exception as e:
    print(f"检查程序运行状态时出错: {e}")
    # 继续执行程序，不退出

# 如果是更新模式，使用简单日志输出
if args.updatemode:
    logger = upmod_logger()
else:
    logger = getlogger()

# 设置全局异常钩子
sys.excepthook = handle_exception

if IS_FROZEN:
    # 更新模式
    if args.updatemode:
        logger.info("进入更新模式...")
        handle_update_mode()

    # 更新结束模式
    if args.endup:
        logger.info("进入更新结束模式...")
        handle_end_update()
else:
    with open("version.json", "w", encoding="utf-8") as f:
        sdata = {
             "name": vname
            ,"version": 2
            ,"VER2": VER2
            ,"gxjs": "优化应用启动，修复无法保存文件的问题等"
        }
        text = json.dumps(sdata, ensure_ascii=False, indent=2)
        frozendata = {
                 "name": Fvname
                ,"version": 2
                ,"VER2": VER2
                ,"updateTime": "2026-09-19-19:58:00"
                ,"gxjs": "状态提示同步: 断连/连接过程/失败原因实时同步到安卓接收端波形上方显示"
                ,"index": f"https://github.com/a191442029/HeartBeat/releases/{Fvname}"
                ,"download": f"https://github.com/a191442029/HeartBeat/releases/download/{Fvname}/HRMLink.exe"
            }
        frozentext = f""",\n\n\n  "frozen":{json.dumps(frozendata, ensure_ascii=False)}\n}}"""
        text = text[0:-2] + frozentext
        f.write(text)
    try:
        from importlib import import_module
        buildbatmain = import_module("build_bat").main
        buildbatmain(VER2, Fvname)
    except Exception: pass

init_config()

def import_pyqt5():
    global QApplication, QtWin
    from PyQt5.QtWidgets import QApplication
    from PyQt5.QtWinExtras import QtWin

def import_qasync():
    global QEventLoop
    from qasync import QEventLoop

def import_models():
    import bleak

def import_mqtt():
    import paho.mqtt.client

def import_aiohttp():
    import aiohttp  # Tailscale数据服务(WS推送+HTTP轮询)

pip_install_models(import_pyqt5, "pyqt5")
pip_install_models(import_qasync, "qasync")
pip_install_models(import_models, "bleak")
pip_install_models(import_mqtt, "paho-mqtt")
pip_install_models(import_aiohttp, "aiohttp")

from importlib.metadata import metadata as get_metadata, distributions, distribution, Distribution
packages: list[dict[str, str|int]] = []
packages.append({"nameandversion": "包名 == 版本号", "len": 10, "license":"开源许可证", "name":""})
if not IS_NUITKA: # 避免nuitka编译后无法运行
    for entry in distributions():
        nameandversion = f"{entry.name} == {entry.version}"
        packages.append({"name": entry.name, "nameandversion": nameandversion, "len": len(nameandversion)})

def add_pak(name:Distribution):
    @try_except(f"手动获取依赖包名{name}", exit_ = False, exc_info=False)
    def add_pak_():
        NaVer = f"{name.name} == {name.version}"
        packages.append({"name": name.name, "nameandversion": NaVer, "len": len(NaVer)})
    if name.name not in [x["name"] for x in packages]:
        add_pak_()

def get_license(name:str):
    try:
        return get_metadata(name).get('License')
    except Exception as e:
        logger.warning(f"无法获取依赖包 {name} 的授权信息: {e}")
        return "Unknown"

# pyinstaller编译的应用必须直接使用`distribution`函数获取模块信息, 否则会报错
data = [
     distribution('bleak'),
     distribution('pyinstaller') if not IS_NUITKA else None,
     distribution('PyQt5'),distribution('PyQt5-Qt5'),distribution('PyQt5_sip')
    ,distribution('qasync'),distribution('winrt-runtime'),distribution('aiohttp')
]
[add_pak(d_) if d_ else None for d_ in data]

max_len = max(map(lambda x: x["len"], packages))

packageslogtext = "\n  ".join(
    map(
    lambda x: f"{x["nameandversion"]:<{max_len+2}}-{get_metadata(x["name"]).get('License') if "license" not in x else x["license"]}"
    , packages
    )
)

logger.info("[项目依赖包清单:\n  "+packageslogtext + "\n]")

from UI import MainWindow
import ctypes 

if __name__ == "__main__":
    app = QApplication(sys.argv)

    app_id = 'a191442029.HRMLink.Main.1'
    QtWin.setCurrentProcessExplicitAppUserModelID(app_id)
    ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(app_id)

    # 设置异步事件循环
    loop = QEventLoop(app)
    asyncio.set_event_loop(loop)

    window = MainWindow()
    window.show()
    hwnd = window.winId()

    def errwin(exc_type, exc_value, exit_=True):
        # errorfunc 统一签名: (exc_type, exc_value, exit_)
        window.verylarge_error(f"{exc_type.__name__}: {exc_value}", exit_)
    add_errorfunc(errwin)


    screen = app.primaryScreen()
    screen.logicalDotsPerInchChanged.connect(window.auto_FixedSize)

    with loop:
        screens = app.screens()
        loop.run_forever()