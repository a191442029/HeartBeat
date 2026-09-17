import os
import sys
import time
import json
import shutil
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

# --------Process Detection--------
def check_run():
    """
    Completely disable duplicate running check
    
    Original function replaced with an empty function that directly returns False
    This avoids any encoding-related issues
    """
    print("Duplicate running check disabled")
    return False

# --------Log Processing--------
class CanNotSaveLogFile(Exception):
    """Failed to create log file"""
    level = 0

class MyHandler(RotatingFileHandler):
    def doRollover(self):
        try:
            super().doRollover()
        except Exception as e:
            raise CanNotSaveLogFile("Log save failed: %s" % e)
        logger.info(f"Running program -{vname} " + " ".join(argv for argv in sys.argv if argv))
        logger.info(f"Python version: {sys.version}; Running location: {sys.executable}")

def getlogger():
    global logger
    # Create logger
    logger = logging.getLogger('__main__')
    logger.setLevel(logging.DEBUG)

    # Initialize log file
    set_logfile()

    try:
        handler = MyHandler(
             'log/loger.log'
            ,maxBytes=5*1024*1024
            ,backupCount=3
            ,encoding='utf-8'
        )
    except Exception:
        # Use standard logger if log file is unavailable
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
    """Set up log file"""
    rt = 0
    while rt < 20:
        try:
            if not os.path.exists('log'):
                os.mkdir('log')
            if os.path.exists('log/loger1.log'):
                print("Deleting old log file 1...")
                os.remove('log/loger1.log')
            if os.path.exists('log/loger2.log'):
                print("Deleting old log file 2...")
                os.remove('log/loger2.log')
            return
        except Exception as e:
            print(f"Error: {e}")
            time.sleep(SLEEP_TIME)
            rt += 1

# --------Error Output Processing Functions--------

errorfunc = None

def handle_exception(exc_type, exc_value, exc_traceback):
    if issubclass(exc_type, KeyboardInterrupt):
        sys.__excepthook__(exc_type, exc_value, exc_traceback)
        return
    elif isinstance(exc_type, ModuleNotFoundError):
        logger.warning("Missing module: %s", exc_type.name)
        logger.error(
            "Module import error: \n",
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
        f"{'Critical' if lv==1 else ''} error: \n",
        exc_info=(exc_type, exc_value, exc_traceback)
    )
    exit_ = False if lv == 0 else True
    if errorfunc:
        errorfunc(exc_type, exc_value, exit_, exit_)

def add_errorfunc(func):
    """Add error handling function
    Function must accept parameters: exc_type, exc_value
    """
    global errorfunc
    errorfunc= func

def try_except(errlogname = "", func_ = None, exit_ = True, exc_info = True):
    """Decorator for initializing error handling"""
    def try_(func):
        def main(*args, **kwargs):
            try:
                if exc_info:
                    logger.info(f"{errlogname} started")
                anything = func(*args, **kwargs)
                logger.info(f"{errlogname} completed")
                return anything
            except Exception as e:
                logger.error(f"{'Critical' if exit_ else ''} error: {errlogname} failed: {e}", exc_info=exc_info)
                if func_ is not None: func_(e=f"{'Critical' if exit_ else ''} error: {errlogname} failed: {e}")
                if exit_:
                    sys.exit(1)
        return main
    return try_

# --------Configuration File Operations--------

from configparser import ConfigParser

SETTINGTYPE = dict[str, Any]
config_file = 'config.ini'

config = ConfigParser()

def init_config():
    global config
    try:
        if not os.path.exists(config_file):
            logger.warning("Configuration file config.ini not found, attempting to create default configuration file")
            save_settings()
        config.read(config_file, encoding='utf-8')
        check_sections()
    except Exception as e:
        logger.error(f"Unable to load configuration file: {e}", exc_info=True)

def check_sections():
    sectionlist = ['GUI', 'FloatingWindow', 'Device', 'MQTT', 'Tailscale']
    s_ = False
    for section in sectionlist:
        if not config.has_section(section):
            config.add_section(section)
            s_ = True
    if s_: save_settings()

@try_except("Modify configuration")
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
        logger.debug(f' [{debugn}] -Get configuration item {option} value: {data}')
        if data is None or data == "None":
            return default
        if type_ is None:
            return data
        else:return type_(data)
    except Exception as e:
        # Corrupted config values (bad JSON / invalid bool) fall back to default, keep app bootable
        logger.warning(f"[{debugn}] Failed to parse config item {option} ({e}), using default: {default!r}")
        return default

def ups(section, option: str, value, debugn = ""):
    if not config.has_section(section):
        config.add_section(section)  # Defensive: no more NoSectionError when config not initialized
    config.set(section, option, str(value))
    logger.debug(f'[{debugn}] Update configuration item {option} value: {value}')
    save_settings()

# --------Sensitive config encryption (Windows DPAPI, per-user, no extra dependency)--------

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
        raise OSError(f"DPAPI {'encrypt' if protect else 'decrypt'} failed")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(blob_out.pbData)

def dpapi_protect(plain: str) -> str:
    """Plain text -> 'dpapi:'+base64 cipher (decryptable by current user only); passthrough on empty/cipher/error"""
    if not plain or plain.startswith(DPAPI_PREFIX):
        return plain
    try:
        return DPAPI_PREFIX + base64.b64encode(_dpapi_crypt(plain.encode('utf-8'), True)).decode('ascii')
    except Exception as e:
        logger.warning(f"Failed to encrypt sensitive config, falling back to plain text: {e}")
        return plain

def dpapi_unprotect(value: str) -> str:
    """'dpapi:' cipher -> plain text; legacy plain config/empty passthrough (backward compatible)"""
    if not value or not value.startswith(DPAPI_PREFIX):
        return value
    try:
        raw = base64.b64decode(value[len(DPAPI_PREFIX):])
        return _dpapi_crypt(raw, False).decode('utf-8')
    except Exception as e:
        logger.warning(f"Failed to decrypt sensitive config (may be from another user): {e}")
        return ""

# --------Download Prerequisites--------

def pip_install_models(import_models_func: callable, pip_modelname: str):
    try:
        import_models_func()
    except ModuleNotFoundError as e:
        logger.warning(f"Missing dependency package {e.name}")
        if IS_FROZEN:
            logger.error("Compilation error: Please ensure all dependency packages are installed at compile time")
            sys.exit(1)
        else:
            pip_install_package(pip_modelname)
    except Exception as e:
        logger.error(f"Unable to import module: {e}")

def pip_install_package(package_name: str):
    # Attempt to download dependency package
    python_exe = sys.executable
    logger.info(f"Attempting to download dependency package to: {python_exe}")
    try:
        try:
            os.system(f"{python_exe} -m pip install {package_name}")
        except Exception as e:
            logger.error(f"Failed to download dependency package {package_name}: {e}")
            logger.warning(f"Attempting to use Aliyun mirror source to download dependency package {package_name}")
            os.system(f"{python_exe} -m pip install {package_name} -i https://mirrors.aliyun.com/pypi/simple/")
        logger.info(f"Dependency package installed: {package_name}")
    except Exception as e:
        logger.error(f"Dependency package installation failed: {e}", exc_info=True)
        sys.exit(1)

# --------Startup Management--------

import winreg as reg
APPNAME = "HRMLink"
KEYPATH = r"Software\Microsoft\Windows\CurrentVersion\Run"

def check_startbat(path):
    "Check startup script"
    try:
        result = subprocess.run([path, "-startup"], capture_output=True, timeout=5)
        logger.info(f"Startup script: {result.stdout.decode('utf-8', errors='ignore')}")
        if "Success!" in result.stdout.decode('utf-8', errors='ignore'): logger.debug("Startup script passed check");return True
        else: logger.warning("Startup script check failed");return False
    except Exception as e:
        logger.error(f"Startup item check failed: {e}", exc_info=True)
        return False

def add_to_startup():
    # Get current executable file path
    if IS_FROZEN:
        # If it's a packaged exe
        value = os.path.abspath(sys.executable)
    else:
        # If it's a script
        b_ = os.path.dirname(os.path.abspath(sys.argv[0]))
        value = os.path.join(b_, "start.bat")
        # Test if startup script runs correctly
        if not check_startbat(value):
            return "Script"

    logger.info(f"Adding to startup items {value}")
    
    # Open registry key for startup items
    key = reg.HKEY_CURRENT_USER

    try:
        registry_key = reg.OpenKey(key, KEYPATH, 0, reg.KEY_WRITE)
        reg.SetValueEx(registry_key, APPNAME, 0, reg.REG_SZ, rf'"{value}" -start_')
        reg.CloseKey(registry_key)
        return "Success"
    except WindowsError:
        logger.error("Failed to add to startup items", exc_info=True)
        return "StartupItem"

def remove_from_startup():
    key = reg.HKEY_CURRENT_USER

    try:
        registry_key = reg.OpenKey(key, KEYPATH, 0, reg.KEY_WRITE)
        reg.DeleteValue(registry_key, APPNAME)
        reg.CloseKey(registry_key)
        return True
    except WindowsError:
        logger.error("Unable to remove startup item from registry", exc_info=True)
        return False
    
def check_startup():
    # Check startup item status
    key = reg.HKEY_CURRENT_USER
    if IS_FROZEN:
        # If it's a packaged exe
        value = os.path.abspath(sys.executable)
    else:
        # If it's a script
        b_ = os.path.dirname(os.path.abspath(sys.argv[0]))
        value = os.path.join(b_, "start.bat")

    try:
        with reg.OpenKey(key, KEYPATH) as registry_key:
            value_, regtype = reg.QueryValueEx(registry_key, APPNAME)
            return (value_ == rf'"{value}" -start_'), value_
    except FileNotFoundError:
        logger.warning("[StartupItem] Key does not exist")
        return False, ""
    except WindowsError:
        logger.warning("[StartupItem] ", exc_info=True)
        return False, ""

# --------Application Update--------

# Handle update mode
def handle_update_mode():
    """Handle update mode, replace old main program"""
    try:
        # Get current executable file path (upd.exe)
        current_exe = sys.executable
        logger.info(f"Current update program path: {current_exe}")

        # Get target path (HRMLink.exe)
        target_dir = os.path.dirname(current_exe)
        target_exe = os.path.join(target_dir, "HRMLink.exe")

        # Delete old main program
        if os.path.exists(target_exe):
            logger.info("Deleting old main program...")
            os.remove(target_exe)

        # Copy upd.exe as HRMLink.exe
        logger.info("Copying update files...")
        shutil.copy2(current_exe, target_exe)
        
        # Run new main program with -endup parameter
        logger.info("Starting new main program...")
        os.startfile(target_exe, arguments="-endup")

        # Exit current process
        logger.info("Update program about to exit...")
        sys.exit(0)
    except Exception as e:
        logger.error(f"Error during update process: {e}")
        sys.exit(1)

# Handle update end mode
def handle_end_update():
    """Handle update end, clean up update files"""
    try:
        # Get current executable file path (HRMLink.exe)
        current_exe = sys.executable
        logger.info(f"Current main program path: {current_exe}")
        
        # Get update file path (upd.exe)
        target_dir = os.path.dirname(current_exe)
        update_exe = os.path.join(target_dir, "upd.exe")
        
        # Delete update file
        if os.path.exists(update_exe):
            logger.info("Cleaning up update files...")
            os.remove(update_exe)
    except Exception as e:
        logger.error(f"Error cleaning up update files: {e}")

# Start update program
def start_update_program():
    """Start update program"""
    try:
        # Get current executable file path (HRMLink.exe)
        current_exe = sys.executable
        logger.info(f"Current main program path: {current_exe}")
        
        # Get update file path (upd.exe)
        target_dir = os.path.dirname(current_exe)
        update_exe = os.path.join(target_dir, "upd.exe")

        # Start update program
        logger.info("Starting update program...")
        os.startfile(update_exe, arguments="-updatemode")

        logger.info("Update program started, please wait...")
        sys.exit(0)
    except Exception as e:
        logger.error(f"Error starting update program: {e}")
        sys.exit(1)

def checkupdate() :
    logger.info("Checking for updates...")
    dtime = time.time() - gs("GUI","upstime",0,float,"Check for updates")
    if dtime<200:
        logger.info(f"Update check disabled ({int(dtime)}s/200s)")
        return False, 'TimeLimit', '', '', ''
    else:
        ups("GUI","upstime",time.time(),"Check for updates")
    try:
        url = "https://raw.githubusercontent.com/a191442029/HeartBeat/main/version.json"
        urlGitee = "https://gitee.com/a191442029/HeartBeat/raw/main/version.json"
        urlGithub = "https://api.github.com/repos/a191442029/HeartBeat/contents/version.json?ref=main"
        return check_with_raw(url)
    except urllib.error.URLError as e:
        logger.warning(f"Update check failed (gitcodeURL unreachable): {e}")
    except Exception as e:
        logger.error(f"Update check failed (unidentified error): {e}", exc_info=True)
    finally:
        try:
            return check_with_raw(urlGitee)
        except Exception as e:
            try:
                logger.warning(f"Update check failed (gitee): {e}", exc_info=True)
                return check_with_githubapi(urlGithub)
            except Exception as e:
                logger.error(f"Update check failed (github): {e}", exc_info=True)
                return False, 'Failed', '', '', ''

def check_with_raw(url: str):
    with urllib.request.urlopen(url) as response: 
        # Read json format
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
            logger.error(f"Update time check failed: Update time reading failed({updatetime})")
    else:
        data_ = data
        up_index = 'https://github.com/a191442029/HeartBeat'
    VER2_VER = data_['VER2']
    vname = data_['name']
    gxjs = data_['gxjs']
    if VER2_VER > [v for v in VER2]:
        logger.info(f"Found new version {vname}[{'.'.join(map(str, VER2_VER))}]")
        return True, up_index, vname, gxjs, durl
    else:
        logger.info("Already the latest version")
    return False, '', '', '', ''
