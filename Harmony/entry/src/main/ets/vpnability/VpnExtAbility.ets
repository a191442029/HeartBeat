import VpnExtensionAbility from '@ohos.app.ability.VpnExtensionAbility';
import { vpnExtension } from '@kit.NetworkKit';
import { connection } from '@kit.NetworkKit';
import { webSocket } from '@kit.NetworkKit';
import { http } from '@kit.NetworkKit';
import notificationManager from '@ohos.notificationManager';
import { liveViewManager } from '@kit.LiveViewKit';
import type Want from '@ohos.app.ability.Want';
import { hilog } from '@kit.PerformanceAnalysisKit';
import { BusinessError } from '@kit.BasicServicesKit';
import { fileIo as fs } from '@kit.CoreFileKit';
import { util } from '@kit.ArkTS';
import backgroundTaskManager from '@ohos.resourceschedule.backgroundTaskManager';
import { wantAgent, WantAgent } from '@kit.AbilityKit';
import testNapi from 'libentry.so';

const TAG: string = 'VpnExtAbility';
const DOMAIN: number = 0x0000;
const KEEP_ALIVE_TASK_ID: number = 1;
const HR_DATA_FILE = '/hr_data.json'; // 心率数据共享文件: 扩展写, UI 读
const WS_RECONNECT_MS = 3000;         // WS 断线 3 秒重连
const HTTP_FALLBACK_MS = 4000;        // WS 断开期间 HTTP 兜底轮询周期
const HR_NOTE_MIN_MS = 5000;          // 常驻心率通知最小更新间隔(系统限流保护)
const HR_NOTE_ID = 60101;             // 常驻心率通知 ID(覆盖更新)
const LV_ID = 60102;                  // 实况窗实例 ID
const LV_UPDATE_MIN_MS = 10000;       // 实况窗最小更新间隔(系统限流保护)

export default class VpnExtAbility extends VpnExtensionAbility {
  private vpnConn: vpnExtension.VpnConnection | null = null;
  private heartbeatTimer: number = -1;
  private connectedIp: string = '';

  // ---- 数据链路(WS + HTTP 兜底): 运行在扩展进程, 有长时任务保活, 不受 UI 冻结影响 ----
  private exeIp: string = '';
  private exePort: string = '8765';
  private wsClient: webSocket.WebSocket | null = null;
  private wsUp: boolean = false;
  private wsReconnectTimer: number = -1;
  private httpTimer: number = -1;

  // 把 VPN 连接状态写到共享文件，供 UI 进程轮询读取
  private writeVpnStatus(status: string, ip: string): void {
    try {
      const path = this.context.filesDir + '/ts_vpn_status.json';
      const content = JSON.stringify({ status: status, ip: ip, ts: Date.now() });
      const file = fs.openSync(path, fs.OpenMode.READ_WRITE | fs.OpenMode.CREATE | fs.OpenMode.TRUNC);
      fs.writeSync(file.fd, content);
      fs.closeSync(file.fd);
      hilog.info(DOMAIN, TAG, 'vpnStatus written: %{public}s', content);
    } catch (e) {
      hilog.error(DOMAIN, TAG, 'writeVpnStatus failed: %{public}s', JSON.stringify(e));
    }
  }

  // 心跳：每 5 秒刷新状态文件时间戳。若进程被杀（SIGKILL）心跳停止，
  // UI 检测到时间戳过期即可判定扩展已死，避免显示虚假的"已连接"。
  private startHeartbeat(): void {
    this.stopHeartbeat();
    this.heartbeatTimer = setInterval(() => {
      this.writeVpnStatus('connected', this.connectedIp);
    }, 5000);
  }

  private stopHeartbeat(): void {
    if (this.heartbeatTimer !== -1) {
      clearInterval(this.heartbeatTimer);
      this.heartbeatTimer = -1;
    }
  }

  // 长时任务保活: dataTransfer 类, 提升扩展进程优先级,
  // 防止用户清理任务/内存回收杀掉 VPN(Tailscale 隧道 + 心跳随进程存活)
  private startKeepAlive(): void {
    try {
      const wantAgentInfo: wantAgent.WantAgentInfo = {
        wants: [
          {
            bundleName: 'com.hrmlink.hrhub',
            abilityName: 'EntryAbility'
          }
        ],
        operationType: wantAgent.OperationType.START_ABILITY,
        requestCode: 0,
        wantAgentFlags: [wantAgent.WantAgentFlags.UPDATE_PRESENT_FLAG]
      };
      wantAgent.getWantAgent(wantAgentInfo).then((agent: WantAgent) => {
        backgroundTaskManager.startBackgroundRunning(
          this.context,
          backgroundTaskManager.BackgroundMode.DATA_TRANSFER,
          agent
        ).then(() => {
          hilog.info(DOMAIN, TAG, 'continuous task started (dataTransfer)');
        }).catch((e: BusinessError) => {
          hilog.error(DOMAIN, TAG, 'startBackgroundRunning failed: %{public}s', JSON.stringify(e));
        });
      }).catch((e: BusinessError) => {
        hilog.error(DOMAIN, TAG, 'wantAgent failed: %{public}s', JSON.stringify(e));
      });
    } catch (e) {
      hilog.error(DOMAIN, TAG, 'startKeepAlive exception: %{public}s', JSON.stringify(e));
    }
  }

  private stopKeepAlive(): void {
    try {
      backgroundTaskManager.stopBackgroundRunning(this.context).then(() => {
        hilog.info(DOMAIN, TAG, 'continuous task stopped');
      }).catch((e: BusinessError) => {
        // 任务可能未申请成功，忽略
        hilog.info(DOMAIN, TAG, 'stopBackgroundRunning: %{public}s', JSON.stringify(e));
      });
    } catch (e) {
      // 任务可能未申请成功，忽略
    }
  }

  onCreate(want: Want): void {
    hilog.info(DOMAIN, TAG, 'onCreate called');

    // 尽早申请长时任务保活(不阻塞后续初始化)
    this.startKeepAlive();

    const filesDir = this.context.filesDir;
    testNapi.setFilesDir(filesDir);
    hilog.info(DOMAIN, TAG, 'filesDir=%{public}s', filesDir);

    // 优先从共享配置文件读取控制面/激活码/密钥路径。
    // VPN 扩展经系统 netmanager 转发拉起时 want.parameters 可能被丢弃，
    // 因此 UI 进程把配置写入 filesDir/ts_vpn_config.json（两进程共享沙箱）。
    let controlUrl: string = '';
    let authKey: string = '';
    let keyFilePath: string = '';
    try {
      const cfgPath = filesDir + '/ts_vpn_config.json';
      const file = fs.openSync(cfgPath, fs.OpenMode.READ_ONLY);
      const stat = fs.statSync(file.fd);
      const buf = new ArrayBuffer(stat.size);
      fs.readSync(file.fd, buf);
      fs.closeSync(file.fd);
      const decoder = util.TextDecoder.create('utf-8');
      const content = decoder.decodeToString(new Uint8Array(buf));
      const cfg = JSON.parse(content) as Record<string, string>;
      controlUrl = cfg.controlUrl ?? '';
      authKey = cfg.authKey ?? '';
      keyFilePath = cfg.keyFilePath ?? '';
      this.exeIp = cfg.exeIp ?? '';
      this.exePort = cfg.exePort ?? '8765';
      hilog.info(DOMAIN, TAG, 'config file loaded, authKey len=%{public}d exeIp=%{public}s', authKey.length, this.exeIp);
    } catch (e) {
      hilog.info(DOMAIN, TAG, 'no config file, fallback to want.parameters');
    }

    // want.parameters 作为回退（同进程直接拉起的场景）
    const params = want.parameters as Record<string, string>;
    if (controlUrl === '') {
      controlUrl = params?.controlUrl ?? '';
    }
    if (authKey === '') {
      authKey = params?.authKey ?? '';
    }
    if (keyFilePath === '') {
      keyFilePath = params?.keyFilePath ?? '';
    }
    hilog.info(DOMAIN, TAG, 'controlUrl=%{public}s keyFilePath=%{public}s', controlUrl, keyFilePath);

    // 报告"启动中"状态
    this.writeVpnStatus('starting', '');

    // 在 VPN 扩展进程中初始化 Tailscale
    const effectiveKeyPath = keyFilePath.length > 0 ? keyFilePath : filesDir + '/tsrs_keys.json';
    hilog.info(DOMAIN, TAG, 'Initializing Tailscale with keyPath=%{public}s', effectiveKeyPath);

    testNapi.tailscaleInit(controlUrl, authKey, effectiveKeyPath).then((ip: string) => {
      hilog.info(DOMAIN, TAG, 'Tailscale connected! tailnet IP = %{public}s', ip);
      this.connectedIp = ip;
      this.writeVpnStatus('connected', ip);
      // 注意：心跳在 TUN 桥接成功后才启动（见 createVpnAndBridge），
      // 否则 TUN 创建失败时心跳仍在刷新 connected 状态，UI 显示虚假的"已连接"。
      this.createVpnAndBridge(ip);
    }).catch((err: string) => {
      hilog.error(DOMAIN, TAG, 'Tailscale init FAILED: %{public}s', err);
      // 上报 error 后 UI 侧会解除 vpnRunning 死锁；扩展进程生命周期由系统管理
      this.writeVpnStatus('error', '');
      this.stopHeartbeat();
    });
  }

  // 统一的失败处理：停心跳 + 上报 error + 销毁 VPN 连接
  private failVpn(msg: string): void {
    hilog.error(DOMAIN, TAG, '%{public}s', msg);
    this.stopHeartbeat();
    this.stopDataLink();
    this.stopHrNotification();
    this.stopLiveView();
    this.writeVpnStatus('error', '');
    if (this.vpnConn) {
      this.vpnConn.destroy().catch((err: BusinessError) => {
        hilog.error(DOMAIN, TAG, 'VPN destroy failed in failVpn: %{public}s', JSON.stringify(err));
      });
    }
  }

  private createVpnAndBridge(tailnetIp: string): void {
    hilog.info(DOMAIN, TAG, 'createVpnAndBridge: tailnetIp=%{public}s', tailnetIp);

    this.vpnConn = vpnExtension.createVpnConnection(this.context);

    // VPN 接口地址
    const vpnAddress: connection.NetAddress = {
      address: tailnetIp,
      family: 1,
      port: 0
    };
    const vpnLinkAddress: connection.LinkAddress = {
      address: vpnAddress,
      prefixLength: 32
    };

    // 路由：100.0.0.0/8 → VPN
    const destAddress: connection.NetAddress = {
      address: '100.0.0.0',
      family: 1,
      port: 0
    };
    const destLinkAddress: connection.LinkAddress = {
      address: destAddress,
      prefixLength: 8
    };
    const gateway: connection.NetAddress = {
      address: tailnetIp,
      family: 1,
      port: 0
    };
    const route: connection.RouteInfo = {
      interface: '',
      destination: destLinkAddress,
      gateway: gateway,
      hasGateway: true,
      isDefaultRoute: false
    };

    const config: vpnExtension.VpnConfig = {
      addresses: [vpnLinkAddress],
      routes: [route],
      dnsAddresses: ['100.100.100.100'],
      mtu: 1280,
      isIPv4Accepted: true,
      isBlocking: true
    };

    hilog.info(DOMAIN, TAG, 'Calling vpnConn.create()...');

    this.vpnConn.create(config).then((fd: number) => {
      hilog.info(DOMAIN, TAG, 'VPN CREATED! TUN fd = %{public}d', fd);
      hilog.info(DOMAIN, TAG, 'Calling vpnBridge(fd)...');

      testNapi.vpnBridge(fd).then((result: number) => {
        hilog.info(DOMAIN, TAG, 'vpnBridge result = %{public}d', result);
        if (result !== 0) {
          this.failVpn('VPN bridge FAILED, destroying VPN');
          return;
        }
        // TUN 桥接成功，才开始心跳上报"已连接"
        this.startHeartbeat();
        // VPN 通路就绪: 启动数据链路(WS→EXE) + 实况窗
        this.startDataLink();
      }).catch((err: BusinessError) => {
        this.failVpn('vpnBridge exception: ' + JSON.stringify(err));
      });
    }).catch((err: BusinessError) => {
      this.failVpn('VPN create FAILED: code=' + err.code + ' msg=' + err.message);
    });
  }

  // ==================== 数据链路(WS + HTTP 兜底) ====================
  // 运行在扩展进程(有长时任务保活), UI 冻结/退出不影响心率接收与实况窗更新

  private startDataLink(): void {
    if (this.exeIp === '') {
      hilog.error(DOMAIN, TAG, 'startDataLink: no exeIp in config, skip');
      return;
    }
    // 先展示"等待连接"常驻通知, 收到数据后由 pushHrNotification 节流更新
    this.startHrNotification();
    this.connectWs();
    // HTTP 兜底: WS 断开期间每 4 秒轮询
    if (this.httpTimer === -1) {
      this.httpTimer = setInterval(() => {
        if (!this.wsUp && this.exeIp !== '') {
          this.pollHttp();
        }
      }, HTTP_FALLBACK_MS);
    }
  }

  private stopDataLink(): void {
    if (this.wsReconnectTimer !== -1) {
      clearTimeout(this.wsReconnectTimer);
      this.wsReconnectTimer = -1;
    }
    if (this.httpTimer !== -1) {
      clearInterval(this.httpTimer);
      this.httpTimer = -1;
    }
    if (this.wsClient) {
      const ws = this.wsClient;
      this.wsClient = null;
      try {
        ws.off('open');
        ws.off('message');
        ws.off('close');
        ws.off('error');
        ws.close(() => {
        });
      } catch (e) {
        // 忽略关闭异常
      }
    }
    this.wsUp = false;
  }

  private wsUrl(): string {
    return 'ws://' + this.exeIp + ':' + this.exePort + '/ws';
  }

  private connectWs(): void {
    if (this.wsUp || this.wsClient) {
      return;
    }
    // 重连前重读配置: 用户在设置页改了 IP/端口无需重启扩展
    this.reloadExeConfig();
    if (this.exeIp === '') {
      return;
    }
    const ws = webSocket.createWebSocket();
    this.wsClient = ws;
    ws.on('open', (): void => {
      this.wsUp = true;
      hilog.info(DOMAIN, TAG, 'WS connected: %{public}s', this.wsUrl());
    });
    ws.on('message', (_err: BusinessError, value: string | ArrayBuffer): void => {
      if (typeof value === 'string') {
        this.onHrMessage(value, 'WS');
      }
    });
    ws.on('close', (): void => {
      this.wsUp = false;
      this.scheduleReconnect();
    });
    ws.on('error', (): void => {
      this.wsUp = false;
      this.scheduleReconnect();
    });
    ws.connect(this.wsUrl(), (err: BusinessError) => {
      if (err) {
        hilog.error(DOMAIN, TAG, 'WS connect failed: %{public}s', JSON.stringify(err));
        this.wsUp = false;
        this.scheduleReconnect();
      }
    });
  }

  private scheduleReconnect(): void {
    if (this.wsReconnectTimer !== -1) {
      return; // 已排程
    }
    this.wsReconnectTimer = setTimeout(() => {
      this.wsReconnectTimer = -1;
      if (this.wsClient) {
        // 旧 socket 清理后重连
        const old = this.wsClient;
        this.wsClient = null;
        try {
          old.off('open');
          old.off('message');
          old.off('close');
          old.off('error');
          old.close(() => {
          });
        } catch (e) {
          // 忽略
        }
      }
      this.connectWs();
    }, WS_RECONNECT_MS);
  }

  private reloadExeConfig(): void {
    try {
      const cfgPath = this.context.filesDir + '/ts_vpn_config.json';
      const file = fs.openSync(cfgPath, fs.OpenMode.READ_ONLY);
      const stat = fs.statSync(file.fd);
      const buf = new ArrayBuffer(stat.size);
      fs.readSync(file.fd, buf);
      fs.closeSync(file.fd);
      const decoder = util.TextDecoder.create('utf-8');
      const content = decoder.decodeToString(new Uint8Array(buf));
      const cfg = JSON.parse(content) as Record<string, string>;
      this.exeIp = cfg.exeIp ?? '';
      this.exePort = cfg.exePort ?? '8765';
    } catch (e) {
      // 配置读取失败沿用旧值
    }
  }

  private pollHttp(): void {
    const req = http.createHttp();
    req.request(`http://${this.exeIp}:${this.exePort}/api/heartrate`, {
      method: http.RequestMethod.GET,
      connectTimeout: 4000,
      readTimeout: 4000
    }, (err: BusinessError, data: http.HttpResponse) => {
      if (!err && data.responseCode === 200 && typeof data.result === 'string') {
        this.onHrMessage(data.result, 'POLL');
      }
      req.destroy();
    });
  }

  // 统一处理 WS / HTTP 数据: 写共享文件(供 UI 读) + 节流更新实况窗
  private onHrMessage(raw: string, src: string): void {
    try {
      const p = JSON.parse(raw) as Record<string, Object>;
      const hr = (p.heart_rate as number) ?? 0;
      const alarm = (p.alarm as boolean) === true;
      const info = (p.info as string) ?? '';
      const tsStr = (p.timestamp as string) ?? '';
      // 写心率数据共享文件: UI 1 秒轮询读取渲染主界面
      try {
        const path = this.context.filesDir + HR_DATA_FILE;
        const content = JSON.stringify({
          heart_rate: hr, timestamp: tsStr, status: (p.status as string) ?? '',
          info: info, alarm: alarm, src: src, ts: Date.now()
        });
        const file = fs.openSync(path, fs.OpenMode.READ_WRITE | fs.OpenMode.CREATE | fs.OpenMode.TRUNC);
        fs.writeSync(file.fd, content);
        fs.closeSync(file.fd);
      } catch (e) {
        // 写文件失败不影响实况窗
      }
      this.presentHr(hr, info, alarm);
    } catch (e) {
      // 非 JSON 消息忽略
    }
  }

  // ==================== 心率呈现(实况窗优先, 常驻通知兜底) ====================
  // 实况窗(灵动岛)需 AGC "实况窗权益"; 未通过时自动回退常驻通知。
  // 上架且权益生效后无需改代码, 启动探测成功即切换灵动岛形态。

  private lvAvailable: boolean | null = null; // null=未探测 true=实况窗可用 false=回退通知
  private lvStarted: boolean = false;
  private lvLastUpdateMs: number = 0;
  private lvLastHr: number = -1;
  private lvLastAlarm: boolean = false;

  // 统一呈现入口: 每条心率数据调用
  private presentHr(hr: number, info: string, alarm: boolean): void {
    if (this.lvAvailable === true) {
      this.pushLiveView(hr, info, alarm);
      return;
    }
    if (this.lvAvailable === false) {
      this.pushHrNotification(hr, info, alarm);
      return;
    }
    // 未探测: 先发常驻通知保底, 同时探测实况窗; 探测成功后取消通知
    this.pushHrNotification(hr, info, alarm);
    this.probeLiveView(hr, info, alarm);
  }

  private lvWantAgent: WantAgent | null = null;

  private ensureLvAgent(): void {
    if (this.lvWantAgent) {
      return;
    }
    try {
      const info: wantAgent.WantAgentInfo = {
        wants: [{ bundleName: 'com.hrmlink.hrhub', abilityName: 'EntryAbility' }],
        operationType: wantAgent.OperationType.START_ABILITY,
        requestCode: 0,
        wantAgentFlags: [wantAgent.WantAgentFlags.UPDATE_PRESENT_FLAG]
      };
      wantAgent.getWantAgent(info).then((agent: WantAgent) => {
        this.lvWantAgent = agent;
      }).catch(() => {
      });
    } catch (e) {
      // 忽略
    }
  }

  private buildLiveView(hrText: string, subText: string, color: string): liveViewManager.LiveView {
    const primary: liveViewManager.PrimaryData = {
      title: '心率监测',
      content: [{ text: hrText + ' ' + subText, textColor: '#FFFFFFFF' }],
      keepTime: 0,
      layoutData: { layoutType: liveViewManager.LayoutType.LAYOUT_TYPE_DEFAULT }
    };
    if (this.lvWantAgent) {
      primary.clickAction = this.lvWantAgent;
    }
    const data: liveViewManager.LiveViewData = {
      primary: primary,
      capsule: {
        type: liveViewManager.CapsuleType.CAPSULE_TYPE_TEXT,
        status: 1,
        icon: 'heart_lv.png',
        title: hrText,
        content: subText,
        backgroundColor: color,
        textColor: '#FFFFFFFF'
      } as liveViewManager.TextCapsule
    };
    const lv: liveViewManager.LiveView = {
      id: LV_ID,
      event: 'WORKOUT',
      isMute: true,
      liveViewData: data
    };
    return lv;
  }

  // 探测实况窗权益: 首条数据时尝试创建; 成功切换灵动岛并撤掉常驻通知
  private probeLiveView(hr: number, info: string, alarm: boolean): void {
    if (this.lvProbeInFlight) {
      return;
    }
    this.ensureLvAgent();
    if (!this.lvWantAgent) {
      return; // agent 未就绪, 下条数据再试
    }
    this.lvProbeInFlight = true;
    try {
      liveViewManager.startLiveView(this.buildLiveView('--', '等待连接', '#CC14141A'))
        .then(() => {
          this.lvAvailable = true;
          this.lvStarted = true;
          hilog.info(DOMAIN, TAG, 'live view available: capsule mode ON');
          // 灵动岛生效, 撤掉常驻通知避免双重呈现
          this.stopHrNotification();
          // 立即推首条真实数据
          this.pushLiveView(hr, info, alarm);
        }).catch((e: BusinessError) => {
          this.lvAvailable = false;
          hilog.info(DOMAIN, TAG, 'live view unavailable (AGC equity pending?), fallback to notification: %{public}s', JSON.stringify(e));
        }).finally(() => {
          this.lvProbeInFlight = false;
        });
    } catch (e) {
      this.lvAvailable = false;
      this.lvProbeInFlight = false;
      hilog.error(DOMAIN, TAG, 'probe live view exception: %{public}s', JSON.stringify(e));
    }
  }

  private lvProbeInFlight: boolean = false;

  // 节流更新实况窗: 心率变化>=3 或报警变化 或 距上次>=10秒
  private pushLiveView(hr: number, info: string, alarm: boolean): void {
    const now = Date.now();
    const significant = this.lvLastHr < 0 || Math.abs(hr - this.lvLastHr) >= 3 || alarm !== this.lvLastAlarm;
    if (this.lvStarted && !significant && now - this.lvLastUpdateMs < LV_UPDATE_MIN_MS) {
      return;
    }
    this.lvLastUpdateMs = now;
    this.lvLastHr = hr;
    this.lvLastAlarm = alarm;
    const hrText = hr > 0 ? hr.toString() : '--';
    const subText = alarm ? '警报!' : (info !== '' ? info : 'BPM');
    const bg = alarm ? '#CCD32F2F' : '#CC14141A';
    const lv = this.buildLiveView(hrText, subText, bg);
    try {
      if (this.lvStarted) {
        liveViewManager.updateLiveView(lv).catch((e: BusinessError) => {
          hilog.error(DOMAIN, TAG, 'updateLiveView failed: %{public}s', JSON.stringify(e));
        });
      } else {
        liveViewManager.startLiveView(lv).then(() => {
          this.lvStarted = true;
        }).catch((e: BusinessError) => {
          hilog.error(DOMAIN, TAG, 'startLiveView failed: %{public}s', JSON.stringify(e));
        });
      }
    } catch (e) {
      hilog.error(DOMAIN, TAG, 'pushLiveView exception: %{public}s', JSON.stringify(e));
    }
  }

  private stopLiveView(): void {
    if (!this.lvStarted) {
      return;
    }
    this.lvStarted = false;
    this.lvLastHr = -1;
    try {
      const lv: liveViewManager.LiveView = {
        id: LV_ID,
        event: 'WORKOUT',
        isMute: true,
        liveViewData: {
          primary: {
            title: '心率监测', content: [{ text: '已结束' }], keepTime: 0,
            layoutData: { layoutType: liveViewManager.LayoutType.LAYOUT_TYPE_DEFAULT }
          }
        }
      };
      liveViewManager.stopLiveView(lv).then(() => {
        hilog.info(DOMAIN, TAG, 'live view stopped');
      }).catch(() => {
      });
    } catch (e) {
      // 忽略
    }
  }

  // ==================== 常驻心率通知(兜底形态) ====================
  // 实况窗权益未通过时的兜底: 常驻通知卡片实时更新/锁屏可见
  // 同 ID publish 覆盖更新; 5 秒节流 + 显著变化立即更新

  private noteWantAgent: WantAgent | null = null;
  private noteShown: boolean = false;
  private noteLastUpdateMs: number = 0;
  private noteLastHr: number = -1;
  private noteLastAlarm: boolean = false;

  private ensureNoteAgent(): void {
    if (this.noteWantAgent) {
      return;
    }
    try {
      const info: wantAgent.WantAgentInfo = {
        wants: [{ bundleName: 'com.hrmlink.hrhub', abilityName: 'EntryAbility' }],
        operationType: wantAgent.OperationType.START_ABILITY,
        requestCode: 0,
        wantAgentFlags: [wantAgent.WantAgentFlags.UPDATE_PRESENT_FLAG]
      };
      wantAgent.getWantAgent(info).then((agent: WantAgent) => {
        this.noteWantAgent = agent;
      }).catch(() => {
      });
    } catch (e) {
      // 忽略
    }
  }

  private buildHrNote(title: string, text: string): notificationManager.NotificationRequest {
    const req: notificationManager.NotificationRequest = {
      id: HR_NOTE_ID,
      isUnremovable: true, // 常驻, 用户不可侧滑清除
      wantAgent: this.noteWantAgent ?? undefined, // 点击通知回应用(顶层字段)
      content: {
        notificationContentType: notificationManager.ContentType.NOTIFICATION_CONTENT_BASIC_TEXT,
        normal: {
          title: title,
          text: text,
          additionalText: ''
        }
      }
    };
    return req;
  }

  private publishHrNote(title: string, text: string): void {
    try {
      notificationManager.publish(this.buildHrNote(title, text)).catch((e: BusinessError) => {
        hilog.error(DOMAIN, TAG, 'publish hr note failed: %{public}s', JSON.stringify(e));
      });
    } catch (e) {
      hilog.error(DOMAIN, TAG, 'publish hr note exception: %{public}s', JSON.stringify(e));
    }
  }

  // 数据链路启动即发"等待连接"通知
  private startHrNotification(): void {
    this.ensureNoteAgent();
    if (!this.noteWantAgent) {
      setTimeout(() => {
        this.startHrNotification();
      }, 1000);
      return;
    }
    if (this.noteShown) {
      return;
    }
    this.noteShown = true;
    this.publishHrNote('心率监测', '等待连接 EXE...');
  }

  // 节流: 心率变化>=3 或报警变化 或 距上次>=5秒
  private pushHrNotification(hr: number, info: string, alarm: boolean): void {
    if (!this.noteShown) {
      return;
    }
    const now = Date.now();
    const significant = this.noteLastHr < 0 || Math.abs(hr - this.noteLastHr) >= 3 || alarm !== this.noteLastAlarm;
    if (!significant && now - this.noteLastUpdateMs < HR_NOTE_MIN_MS) {
      return;
    }
    this.noteLastUpdateMs = now;
    this.noteLastHr = hr;
    this.noteLastAlarm = alarm;
    const title = hr > 0 ? `❤ ${hr} BPM` : '心率监测';
    const text = alarm ? `⚠ 心率警报!` : (info !== '' ? info : '心率接收中');
    this.publishHrNote(title, text);
  }

  private stopHrNotification(): void {
    if (!this.noteShown) {
      return;
    }
    this.noteShown = false;
    this.noteLastHr = -1;
    try {
      notificationManager.cancel(HR_NOTE_ID).catch(() => {
      });
    } catch (e) {
      // 忽略
    }
  }

  onDestroy(): void {
    hilog.info(DOMAIN, TAG, 'onDestroy called');
    this.stopKeepAlive();
    this.stopHeartbeat();
    this.stopDataLink();
    this.stopHrNotification();
    this.stopLiveView();
    this.writeVpnStatus('stopped', '');
    if (this.vpnConn) {
      this.vpnConn.destroy().then(() => {
        hilog.info(DOMAIN, TAG, 'VPN destroyed OK');
        testNapi.tailscaleDeinit();
      }).catch((err: BusinessError) => {
        hilog.error(DOMAIN, TAG, 'VPN destroy failed: %{public}s', JSON.stringify(err));
      });
    } else {
      testNapi.tailscaleDeinit();
    }
  }
}
