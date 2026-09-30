package com.hrmlink.hrserver;

import android.util.Base64;
import android.util.Log;

import org.w3c.dom.Document;
import org.w3c.dom.NamedNodeMap;
import org.w3c.dom.Node;
import org.w3c.dom.NodeList;
import org.xml.sax.InputSource;

import java.io.StringReader;
import java.io.UnsupportedEncodingException;
import java.net.URLEncoder;
import java.security.MessageDigest;
import java.security.SecureRandom;
import java.text.SimpleDateFormat;
import java.util.ArrayList;
import java.util.Date;
import java.util.LinkedHashMap;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.TimeZone;
import java.util.concurrent.TimeUnit;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

import javax.xml.parsers.DocumentBuilderFactory;

import okhttp3.Authenticator;
import okhttp3.MediaType;
import okhttp3.OkHttpClient;
import okhttp3.Request;
import okhttp3.RequestBody;
import okhttp3.Response;
import okhttp3.Route;

/**
 * 极简 ONVIF 客户端（照 EXE camera/onvif_client.py 移植, 手写 SOAP + OkHttp, 零第三方依赖）
 *
 * 覆盖摄像头联动所需的最小方法集（P1 范围, PTZ/事件订阅属 P2 移动侦测暂不移植）:
 *   getDeviceInformation / getProfiles / getStreamUri / pickProfile / detectPort
 * 认证: WS-UsernameToken(Nonce+Created+Password 摘要, ONVIF 标准方式)。
 * 认证变体尝试序列与 py 完全一致: WS标准摘要 → HTTP Digest → SHA-256摘要 → b64nonce变体 → 明文;
 * 非认证类错误(网络/版本)不换认证方式; 认证失败时逐模式尝试, 成功后缓存该模式。
 * SOAP 信封每模式先 1.2 后 1.1(部分设备不支持 1.2), 可重试错误码: 400/401/415/426/500/VersionMismatch。
 * 设备时间偏移从 HTTP Date 响应头学习(摘要认证的 Created 需与设备时间同步)。
 *
 * 线程模型: 所有方法为同步阻塞网络 IO, 严禁在主线程调用(Caller 请自行安排后台线程)。
 * 每个实例持有独立 OkHttpClient(共享连接池, 由 PushChannels.httpClient() 派生),
 * HTTP Digest 认证经 OkHttp Authenticator 实现——仅当当前认证模式为 MODE_HTTP 时才应答 401 挑战,
 * 其余模式返回 null 让 401 走正常错误路径(与 py 的 opener 行为对齐)。
 */
public class OnvifClient {

    private static final String TAG = "HRServer";

    /** ONVIF 异常(消息对齐 py OnvifError 文案, 供 UI/日志直接展示) */
    public static class OnvifException extends Exception {
        OnvifException(String message) { super(message); }
    }

    /** 码流 profile: token + 名称 + 分辨率(w×h, 无分辨率信息时 0×0) */
    public static final class Profile {
        public final String token;
        public final String name;
        public final int width;
        public final int height;

        Profile(String token, String name, int width, int height) {
            this.token = token == null ? "" : token;
            this.name = name == null ? "" : name;
            this.width = width;
            this.height = height;
        }

        /** "宽x高", 无分辨率时空串 */
        public String res() {
            return width > 0 && height > 0 ? width + "x" + height : "";
        }

        @Override
        public String toString() {
            String r = res();
            return (name.isEmpty() ? token : name) + (r.isEmpty() ? "" : " (" + r + ")");
        }
    }

    /** 设备信息(GetDeviceInformation 应答, 端口探测/UI测试展示用) */
    public static final class DeviceInfo {
        public final String manufacturer;
        public final String model;
        public final String firmware;
        public final String serial;

        DeviceInfo(String manufacturer, String model, String firmware, String serial) {
            this.manufacturer = manufacturer;
            this.model = model;
            this.firmware = firmware;
            this.serial = serial;
        }

        /** 一行摘要, 空字段跳过 */
        public String summary() {
            List<String> ps = new ArrayList<>();
            if (!manufacturer.isEmpty()) ps.add(manufacturer);
            if (!model.isEmpty()) ps.add(model);
            if (!firmware.isEmpty()) ps.add(firmware);
            if (!serial.isEmpty()) ps.add(serial);
            return join(ps, " ");
        }
    }

    // 常见 ONVIF 端口, 探测时按序尝试(对齐 py COMMON_PORTS)
    private static final int[] COMMON_PORTS = {80, 8899, 8000, 2020, 7575, 5000, 8999};

    // media 服务地址兜底序列(对齐 py MEDIA_PATHS)
    private static final String[] MEDIA_PATHS = {
            "/onvif/media_service", "/onvif/Media", "/onvif/media", "/onvif/device_service"};

    private static final String SOAP_NS = "http://www.w3.org/2003/05/soap-envelope";       // SOAP 1.2
    private static final String SOAP11_NS = "http://schemas.xmlsoap.org/soap/envelope/";   // SOAP 1.1
    private static final String TT_NS = "http://www.onvif.org/ver10/schema";
    private static final String TRT_NS = "http://www.onvif.org/ver10/media/wsdl";
    private static final String TD_NS = "http://www.onvif.org/ver10/device/wsdl";

    private static final MediaType SOAP12_CT =
            MediaType.parse("application/soap+xml; charset=utf-8; action=\"\"");
    private static final MediaType SOAP11_CT = MediaType.parse("text/xml; charset=utf-8");

    // 认证模式索引(对齐 py AUTH_MODES 顺序)
    private static final int MODE_DIGEST = 0;    // WS标准摘要(SHA-1, 原始nonce字节)
    private static final int MODE_HTTP = 1;      // HTTP Digest(不带wsse头, 401挑战自动应答)
    private static final int MODE_SHA256 = 2;    // SHA-256摘要
    private static final int MODE_B64NONCE = 3;  // base64(nonce)字符串参与摘要(部分国产固件)
    private static final int MODE_PLAIN = 4;     // 明文密码
    private static final String[] MODE_NAMES = {
            "WS标准摘要", "HTTP Digest", "SHA-256摘要", "b64nonce摘要", "明文密码"};

    private static final SecureRandom RNG = new SecureRandom();
    private static final Pattern RTSP_URI_RE = Pattern.compile("^(rtsp://)([^/]+)(.*)$");

    private final String ip;
    private final String base;
    private final String username;
    private final String password;
    private final OkHttpClient http;

    /** 设备时间偏移秒数(从 Date 头学习; authenticator 回调线程/调用线程都会读) */
    private volatile long timeOffsetMs = 0;
    /** 当前认证模式(尝试序列推进; authenticator 回调线程也会读) */
    private volatile int authIdx = MODE_DIGEST;

    public OnvifClient(String ip, int port, String username, String password) {
        this(ip, port, username, password, 5000);
    }

    public OnvifClient(String ip, int port, String username, String password, int timeoutMs) {
        this.ip = ip == null ? "" : ip;
        this.base = "http://" + this.ip + ":" + port;
        this.username = username == null ? "" : username;
        this.password = password == null ? "" : password;
        this.http = PushChannels.httpClient().newBuilder()
                .callTimeout(timeoutMs, TimeUnit.MILLISECONDS)
                .authenticator(new HttpDigestAuth())
                .build();
    }

    // ================================ 公开方法 ================================

    /** 设备信息(设备服务, 端口探测/UI 测试用) */
    public DeviceInfo getDeviceInformation() throws OnvifException {
        Document root = call(base + "/onvif/device_service",
                "<td:GetDeviceInformation xmlns:td=\"" + TD_NS + "\"/>");
        return new DeviceInfo(
                textOf(root, "Manufacturer"),
                textOf(root, "Model"),
                textOf(root, "FirmwareVersion"),
                textOf(root, "SerialNumber"));
    }

    /**
     * 码流 profile 列表。解析对命名空间宽容: 部分设备(Arenti等)Profile元素不在标准trt命名空间下,
     * ONVIF规范用单数Profile, 部分设备返回复数Profiles, 两者都接受(对齐 py get_profiles)。
     */
    public List<Profile> getProfiles() throws OnvifException {
        Document root = mediaCall("<trt:GetProfiles xmlns:trt=\"" + TRT_NS + "\"/>");
        List<Profile> out = new ArrayList<>();
        for (Node p : allByLocal(root, "Profile", "Profiles")) {
            String token = attrByLocal(p, "token", "Token");
            Node nameEl = findByLocal(p, "Name");
            String name = nameEl == null ? "" : text(nameEl);
            int w = 0, h = 0;
            Node vec = findByLocal(p, "VideoEncoderConfiguration");
            if (vec != null) {
                Node rs = findByLocal(vec, "Resolution");
                if (rs != null) {
                    Node we = findByLocal(rs, "Width");
                    Node he = findByLocal(rs, "Height");
                    if (we != null && he != null) {
                        try {
                            w = Integer.parseInt(text(we).trim());
                            h = Integer.parseInt(text(he).trim());
                        } catch (NumberFormatException ignore) {
                        }
                    }
                }
            }
            out.add(new Profile(token, name, w, h));
        }
        if (out.isEmpty()) {
            // 诊断: 设备私有格式解析失败时记日志, 便于按真实结构适配(对齐 py warning)
            Log.w(TAG, "[ONVIF:" + ip + "] GetProfiles 未解析到Profile");
        }
        return out;
    }

    /** 取流地址(RTSP), 已注入 user:pass@ 凭据(对齐 py get_stream_uri, StreamType=RTP-Unicast/RTSP) */
    public String getStreamUri(String profileToken) throws OnvifException {
        String body = "<trt:GetStreamUri xmlns:trt=\"" + TRT_NS + "\">"
                + "<StreamSetup><Stream xmlns=\"" + TT_NS + "\">RTP-Unicast</Stream>"
                + "<Transport xmlns=\"" + TT_NS + "\"><Protocol>RTSP</Protocol></Transport>"
                + "</StreamSetup><ProfileToken>" + esc(profileToken) + "</ProfileToken></trt:GetStreamUri>";
        Document root = mediaCall(body);
        Node u = findByLocal(root, "Uri");   // tt:Uri 及非标准命名空间设备兜底一并覆盖
        String uri = u == null ? "" : text(u).trim();
        if (uri.isEmpty()) {
            throw new OnvifException("设备未返回流地址");
        }
        return injectCred(uri, username, password);
    }

    /**
     * 选取码流 profile。preferSub=true 取子码流(分辨率最低), false 取主码流(分辨率最高);
     * 全部无分辨率信息时按名字猜(sub/子/low/second 或 main/主/high), 再不行取第一个(对齐 py)。
     */
    public Profile pickProfile(boolean preferSub) throws OnvifException {
        List<Profile> profiles = getProfiles();
        if (profiles.isEmpty()) {
            throw new OnvifException("设备无可用 Profile");
        }
        List<Profile> haveRes = new ArrayList<>();
        for (Profile p : profiles) {
            if (p.width > 0 && p.height > 0) haveRes.add(p);
        }
        if (!haveRes.isEmpty()) {
            Profile best = haveRes.get(0);
            for (Profile p : haveRes) {
                long a = (long) p.width * p.height;
                long b = (long) best.width * best.height;
                if (preferSub ? a < b : a > b) best = p;
            }
            return best;
        }
        for (Profile p : profiles) {
            String n = p.name.toLowerCase(Locale.US);
            if (preferSub && (n.contains("sub") || p.name.contains("子")
                    || n.contains("low") || n.contains("second"))) return p;
            if (!preferSub && (n.contains("main") || p.name.contains("主") || n.contains("high"))) return p;
        }
        return profiles.get(0);
    }

    /**
     * 依次探测常见端口, 返回第一个能通过 GetDeviceInformation 认证的端口(对齐 py detect_onvif_port)。
     * 连接层面失败继续试下一端口; 认证层面失败(端口对但密码错)直接抛出更友好。
     */
    public static int detectPort(String ip, String username, String password) throws OnvifException {
        return detectPort(ip, username, password, 3000);
    }

    public static int detectPort(String ip, String username, String password, int timeoutMs)
            throws OnvifException {
        OnvifException last = null;
        for (int port : COMMON_PORTS) {
            try {
                OnvifClient c = new OnvifClient(ip, port, username, password, timeoutMs);
                c.getDeviceInformation();
                return port;
            } catch (OnvifException e) {
                last = e;
                String low = e.getMessage() == null ? "" : e.getMessage().toLowerCase(Locale.US);
                if (low.contains("notauthorized") || low.contains("auth")
                        || low.contains("sender") || low.contains("user")) {
                    throw new OnvifException("端口 " + port + " 可达但认证失败: " + e.getMessage());
                }
            }
        }
        throw new OnvifException("所有常见端口均不可达(" + ip + "), 最后错误: "
                + (last == null ? "" : last.getMessage()));
    }

    // ================================ SOAP 底层 ================================

    /** media 服务调用: 路径探测兜底, SOAP Fault(认证/参数问题)换路径也无解直接抛(对齐 py _media_call) */
    private Document mediaCall(String body) throws OnvifException {
        OnvifException last = null;
        for (String path : MEDIA_PATHS) {
            try {
                return call(base + path, body);
            } catch (OnvifException e) {
                last = e;
                if (e.getMessage() != null && e.getMessage().contains("SOAP Fault")) throw e;
            }
        }
        throw last != null ? last : new OnvifException("media服务不可达");
    }

    /**
     * 执行请求; 认证失败时按认证模式序列逐个尝试(成功后缓存该模式), 非认证类错误不换认证方式。
     * 每模式先 SOAP 1.2 后 1.1(对齐 py _call/_attempt)。
     */
    private Document call(String url, String body) throws OnvifException {
        String raw = null;
        OnvifException err = null;
        List<String> tried = new ArrayList<>();
        for (int i = 0; i <= MODE_PLAIN; i++) {
            authIdx = i;
            try {
                raw = attempt(url, body);
                break;
            } catch (OnvifException e) {
                err = e;
                String s = e.getMessage() == null ? "" : e.getMessage();
                tried.add(MODE_NAMES[i]);
                String low = s.toLowerCase(Locale.US);
                // 裸401(无SOAP Fault体)也属认证类错误
                boolean notAuth = s.contains("NotAuthorized") || low.contains("not authorized")
                        || low.contains("http 401");
                if (!notAuth) throw e;   // 非认证类错误(网络/版本等)不换认证方式
            }
        }
        if (raw == null) {
            throw new OnvifException((err == null ? "" : err.getMessage())
                    + " (已尝试" + join(tried, "/") + "均被拒, 请核对ONVIF密码)");
        }
        Document root = parseDom(raw);
        Node fault = findFault(root);
        if (fault != null) {
            LinkedHashSet<String> texts = new LinkedHashSet<>();
            collectTexts(fault, texts);
            List<String> parts = new ArrayList<>(texts);
            throw new OnvifException(trunc("SOAP Fault: " + join(parts, " | "), 200));
        }
        return root;
    }

    /** 按当前认证模式尝试 1.2 → 1.1 两种信封(可重试错误继续, 其余直接抛; 对齐 py _attempt) */
    private String attempt(String url, String body) throws OnvifException {
        OnvifException last = null;
        for (boolean soap11 : new boolean[]{false, true}) {
            try {
                return post(url, body, soap11);
            } catch (OnvifException e) {
                last = e;
                String s = e.getMessage() == null ? "" : e.getMessage();
                boolean retryable = s.contains("HTTP 400") || s.contains("HTTP 401")
                        || s.contains("HTTP 415") || s.contains("HTTP 426") || s.contains("HTTP 500")
                        || s.contains("VersionMismatch")
                        || s.contains("NotAuthorized") || s.toLowerCase(Locale.US).contains("not authorized");
                if (!retryable) throw e;
            }
        }
        throw last != null ? last : new OnvifException("请求失败");
    }

    /** 发送 SOAP 请求; soap11=True 用 SOAP 1.1 格式(部分设备不支持 1.2) */
    private String post(String url, String body, boolean soap11) throws OnvifException {
        String env = soap11 ? "env" : "s";
        String ns = soap11 ? SOAP11_NS : SOAP_NS;
        // HTTP Digest 模式不带 wsse 头(对齐 py _wsse_kwargs 返回 None)
        String hdr = authIdx == MODE_HTTP ? "" : wsseHeader(env);
        String xml = "<?xml version=\"1.0\" encoding=\"UTF-8\"?>"
                + "<" + env + ":Envelope xmlns:" + env + "=\"" + ns + "\">"
                + "<" + env + ":Header>" + hdr + "</" + env + ":Header>"
                + "<" + env + ":Body>" + body + "</" + env + ":Body></" + env + ":Envelope>";
        Request.Builder rb = new Request.Builder().url(url)
                .post(RequestBody.create(soap11 ? SOAP11_CT : SOAP12_CT, xml));
        if (soap11) rb.header("SOAPAction", "\"\"");
        Response resp;
        try {
            resp = http.newCall(rb.build()).execute();
        } catch (Exception e) {
            throw new OnvifException("连接失败: " + e);
        }
        try {
            syncTime(resp);
            String text = readBody(resp);
            if (resp.code() < 200 || resp.code() >= 300) {
                // 设备常以 400/500 返回 SOAP Fault, 提取关键信息便于定位(对齐 py)
                throw new OnvifException("HTTP " + resp.code() + " " + faultSummary(text));
            }
            return text;
        } catch (OnvifException e) {
            throw e;
        } catch (Exception e) {
            throw new OnvifException("连接失败: " + e);
        } finally {
            resp.close();
        }
    }

    /** WS-UsernameToken 安全头(对齐 py _wsse_header: env前缀/摘要算法/nonce变体/明文) */
    private String wsseHeader(String env) {
        byte[] nonce = new byte[16];
        RNG.nextBytes(nonce);
        String nonceB64 = b64(nonce);
        String created = utcCreated(timeOffsetMs);
        String pwdXml;
        try {
            if (authIdx == MODE_PLAIN) {
                pwdXml = "<wsse:Password Type=\"http://docs.oasis-open.org/wss/2004/01/"
                        + "oasis-200401-wss-username-token-profile-1.0#PasswordText\">"
                        + esc(password) + "</wsse:Password>";
            } else {
                // 摘要输入: 标准为原始nonce字节; 变体设备(部分国产固件)用 base64(nonce) 字符串
                byte[] nonceIn = authIdx == MODE_B64NONCE ? nonceB64.getBytes("UTF-8") : nonce;
                MessageDigest h = MessageDigest.getInstance(authIdx == MODE_SHA256 ? "SHA-256" : "SHA-1");
                h.update(nonceIn);
                h.update(created.getBytes("UTF-8"));
                h.update(password.getBytes("UTF-8"));
                String digest = b64(h.digest());
                pwdXml = "<wsse:Password Type=\"http://docs.oasis-open.org/wss/2004/01/"
                        + "oasis-200401-wss-username-token-profile-1.0#PasswordDigest\">"
                        + digest + "</wsse:Password>";
            }
        } catch (Exception e) {
            // MessageDigest/UTF-8 平台必然支持, 理论不可达
            throw new IllegalStateException(e);
        }
        return "<wsse:Security " + env + ":mustUnderstand=\"1\" "
                + "xmlns:wsse=\"http://docs.oasis-open.org/wss/2004/01/"
                + "oasis-200401-wss-wssecurity-secext-1.0.xsd\">"
                + "<wsse:UsernameToken><wsse:Username>" + esc(username) + "</wsse:Username>"
                + pwdXml
                + "<wsse:Nonce EncodingType=\"http://docs.oasis-open.org/wss/2004/01/"
                + "oasis-200401-wss-soap-message-security-1.0#Base64Binary\">" + nonceB64 + "</wsse:Nonce>"
                + "<wsu:Created xmlns:wsu=\"http://docs.oasis-open.org/wss/2004/01/"
                + "oasis-200401-wss-wssecurity-utility-1.0.xsd\">" + created + "</wsu:Created>"
                + "</wsse:UsernameToken></wsse:Security>";
    }

    /** 从 HTTP Date 响应头学习设备时间偏移(摘要认证的 Created 需与设备时间同步) */
    private void syncTime(Response resp) {
        Date d = resp.headers().getDate("Date");
        if (d != null) {
            timeOffsetMs = d.getTime() - System.currentTimeMillis();
        }
    }

    // ================================ HTTP Digest(OkHttp Authenticator) ================================

    /** 仅在 MODE_HTTP 模式下应答 401 Digest 挑战(qop=auth / 无qop, MD5 / MD5-sess) */
    private final class HttpDigestAuth implements Authenticator {
        @Override
        public Request authenticate(Route route, Response response) {
            // 已带 Authorization 仍401或已重试过: 放弃, 让上层走错误路径
            if (response.request().header("Authorization") != null || response.priorResponse() != null) {
                return null;
            }
            if (authIdx != MODE_HTTP) return null;   // 其余模式401走正常错误路径(对齐 py)
            String ch = response.header("WWW-Authenticate");
            if (ch == null) return null;
            String c = ch.trim();
            if (!c.regionMatches(true, 0, "Digest", 0, 6)) return null;
            Map<String, String> p = parseChallenge(c.substring(6));
            String realm = p.get("realm");
            String nonce = p.get("nonce");
            if (realm == null || nonce == null) return null;
            String pathQuery = response.request().url().encodedQuery() == null
                    ? response.request().url().encodedPath()
                    : response.request().url().encodedPath() + "?" + response.request().url().encodedQuery();
            String method = response.request().method();
            String cnonce = randomHex();
            String nc = "00000001";
            String algo = p.containsKey("algorithm") ? p.get("algorithm") : "MD5";
            String ha1;
            if ("MD5-sess".equalsIgnoreCase(algo)) {
                ha1 = md5Hex(md5Hex(username + ":" + realm + ":" + password) + ":" + nonce + ":" + cnonce);
            } else {
                ha1 = md5Hex(username + ":" + realm + ":" + password);
            }
            String ha2 = md5Hex(method + ":" + pathQuery);
            String qop = p.get("qop");
            boolean useQop = qop != null && qop.contains("auth");
            String respHash = useQop
                    ? md5Hex(ha1 + ":" + nonce + ":" + nc + ":" + cnonce + ":auth:" + ha2)
                    : md5Hex(ha1 + ":" + nonce + ":" + ha2);
            StringBuilder auth = new StringBuilder("Digest username=\"").append(quoteEsc(username))
                    .append("\", realm=\"").append(quoteEsc(realm))
                    .append("\", nonce=\"").append(nonce)
                    .append("\", uri=\"").append(pathQuery)
                    .append("\", response=\"").append(respHash).append("\"");
            if (p.containsKey("opaque")) auth.append(", opaque=\"").append(quoteEsc(p.get("opaque"))).append("\"");
            if (useQop) auth.append(", qop=auth, nc=").append(nc).append(", cnonce=\"").append(cnonce).append("\"");
            if (p.containsKey("algorithm")) auth.append(", algorithm=").append(algo);
            return response.request().newBuilder().header("Authorization", auth.toString()).build();
        }

        /** 解析 Digest 挑战参数: k="v" 或 k=v(对齐设备实际格式, key 转小写) */
        private Map<String, String> parseChallenge(String rest) {
            Map<String, String> out = new LinkedHashMap<>();
            Matcher m = Pattern.compile("(\\w+)\\s*=\\s*(?:\"([^\"]*)\"|([^\\s,]+))").matcher(rest);
            while (m.find()) {
                out.put(m.group(1).toLowerCase(Locale.US), m.group(2) != null ? m.group(2) : m.group(3));
            }
            return out;
        }
    }

    // ================================ XML 工具(局部名匹配, 兼容设备私有格式) ================================

    private static Document parseDom(String xml) throws OnvifException {
        try {
            DocumentBuilderFactory f = DocumentBuilderFactory.newInstance();
            f.setNamespaceAware(true);
            try {
                f.setFeature("http://apache.org/xml/features/disallow-doctype-decl", true);
            } catch (Exception ignore) {
                // 部分实现不支持该 feature, 忽略
            }
            return f.newDocumentBuilder().parse(new InputSource(new StringReader(xml)));
        } catch (Exception e) {
            throw new OnvifException("应答解析失败: " + e);
        }
    }

    /** 查找 SOAP 1.2/1.1 信封下的 Fault 元素(命名空间宽容: null 也接受) */
    private static Node findFault(Document root) {
        for (Node n : allByLocal(root, "Fault")) {
            String ns = n.getNamespaceURI();
            if (SOAP_NS.equals(ns) || SOAP11_NS.equals(ns) || ns == null || ns.isEmpty()) return n;
        }
        return null;
    }

    /** 深度优先按局部名查第一个匹配节点(忽略命名空间, 对齐 py _find_local) */
    private static Node findByLocal(Node root, String... locals) {
        List<Node> out = allByLocal(root, locals);
        return out.isEmpty() ? null : out.get(0);
    }

    /** 深度先序遍历收集局部名匹配的元素节点(忽略命名空间) */
    private static List<Node> allByLocal(Node root, String... locals) {
        List<Node> out = new ArrayList<>();
        walk(root, locals, out);
        return out;
    }

    private static void walk(Node n, String[] locals, List<Node> out) {
        short t = n.getNodeType();
        if (t == Node.ELEMENT_NODE) {
            String ln = local(n);
            for (String want : locals) {
                if (want.equals(ln)) {
                    out.add(n);
                    break;
                }
            }
        }
        NodeList ch = n.getChildNodes();
        for (int i = 0; i < ch.getLength(); i++) {
            walk(ch.item(i), locals, out);
        }
    }

    /** 元素局部名(裸 nodeName 剥前缀, 设备 XML 命名空间残缺时 getLocalName 可能为 null) */
    private static String local(Node n) {
        String nn = n.getNodeName();
        int i = nn.indexOf(':');
        return i >= 0 ? nn.substring(i + 1) : nn;
    }

    /** 按局部名(大小写不敏感)取属性值 */
    private static String attrByLocal(Node n, String... names) {
        NamedNodeMap a = n.getAttributes();
        if (a == null) return "";
        for (int i = 0; i < a.getLength(); i++) {
            Node it = a.item(i);
            String ln = local(it);
            for (String want : names) {
                if (want.equalsIgnoreCase(ln)) return it.getNodeValue() == null ? "" : it.getNodeValue();
            }
        }
        return "";
    }

    /** 找指定局部名元素并取其合并文本(trim, 对齐 py _text) */
    private static String textOf(Node ctx, String name) {
        Node el = findByLocal(ctx, name);
        return el == null ? "" : text(el);
    }

    private static String text(Node n) {
        StringBuilder sb = new StringBuilder();
        NodeList ch = n.getChildNodes();
        for (int i = 0; i < ch.getLength(); i++) {
            Node c = ch.item(i);
            if (c.getNodeType() == Node.TEXT_NODE || c.getNodeType() == Node.CDATA_SECTION_NODE) {
                sb.append(c.getNodeValue());
            }
        }
        return sb.toString().trim();
    }

    /** 收集子树全部非空文本(保持顺序, 去重由调用方 LinkedHashSet 完成) */
    private static void collectTexts(Node n, LinkedHashSet<String> out) {
        if (n.getNodeType() == Node.TEXT_NODE) {
            String v = n.getNodeValue() == null ? "" : n.getNodeValue().trim();
            if (!v.isEmpty()) out.add(v);
        }
        NodeList ch = n.getChildNodes();
        for (int i = 0; i < ch.getLength(); i++) {
            collectTexts(ch.item(i), out);
        }
    }

    /** 从设备 SOAP 应答提取 Fault 关键信息, 避免整段 namespace 轰炸UI(对齐 py _fault_summary) */
    private static String faultSummary(String detail) {
        Document d;
        try {
            d = parseDom(detail);
        } catch (Exception e) {
            return trunc(detail, 150);
        }
        List<String> parts = new ArrayList<>();
        LinkedHashSet<String> texts = new LinkedHashSet<>();
        for (Node n : allByLocal(d, "Value", "Text", "faultcode", "faultstring")) {
            texts.clear();
            collectTexts(n, texts);
            for (String t : texts) {
                if (!parts.contains(t)) parts.add(t);
            }
        }
        return parts.isEmpty() ? trunc(detail, 150) : trunc(join(parts, " | "), 200);
    }

    // ================================ 通用小工具 ================================

    /** 设备返回的 rtsp 地址可能不带账号密码, 注入 rtsp://user:pass@host 标准形式(对齐 py _inject_credentials) */
    private static String injectCred(String uri, String user, String pass) {
        if (uri.contains("@") || user == null || user.isEmpty()) return uri;
        Matcher m = RTSP_URI_RE.matcher(uri);
        if (m.matches()) {
            return "rtsp://" + qenc(user) + ":" + qenc(pass == null ? "" : pass)
                    + "@" + m.group(2) + m.group(3);
        }
        return uri;
    }

    /** URL 编码(空格用 %20 而非 +, 对齐 py urllib quote safe="") */
    private static String qenc(String v) {
        try {
            return URLEncoder.encode(v, "UTF-8").replace("+", "%20");
        } catch (UnsupportedEncodingException e) {
            return v == null ? "" : v;
        }
    }

    private static String b64(byte[] data) {
        return Base64.encodeToString(data, Base64.NO_WRAP);
    }

    private static String utcCreated(long offsetMs) {
        SimpleDateFormat f = new SimpleDateFormat("yyyy-MM-dd'T'HH:mm:ss'Z'", Locale.US);
        f.setTimeZone(TimeZone.getTimeZone("UTC"));
        return f.format(new Date(System.currentTimeMillis() + offsetMs));
    }

    private static String randomHex() {
        byte[] b = new byte[8];
        RNG.nextBytes(b);
        StringBuilder sb = new StringBuilder(16);
        for (byte x : b) sb.append(String.format(Locale.US, "%02x", x));
        return sb.toString();
    }

    private static String md5Hex(String s) {
        try {
            MessageDigest md = MessageDigest.getInstance("MD5");
            byte[] d = md.digest(s.getBytes("UTF-8"));
            StringBuilder sb = new StringBuilder(32);
            for (byte x : d) sb.append(String.format(Locale.US, "%02x", x));
            return sb.toString();
        } catch (Exception e) {
            throw new IllegalStateException(e);   // MD5/UTF-8 平台必然支持
        }
    }

    private static String esc(String s) {
        if (s == null) return "";
        return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;");
    }

    private static String quoteEsc(String s) {
        return s == null ? "" : s.replace("\\", "\\\\").replace("\"", "\\\"");
    }

    private static String readBody(Response resp) {
        try {
            String s = resp.body() == null ? "" : resp.body().string();
            return s.length() > 512 * 1024 ? s.substring(0, 512 * 1024) : s;   // SOAP 应答极小, 防御性截断
        } catch (Exception e) {
            return "";
        }
    }

    private static String trunc(String s, int n) {
        if (s == null) return "";
        return s.length() > n ? s.substring(0, n) : s;
    }

    private static String join(List<String> list, String sep) {
        StringBuilder sb = new StringBuilder();
        for (int i = 0; i < list.size(); i++) {
            if (i > 0) sb.append(sep);
            sb.append(list.get(i));
        }
        return sb.toString();
    }
}
