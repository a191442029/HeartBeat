# -*- coding: utf-8 -*-
"""relay_hub 端到端回归测试: 模拟节点验证 WS服务/分发/仲裁/心率回调/放手规则

运行: 项目根目录执行 python ESP32/_smoke_hub_test.py  (不改任何项目配置, 测试完自动停止)
"""
import asyncio, logging, sys, os, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
logging.basicConfig(level=logging.INFO, format="%(message)s")

import system_utils
system_utils.logger = logging.getLogger("smoke")
system_utils.init_config()  # 冒烟环境手动加载config.ini(应用启动时自动执行)
import relay_hub

received_hr = []
relay_hub.set_hr_callback(lambda ts, bpm: received_hr.append((time.time(), bpm)))
relay_hub.set_alarm_check(lambda: False)
relay_hub.set_direct_check(lambda: False)

async def fake_node():
    import aiohttp
    await asyncio.sleep(1.0)  # 等hub起服务
    session = aiohttp.ClientSession()
    ws = await session.ws_connect("http://127.0.0.1:8899/relay",
                                  headers={"Authorization": "Bearer HRMLink-ESP32-2025"})
    # hello -> 应收到cfg带手环MAC
    await ws.send_str('{"type":"hello","name":"测试卧室","fw":"1.1.0","ip":"127.0.0.1"}')
    msg = await asyncio.wait_for(ws.receive(), 5)
    cfg = msg.json()
    print("<< cfg:", cfg)
    assert cfg["type"] == "cfg", "hello后未收到cfg"
    assert cfg.get("mac"), "cfg未携带手环MAC(config.ini [Device]未读到)"
    band_mac = cfg["mac"]
    # hb -> 仲裁器应自动下发connect命令(空闲自动申请)
    await ws.send_str('{"type":"hb","state":"idle","rssi":-55,"up":10}')
    msg = await asyncio.wait_for(ws.receive(), 8)
    cmd = msg.json()
    print("<< cmd:", cmd)
    assert cmd["type"] == "connect" and cmd.get("mac") == band_mac, "空闲未自动申请连接"
    # 模拟连接成功 -> hr应进入统一入口
    await ws.send_str('{"type":"evt","evt":"ble_up","mac":"%s"}' % band_mac)
    await ws.send_str('{"type":"hr","bpm":72,"rssi":-52,"ts":1}')
    await asyncio.sleep(0.5)
    print("hr回调:", received_hr)
    assert received_hr and received_hr[-1][1] == 72, "心率未进入统一入口"
    # 非法心率应被拒收
    await ws.send_str('{"type":"hr","bpm":9999,"rssi":-52,"ts":2}')
    await asyncio.sleep(0.3)
    assert all(b <= 250 for _, b in received_hr), "非法心率未被过滤"
    # 断开 -> 放手规则: hub应回收连接权并标记离线
    await ws.close()
    await asyncio.sleep(1.0)
    rows = relay_hub.node_table()
    print("节点表:", rows)
    assert rows[0]["state"] == "offline", "断开后未标记离线"
    hub = relay_hub.get_hub()
    assert hub.active_node is None, "WS断开后未回收连接权(放手规则)"
    print("SMOKE_ALL_OK")
    await session.close()

hub = relay_hub.get_hub()
hub.start()
try:
    asyncio.run(fake_node())
    print("RESULT=PASS")
finally:
    relay_hub.stop_relay_hub()
