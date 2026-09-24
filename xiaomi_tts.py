# -*- coding: utf-8 -*-
"""小米云端TTS直连(小爱音箱): EXE内完成小米账号登录与播报, 替代xiaoi桥接服务(免Docker)

链路: MiAccount登录(token本地缓存xiaomi_token.json, 过期自动刷新)
  → 每台音箱: MiOT TTS动作(siid=5,aiid=1)优先, 失败回退MiNA text_to_speech
  (与xiaoi桥接服务同款双链路策略: 部分新机型仅支持其一)
2FA/风控: 登录需二次验证时抛出带指引的异常, 由设置页弹窗/推送记录展示
线程模型: 推送/设置页均为同步线程 → asyncio.run包装, 每次调用独立会话,
  token经文件复用避免重复登录
"""
from __future__ import annotations

import asyncio
import os
import re
import threading

import system_utils as _su

# 串行化锁: 两个sync入口各自asyncio.run, 若推送线程与设置页测试线程并发执行,
# 两个事件循环会同时刷新xiaomi_token.json, 可能写坏token或重复登录触发风控;
# TTS低频, 串行化无性能影响
_tts_lock = threading.Lock()


def _token_path() -> str:
    """token缓存路径(与config.ini同目录, EXE启动时cwd已切到程序目录)"""
    return os.path.join(os.getcwd(), "xiaomi_token.json")


def _password(pass_b64: str) -> str:
    """DPAPI解密配置中保存的密码"""
    return _su.dpapi_unprotect(pass_b64) if pass_b64 else ""


def _friendly_error(e: Exception) -> str:
    """小米云端异常 → 一句话人话提示(miservice会把小米返回的dict整串拼进异常, 直接展示不可读)"""
    s = str(e)
    m = re.search(r"No module named '(\w+)'", s)
    if m:  # 依赖缺失(免打包环境常见): 给出可执行的修复指令
        return f"缺少运行库 {m.group(1)}, 请在程序运行环境执行: pip install {m.group(1)}"
    if "70016" in s:
        return "小米账号或密码错误 (code 70016)"
    if "70081" in s or "87001" in s or "captchaUrl" in s:
        return "小米登录需要验证码/风控校验, 请先在手机米家APP或网页版登录一次并允许新设备后重试"
    if "Login auth failed" in s or "Login failed" in s:
        return "小米登录被拒绝: 请核对账号密码; 若无误, 先在米家APP/网页版登录一次允许新设备再试"
    return s


def _otp_callback():
    """两步验证回调: GUI环境无终端可输入, 明确报错而非挂起"""
    async def _cb(_tip: str) -> str:
        raise RuntimeError(
            "小米账号登录需要二次验证(新设备风控)。请先在手机米家APP或网页版"
            "登录一次并允许新设备, 或稍后重试")
    return _cb


async def _make_mina(user: str, pass_b64: str):
    """创建会话+账号+MiNA服务(调用方负责close会话)"""
    import aiohttp
    from miservice import MiAccount, MiNAService
    session = aiohttp.ClientSession()
    account = MiAccount(session, user, _password(pass_b64),
                        _token_path(), otp_callback=_otp_callback())
    return session, account, MiNAService(account)


def xiaoi_speakers_sync(user: str, pass_b64: str) -> tuple:
    """列出账号下音箱(设置页"登录并获取音箱"按钮): 返回 (ok, 音箱列表或错误串)"""
    try:
        with _tts_lock:  # 防与其他入口并发刷新token文件
            return True, asyncio.run(_speakers(user, pass_b64))
    except Exception as e:
        return False, _friendly_error(e)


def xiaoi_tts_sync(user: str, pass_b64: str, dids: list, text: str) -> tuple:
    """同步播报(推送线程调用): dids为MiNA deviceID列表, 空则播第一台; 返回 (ok, msg)"""
    try:
        with _tts_lock:  # 防与其他入口并发刷新token文件
            return asyncio.run(_tts(user, pass_b64, dids, text))
    except Exception as e:
        return False, f"小米云端: {_friendly_error(e)}"


async def _speakers(user: str, pass_b64: str) -> list:
    session, _, mina = await _make_mina(user, pass_b64)
    try:
        devs = await mina.device_list()
        if not devs:
            raise RuntimeError("登录成功但账号下未发现小爱音箱")
        out = []
        for d in devs:
            out.append({
                "deviceID": str(d.get("deviceID", "")),
                "name": d.get("name") or d.get("alias") or str(d.get("deviceID", "")),
                "alias": d.get("alias", ""),
                "hardware": d.get("hardware", ""),
            })
        return out
    finally:
        await session.close()


async def _tts(user: str, pass_b64: str, dids: list, text: str) -> tuple:
    session, account, mina = await _make_mina(user, pass_b64)
    try:
        devs = await mina.device_list()
        if not devs:
            return False, "账号下未发现小爱音箱"
        if dids:  # 按勾选过滤
            targets = [d for d in devs if str(d.get("deviceID", "")) in dids]
        else:     # 未选: 播第一台(与旧webhook模式"默认音箱路由"语义对齐)
            targets = devs[:1]
        if not targets:
            return False, "所选音箱不在账号设备列表中(可能已解绑, 请重新登录获取)"
        from miservice import MiIOService
        io = MiIOService(account)
        oks, errs = [], []
        for d in targets:
            name = d.get("name") or d.get("alias") or str(d.get("deviceID", ""))
            did = str(d.get("deviceID", ""))
            ok, via = False, ""
            try:  # 链路1: MiOT TTS动作(siid=5,aiid=1), code==0即成功
                if await io.miot_action(did, (5, 1), [text]) == 0:
                    ok, via = True, "MiOT"
            except Exception:
                pass
            if not ok:  # 链路2: MiNA text_to_speech兜底
                try:
                    if await mina.text_to_speech(did, text):
                        ok, via = True, "MiNA"
                except Exception:
                    pass
            if ok:
                oks.append(f"{name}({via})")
            else:
                errs.append(name)
        if errs:
            if oks:
                return True, f"部分成功: {'、'.join(oks)}; 失败: {'、'.join(errs)}"
            return False, f"播报失败: {'、'.join(errs)} (检查音箱在线/音量/机型TTS支持)"
        return True, "已播报: " + "、".join(oks)
    finally:
        await session.close()
