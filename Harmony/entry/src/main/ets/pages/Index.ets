import { vpnExtension } from '@kit.NetworkKit';
import { preferences } from '@kit.ArkData';
import { fileIo as fs } from '@kit.CoreFileKit';
import { util } from '@kit.ArkTS';
import type Want from '@ohos.app.ability.Want';
import { BusinessError } from '@kit.BasicServicesKit';
import { common } from '@kit.AbilityKit';
import { media } from '@kit.MediaKit';
import notificationManager from '@ohos.notificationManager';
import { audio } from '@kit.AudioKit';
import resourceManager from '@ohos.resourceManager';

const BUNDLE_NAME = 'com.hrmlink.hrhub';
const VPN_STATUS_FILE = '/ts_vpn_status.json';
const VPN_CONFIG_FILE = '/ts_vpn_config.json';
const KEY_FILE = '/tsrs_keys.json';
const HEARTBEAT_TIMEOUT_MS = 15000;   // 扩展每 5 秒心跳，超 15 秒视为扩展已死
const WS_RECONNECT_MS = 3000;         // WS 断线 3 秒后重连
const HTTP_FALLBACK_MS = 4000;        // WS 断开期间 HTTP 兜底轮询周期
const START_GRACE_MS = 90000;         // 启动宽限期: 覆盖 tailscaleInit 最长 60 秒 + TUN 桥接

// 安卓接收端同款配色
const COL_BG = '#14141A';
const COL_STATUS_TEXT = '#99FFFFFF';
const COL_DOT_WS = '#4CAF50';     // 绿: WS 已连接
const COL_DOT_POLL = '#FFC107';   // 黄: HTTP 兜底有数据
const COL_DOT_TIMEOUT = '#9E9E9E';// 灰: 无数据/断连
const COL_TIME = '#CCCCCC';
const COL_INFO = '#FFA726';       // 橙: 重连/状态提示
const COL_LINE = '#FF6B6B';       // 波形折线(EXE 同款)
const COL_GRID = '#30FFFFFF';
const COL_LABEL = '#B3FFFFFF';

// VPN 扩展进程写入 ts_vpn_status.json 的结构
interface VpnStatus {
  status: string;
  ip: string;
  ts: number;
}

// EXE webpush_server 推送的 JSON（WS 与 HTTP 兜底同构）
interface HrPayload {
  heart_rate?: number;
  timestamp?: string;
  status?: string;
  device?: string;
  info?: string;
  alarm?: boolean;
}

// 扩展进程写入 hr_data.json 的结构(推送字段 + 本地元数据)
interface HrDataFile extends HrPayload {
  src?: string;
  ts?: number;
}

// 心率区间颜色(EXE波形区间同款): <60蓝 60-100绿 100-120橙 >120红
function zoneColor(hr: number): string {
  if (hr < 60) {
    return '#3498DB';
  }
  if (hr <= 100) {
    return '#2ECC71';
  }
  if (hr <= 120) {
    return '#F39C12';
  }
  return '#E74C3C';
}

// 波形区间背景带
interface Band {
  lo: number;
  hi: number;
  color: string;
}

@Entry
@Component
struct Index {
  // ---- 配置 ----
  @State isSetupDone: boolean = false;
  @State editingSetup: boolean = false;
  @State controlUrl: string = '';
  @State authKey: string = '';
  @State exeIp: string = '';
  @State exePort: string = '8765';
  @State hasKeyFile: boolean = false;
  @State statusMsg: string = '';
  @State hrFontSize: number = 88;
  @State timeFontSize: number = 20;
  @State axisFontSize: number = 16;

  // ---- VPN 状态 ----
  @State vpnRunning: boolean = false;
  @State vpnConnected: boolean = false;
  @State vpnIp: string = '';

  // ---- 主界面显示(对齐安卓) ----
  @State clockText: string = '';
  @State statusText: string = '未启动';
  @State dotColor: string = COL_DOT_TIMEOUT;
  @State heartRate: number = 0;
  @State hrColor: string = COL_DOT_TIMEOUT;
  @State hrTime: string = '--:--:--';
  @State hrInfo: string = '';
  @State hrAlarm: boolean = false;
  @State gotData: boolean = false; // 最近是否收到过有效心率(区分黄点)

  private filesDir: string = '';
  private statusTimerId: number = -1;
  private vpnStartAt: number = 0; // 本次 startVpn 时刻: 早于它的状态文件都是上一轮残留
  private vpnAutoRetry: number = 0;    // 扩展死亡自动重拉计数(防死循环上限3)
  private userStoppedVpn: boolean = false; // 用户主动停止后, 本次会话不再自动拉起
  private clockTimerId: number = -1;

  // 波形数据(60 秒窗口, 安卓 HeartWaveView 同款)
  private hrData: number[] = [];
  private readonly MAX_POINTS: number = 60;
  private waveSettings: RenderingContextSettings = new RenderingContextSettings(true);
  private waveCtx: CanvasRenderingContext2D = new CanvasRenderingContext2D(this.waveSettings);
  private waveReady: boolean = false;

  // 报警音(EXE 远程报警, 安卓 AlarmPlayer 同款语义)
  private alarmPlayer: media.AVPlayer | null = null;
  private alarmStopTimer: number = -1;

  aboutToAppear(): void {
    this.loadConfig();
    this.startClock();
    this.requestNotificationAuth();
  }

  // 请求通知授权: 实况窗(灵动岛)依赖通知通道发布
  private requestNotificationAuth(): void {
    try {
      notificationManager.requestEnableNotification(getContext(this) as common.UIAbilityContext)
        .catch((e: BusinessError) => {
          // 用户拒绝或已授权过(不再弹)都静默
        });
    } catch (e) {
      // 忽略
    }
  }

  aboutToDisappear(): void {
    this.stopStatusPolling();
    this.stopClock();
    this.stopAlarmSound();
  }

  // ---- 报警音(EXE 远程报警): alarm.mp3 闹钟音量流循环播放, 10 秒自动停; 幂等 ----
  private startAlarmSound(): void {
    if (this.alarmPlayer) {
      return; // 已在响
    }
    try {
      media.createAVPlayer().then((player: media.AVPlayer) => {
        getContext(this).resourceManager.getRawFd('alarm.mp3')
          .then((rawFd: resourceManager.RawFileDescriptor) => {
            player.fdSrc = { fd: rawFd.fd, offset: rawFd.offset, length: rawFd.length };
            player.audioRendererInfo = {
              usage: audio.StreamUsage.STREAM_USAGE_ALARM,
              rendererFlags: 0
            };
            player.loop = true; // 无限循环, 由 10 秒定时器停止
            player.prepare().then(() => {
              player.play().then(() => {
                this.alarmPlayer = player;
                this.alarmStopTimer = setTimeout(() => {
                  this.stopAlarmSound();
                }, 10000);
              }).catch(() => {
                player.release();
              });
            }).catch(() => {
              player.release();
            });
          }).catch(() => {
            player.release();
          });
      }).catch(() => {
        // 创建播放器失败静默, 不干扰主流程
      });
    } catch (e) {
      // 静默
    }
  }

  private stopAlarmSound(): void {
    if (this.alarmStopTimer !== -1) {
      clearTimeout(this.alarmStopTimer);
      this.alarmStopTimer = -1;
    }
    const player = this.alarmPlayer;
    this.alarmPlayer = null;
    if (player) {
      try {
        player.stop();
        player.release();
      } catch (e) {
        // 忽略
      }
    }
  }

  // ==================== 配置 ====================

  private async loadConfig(): Promise<void> {
    try {
      const context = getContext(this) as common.UIAbilityContext;
      this.filesDir = context.filesDir;
      this.hasKeyFile = fs.accessSync(this.filesDir + KEY_FILE);
      const prefs = await preferences.getPreferences(context, 'hrhub_config');
      this.controlUrl = (await prefs.get('controlUrl', '')).toString();
      this.authKey = (await prefs.get('authKey', '')).toString();
      this.exeIp = (await prefs.get('exeIp', '')).toString();
      this.exePort = (await prefs.get('exePort', '8765')).toString();
      this.isSetupDone = (await prefs.get('isSetupDone', false)) as boolean;
      this.vpnRunning = (await prefs.get('vpnRunning', false)) as boolean;
      this.hrFontSize = (await prefs.get('hrFontSize', 88)) as number;
      this.timeFontSize = (await prefs.get('timeFontSize', 20)) as number;
      this.axisFontSize = (await prefs.get('axisFontSize', 16)) as number;
      if (this.isSetupDone) {
        // 打开即连: 上次在跑→恢复轮询; 未在跑→自动拉起 VPN(已授权过不会弹窗)
        if (this.vpnRunning) {
          this.startStatusPolling();
        } else {
          this.startVpn();
        }
      }
    } catch (e) {
      this.statusText = '读取配置失败';
    }
  }

  private async saveConfig(key: string, value: string | boolean | number): Promise<void> {
    try {
      const context = getContext(this) as common.UIAbilityContext;
      const prefs = await preferences.getPreferences(context, 'hrhub_config');
      await prefs.put(key, value);
      await prefs.flush();
    } catch (e) {
      // 忽略持久化失败
    }
  }

  private async saveSettings(): Promise<void> {
    const ip = this.exeIp.trim();
    const key = this.authKey.trim();
    let port = this.exePort.trim();
    if (port === '') {
      port = '8765';
    }
    if (ip === '') {
      this.statusMsg = '请填写电脑的 Tailscale IP';
      return;
    }
    const portNum = Number(port);
    if (Number.isNaN(portNum) || portNum <= 0 || portNum > 65535) {
      this.statusMsg = '端口号无效';
      return;
    }
    // VPN 参数(控制面/激活码)有效值变化才重启 VPN; IP/端口只影响数据链路
    const oldControlUrl = this.controlUrl.trim();
    const oldAuthKey = this.authKey.trim();
    const firstSetup = !this.isSetupDone;
    const vpnWasRunning = this.vpnRunning;
    this.controlUrl = this.controlUrl.trim();
    this.exeIp = ip;
    this.exePort = port;
    this.authKey = key;
    await this.saveConfig('controlUrl', this.controlUrl);
    await this.saveConfig('authKey', this.authKey);
    await this.saveConfig('exeIp', this.exeIp);
    await this.saveConfig('exePort', this.exePort);
    await this.saveConfig('hrFontSize', this.hrFontSize);
    await this.saveConfig('timeFontSize', this.timeFontSize);
    await this.saveConfig('axisFontSize', this.axisFontSize);
    await this.saveConfig('isSetupDone', true);
    this.isSetupDone = true;
    this.editingSetup = false;
    this.statusMsg = '';
    // 数据链路(IP/端口)变化: 扩展进程每次 WS 重连前都会重读共享配置, 无需重启扩展
    const vpnChanged = this.controlUrl !== oldControlUrl || this.authKey !== oldAuthKey;
    if (firstSetup) {
      // 首次保存: 自动启动 VPN
      this.startVpn();
    } else if (vpnChanged) {
      // VPN 参数变化: 重启扩展生效
      if (vpnWasRunning) {
        await this.stopVpn();
      }
      this.startVpn();
    }
  }

  // ==================== VPN 管理 ====================
  // 铁律：UI 进程绝不调 tailscaleInit。一律拉起 VPN 扩展进程，
  // UI 只轮询 ts_vpn_status.json（心跳时间戳超 15 秒 = 扩展已死）。

  private startVpn(): void {
    // 记录启动时刻: 状态文件时间戳早于它 = 上一轮残留数据, 不参与死活判断
    this.vpnStartAt = Date.now();
    // 配置写入共享文件：VPN 扩展经系统 netmanager 转发拉起时
    // want.parameters 会被丢弃，扩展进程从 filesDir 读配置（两进程共享沙箱）
    try {
      const cfgPath = this.filesDir + VPN_CONFIG_FILE;
      const content = JSON.stringify({
        controlUrl: this.controlUrl,
        authKey: this.authKey.trim(),
        keyFilePath: this.filesDir + KEY_FILE,
        exeIp: this.exeIp,
        exePort: this.exePort
      });
      const file = fs.openSync(cfgPath, fs.OpenMode.READ_WRITE | fs.OpenMode.CREATE | fs.OpenMode.TRUNC);
      fs.writeSync(file.fd, content);
      fs.closeSync(file.fd);
    } catch (e) {
      this.statusText = '写入 VPN 配置失败';
      return;
    }
    const want: Want = {
      bundleName: BUNDLE_NAME,
      abilityName: 'VpnExtAbility',
      parameters: {
        'controlUrl': this.controlUrl,
        'authKey': this.authKey.trim(),
        'keyFilePath': this.filesDir + KEY_FILE
      }
    };
    this.statusText = 'VPN 启动中...';
    this.dotColor = COL_DOT_TIMEOUT;
    vpnExtension.startVpnExtensionAbility(want).then(() => {
      this.vpnRunning = true;
      this.saveConfig('vpnRunning', true);
      this.startStatusPolling();
    }).catch((err: BusinessError) => {
      this.statusText = 'VPN 启动失败: ' + err.message;
    });
  }

  // 扩展死亡自动重拉: 上限 3 次, 防止反复失败死循环; 超限后提示手动处理
  private autoRetryVpn(reason: string): void {
    if (this.userStoppedVpn) {
      this.statusText = 'VPN 已停止';
      this.dotColor = COL_DOT_TIMEOUT;
      return;
    }
    if (this.vpnAutoRetry < 3) {
      this.vpnAutoRetry++;
      this.statusText = reason + '，自动重试 (' + this.vpnAutoRetry + '/3)...';
      this.startVpn();
    } else {
      this.statusText = reason + '多次，请检查网络后到设置页手动启动';
      this.dotColor = COL_DOT_TIMEOUT;
    }
  }

  private async stopVpn(): Promise<void> {
    // 用户主动停止: 本次会话不再自动拉起(冷启动仍会自动连)
    this.userStoppedVpn = true;
    const want: Want = {
      bundleName: BUNDLE_NAME,
      abilityName: 'VpnExtAbility'
    };
    try {
      await vpnExtension.stopVpnExtensionAbility(want);
    } catch (e) {
      // 扩展可能已死，忽略
    }
    this.vpnRunning = false;
    this.vpnConnected = false;
    this.vpnIp = '';
    this.saveConfig('vpnRunning', false);
    this.stopStatusPolling();
    this.statusText = '未启动';
    this.dotColor = COL_DOT_TIMEOUT;
  }

  // ---- VPN 状态轮询（每 2 秒读状态文件）----

  private startStatusPolling(): void {
    this.stopStatusPolling();
    this.statusTimerId = setInterval(() => {
      this.readVpnStatus();
    }, 1000); // 1 秒: 兼顾 VPN 状态与心率数据渲染流畅度
  }

  private stopStatusPolling(): void {
    if (this.statusTimerId !== -1) {
      clearInterval(this.statusTimerId);
      this.statusTimerId = -1;
    }
  }

  private readVpnStatus(): void {
    try {
      const path = this.filesDir + VPN_STATUS_FILE;
      const file = fs.openSync(path, fs.OpenMode.READ_ONLY);
      const stat = fs.statSync(file.fd);
      const buf = new ArrayBuffer(stat.size);
      fs.readSync(file.fd, buf);
      fs.closeSync(file.fd);
      const decoder = util.TextDecoder.create('utf-8');
      const content = decoder.decodeToString(new Uint8Array(buf));
      const obj = JSON.parse(content) as VpnStatus;
      // 早于本次启动时刻的状态 = 上一轮残留, 不可信
      const stale = obj.ts < this.vpnStartAt;
      const now = Date.now();
      if (stale) {
        if (now - this.vpnStartAt > START_GRACE_MS) {
          // 宽限期已过仍无新心跳 = 扩展确实没起来 → 自动重拉
          this.vpnConnected = false;
          this.vpnIp = '';
          this.vpnRunning = false;
          this.saveConfig('vpnRunning', false);
          this.autoRetryVpn('VPN 扩展未响应');
        } else {
          // 宽限期内: 扩展可能在初始化(Tailscale 连接最长 60 秒)
          this.statusText = 'VPN 启动中...';
        }
        return;
      }
      // 心跳校验：扩展每 5 秒刷新时间戳，超 15 秒未更新 = 扩展进程已死 → 自动重拉
      const age = now - obj.ts;
      if (age > HEARTBEAT_TIMEOUT_MS) {
        this.vpnConnected = false;
        this.vpnIp = '';
        this.vpnRunning = false;
        this.saveConfig('vpnRunning', false);
        this.autoRetryVpn('VPN 扩展已停止');
        return;
      }
      if (obj.status === 'connected') {
        this.vpnIp = obj.ip;
        if (!this.vpnConnected) {
          this.vpnConnected = true;
          this.statusText = 'VPN 已连接，等待心率数据...';
        }
        // VPN 在线: 心率数据由扩展进程收(WS/HTTP), 此处读共享文件渲染
        this.readHrData();
      } else if (obj.status === 'starting') {
        this.vpnConnected = false;
        this.vpnIp = '';
        this.statusText = 'VPN 启动中...';
      } else if (obj.status === 'error') {
        // error 必须解除 vpnRunning，否则 UI 永久卡在"启动中"且无法重试
        this.vpnConnected = false;
        this.vpnIp = '';
        this.statusText = 'VPN 启动失败（检查激活码/网络），请重试';
        if (this.vpnRunning) {
          this.vpnRunning = false;
          this.saveConfig('vpnRunning', false);
        }
      } else if (obj.status === 'stopped') {
        this.vpnConnected = false;
        this.vpnIp = '';
      }
    } catch (e) {
      // 状态文件不存在或还没写入，保持现状
    }
  }

  // ==================== 心率数据渲染(读扩展进程共享文件) ====================
  // 数据链路(WS/HTTP 兜底/实况窗)在 VPN 扩展进程内运行, UI 只读 hr_data.json 渲染

  private readHrData(): void {
    try {
      const path = this.filesDir + '/hr_data.json';
      const file = fs.openSync(path, fs.OpenMode.READ_ONLY);
      const stat = fs.statSync(file.fd);
      const buf = new ArrayBuffer(stat.size);
      fs.readSync(file.fd, buf);
      fs.closeSync(file.fd);
      const decoder = util.TextDecoder.create('utf-8');
      const content = decoder.decodeToString(new Uint8Array(buf));
      const obj = JSON.parse(content) as HrDataFile;
      // 时效: 扩展 6 秒没写新数据 = EXE 断连/VPN 断
      const ts = obj.ts ?? 0;
      if (Date.now() - ts > 6000) {
        this.dotColor = COL_DOT_TIMEOUT;
        this.statusText = '无数据（检查 EXE 推送服务）';
        return;
      }
      const hr = obj.heart_rate ?? 0;
      const src = obj.src ?? 'WS';
      if (hr > 0) {
        this.heartRate = hr;
        this.gotData = true;
        this.pushWave(hr);
        this.hrColor = zoneColor(hr);
        // 状态点: 绿=WS 黄=HTTP 轮询(安卓同款)
        this.dotColor = src === 'WS' ? COL_DOT_WS : COL_DOT_POLL;
        this.statusText = src === 'WS' ? 'WS 已连接 (' + this.exeIp + ')' : 'HTTP 轮询 (' + this.exeIp + ')';
      } else {
        this.hrColor = COL_DOT_TIMEOUT;
        this.dotColor = src === 'WS' ? COL_DOT_WS : COL_DOT_POLL;
      }
      // 时间显示 EXE 数据包里的 timestamp(PC 侧生成), 仅取 HH:mm:ss
      const tsStr = obj.timestamp ?? '';
      this.hrTime = tsStr.length > 8 ? tsStr.substring(tsStr.length - 8) : '--:--:--';
      this.hrInfo = obj.info ?? '';
      this.hrAlarm = obj.alarm === true;
      // 报警音: true=循环响铃(10秒自动停), false=立即停(安卓 AlarmPlayer 同款)
      if (this.hrAlarm) {
        this.startAlarmSound();
      } else {
        this.stopAlarmSound();
      }
    } catch (e) {
      // 文件不存在或未就绪, 保持现状
    }
  }

  // ---- 标题栏时钟(安卓同款: 每秒刷新) ----
  private startClock(): void {
    this.stopClock();
    const fmt = (d: Date): string => {
      const p = (n: number): string => n < 10 ? '0' + n : n.toString();
      return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} `
        + `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
    };
    this.clockText = fmt(new Date());
    this.clockTimerId = setInterval(() => {
      this.clockText = fmt(new Date());
    }, 1000);
  }

  private stopClock(): void {
    if (this.clockTimerId !== -1) {
      clearInterval(this.clockTimerId);
      this.clockTimerId = -1;
    }
  }

  // ==================== 波形(对齐安卓 HeartWaveView) ====================

  private pushWave(hr: number): void {
    this.hrData.push(hr);
    if (this.hrData.length > this.MAX_POINTS) {
      this.hrData.splice(0, this.hrData.length - this.MAX_POINTS);
    }
    this.drawWave();
  }

  private clearWave(): void {
    this.hrData = [];
    this.drawWave();
  }

  private drawWave(): void {
    if (!this.waveReady) {
      return;
    }
    const ctx = this.waveCtx;
    const w = ctx.width;
    const h = ctx.height;
    if (w <= 0 || h <= 0) {
      return;
    }
    // 留白随刻度字号自适应; Y 刻度右对齐贴波形区, 左留白只需容纳文字宽度
    const axisFont = this.axisFontSize;
    const gutterL = 12 + Math.round(axisFont * 1.3);
    const gutterB = 14 + Math.round(axisFont * 0.8);
    const x0 = gutterL;
    const plotW = w - x0;
    const plotB = h - gutterB;

    ctx.clearRect(0, 0, w, h);

    const n = this.hrData.length;
    // Y 量程(EXE 同款): 无数据默认 40~200, 自适应±25% margin
    let mn = Number.MAX_VALUE;
    let mx = Number.MIN_VALUE;
    for (const v of this.hrData) {
      if (v > 0) {
        if (v < mn) {
          mn = v;
        }
        if (v > mx) {
          mx = v;
        }
      }
    }
    let yMin = 40;
    let yMax = 120; // 无数据初始量程(EXE HeartRateWaveform: set_ylim(40,120))
    if (mn <= mx) {
      const span = mx - mn;
      const margin = Math.max(span * 0.25, 15);
      yMin = Math.max(40, mn - margin);
      yMax = Math.min(200, mx + margin);
      if (yMax - yMin < 10) {
        yMin = Math.max(40, yMin - 5);
        yMax = Math.min(200, yMax + 5);
      }
    }
    const range = yMax - yMin;
    const yOf = (v: number): number => plotB - (v - yMin) / range * plotB;

    // 心率区间背景带(低透明度): <60蓝 60-100绿 100-120橙 >120红
    const bands: Band[] = [
      { lo: 40, hi: 60, color: '#153498DB' },
      { lo: 60, hi: 100, color: '#152ECC71' },
      { lo: 100, hi: 120, color: '#15F39C12' },
      { lo: 120, hi: 200, color: '#15E74C3C' }
    ];
    for (const b of bands) {
      const top = Math.max(b.lo, yMin);
      const bot = Math.min(b.hi, yMax);
      if (bot > top) {
        ctx.fillStyle = b.color;
        ctx.fillRect(x0, yOf(bot), plotW, yOf(top) - yOf(bot));
      }
    }

    // 横向网格 + 左侧心率刻度
    ctx.strokeStyle = COL_GRID;
    ctx.lineWidth = 1;
    const Y_TICKS = 5;
    ctx.fillStyle = COL_LABEL;
    ctx.font = axisFont + 'px sans-serif';
    for (let i = 0; i <= Y_TICKS; i++) {
      const y = plotB * i / Y_TICKS;
      ctx.beginPath();
      ctx.moveTo(x0, y);
      ctx.lineTo(w, y);
      ctx.stroke();
      const val = Math.round(yMax - range * i / Y_TICKS);
      // 右对齐贴着波形区左缘, 避免字号大时刻度与波形间出现空隙
      ctx.textAlign = 'right';
      ctx.fillText(val.toString(), x0 - 6, Math.max(axisFont, y + Math.round(axisFont * 0.3)));
      ctx.textAlign = 'left';
    }
    // 底部时间轴 + 竖向虚线: -60秒...-10秒,现在
    ctx.setLineDash([6, 6]);
    for (let t = 0; t <= this.MAX_POINTS; t += 10) {
      const px = x0 + plotW * t / this.MAX_POINTS;
      if (t < this.MAX_POINTS) {
        ctx.beginPath();
        ctx.moveTo(px, 0);
        ctx.lineTo(px, plotB);
        ctx.stroke();
      }
    }
    ctx.setLineDash([]);
    ctx.strokeStyle = COL_GRID;
    ctx.beginPath();
    ctx.moveTo(x0, plotB);
    ctx.lineTo(w, plotB);
    ctx.stroke();
    ctx.fillStyle = COL_LABEL;
    for (let t = 0; t <= this.MAX_POINTS; t += 10) {
      const px = x0 + plotW * t / this.MAX_POINTS;
      const s = t >= this.MAX_POINTS ? '现在' : '-' + (this.MAX_POINTS - t) + '秒';
      const tw = ctx.measureText(s).width;
      const tx = Math.max(x0 + 2, Math.min(w - tw - 2, px - tw / 2));
      ctx.fillText(s, tx, h - Math.max(6, Math.round(axisFont * 0.4)));
    }

    // 折线(0 = 断连, 跳笔画断点)
    ctx.strokeStyle = COL_LINE;
    ctx.lineWidth = 4;
    ctx.lineJoin = 'round';
    ctx.lineCap = 'round';
    let drawing = false;
    ctx.beginPath();
    for (let i = 0; i < n; i++) {
      const v = this.hrData[i];
      const px = x0 + plotW * (this.MAX_POINTS - n + i) / this.MAX_POINTS;
      if (v <= 0) {
        drawing = false;
        continue;
      }
      const py = yOf(v);
      if (!drawing) {
        ctx.moveTo(px, py);
        drawing = true;
      } else {
        ctx.lineTo(px, py);
      }
    }
    if (n > 0) {
      ctx.stroke();
    }
  }

  // ==================== UI ====================

  build() {
    Stack() {
      Column() {
        if (this.isSetupDone && !this.editingSetup) {
          this.MainView()
        } else {
          this.SettingsView()
        }
      }
      .width('100%')
      .height('100%')

      // 心率警报横幅(置顶悬浮)
      if (this.hrAlarm && !this.editingSetup) {
        Text('心率警报！')
          .fontSize(16)
          .fontWeight(FontWeight.Bold)
          .fontColor(Color.White)
          .backgroundColor('#C62828')
          .textAlign(TextAlign.Center)
          .borderRadius(8)
          .padding(10)
          .width('80%')
          .margin({ top: 60 })
      }
    }
    .width('100%')
    .height('100%')
    .backgroundColor(COL_BG)
  }

  // ---- 主界面(竖排: 中部心形+大号心率, 其下时间/info/波形, 折叠屏半折叠态自适应良好) ----
  @Builder
  MainView() {
    Column() {
      // 顶栏: 标题+日期时间(安卓 ActionBar 同款), 右侧设置入口
      Row() {
        Text('HRBubble  ' + this.clockText)
          .fontSize(16)
          .fontWeight(FontWeight.Bold)
          .fontColor('#FFFFFF')
          .layoutWeight(1)
          .maxLines(1)
        Text('设置')
          .fontSize(14)
          .fontColor('#64B5F6')
          .padding({ left: 12, right: 4, top: 8, bottom: 8 })
          .onClick(() => {
            this.editingSetup = true;
            this.statusMsg = '';
          })
      }
      .width('100%')
      .padding({ left: 12, right: 12, top: 8 })

      // 状态行: 数据源状态点 + 服务状态文本
      Row({ space: 8 }) {
        Circle({ width: 14, height: 14 }).fill(this.dotColor)
        Text(this.statusText)
          .fontSize(13)
          .fontColor(COL_STATUS_TEXT)
          .layoutWeight(1)
          .maxLines(1)
          .textOverflow({ overflow: TextOverflow.Ellipsis })
      }
      .width('100%')
      .padding({ left: 12, right: 12, top: 8 })

      // 中部: 心形+大号心率+BPM / 时间 / info行 / 波形
      Column() {
        Row() {
          SymbolGlyph($r('sys.symbol.heart_fill'))
            .fontSize(52)
            .fontColor([this.hrColor])
          Text(this.heartRate > 0 ? this.heartRate.toString() : '--')
            .fontSize(this.hrFontSize)
            .fontWeight(FontWeight.Bold)
            .fontColor(this.hrColor)
            .margin({ left: 16 })
          Text('BPM')
            .fontSize(22)
            .fontColor(this.hrColor)
            .margin({ left: 8 })
        }
        .alignItems(VerticalAlign.Center)

        Text(this.hrTime)
          .fontSize(this.timeFontSize)
          .fontColor(COL_TIME)
          .margin({ top: 4 })

        if (this.hrInfo !== '') {
          Text(this.hrInfo)
            .fontSize(14)
            .fontWeight(FontWeight.Bold)
            .fontColor(COL_INFO)
            .margin({ top: 2 })
            .maxLines(1)
            .textOverflow({ overflow: TextOverflow.Ellipsis })
        }

        Canvas(this.waveCtx)
          .onReady(() => {
            this.waveReady = true;
            this.drawWave();
          })
          .width('100%')
          .height(160)
          .margin({ top: 8, left: 12, right: 12 })
      }
      .width('100%')
      .layoutWeight(1)
      .justifyContent(FlexAlign.Center)
    }
    .width('100%')
    .height('100%')
  }

  // ---- 设置页(对齐安卓 activity_settings + 内置VPN区块) ----
  @Builder
  SettingsView() {
    Scroll() {
      Column({ space: 10 }) {
        Text('前置条件: 本机已内置 Tailscale 并加入与电脑相同的 tailnet, 电脑端 HRMLink 已启用心率数据服务')
          .fontSize(13)
          .fontColor('#8A8A8A')
          .margin({ top: 16 })

        // === 数据源(安卓同款两项) ===
        TextInput({ placeholder: '电脑Tailscale IP（在HRMLink的Tailscale页可查看）', text: this.exeIp })
          .fontColor('#FFFFFF')
          .placeholderColor('#7A8A99')
          .backgroundColor('#1E2A38')
          .borderRadius(8)
          .caretColor('#64B5F6')
          .onChange((v: string) => {
            this.exeIp = v;
          })

        TextInput({ placeholder: '端口（默认 8765）', text: this.exePort })
          .type(InputType.Number)
          .fontColor('#FFFFFF')
          .placeholderColor('#7A8A99')
          .backgroundColor('#1E2A38')
          .borderRadius(8)
          .caretColor('#64B5F6')
          .onChange((v: string) => {
            this.exePort = v;
          })

        // === 内置 Tailscale VPN 区块(鸿蒙版特有, 安卓为独立Tailscale App) ===
        Text('内置 Tailscale VPN')
          .fontSize(14)
          .fontColor('#B0BEC5')
          .margin({ top: 8 })

        TextInput({ placeholder: '控制服务器 URL（留空 = 官方）', text: this.controlUrl })
          .fontColor('#FFFFFF')
          .placeholderColor('#7A8A99')
          .backgroundColor('#1E2A38')
          .borderRadius(8)
          .caretColor('#64B5F6')
          .onChange((v: string) => {
            this.controlUrl = v;
          })

        TextInput({ placeholder: this.hasKeyFile ? '激活码（已注册设备可留空）' : 'Tailscale 激活码（reusable）', text: this.authKey })
          .type(InputType.Password)
          .showPasswordIcon(true)
          .fontColor('#FFFFFF')
          .placeholderColor('#7A8A99')
          .backgroundColor('#1E2A38')
          .borderRadius(8)
          .caretColor('#64B5F6')
          .onChange((v: string) => {
            this.authKey = v;
          })

        Row({ space: 10 }) {
          Text(this.vpnConnected ? 'VPN 已连接' + (this.vpnIp !== '' ? ' (' + this.vpnIp + ')' : '')
            : this.statusText)
            .fontSize(13)
            .fontColor(this.vpnConnected ? COL_DOT_WS : COL_INFO)
            .layoutWeight(1)
            .maxLines(2)
          Button(this.vpnRunning ? '停止 VPN' : '启动 VPN')
            .height(34)
            .fontColor('#FFFFFF')
            .backgroundColor(this.vpnRunning ? '#455A64' : '#2E7D32')
            .onClick(() => {
              if (this.vpnRunning) {
                this.stopVpn();
              } else {
                this.vpnAutoRetry = 0; // 手动启动: 重置自动重拉计数
                this.userStoppedVpn = false;
                this.startVpn();
              }
            })
        }
        .width('100%')

        // === 字体大小(安卓同款两项 Slider) ===
        Text('心率数字大小: ' + Math.round(this.hrFontSize))
          .fontSize(14)
          .fontColor('#B0BEC5')
          .margin({ top: 8 })
        Slider({ value: this.hrFontSize, min: 48, max: 128, step: 1, style: SliderStyle.OutSet })
          .selectedColor('#64B5F6')
          .trackColor('#37474F')
          .width('100%')
          .onChange((v: number) => {
            this.hrFontSize = Math.round(v);
          })

        Text('时间字体大小: ' + Math.round(this.timeFontSize))
          .fontSize(14)
          .fontColor('#B0BEC5')
        Slider({ value: this.timeFontSize, min: 12, max: 28, step: 1, style: SliderStyle.OutSet })
          .selectedColor('#64B5F6')
          .trackColor('#37474F')
          .width('100%')
          .onChange((v: number) => {
            this.timeFontSize = Math.round(v);
          })

        Text('波形刻度字号: ' + Math.round(this.axisFontSize))
          .fontSize(14)
          .fontColor('#B0BEC5')
        Slider({ value: this.axisFontSize, min: 10, max: 24, step: 1, style: SliderStyle.OutSet })
          .selectedColor('#64B5F6')
          .trackColor('#37474F')
          .width('100%')
          .onChange((v: number) => {
            this.axisFontSize = Math.round(v);
          })

        Button('保存设置')
          .width('100%')
          .fontColor('#FFFFFF')
          .backgroundColor('#2E7D32')
          .margin({ top: 12 })
          .onClick(() => {
            this.saveSettings();
          })

        Text('HRBubble 1.0.0')
          .fontSize(12)
          .fontColor('#999999')
          .margin({ top: 12, bottom: 24 })

        if (this.statusMsg !== '') {
          Text(this.statusMsg)
            .fontSize(13)
            .fontColor(COL_INFO)
        }

        if (this.isSetupDone) {
          Button('返回')
            .width('100%')
            .fontColor('#FFFFFF')
            .backgroundColor('#455A64')
            .onClick(() => {
              this.editingSetup = false;
              this.statusMsg = '';
            })
        }
      }
      .padding({ left: 16, right: 16 })
      .alignItems(HorizontalAlign.Start)
    }
    .width('100%')
    .height('100%')
    .scrollBar(BarState.Auto)
  }
}
