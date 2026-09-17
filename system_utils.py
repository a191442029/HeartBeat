import os
import sys
import time
import json
import shutil
import inspect
import logging
import datetime
import subprocess
import urllib.error
import urllib.request
from typing import Any
from logging.handlers import RotatingFileHandler

VER2:tuple[int,int,int,int]
vname = "no version name"
IS_FROZEN = None
SLEEP_TIME = 1
class AppisRunning(Exception):pass

# --------进程检测--------
def check_run():
    """
    检查环境变量，如果设置了SKIP_DUPLICATE_CHECK，则跳过重复运行检查
    否则执行原始的检查逻辑
    """
    # 检查环境变量
    if os.environ.get('SKIP_DUPLICATE_CHECK'):
        print("Duplicate running check skipped due to environment variable")
        return False
    
    # 完全禁用重复运行检查功能
    print("Duplicate running check disabled")
    return False

# --------日志处理--------
class CanNotSaveLogFile(Exception):
    """创建日志文件失败"""
    level = 0

class MyHandler(RotatingFileHandler):
    def doRollover(self):
        try:
            super().doRollover()
        except Exception as e:
            raise CanNotSaveLogFile("日志保存失败: %s" % e)
        logger.info(f"运行程序 -{vname} " + " ".join(argv for argv in sys.argv if argv))
        logger.info(f"Python版本: {sys.version}; 运行位置：{sys.executable}")

# 初始化logger为None，避免导入错误
logger = None

def getlogger():
    global logger
    # 创建日志记录器
    logger = logging.getLogger('__main__')
    logger.setLevel(logging.DEBUG)

    # 初始化日志文件
    set_logfile()

    try:
        handler = MyHandler(
             'log/loger.log'
            ,maxBytes=5*1024*1024
            ,backupCount=3
            ,encoding='utf-8'
        )
    except Exception:
        # 无法使用日志文件时使用一般的日志记录器
        handler = logging.StreamHandler()
    handler.setLevel(logging.DEBUG)

    formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    if isinstance(handler, MyHandler):
        handler.doRollover()
    return logger

def upmod_logger():
    global logger

    logger = logging.getLogger('__main__')

    if not os.path.exists('log'):
        os.mkdir('log')

    handler = logging.FileHandler('log/uplog.log', 'a', encoding='utf-8')
    handler.setLevel(logging.DEBUG)

    formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    return logger

def set_logfile():
    """设置日志文件"""
    rt = 0
    while rt < 20:
        try:
            if not os.path.exists('log'):
                os.mkdir('log')
            if os.path.exists('log/loger1.log'):
                print("正在删除旧日志文件1...")
                os.remove('log/loger1.log')
            if os.path.exists('log/loger2.log'):
                print("正在删除旧日志文件2...")
                os.remove('log/loger2.log')
            return
        except Exception as e:
            print(f"错误: {e}")
            time.sleep(SLEEP_TIME)
            rt += 1

# --------错误输出处理函数--------

errorfunc = None

def handle_exception(exc_type, exc_value, exc_traceback):
    if issubclass(exc_type, KeyboardInterrupt):
        sys.__excepthook__(exc_type, exc_value, exc_traceback)
        return
    elif isinstance(exc_type, ModuleNotFoundError):
        logger.warning("缺少模块: %s", exc_type.name)
        logger.error(
            "模块导入错误: \n",
            exc_info=(exc_type, exc_value, exc_traceback)
        )
        if errorfunc:
            errorfunc(exc_type, exc_value, False)
        pip_install_package(exc_type.name)
        sys.exit(1)

    if hasattr(exc_value, 'level'):
        lv = exc_value.level
    else:
        lv = 1
    logger.error(
        f"{"严重"if lv==1 else""}错误: \n",
        exc_info=(exc_type, exc_value, exc_traceback)
    )
    exit_ = False if lv == 0 else True
    if errorfunc:
        errorfunc(exc_type, exc_value, exit_)

def add_errorfunc(func):
    """用于添加错误处理函数
    函数必须接收参数: exc_type, exc_value
    """
    global errorfunc
    errorfunc= func

def try_except(errlogname = "", func_ = None, exit_ = True, exc_info = True):
    """用于初始化错误处理的装饰器, 兼容同步函数和异步函数"""
    def try_(func):
        def main(*args, **kwargs):
            try:
                if exc_info:
                    logger.info(f"{errlogname} 开始")
                anything = func(*args, **kwargs)
                logger.info(f"{errlogname} 完成")
                return anything
            except Exception as e:
                logger.error(f"{"严重" if exit_ else ""}错误: {errlogname} 失败: {e}", exc_info=exc_info)
                if func_ is not None: func_(e=f"{"严重" if exit_ else ""}错误: {errlogname} 失败: {e}")
                if exit_:
                    sys.exit(1)
        async def async_main(*args, **kwargs):
            # 异步版本: 必须在协程内部捕获异常, 否则装饰器立即返回协程对象,
            # "完成"日志错位且异常捕获完全失效
            try:
                if exc_info:
                    logger.info(f"{errlogname} 开始")
                anything = await func(*args, **kwargs)
                logger.info(f"{errlogname} 完成")
                return anything
            except Exception as e:
                logger.error(f"{"严重" if exit_ else ""}错误: {errlogname} 失败: {e}", exc_info=exc_info)
                if func_ is not None: func_(e=f"{"严重" if exit_ else ""}错误: {errlogname} 失败: {e}")
                if exit_:
                    sys.exit(1)
        if inspect.iscoroutinefunction(func):
            return async_main
        return main
    return try_

# --------配置文件操作--------

from configparser import ConfigParser

SETTINGTYPE = dict[str, Any]
config_file = 'config.ini'

config = ConfigParser()

def init_config():
    global config
    try:
        if not os.path.exists(config_file):
            logger.warning("未找到配置文件 config.ini, 尝试创建默认配置文件")
            save_settings()
        config.read(config_file, encoding='utf-8')
        check_sections()
    except Exception as e:
        logger.error(f"无法加载配置文件: {e}", exc_info=True)

def check_sections():
    sectionlist = ['GUI', 'FloatingWindow', 'Device', 'MQTT', 'InfluxDB', 'Logger', 'Push', 'Tailscale']
    s_ = False
    for section in sectionlist:
        if not config.has_section(section):
            config.add_section(section)
            s_ = True
    if s_: save_settings()
    logger.info(f"配置文件sections检查完成: {sectionlist}")

@try_except("修改配置")
def update_settings(**kwargs: SETTINGTYPE):
    global config
    logger.info(f"{kwargs}")
    for section in kwargs.keys():
        if not config.has_section(section):
            config.add_section(section)
        data = kwargs[section]
        for key in data.keys():
            config.set(section, key, str(data[key]))
    save_settings()

def save_settings():
    global config
    with open(config_file, 'w', encoding='utf-8') as configfile:
        config.write(configfile)

def gs(section, option, default, type_:type = None, debugn = ""):
    try:
        if type_ == bool:
            data = config.getboolean(section, option, fallback=default)
        else :
            data = config.get(section, option, fallback=default)
        logger.debug(f' [{debugn}] -获取配置项 {option} 的值: {data}')
        if data is None or data == "None":
            return default
        if type_ is None:
            return data
        else:return type_(data)
    except Exception as e:
        # 配置值损坏(如手改坏JSON/非法布尔)时回退默认值, 不让程序起不来
        logger.warning(f"[{debugn}] 配置项 {option} 值解析失败({e}), 使用默认值: {default!r}")
        return default

def ups(section, option: str, value, debugn = ""):
    if not config.has_section(section):
        config.add_section(section)  # 防御: 配置未init/section缺失时不再抛NoSectionError
    config.set(section, option, str(value))
    logger.debug(f'[{debugn}] 更新配置项 {option} 的值: {value}')
    save_settings()

# --------敏感配置加密(Windows DPAPI, 当前用户级, 无需额外依赖)--------

import base64
DPAPI_PREFIX = "dpapi:"

def _dpapi_crypt(data: bytes, protect: bool) -> bytes:
    import ctypes
    import ctypes.wintypes

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", ctypes.wintypes.DWORD),
                    ("pbData", ctypes.POINTER(ctypes.c_char))]

    blob_in = DATA_BLOB(len(data), ctypes.cast(
        ctypes.create_string_buffer(data, len(data)), ctypes.POINTER(ctypes.c_char)))
    blob_out = DATA_BLOB()
    crypt32 = ctypes.windll.crypt32
    if protect:
        ok = crypt32.CryptProtectData(ctypes.byref(blob_in), ctypes.c_wchar_p("HRMLink"),
                                      None, None, None, 0, ctypes.byref(blob_out))
    else:
        ok = crypt32.CryptUnprotectData(ctypes.byref(blob_in), None,
                                        None, None, None, 0, ctypes.byref(blob_out))
    if not ok:
        raise OSError(f"DPAPI {'加密' if protect else '解密'}失败")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(blob_out.pbData)

def dpapi_protect(plain: str) -> str:
    """明文→'dpapi:'+base64密文(仅当前Windows用户可解); 空值/已是密文/异常时原样返回"""
    if not plain or plain.startswith(DPAPI_PREFIX):
        return plain
    try:
        return DPAPI_PREFIX + base64.b64encode(_dpapi_crypt(plain.encode('utf-8'), True)).decode('ascii')
    except Exception as e:
        logger.warning(f"敏感配置加密失败, 回退明文存储: {e}")
        return plain

def dpapi_unprotect(value: str) -> str:
    """'dpapi:'前缀密文→明文; 旧版明文配置/空值原样返回(向后兼容)"""
    if not value or not value.startswith(DPAPI_PREFIX):
        return value
    try:
        raw = base64.b64decode(value[len(DPAPI_PREFIX):])
        return _dpapi_crypt(raw, False).decode('utf-8')
    except Exception as e:
        logger.warning(f"敏感配置解密失败(配置可能来自其他用户): {e}")
        return ""

# --------下载前置--------

def pip_install_models(import_models_func: callable, pip_modelname: str):
    try:
        import_models_func()
    except ModuleNotFoundError as e:
        logger.warning(f"缺少依赖包 {e.name}")
        if IS_FROZEN:
            logger.error("编译时错误: 请确保编译时已安装所有依赖包")
            sys.exit(1)
        else:
            pip_install_package(pip_modelname)
    except Exception as e:
        logger.error(f"无法导入模块: {e}")

def pip_install_package(package_name: str):
    # 尝试下载依赖包
    python_exe = sys.executable
    logger.info(f"正在尝试下载依赖包到: {python_exe}")
    try:
        try:
            os.system(f"{python_exe} -m pip install {package_name}")
        except Exception as e:
            logger.error(f"下载依赖包 {package_name} 失败: {e}")
            logger.warning(f"尝试使用阿里云镜像源下载依赖包 {package_name}")
            os.system(f"{python_exe} -m pip install {package_name} -i https://mirrors.aliyun.com/pypi/simple/")
        logger.info(f"已安装依赖包: {package_name}")
    except Exception as e:
        logger.error(f"依赖包安装失败: {e}", exc_info=True)
        sys.exit(1)

# --------启动管理--------

import winreg as reg
APPNAME = "HRMLink"
KEYPATH = r"Software\Microsoft\Windows\CurrentVersion\Run"

def check_startbat(path):
    "检查启动脚本"
    try:
        result = subprocess.run([path, "-startup"], capture_output=True, timeout=5)
        logger.info(f"启动脚本: {result.stdout.decode('utf-8', errors='ignore')}")
        if "Success!" in result.stdout.decode('utf-8', errors='ignore'): logger.debug("启动脚本通过检查");return True
        else: logger.warning("启动脚本检查不通过");return False
    except Exception as e:
        logger.error(f"启动项检查失败: {e}", exc_info=True)
        return False

def add_to_startup():
    # 获取当前可执行文件路径
    if IS_FROZEN:
        # 如果是打包后的exe
        value = os.path.abspath(sys.executable)
    else:
        # 如果是脚本
        b_ = os.path.dirname(os.path.abspath(sys.argv[0]))
        value = os.path.join(b_, "start.bat")
        # 测试启动脚本是否正确运行
        if not check_startbat(value):
            return "脚本"

    logger.info(f"正在添加到启动项 {value}")
    
    # 打开注册表中的启动项键
    key = reg.HKEY_CURRENT_USER

    try:
        registry_key = reg.OpenKey(key, KEYPATH, 0, reg.KEY_WRITE)
        reg.SetValueEx(registry_key, APPNAME, 0, reg.REG_SZ, rf'"{value}" -start_')
        reg.CloseKey(registry_key)
        return "成功"
    except WindowsError:
        logger.error("添加到启动项失败", exc_info=True)
        return "启动项"

def remove_from_startup():
    key = reg.HKEY_CURRENT_USER

    try:
        registry_key = reg.OpenKey(key, KEYPATH, 0, reg.KEY_WRITE)
        reg.DeleteValue(registry_key, APPNAME)
        reg.CloseKey(registry_key)
        return True
    except WindowsError:
        logger.error("无法从注册表中删除启动项", exc_info=True)
        return False
    
def check_startup():
    # 检查启动项状态
    key = reg.HKEY_CURRENT_USER
    if IS_FROZEN:
        # 如果是打包后的exe
        value = os.path.abspath(sys.executable)
    else:
        # 如果是脚本
        b_ = os.path.dirname(os.path.abspath(sys.argv[0]))
        value = os.path.join(b_, "start.bat")

    try:
        with reg.OpenKey(key, KEYPATH) as registry_key:
            value_, regtype = reg.QueryValueEx(registry_key, APPNAME)
            return (value_ == rf'"{value}" -start_'), value_
    except FileNotFoundError:
        logger.warning("[启动项] 键不存在")
        return False, ""
    except WindowsError:
        logger.warning("[启动项] ", exc_info=True)
        return False, ""

# --------应用更新--------

# 处理更新模式
def handle_update_mode():
    """处理更新模式，替换旧的主程序"""
    try:
        # 获取当前可执行文件路径(upd.exe)
        current_exe = sys.executable
        logger.info(f"当前更新程序路径: {current_exe}")

        # 获取目标路径(HRMLink.exe)
        target_dir = os.path.dirname(current_exe)
        target_exe = os.path.join(target_dir, "HRMLink.exe")

        # 删除旧的主程序
        if os.path.exists(target_exe):
            logger.info("正在删除旧的主程序...")
            os.remove(target_exe)

        # 将upd.exe复制为HRMLink.exe
        logger.info("正在复制更新文件...")
        shutil.copy2(current_exe, target_exe)
        
        # 以-endup参数运行新的主程序
        logger.info("启动新的主程序...")
        os.startfile(target_exe, arguments="-endup")

        # 退出当前进程
        logger.info("更新程序即将退出...")
        sys.exit(0)
    except Exception as e:
        logger.error(f"更新过程中出错: {e}")
        sys.exit(1)

# 处理更新结束模式
def handle_end_update():
    """处理更新结束，清理更新文件"""
    try:
        # 获取当前可执行文件路径(HRMLink.exe)
        current_exe = sys.executable
        logger.info(f"当前主程序路径: {current_exe}")
        
        # 获取更新文件路径(upd.exe)
        target_dir = os.path.dirname(current_exe)
        update_exe = os.path.join(target_dir, "upd.exe")
        
        # 删除更新文件
        if os.path.exists(update_exe):
            logger.info("正在清理更新文件...")
            os.remove(update_exe)
    except Exception as e:
        logger.error(f"清理更新文件时出错: {e}")

# 启动更新程序
def start_update_program():
    """启动更新程序"""
    try:
        # 获取当前可执行文件路径(HRMLink.exe)
        current_exe = sys.executable
        logger.info(f"当前主程序路径: {current_exe}")
        
        # 获取更新文件路径(upd.exe)
        target_dir = os.path.dirname(current_exe)
        update_exe = os.path.join(target_dir, "upd.exe")

        # 启动更新程序
        logger.info("正在启动更新程序...")
        os.startfile(update_exe, arguments="-updatemode")

        logger.info("更新程序已启动，请稍等...")
        sys.exit(0)
    except Exception as e:
        logger.error(f"启动更新程序时出错: {e}")
        sys.exit(1)

def checkupdate() :
    logger.info("检查更新中...")
    dtime = time.time() - gs("GUI","upstime",0,float,"检查更新")
    if dtime<200:
        logger.info(f"禁用检查更新中({int(dtime)}s/200s)")
        return False, '时限禁用', '', '', ''
    else:
        ups("GUI","upstime",time.time(),"检查更新")
    url = "https://raw.githubusercontent.com/a191442029/HeartBeat/main/version.json"
    urlGitee = "https://gitee.com/a191442029/HeartBeat/raw/main/version.json"
    urlGithub = "https://api.github.com/repos/a191442029/HeartBeat/contents/version.json?ref=main"
    # 顺序回退: 任一源成功即返回(原finally+return结构会覆盖前一源的成功结果)
    for name, fn, u in (("gitcode", check_with_raw, url),
                        ("gitee", check_with_raw, urlGitee),
                        ("github", check_with_githubapi, urlGithub)):
        try:
            return fn(u)
        except Exception as e:
            logger.warning(f"更新检查失败({name}): {e}", exc_info=True)
    return False, '失败', '', '', ''

def check_with_raw(url: str):
    with urllib.request.urlopen(url) as response: 
        # 读取json格式
        data = json.loads(response.read().decode('utf-8'))
        return read_data(data)

def check_with_githubapi(url: str):
    import base64
    with urllib.request.urlopen(url) as response:
        data = json.loads(response.read().decode('utf-8'))
        if data['content']:
            data_ = base64.b64decode(data['content']).decode('utf-8')
            return read_data(json.loads(data_))

def read_data(data)-> tuple[bool, str, str, str, str]:
    durl = data['frozen']['download']

    if IS_FROZEN:
        data_ = data['frozen']
        up_index = data_['index']
        updatetime = data_['updateTime']
        try:
            if datetime.datetime.now() < datetime.datetime.strptime(updatetime, '%Y-%m-%d-%H:%M:%S'):
                return False, '', '', '', ''
        except Exception as e:
            logger.error(f"更新时间检查失败: 更新时间读取失败({updatetime})")
    else:
        data_ = data
        up_index = 'https://github.com/a191442029/HeartBeat'
    VER2_VER = data_['VER2']
    vname = data_['name']
    gxjs = data_['gxjs']
    if VER2_VER > [v for v in VER2]:
        logger.info(f"发现新版本 {vname}[{'.'.join(map(str, VER2_VER))}]")
        return True, up_index, vname, gxjs, durl
    else:
        logger.info("当前已是最新版本")
    return False, '', '', '', ''