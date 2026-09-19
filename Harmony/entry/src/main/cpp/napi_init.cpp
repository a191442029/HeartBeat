#include "napi/native_api.h"

#include "tailscale.h"
#include <string>
#include <hilog/log.h>
#include <unistd.h>
#include <cstring>
#include <cstdio>
#include <atomic>
#include <sys/socket.h>
#include <netinet/in.h>
#include <arpa/inet.h>
#include <sys/time.h>
#include <errno.h>
#include <fcntl.h>
#include <poll.h>
#include <time.h>

#define LOG_TAG "GoVerify"
#define LOG_DOMAIN 0x0000


static char g_filesDir[512] = {0};

static napi_value SetFilesDir(napi_env env, napi_callback_info info) {
    size_t argc = 1;
    napi_value args[1];
    napi_get_cb_info(env, info, &argc, args, nullptr, nullptr);
    size_t len = 0;
    napi_get_value_string_utf8(env, args[0], g_filesDir, sizeof(g_filesDir), &len);
    OH_LOG_INFO(LOG_APP, "SetFilesDir: %{public}s", g_filesDir);
    napi_value result;
    napi_get_undefined(env, &result);
    return result;
}


// ==================== Tailscale 控制面 NAPI ====================

static std::atomic<ts_device*> g_ts_device{nullptr};
// 防重入保护：tailscaleInit 是长阻塞操作（最长60秒+），
// 并发调用会导致两个 ts_init 实例争抢同一设备身份、旧句柄泄漏。
static std::atomic<bool> g_init_in_progress{false};

// 辅助：napi_value -> std::string
static std::string NapiToString(napi_env env, napi_value v) {
    if (v == nullptr) return "";
    size_t len = 0;
    napi_get_value_string_utf8(env, v, nullptr, 0, &len);
    if (len == 0) return "";
    std::string s(len, '\0');
    size_t copied = 0;
    napi_get_value_string_utf8(env, v, s.data(), len + 1, &copied);
    return s;
}

// tailscaleInit(controlUrl, authKey, keyFilePath): Promise<string>
//   controlUrl  - 控制服务器 URL，空串=默认(Tailscale 官方)
//   authKey     - auth key，空串=交互式授权
//   keyFilePath - key file 路径，空串=ephemeral
//   resolve: tailnet IPv4; reject: 错误信息
struct TsInitData {
    std::string controlUrl;
    std::string authKey;
    std::string keyFilePath;
    std::string result;
    bool ok = false;
    napi_deferred deferred = nullptr;
    napi_async_work work = nullptr;
};

static napi_value TailscaleInitNapi(napi_env env, napi_callback_info info) {
    size_t argc = 3;
    napi_value args[3] = {nullptr};
    napi_get_cb_info(env, info, &argc, args, nullptr, nullptr);

    // 防重入：已有初始化进行中时直接拒绝，避免并发 ts_init
    bool expected = false;
    if (!g_init_in_progress.compare_exchange_strong(expected, true)) {
        napi_deferred deferred;
        napi_value promise;
        napi_value reason;
        napi_create_string_utf8(env, "已有初始化进行中，请稍候", NAPI_AUTO_LENGTH, &reason);
        napi_create_promise(env, &deferred, &promise);
        napi_reject_deferred(env, deferred, reason);
        return promise;
    }

    auto* data = new TsInitData{
        NapiToString(env, args[0]),
        NapiToString(env, args[1]),
        NapiToString(env, args[2]),
    };

    napi_value promise;
    napi_create_promise(env, &data->deferred, &promise);

    napi_value async_name;
    napi_create_string_utf8(env, "tailscaleInit", NAPI_AUTO_LENGTH, &async_name);
    napi_create_async_work(env, nullptr, async_name,
        [](napi_env, void* raw) {
            auto* d = static_cast<TsInitData*>(raw);

            // 重定向 Rust tracing 输出到文件（调试：ts_ffi 内部日志走 stdout/stderr）
            // 注意：dup2 后必须 fclose 原始 FILE*，否则每次 init 泄漏一个文件描述符
            std::string logPath = std::string(g_filesDir) + "/ts_rust.log";
            FILE* logF = fopen(logPath.c_str(), "w");
            if (logF) {
                dup2(fileno(logF), 1);
                dup2(fileno(logF), 2);
                fclose(logF);
            }
            OH_LOG_INFO(LOG_APP, "tailscaleInit: controlUrl=[%{public}s] keyFile=[%{public}s]",
                        d->controlUrl.c_str(), d->keyFilePath.c_str());

            ts_init_tracing();

            // 1. 加载/生成 key file（overwrite=true 首次生成新密钥）
            ts_persisted_key_state keyState{};
            bool useKeyFile = !d->keyFilePath.empty();
            bool keyFileExisted = false;
            if (useKeyFile) {
                keyFileExisted = (access(d->keyFilePath.c_str(), F_OK) == 0);
                int kr = ts_load_key_file(d->keyFilePath.c_str(), true, &keyState);
                if (kr < 0) {
                    d->ok = false;
                    d->result = "ts_load_key_file failed: " + d->keyFilePath;
                    return;
                }
            }

            // 2. 构造 config（零初始化）
            ts_config config{};
            config.control_server_url = d->controlUrl.empty() ? nullptr : d->controlUrl.c_str();
            // 设置 hostname：尝试获取系统主机名，失败则用 "HarmonyOS"
            char hostnameBuf[256] = {0};
            if (gethostname(hostnameBuf, sizeof(hostnameBuf) - 1) == 0 && hostnameBuf[0] != '\0'
                && std::string(hostnameBuf) != "node") {
              config.hostname = nullptr; // 让 Tailscale 用系统主机名
            } else {
              config.hostname = "HarmonyOS";
            }
            config.tags = nullptr;
            config.client_name = "TailMesh";
            config.key_state = useKeyFile ? &keyState : nullptr;
            config.ephemeral = !useKeyFile;

            // authKey：已有 key file（设备已注册过）时可留空，传 NULL 自动重连
            const char* authToken = d->authKey.empty() ? nullptr : d->authKey.c_str();
            if (!authToken && !keyFileExisted) {
                d->ok = false;
                d->result = "首次使用请输入激活码；已注册设备可留空自动重连";
                return;
            }
            OH_LOG_INFO(LOG_APP, "tailscaleInit: authKey len=%{public}d, useKeyFile=%{public}d",
                        (int)d->authKey.length(), (int)useKeyFile);

            // 3. 初始化设备（阻塞直到控制面连接结果）
            ts_device* dev = ts_init(&config, authToken);
            if (!dev) {
                d->ok = false;
                d->result = "ts_init 返回 null（控制面连接失败）。常见原因："
                            "① 激活码无效——一次性激活码用过一次即失效，卸载重装后必须到管理后台重新生成"
                            "（建议勾选 reusable 可复用类型）；"
                            "② 网络无法访问控制服务器（DNS/防火墙）；"
                            "③ 控制服务器 URL 填写错误";
                return;
            }
            g_ts_device.store(dev);

            // 4. 获取 tailnet IPv4（重试最多 60 秒，等 netmap 下发）
            ts_in_addr_t addr{};
            int r = -1;
            for (int attempt = 0; attempt < 30; ++attempt) {
                r = ts_ipv4_addr(g_ts_device.load(), &addr);
                if (r == 0) break;
                OH_LOG_INFO(LOG_APP, "ts_ipv4_addr attempt %{public}d failed, retry in 2s", attempt);
                usleep(2000000);
            }
            if (r < 0) {
                d->ok = false;
                // 注意：不把 Rust 日志回显进 UI 错误消息——日志可能含 token/节点 IP 等敏感信息，
                // 且大文本会导致渲染卡顿。完整日志请通过"诊断日志"入口按需查看。
                d->result = "ts_ipv4_addr 失败（已重试30次/60秒）。请检查网络后重试，"
                            "或在设置中重置配置重新激活。详细原因见诊断日志。";
                return;
            }
            char ip[32];
            snprintf(ip, sizeof(ip), "%u.%u.%u.%u",
                     (unsigned)addr[0], (unsigned)addr[1], (unsigned)addr[2], (unsigned)addr[3]);
            d->ok = true;
            d->result = std::string(ip);
            OH_LOG_INFO(LOG_APP, "tailscaleInit OK, tailnet IP=%{public}s", ip);
        },
        [](napi_env env, napi_status, void* raw) {
            auto* d = static_cast<TsInitData*>(raw);
            napi_value result;
            napi_create_string_utf8(env, d->result.c_str(), d->result.length(), &result);
            if (d->ok) {
                napi_resolve_deferred(env, d->deferred, result);
            } else {
                napi_reject_deferred(env, d->deferred, result);
            }
            // 无论成功失败都重置防重入标志
            g_init_in_progress.store(false);
            napi_delete_async_work(env, d->work);
            delete d;
        },
        data, &data->work);
    napi_queue_async_work(env, data->work);

    return promise;
}

// tailscaleGetPeerIpv4(peerName): Promise<string>
struct TsPeerData {
    std::string peerName;
    std::string result;
    bool ok = false;
    napi_deferred deferred = nullptr;
    napi_async_work work = nullptr;
};

static napi_value TailscaleGetPeerIpv4Napi(napi_env env, napi_callback_info info) {
    size_t argc = 1;
    napi_value args[1] = {nullptr};
    napi_get_cb_info(env, info, &argc, args, nullptr, nullptr);

    auto* data = new TsPeerData{NapiToString(env, args[0])};

    napi_value promise;
    napi_create_promise(env, &data->deferred, &promise);

    napi_value async_name;
    napi_create_string_utf8(env, "tailscaleGetPeerIpv4", NAPI_AUTO_LENGTH, &async_name);
    napi_create_async_work(env, nullptr, async_name,
        [](napi_env, void* raw) {
            auto* d = static_cast<TsPeerData*>(raw);
            ts_device* dev = g_ts_device.load();
            if (!dev) {
                d->ok = false;
                d->result = "未初始化";
                return;
            }
            ts_in_addr_t addr{};
            int r = ts_peer_ipv4_addr(dev, d->peerName.c_str(), &addr);
            if (r < 0) {
                d->ok = false;
                d->result = "查询 peer 出错";
            } else if (r == 0) {
                d->ok = false;
                d->result = "未找到 peer: " + d->peerName;
            } else {
                char ip[32];
                snprintf(ip, sizeof(ip), "%u.%u.%u.%u",
                         (unsigned)addr[0], (unsigned)addr[1], (unsigned)addr[2], (unsigned)addr[3]);
                d->ok = true;
                d->result = std::string(ip);
            }
        },
        [](napi_env env, napi_status, void* raw) {
            auto* d = static_cast<TsPeerData*>(raw);
            napi_value result;
            napi_create_string_utf8(env, d->result.c_str(), d->result.length(), &result);
            if (d->ok) {
                napi_resolve_deferred(env, d->deferred, result);
            } else {
                napi_reject_deferred(env, d->deferred, result);
            }
            napi_delete_async_work(env, d->work);
            delete d;
        },
        data, &data->work);
    napi_queue_async_work(env, data->work);

    return promise;
}

// tailscaleDeinit(): void
static napi_value TailscaleDeinitNapi(napi_env env, napi_callback_info info) {
    ts_device* dev = g_ts_device.exchange(nullptr);
    if (dev) {
        ts_deinit(dev);
        OH_LOG_INFO(LOG_APP, "tailscaleDeinit OK");
    }
    napi_value undefined;
    napi_get_undefined(env, &undefined);
    return undefined;
}

// tailscaleStatus(): string  — 同步返回当前状态
static napi_value TailscaleStatusNapi(napi_env env, napi_callback_info info) {
    ts_device* dev = g_ts_device.load();
    std::string status = dev ? "connected" : "disconnected";
    if (dev) {
        ts_in_addr_t addr{};
        if (ts_ipv4_addr(dev, &addr) == 0) {
            char ip[32];
            snprintf(ip, sizeof(ip), "%u.%u.%u.%u",
                     (unsigned)addr[0], (unsigned)addr[1], (unsigned)addr[2], (unsigned)addr[3]);
            status += "; tailnet IP=" + std::string(ip);
        }
    }
    napi_value result;
    napi_create_string_utf8(env, status.c_str(), status.length(), &result);
    return result;
}

// tailscaleTcpTest(host, port): Promise<string>
//   通过 Tailscale 网络栈连 host:port，读 SSH banner，返回结果
//   用于验证 ts_tcp_connect 在 OHOS 沙箱里是否可用
struct TsTcpTestData {
    std::string host;
    uint16_t port;
    std::string result;
    bool ok = false;
    napi_deferred deferred = nullptr;
    napi_async_work work = nullptr;
};

static napi_value TailscaleTcpTestNapi(napi_env env, napi_callback_info info) {
    size_t argc = 2;
    napi_value args[2] = {nullptr};
    napi_get_cb_info(env, info, &argc, args, nullptr, nullptr);

    auto* data = new TsTcpTestData{};
    data->host = NapiToString(env, args[0]);
    double portD = 0;
    napi_get_value_double(env, args[1], &portD);
    data->port = (uint16_t)portD;

    napi_value promise;
    napi_create_promise(env, &data->deferred, &promise);

    napi_value async_name;
    napi_create_string_utf8(env, "tailscaleTcpTest", NAPI_AUTO_LENGTH, &async_name);
    napi_create_async_work(env, nullptr, async_name,
        [](napi_env, void* raw) {
            auto* d = static_cast<TsTcpTestData*>(raw);
            ts_device* dev = g_ts_device.load();
            if (!dev) {
                d->ok = false;
                d->result = "未初始化 tailscale，请先连接";
                return;
            }

            // 构造 sockaddr（用 ts_parse_sockaddr 一步解析 "host:port"）
            std::string addrStr = d->host + ":" + std::to_string(d->port);
            ts_sockaddr addr{};
            if (ts_parse_sockaddr(addrStr.c_str(), &addr) < 0) {
                d->ok = false;
                d->result = "ts_parse_sockaddr 失败: " + addrStr;
                return;
            }

            OH_LOG_INFO(LOG_APP, "tsTcpTest: connecting to %{public}s:%{public}d via Tailscale",
                        d->host.c_str(), (int)d->port);

            // TCP 连接（阻塞，走 Tailscale 网络栈）
            ts_tcp_stream* stream = ts_tcp_connect(dev, &addr);
            if (!stream) {
                d->ok = false;
                d->result = "ts_tcp_connect 返回 null（连接失败）。\nRust 日志（最后 4000 字节）:\n";
                std::string logPath = std::string(g_filesDir) + "/ts_rust.log";
                FILE* rf = fopen(logPath.c_str(), "r");
                if (rf) {
                    fseek(rf, 0, SEEK_END);
                    long sz = ftell(rf);
                    long readSz = sz > 4000 ? 4000 : sz;
                    fseek(rf, sz - readSz, SEEK_SET);
                    std::string content(readSz, '\0');
                    size_t n = fread(content.data(), 1, readSz, rf);
                    content.resize(n);
                    d->result += content;
                    fclose(rf);
                } else {
                    d->result += "(无法读取日志文件: " + logPath + ")";
                }
                return;
            }

            OH_LOG_INFO(LOG_APP, "tsTcpTest: TCP connected, waiting for SSH banner...");

            // 读 SSH banner（SSH 服务器连接后立即发 "SSH-2.0-...\r\n"）
            uint8_t buf[256] = {0};
            int n = ts_tcp_recv(stream, buf, sizeof(buf) - 1);
            if (n > 0) {
                d->ok = true;
                d->result = "✓ 连接成功！收到 " + std::to_string(n) + " 字节:\n" + std::string((char*)buf, n);
            } else {
                d->ok = false;
                d->result = "ts_tcp_recv 返回 " + std::to_string(n) + "（连接已建立但读取失败/对端关闭）";
            }

            ts_tcp_close(stream);
            OH_LOG_INFO(LOG_APP, "tsTcpTest: done, ok=%{public}d", d->ok ? 1 : 0);
        },
        [](napi_env env, napi_status, void* raw) {
            auto* d = static_cast<TsTcpTestData*>(raw);
            napi_value result;
            napi_create_string_utf8(env, d->result.c_str(), d->result.length(), &result);
            if (d->ok) {
                napi_resolve_deferred(env, d->deferred, result);
            } else {
                napi_reject_deferred(env, d->deferred, result);
            }
            napi_delete_async_work(env, d->work);
            delete d;
        },
        data, &data->work);
    napi_queue_async_work(env, data->work);

    return promise;
}

// ==================== SSH 客户端 NAPI ====================

static std::atomic<ts_ssh_session*> g_ssh_session{nullptr};
static std::atomic<ts_ssh_channel*> g_ssh_channel{nullptr};

// sshConnect(host, port, username, password): Promise<string>
struct SshConnectData {
    std::string host;
    uint16_t port;
    std::string username;
    std::string password;
    std::string result;
    bool ok = false;
    napi_deferred deferred = nullptr;
    napi_async_work work = nullptr;
};

static napi_value SshConnectNapi(napi_env env, napi_callback_info info) {
    size_t argc = 4;
    napi_value args[4] = {nullptr};
    napi_get_cb_info(env, info, &argc, args, nullptr, nullptr);

    auto* data = new SshConnectData{};
    data->host = NapiToString(env, args[0]);
    double portD = 0;
    napi_get_value_double(env, args[1], &portD);
    data->port = (uint16_t)portD;
    data->username = NapiToString(env, args[2]);
    data->password = NapiToString(env, args[3]);

    napi_value promise;
    napi_create_promise(env, &data->deferred, &promise);

    napi_value async_name;
    napi_create_string_utf8(env, "sshConnect", NAPI_AUTO_LENGTH, &async_name);
    napi_create_async_work(env, nullptr, async_name,
        [](napi_env, void* raw) {
            auto* d = static_cast<SshConnectData*>(raw);
            ts_device* dev = g_ts_device.load();
            if (!dev) {
                d->ok = false;
                d->result = "Tailscale 未初始化，请先连接";
                return;
            }

            // 构造 sockaddr
            std::string addrStr = d->host + ":" + std::to_string(d->port);
            ts_sockaddr addr{};
            if (ts_parse_sockaddr(addrStr.c_str(), &addr) < 0) {
                d->ok = false;
                d->result = "ts_parse_sockaddr 失败: " + addrStr;
                return;
            }

            OH_LOG_INFO(LOG_APP, "sshConnect: %{public}s as %{public}s", addrStr.c_str(), d->username.c_str());

            // SSH 连接（通过 Tailscale 网络栈）
            ts_ssh_session* sess = ts_ssh_connect(dev, &addr, d->username.c_str(), d->password.c_str());
            if (!sess) {
                d->ok = false;
                d->result = "ts_ssh_connect 失败（检查 Tailscale 连接/用户名/密码）";
                // 附加 Rust 日志
                std::string logPath = std::string(g_filesDir) + "/ts_rust.log";
                FILE* rf = fopen(logPath.c_str(), "r");
                if (rf) {
                    fseek(rf, 0, SEEK_END);
                    long sz = ftell(rf);
                    long readSz = sz > 2000 ? 2000 : sz;
                    fseek(rf, sz - readSz, SEEK_SET);
                    std::string content(readSz, '\0');
                    size_t n = fread(content.data(), 1, readSz, rf);
                    content.resize(n);
                    d->result += "\n" + content;
                    fclose(rf);
                }
                return;
            }

            // 打开 shell
            ts_ssh_channel* ch = ts_ssh_open_shell(sess);
            if (!ch) {
                ts_ssh_disconnect(sess);
                d->ok = false;
                d->result = "ts_ssh_open_shell 失败";
                return;
            }

            g_ssh_session.store(sess);
            g_ssh_channel.store(ch);
            d->ok = true;
            d->result = "SSH 连接成功";
            OH_LOG_INFO(LOG_APP, "sshConnect OK");
        },
        [](napi_env env, napi_status, void* raw) {
            auto* d = static_cast<SshConnectData*>(raw);
            napi_value result;
            napi_create_string_utf8(env, d->result.c_str(), d->result.length(), &result);
            if (d->ok) {
                napi_resolve_deferred(env, d->deferred, result);
            } else {
                napi_reject_deferred(env, d->deferred, result);
            }
            napi_delete_async_work(env, d->work);
            delete d;
        },
        data, &data->work);
    napi_queue_async_work(env, data->work);

    return promise;
}

// sshRead(): Promise<string> — 阻塞读 shell 输出
struct SshReadData {
    std::string result;
    bool ok = false;
    napi_deferred deferred = nullptr;
    napi_async_work work = nullptr;
};

static napi_value SshReadNapi(napi_env env, napi_callback_info info) {
    auto* data = new SshReadData{};

    napi_value promise;
    napi_create_promise(env, &data->deferred, &promise);

    napi_value async_name;
    napi_create_string_utf8(env, "sshRead", NAPI_AUTO_LENGTH, &async_name);
    napi_create_async_work(env, nullptr, async_name,
        [](napi_env, void* raw) {
            auto* d = static_cast<SshReadData*>(raw);
            ts_ssh_channel* ch = g_ssh_channel.load();
            if (!ch) {
                d->ok = false;
                d->result = "SSH 未连接";
                return;
            }

            uint8_t buf[8192];
            int n = ts_ssh_channel_read(ch, buf, sizeof(buf));
            if (n > 0) {
                d->ok = true;
                d->result.assign((char*)buf, n);
            } else {
                d->ok = false;
                d->result = "";  // EOF 或错误
            }
        },
        [](napi_env env, napi_status, void* raw) {
            auto* d = static_cast<SshReadData*>(raw);
            napi_value result;
            napi_create_string_utf8(env, d->result.c_str(), d->result.length(), &result);
            if (d->ok) {
                napi_resolve_deferred(env, d->deferred, result);
            } else {
                napi_reject_deferred(env, d->deferred, result);
            }
            napi_delete_async_work(env, d->work);
            delete d;
        },
        data, &data->work);
    napi_queue_async_work(env, data->work);

    return promise;
}

// sshWrite(data: string): Promise<void>
struct SshWriteData {
    std::string data;
    std::string result;
    bool ok = false;
    napi_deferred deferred = nullptr;
    napi_async_work work = nullptr;
};

static napi_value SshWriteNapi(napi_env env, napi_callback_info info) {
    size_t argc = 1;
    napi_value args[1] = {nullptr};
    napi_get_cb_info(env, info, &argc, args, nullptr, nullptr);

    auto* data = new SshWriteData{NapiToString(env, args[0])};

    napi_value promise;
    napi_create_promise(env, &data->deferred, &promise);

    napi_value async_name;
    napi_create_string_utf8(env, "sshWrite", NAPI_AUTO_LENGTH, &async_name);
    napi_create_async_work(env, nullptr, async_name,
        [](napi_env, void* raw) {
            auto* d = static_cast<SshWriteData*>(raw);
            ts_ssh_channel* ch = g_ssh_channel.load();
            if (!ch) {
                d->ok = false;
                d->result = "SSH 未连接";
                return;
            }

            int n = ts_ssh_channel_write(ch, (const uint8_t*)d->data.c_str(), d->data.length());
            if (n >= 0) {
                d->ok = true;
                d->result = "";
            } else {
                d->ok = false;
                d->result = "写入失败";
            }
        },
        [](napi_env env, napi_status, void* raw) {
            auto* d = static_cast<SshWriteData*>(raw);
            napi_value result;
            napi_create_string_utf8(env, d->result.c_str(), d->result.length(), &result);
            if (d->ok) {
                napi_resolve_deferred(env, d->deferred, result);
            } else {
                napi_reject_deferred(env, d->deferred, result);
            }
            napi_delete_async_work(env, d->work);
            delete d;
        },
        data, &data->work);
    napi_queue_async_work(env, data->work);

    return promise;
}

// sshClose(): void
static napi_value SshCloseNapi(napi_env env, napi_callback_info info) {
    ts_ssh_channel* ch = g_ssh_channel.exchange(nullptr);
    if (ch) ts_ssh_channel_close(ch);
    ts_ssh_session* sess = g_ssh_session.exchange(nullptr);
    if (sess) ts_ssh_disconnect(sess);
    OH_LOG_INFO(LOG_APP, "sshClose OK");
    napi_value undefined;
    napi_get_undefined(env, &undefined);
    return undefined;
}

// ==================== VPN Bridge NAPI ====================

// vpnBridge(tunFd: number): Promise<number>
//   tunFd - TUN 文件描述符（从 vpnExtension VpnConnection.create() 获取）
//   resolve: 0=成功, -1=失败
struct VpnBridgeData {
    int tunFd;
    int result;
    napi_deferred deferred = nullptr;
    napi_async_work work = nullptr;
};

static napi_value VpnBridgeNapi(napi_env env, napi_callback_info info) {
    size_t argc = 1;
    napi_value args[1] = {nullptr};
    napi_get_cb_info(env, info, &argc, args, nullptr, nullptr);

    int32_t tunFd = 0;
    napi_get_value_int32(env, args[0], &tunFd);
    OH_LOG_INFO(LOG_APP, "vpnBridge called, tunFd=%{public}d", tunFd);

    auto* data = new VpnBridgeData{tunFd, -1};

    napi_value promise;
    napi_create_promise(env, &data->deferred, &promise);

    napi_value async_name;
    napi_create_string_utf8(env, "vpnBridge", NAPI_AUTO_LENGTH, &async_name);

    napi_create_async_work(env, nullptr, async_name,
        [](napi_env env, void* d) {
            auto* data = static_cast<VpnBridgeData*>(d);
            ts_device* dev = g_ts_device.load();
            if (!dev) {
                OH_LOG_ERROR(LOG_APP, "vpnBridge: no ts_device");
                data->result = -1;
                return;
            }
            data->result = ts_vpn_bridge(dev, data->tunFd);
            OH_LOG_INFO(LOG_APP, "vpnBridge result=%{public}d", data->result);
        },
        [](napi_env env, napi_status status, void* d) {
            auto* data = static_cast<VpnBridgeData*>(d);
            napi_value result;
            napi_create_int32(env, data->result, &result);
            napi_resolve_deferred(env, data->deferred, result);
            napi_delete_async_work(env, data->work);
            delete data;
        },
        data, &data->work);
    napi_queue_async_work(env, data->work);

    return promise;
}

// ==================== VPN 诊断 NAPI ====================

// vpnDiag(host, port): Promise<string>
//   用普通 OS socket 连接 host:port（不经过 Tailscale 网络栈）。
//   如果 VPN 路由生效，此连接会走 VPN TUN → Tailscale 数据面 → WireGuard → peer。
//   用于诊断 VPN 路由是否正常。
struct VpnDiagData {
    std::string host;
    uint16_t port;
    std::string result;
    bool ok = false;
    napi_deferred deferred = nullptr;
    napi_async_work work = nullptr;
};

static napi_value VpnDiagNapi(napi_env env, napi_callback_info info) {
    size_t argc = 2;
    napi_value args[2] = {nullptr};
    napi_get_cb_info(env, info, &argc, args, nullptr, nullptr);

    auto* data = new VpnDiagData{};
    data->host = NapiToString(env, args[0]);
    double portD = 0;
    napi_get_value_double(env, args[1], &portD);
    data->port = (uint16_t)portD;

    napi_value promise;
    napi_create_promise(env, &data->deferred, &promise);

    napi_value async_name;
    napi_create_string_utf8(env, "vpnDiag", NAPI_AUTO_LENGTH, &async_name);
    napi_create_async_work(env, nullptr, async_name,
        [](napi_env, void* raw) {
            auto* d = static_cast<VpnDiagData*>(raw);
            OH_LOG_INFO(LOG_APP, "vpnDiag: connecting to %{public}s:%{public}d via OS socket (VPN route test)",
                        d->host.c_str(), (int)d->port);

            int sock = socket(AF_INET, SOCK_STREAM, 0);
            if (sock < 0) {
                d->ok = false;
                d->result = "socket() failed: " + std::to_string(errno);
                return;
            }

            // 5 秒超时
            struct timeval tv { .tv_sec = 5, .tv_usec = 0 };
            setsockopt(sock, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof(tv));
            setsockopt(sock, SOL_SOCKET, SO_SNDTIMEO, &tv, sizeof(tv));

            struct sockaddr_in addr {};
            addr.sin_family = AF_INET;
            addr.sin_port = htons(d->port);
            inet_pton(AF_INET, d->host.c_str(), &addr.sin_addr);

            int r = connect(sock, (struct sockaddr*)&addr, sizeof(addr));
            if (r < 0) {
                d->ok = false;
                d->result = "connect() failed: errno=" + std::to_string(errno) +
                            " (" + std::string(strerror(errno)) + ")";
                close(sock);
                OH_LOG_ERROR(LOG_APP, "vpnDiag: %{public}s", d->result.c_str());
                return;
            }

            OH_LOG_INFO(LOG_APP, "vpnDiag: connected, reading SSH banner...");

            char buf[256] = {0};
            ssize_t n = recv(sock, buf, sizeof(buf) - 1, 0);
            if (n > 0) {
                d->ok = true;
                d->result = "✓ VPN 路由正常！收到 " + std::to_string(n) + " 字节:\n" + std::string(buf, n);
            } else {
                d->ok = false;
                d->result = "recv() returned " + std::to_string(n) + " errno=" + std::to_string(errno);
            }
            close(sock);
            OH_LOG_INFO(LOG_APP, "vpnDiag: done, ok=%{public}d", d->ok ? 1 : 0);
        },
        [](napi_env env, napi_status, void* raw) {
            auto* d = static_cast<VpnDiagData*>(raw);
            napi_value result;
            napi_create_string_utf8(env, d->result.c_str(), d->result.length(), &result);
            if (d->ok) {
                napi_resolve_deferred(env, d->deferred, result);
            } else {
                napi_reject_deferred(env, d->deferred, result);
            }
            napi_delete_async_work(env, d->work);
            delete d;
        },
        data, &data->work);
    napi_queue_async_work(env, data->work);

    return promise;
}

// vpnLatencyTest(host, port, count): Promise<string>
//   连续 count 次通过 OS socket 建立 TCP 连接（走 VPN 路由），测量每次 connect 耗时。
//   返回 min/avg/max 统计和各次明细，用于量化 VPN 数据面延迟。
struct LatencyData {
    std::string host;
    uint16_t port;
    int count;
    std::string result;
    bool ok = false;
    napi_deferred deferred = nullptr;
    napi_async_work work = nullptr;
};

// 非阻塞 connect + poll 超时，返回 0 成功 / -1 失败，elapsed_ms 输出耗时
static int connect_with_timeout(const struct sockaddr_in& addr, int timeout_ms, double* elapsed_ms) {
    int sock = socket(AF_INET, SOCK_STREAM, 0);
    if (sock < 0) return -1;

    int flags = fcntl(sock, F_GETFL, 0);
    fcntl(sock, F_SETFL, flags | O_NONBLOCK);

    struct timespec t0, t1;
    clock_gettime(CLOCK_MONOTONIC, &t0);

    int r = connect(sock, (struct sockaddr*)&addr, sizeof(addr));
    if (r < 0 && errno != EINPROGRESS) {
        close(sock);
        return -1;
    }
    if (r < 0) {
        struct pollfd pfd;
        pfd.fd = sock;
        pfd.events = POLLOUT;
        pfd.revents = 0;
        int pr = poll(&pfd, 1, timeout_ms);
        if (pr <= 0) {
            close(sock);
            return -1;
        }
        int err = 0;
        socklen_t len = sizeof(err);
        getsockopt(sock, SOL_SOCKET, SO_ERROR, &err, &len);
        if (err != 0) {
            close(sock);
            return -1;
        }
    }
    clock_gettime(CLOCK_MONOTONIC, &t1);
    *elapsed_ms = (t1.tv_sec - t0.tv_sec) * 1000.0 + (t1.tv_nsec - t0.tv_nsec) / 1000000.0;
    close(sock);
    return 0;
}

static napi_value VpnLatencyNapi(napi_env env, napi_callback_info info) {
    size_t argc = 3;
    napi_value args[3] = {nullptr};
    napi_get_cb_info(env, info, &argc, args, nullptr, nullptr);

    auto* data = new LatencyData{};
    data->host = NapiToString(env, args[0]);
    double portD = 0, countD = 0;
    napi_get_value_double(env, args[1], &portD);
    napi_get_value_double(env, args[2], &countD);
    data->port = (uint16_t)portD;
    data->count = (int)countD;
    if (data->count < 1) data->count = 10;
    if (data->count > 50) data->count = 50;

    napi_value promise;
    napi_create_promise(env, &data->deferred, &promise);

    napi_value async_name;
    napi_create_string_utf8(env, "vpnLatency", NAPI_AUTO_LENGTH, &async_name);
    napi_create_async_work(env, nullptr, async_name,
        [](napi_env, void* raw) {
            auto* d = static_cast<LatencyData*>(raw);
            OH_LOG_INFO(LOG_APP, "vpnLatency: %{public}s:%{public}d x%d via OS socket",
                        d->host.c_str(), (int)d->port, d->count);

            struct sockaddr_in addr {};
            addr.sin_family = AF_INET;
            addr.sin_port = htons(d->port);
            inet_pton(AF_INET, d->host.c_str(), &addr.sin_addr);

            double times[50];
            int okCount = 0;
            for (int i = 0; i < d->count; i++) {
                double ms = 0;
                if (connect_with_timeout(addr, 2000, &ms) == 0) {
                    times[okCount++] = ms;
                }
                usleep(100000); // 100ms 间隔
            }

            char buf[2048];
            if (okCount == 0) {
                snprintf(buf, sizeof(buf), "✗ 全部 %d 次连接失败（隧道未建立或目标不可达）", d->count);
                d->result = buf;
                d->ok = false;
                return;
            }
            double mn = times[0], mx = times[0], sum = 0;
            for (int i = 0; i < okCount; i++) {
                if (times[i] < mn) mn = times[i];
                if (times[i] > mx) mx = times[i];
                sum += times[i];
            }
            double avg = sum / okCount;

            std::string detail;
            for (int i = 0; i < okCount && i < 20; i++) {
                char t[32];
                snprintf(t, sizeof(t), "%.1f", times[i]);
                detail += (i > 0 ? ", " : "") + std::string(t);
            }
            snprintf(buf, sizeof(buf),
                     "✓ 成功 %d/%d | min=%.1fms avg=%.1fms max=%.1fms\n各次(ms): %s",
                     okCount, d->count, mn, avg, mx, detail.c_str());
            d->result = buf;
            d->ok = true;
            OH_LOG_INFO(LOG_APP, "vpnLatency: done, ok=%{public}d/%{public}d avg=%.1fms",
                        okCount, d->count, avg);
        },
        [](napi_env env, napi_status, void* raw) {
            auto* d = static_cast<LatencyData*>(raw);
            napi_value result;
            napi_create_string_utf8(env, d->result.c_str(), d->result.length(), &result);
            if (d->ok) {
                napi_resolve_deferred(env, d->deferred, result);
            } else {
                napi_reject_deferred(env, d->deferred, result);
            }
            napi_delete_async_work(env, d->work);
            delete d;
        },
        data, &data->work);
    napi_queue_async_work(env, data->work);

    return promise;
}

// getRustLog(): string — 读取 Rust 日志文件最后 8000 字节
static napi_value GetRustLogNapi(napi_env env, napi_callback_info info) {
    std::string logPath = std::string(g_filesDir) + "/ts_rust.log";
    std::string content;
    FILE* rf = fopen(logPath.c_str(), "r");
    if (rf) {
        fseek(rf, 0, SEEK_END);
        long sz = ftell(rf);
        long readSz = sz > 8000 ? 8000 : sz;
        fseek(rf, sz - readSz, SEEK_SET);
        content.resize(readSz);
        size_t n = fread(content.data(), 1, readSz, rf);
        content.resize(n);
        fclose(rf);
    } else {
        content = "(无法读取日志: " + logPath + ")";
    }
    napi_value result;
    napi_create_string_utf8(env, content.c_str(), content.length(), &result);
    return result;
}

// getPeers(): string — 获取 tailnet 中所有设备列表（JSON）
static napi_value GetPeersNapi(napi_env env, napi_callback_info info) {
    ts_device* dev = g_ts_device.load();
    if (!dev) {
        napi_value result;
        napi_create_string_utf8(env, "[]", 2, &result);
        return result;
    }
    char* json = ts_peers(dev);
    napi_value result;
    if (json) {
        napi_create_string_utf8(env, json, NAPI_AUTO_LENGTH, &result);
        ts_free_string(json);
    } else {
        napi_create_string_utf8(env, "[]", 2, &result);
    }
    return result;
}

EXTERN_C_START
static napi_value Init(napi_env env, napi_value exports) {
    napi_property_descriptor desc[] = {

        { "setFilesDir", nullptr, SetFilesDir, nullptr, nullptr, nullptr, napi_default, nullptr },
        { "tailscaleInit", nullptr, TailscaleInitNapi, nullptr, nullptr, nullptr, napi_default, nullptr },
        { "tailscaleGetPeerIpv4", nullptr, TailscaleGetPeerIpv4Napi, nullptr, nullptr, nullptr, napi_default, nullptr },
        { "tailscaleDeinit", nullptr, TailscaleDeinitNapi, nullptr, nullptr, nullptr, napi_default, nullptr },
        { "tailscaleStatus", nullptr, TailscaleStatusNapi, nullptr, nullptr, nullptr, napi_default, nullptr },
        { "tailscaleTcpTest", nullptr, TailscaleTcpTestNapi, nullptr, nullptr, nullptr, napi_default, nullptr },
        { "sshConnect", nullptr, SshConnectNapi, nullptr, nullptr, nullptr, napi_default, nullptr },
        { "sshRead", nullptr, SshReadNapi, nullptr, nullptr, nullptr, napi_default, nullptr },
        { "sshWrite", nullptr, SshWriteNapi, nullptr, nullptr, nullptr, napi_default, nullptr },
        { "sshClose", nullptr, SshCloseNapi, nullptr, nullptr, nullptr, napi_default, nullptr },
        { "vpnBridge", nullptr, VpnBridgeNapi, nullptr, nullptr, nullptr, napi_default, nullptr },
        { "vpnDiag", nullptr, VpnDiagNapi, nullptr, nullptr, nullptr, napi_default, nullptr },
        { "vpnLatencyTest", nullptr, VpnLatencyNapi, nullptr, nullptr, nullptr, napi_default, nullptr },
        { "getRustLog", nullptr, GetRustLogNapi, nullptr, nullptr, nullptr, napi_default, nullptr },
        { "getPeers", nullptr, GetPeersNapi, nullptr, nullptr, nullptr, napi_default, nullptr },
    };
    napi_define_properties(env, exports, sizeof(desc) / sizeof(desc[0]), desc);
    return exports;
}
EXTERN_C_END

static napi_module Module = {
    .nm_version = 1,
    .nm_flags = 0,
    .nm_filename = nullptr,
    .nm_register_func = Init,
    .nm_modname = "entry",
    .nm_priv = ((void*)0),
    .reserved = { 0 },
};

extern "C" __attribute__((constructor)) void RegisterModule(void) {
    napi_module_register(&Module);
}
