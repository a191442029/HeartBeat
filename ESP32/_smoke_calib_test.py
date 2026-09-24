# -*- coding: utf-8 -*-
"""relay_hub 信号标定/偏差双轨 回归测试: 模拟2节点应答scan验证标定流水线与仲裁双轨

运行: 项目根目录执行 python ESP32/_smoke_calib_test.py
结束后还原测试写入的 config.ini [esp32_relay] node_biases。
"""
import asyncio, json, logging, sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
logging.basicConfig(level=logging.INFO, format="%(message)s")

import system_utils
system_utils.logger = logging.getLogger("smoke")
system_utils.init_config()
_orig_biases_raw = system_utils.gs("esp32_relay", "node_biases", "", str, "-smoke")
import relay_hub

RSSI_A, RSSI_B = -50, -60      # 常量RSSI: 期望 bias A=+5, B=-5 (ref=-55)
received_connect = []


async def run_test():
    hub = relay_hub.get_hub()
    import aiohttp
    sessions, readers = [], []
    ws_map = {}
    for name, rssi in (("节点A", RSSI_A), ("节点B", RSSI_B)):
        session = aiohttp.ClientSession()
        sessions.append(session)
        ws = await session.ws_connect("http://127.0.0.1:8899/relay",
                                      headers={"Authorization": "Bearer HRMLink-ESP32-2025"})
        await ws.send_str(json.dumps({"type": "hello", "name": name, "fw": "1.1.0", "ip": "127.0.0.1"}))
        ws_map[name] = (ws, rssi)

        async def reader(ws=ws, my_rssi=rssi, my_name=name):
            try:
                async for msg in ws:
                    if msg.type != aiohttp.WSMsgType.TEXT:
                        break
                    data = msg.json()
                    if data.get("type") == "scan":
                        await asyncio.sleep(int(data.get("dur", 300)) / 1000.0 + 0.05)
                        await ws.send_str(json.dumps({"type": "scan", "rssi": my_rssi}))
                    elif data.get("type") == "connect":
                        received_connect.append(my_name)
            except Exception:
                pass
        readers.append(asyncio.create_task(reader()))
    await asyncio.sleep(1.0)   # 等hello处理完

    # ---- 1) 标定: 6轮x300ms, 常量RSSI → 零和偏差 ----
    ok, msg = relay_hub.start_calibration(rounds=6, scan_ms=300)
    assert ok, f"标定启动失败: {msg}"
    st = {}
    for _ in range(40):
        await asyncio.sleep(0.5)
        st = relay_hub.calibration_status()
        if st.get("started") and not st.get("running"):
            break
    print("标定状态:", json.dumps(st, ensure_ascii=False, default=str))
    assert st.get("started") and not st.get("running"), "标定未在时限内结束"
    assert not st.get("error"), f"标定报错: {st.get('error')}"
    biases = relay_hub.get_biases()
    print("偏差:", biases)
    assert biases.get("节点A") == 5 and biases.get("节点B") == -5, f"零和偏差不符: {biases}"
    rows = relay_hub.node_table()
    assert all("bias" in r for r in rows) and len(rows) == 2, f"node_table缺bias字段: {rows}"

    # ---- 2) 白盒双轨: 冻结仲裁器防干扰 ----
    hub._calib = {"running": True}
    try:
        with hub._lock:
            hub._nodes["节点A"].update(state="idle", rssi_ema=-46, fail_count=0)
            hub._nodes["节点B"].update(state="idle", rssi_ema=-48, fail_count=0)
        # raw: A(-46)强于B(-48); 校准: A(-46-5=-51) vs B(-48+5=-43) → B应胜出
        assert hub._best_idle() == "节点B", "选路未应用偏差(应选校准更强的B)"
        # 探测切换: 原持有A raw -70(校准-75), 候选B -62(校准-57) ≥ -75+10 → 切换
        hub.probe_old_name, hub.probe_pre_rssi = "节点A", -70
        hub._band_mac = lambda: "AA:BB:CC:DD:EE:FF"
        hub._scan_results = {"节点B": -62}
        hub._resolve_probe()
        assert hub.pending_connect == "节点B", "探测切换未用校准值(应切换到B)"
        # 候选不够强: B -71(校准-66) < -75+10=-65 → 回连原持有者
        hub._scan_results = {"节点B": -71}
        hub._resolve_probe()
        assert hub.pending_connect == "节点A", "迟滞判定未用校准值(应回连A)"
        print("双轨选路/切换断言通过")
    finally:
        hub._calib = None
    for r in readers:
        r.cancel()
    for s in sessions:
        await s.close()


def _restore_config():
    with system_utils._config_lock:
        if _orig_biases_raw:
            system_utils.config.set("esp32_relay", "node_biases", _orig_biases_raw)
        elif system_utils.config.has_option("esp32_relay", "node_biases"):
            system_utils.config.remove_option("esp32_relay", "node_biases")
        system_utils._write_config_file()


hub = relay_hub.get_hub()
hub.start()
try:
    asyncio.run(run_test())
    print("RESULT=PASS")
finally:
    relay_hub.stop_relay_hub()
    _restore_config()
