package com.cleanmaster.link;

import android.content.Context;

import org.json.JSONObject;

import java.net.DatagramPacket;
import java.net.DatagramSocket;
import java.net.InetAddress;
import java.net.InetSocketAddress;
import java.net.InterfaceAddress;
import java.net.NetworkInterface;
import java.net.SocketTimeoutException;
import java.security.SecureRandom;
import java.util.ArrayList;
import java.util.Enumeration;
import java.util.HashMap;
import java.util.Iterator;
import java.util.List;
import java.util.Map;

/**
 * 局域网发现 + 「双方同意后连接」（UDP 47777，UTF-8 JSON，单包 &lt;2KB）。
 *
 * 手机侧职责：
 *   · 绑定 47777（失败退化为随机端口，并把实际端口写进 ANNOUNCE 的 port）；
 *   · 未连接时每 3 秒广播 DISCOVER（找电脑）与 ANNOUNCE（让电脑发现自己）；
 *   · 维护发现的电脑列表（OFFER，12 秒过期）；
 *   · 手机发起连接：从监听 socket 单播 CONNECT_REQ，等待 ACCEPT / DENY；
 *   · 电脑发起连接：收到 CONNECT_REQ 后回调界面弹框，允许则从同一 socket 回 ACCEPT，
 *     随后等待 TOKEN；拒绝则回 DENY；
 *   · 连接成功后不再广播，收到电脑的连接请求直接回 DENY（reason「手机已连接」）。
 *
 * 注意：REQ / ACCEPT / DENY / TOKEN 都按「包的来源地址+端口」单播，
 * 所以所有发送都必须复用同一个监听 socket，不能临时 new 一个。
 *
 * 回调统一抛给 {@link Listener}，由调用方自己切主线程（MainActivity 用 Handler）。
 */
public final class LanDiscovery {

    public static final int PORT = 47777;

    /** 发现的电脑超过该时间没再收到 OFFER 就算离线 */
    private static final long OFFER_TTL = 12000;
    /** 未连接时的广播间隔 */
    private static final long BROADCAST_INTERVAL = 3000;
    /** 等待电脑确认 / 下发 token 的超时 */
    private static final long REQ_TIMEOUT = 45000;
    /** 收到的连接请求在本地保留的时长（超时后不再允许点「允许」） */
    private static final long INCOMING_TTL = 60000;

    private static final String MAGIC_DISCOVER = "CMLINK_DISCOVER";
    private static final String MAGIC_OFFER = "CMLINK_OFFER";
    private static final String MAGIC_ANNOUNCE = "CMLINK_ANNOUNCE";
    private static final String MAGIC_CONNECT_REQ = "CMLINK_CONNECT_REQ";
    private static final String MAGIC_CONNECT_ACCEPT = "CMLINK_CONNECT_ACCEPT";
    private static final String MAGIC_CONNECT_DENY = "CMLINK_CONNECT_DENY";
    private static final String MAGIC_CONNECT_TOKEN = "CMLINK_CONNECT_TOKEN";

    private static volatile LanDiscovery sInstance;

    /** 局域网事件回调（在后台线程触发，调用方自行切主线程） */
    public interface Listener {
        /** 电脑请求连接本机，需要弹「是否允许」 */
        void onPcConnectRequest(ConnectRequest request);

        /** 连接已谈妥（手机发起收到 ACCEPT，或电脑发起收到 TOKEN），可保存并连接 */
        void onPcReady(Computer pc, String token);

        /** 电脑拒绝 / 请求被取消 */
        void onPcDenied(String reqId, String reason);

        /** 本机发起的请求超时（45 秒） */
        void onRequestTimeout(String reqId);
    }

    /** 局域网里发现的一台电脑 */
    public static class Computer {
        public String name = "";
        public String ip = "";
        public String id = "";
        public int port = 8080;
        public boolean needPair = true;
        public long lastSeen = 0;

        public String label() {
            return (name == null || name.isEmpty() ? "未知电脑" : name)
                    + "  (" + ip + ":" + port + ")";
        }
    }

    /** 电脑发来的连接请求（记下来源地址+端口，回 ACCEPT / DENY 用） */
    public static class ConnectRequest {
        public String reqId = "";
        public String pcId = "";
        public String pcName = "";
        public String ip = "";
        public int httpPort = 8080;
        public int srcPort = PORT;
        public long ts = 0;
    }

    private final Context app;
    private final SecureRandom random = new SecureRandom();

    private final Map<String, Computer> computers = new HashMap<String, Computer>();
    /** 本机发起的请求：reqId -> 超时时间戳 */
    private final Map<String, Long> outgoing = new HashMap<String, Long>();
    /** 已回 ACCEPT、等待电脑下发 TOKEN 的请求：reqId -> 超时时间戳 */
    private final Map<String, Long> awaitingToken = new HashMap<String, Long>();
    /** 电脑发起、等待界面选择的请求：reqId -> 请求 */
    private final Map<String, ConnectRequest> incoming = new HashMap<String, ConnectRequest>();

    private volatile Listener listener;
    private volatile boolean running = false;
    private volatile DatagramSocket socket;
    private volatile int localPort = PORT;
    private volatile Thread rxThread;
    private volatile Thread txThread;
    private byte[] announcePayload;
    private byte[] discoverPayload;
    private String phoneId = "";
    private String phoneName = "";

    private final Object wakeLock = new Object();

    private LanDiscovery(Context context) {
        app = context;
    }

    public static LanDiscovery get(Context context) {
        LanDiscovery d = sInstance;
        if (d == null) {
            synchronized (LanDiscovery.class) {
                d = sInstance;
                if (d == null) {
                    d = new LanDiscovery(context.getApplicationContext());
                    sInstance = d;
                }
            }
        }
        return d;
    }

    public void setListener(Listener l) {
        listener = l;
    }

    public boolean isRunning() {
        return running;
    }

    /** 当前监听端口（绑定失败时会退化为随机端口） */
    public int getLocalPort() {
        return localPort;
    }

    // ==================== 启停 ====================

    public synchronized void start(Context context) {
        if (running) {
            return;
        }
        Context ctx = context == null ? app : context.getApplicationContext();
        phoneId = Prefs.getDeviceId(ctx);
        phoneName = Prefs.getDeviceName(ctx);

        DatagramSocket s = openSocket();
        if (s == null) {
            AppLog.add("局域网发现启动失败：UDP " + PORT + " 不可用");
            return;
        }
        socket = s;
        running = true;
        discoverPayload = buildDiscover();
        announcePayload = buildAnnounce();

        rxThread = new Thread(new RxLoop(), "cmlink-lan-rx");
        rxThread.setDaemon(true);
        rxThread.start();
        txThread = new Thread(new TxLoop(), "cmlink-lan-tx");
        txThread.setDaemon(true);
        txThread.start();
        AppLog.add("局域网发现已开启（UDP " + localPort + "）");
    }

    public synchronized void stop() {
        running = false;
        DatagramSocket s = socket;
        socket = null;
        if (s != null) {
            try {
                s.close();
            } catch (Exception ignored) {
            }
        }
        synchronized (wakeLock) {
            wakeLock.notifyAll();
        }
        Thread r = rxThread;
        rxThread = null;
        if (r != null) {
            r.interrupt();
        }
        Thread t = txThread;
        txThread = null;
        if (t != null) {
            t.interrupt();
        }
        synchronized (this) {
            computers.clear();
            outgoing.clear();
            awaitingToken.clear();
            incoming.clear();
        }
    }

    private DatagramSocket openSocket() {
        DatagramSocket s = null;
        try {
            s = new DatagramSocket(null);
            s.setReuseAddress(true);
            s.setBroadcast(true);
            s.bind(new InetSocketAddress(PORT));
            s.setSoTimeout(800);
            localPort = PORT;
            return s;
        } catch (Exception e) {
            if (s != null) {
                try {
                    s.close();
                } catch (Exception ignored) {
                }
            }
        }
        try {
            DatagramSocket r = new DatagramSocket();
            r.setBroadcast(true);
            r.setSoTimeout(800);
            localPort = r.getLocalPort();
            return r;
        } catch (Exception e) {
            localPort = PORT;
            return null;
        }
    }

    // ==================== 对外操作 ====================

    /** 立即广播一次（搜索按钮用），之后每 3 秒仍会自动广播 */
    public void searchNow() {
        final DatagramSocket s = socket;
        if (s == null || !running || LinkService.isConnected()) {
            return;
        }
        final byte[] d = discoverPayload;
        final byte[] a = announcePayload;
        // 广播也是网络操作，不能跑在主线程
        Thread t = new Thread(new Runnable() {
            @Override
            public void run() {
                sendBroadcasts(s, d);
                sendBroadcasts(s, a);
            }
        }, "cmlink-lan-search");
        t.setDaemon(true);
        t.start();
    }

    /**
     * 在后台线程发送一个 UDP 包。
     *
     * Android 主线程做网络 I/O 会抛 NetworkOnMainThreadException，
     * 之前 requestConnect / acceptRequest 直接在按钮回调里发送，
     * 异常被 sendTo 吞掉后表现为「请求发送失败」和「一直等不到凭据」。
     */
    private void sendAsync(final DatagramSocket s, final String ip, final int port,
                           final byte[] data, final Runnable onFail) {
        Thread t = new Thread(new Runnable() {
            @Override
            public void run() {
                if (sendTo(s, ip, port, data)) {
                    return;
                }
                if (onFail != null) {
                    try {
                        onFail.run();
                    } catch (Exception ignored) {
                    }
                }
            }
        }, "cmlink-lan-send");
        t.setDaemon(true);
        t.start();
    }

    /** 当前在线的电脑列表（OFFER 已过期的不返回） */
    public List<Computer> getComputers() {
        long now = System.currentTimeMillis();
        List<Computer> out = new ArrayList<Computer>();
        synchronized (this) {
            Iterator<Map.Entry<String, Computer>> it = computers.entrySet().iterator();
            while (it.hasNext()) {
                Computer c = it.next().getValue();
                if (c == null) {
                    it.remove();
                    continue;
                }
                if (now - c.lastSeen > OFFER_TTL) {
                    it.remove();
                    continue;
                }
                out.add(c);
            }
        }
        return out;
    }

    /** 是否已有等待电脑确认的请求（同一时间只保留一个） */
    public boolean isWaiting() {
        synchronized (this) {
            return !outgoing.isEmpty();
        }
    }

    /**
     * 向发现到的电脑发起连接请求。
     *
     * @return false 表示发送失败，或已有一个请求在等待确认（用 isWaiting 区分）
     */
    public boolean requestConnect(Computer pc) {
        if (pc == null || pc.ip == null || pc.ip.isEmpty()) {
            return false;
        }
        final DatagramSocket s = socket;
        if (s == null || !running) {
            return false;
        }
        synchronized (this) {
            if (!outgoing.isEmpty()) {
                return false;
            }
        }
        final String reqId = randomHex(8);
        final byte[] data;
        try {
            JSONObject o = new JSONObject();
            o.put("magic", MAGIC_CONNECT_REQ);
            o.put("v", 1);
            o.put("reqId", reqId);
            o.put("from", "phone");
            o.put("id", phoneId);
            o.put("name", phoneName);
            o.put("to", pc.id == null ? "" : pc.id);
            data = o.toString().getBytes("UTF-8");
        } catch (Exception e) {
            return false;
        }
        synchronized (this) {
            outgoing.put(reqId, Long.valueOf(System.currentTimeMillis() + REQ_TIMEOUT));
        }
        // 注意：一定要在后台线程发送，主线程直接 sendto 会被系统拒绝
        sendAsync(s, pc.ip, PORT, data, new Runnable() {
            @Override
            public void run() {
                boolean removed;
                synchronized (LanDiscovery.this) {
                    removed = outgoing.remove(reqId) != null;
                }
                if (removed) {
                    AppLog.add("连接请求发送失败");
                    Listener l = listener;
                    if (l != null) {
                        l.onPcDenied(reqId, "连接请求发送失败，请重试");
                    }
                }
            }
        });
        return true;
    }

    /** 同意电脑的连接请求：从同一监听 socket 回 ACCEPT，之后等电脑下发 TOKEN */
    public void acceptRequest(ConnectRequest req) {
        if (req == null) {
            return;
        }
        final DatagramSocket s = socket;
        if (s == null) {
            synchronized (this) {
                incoming.remove(req.reqId);
            }
            return;
        }
        final byte[] data;
        try {
            JSONObject o = new JSONObject();
            o.put("magic", MAGIC_CONNECT_ACCEPT);
            o.put("v", 1);
            o.put("reqId", req.reqId);
            data = o.toString().getBytes("UTF-8");
        } catch (Exception e) {
            return;
        }
        synchronized (this) {
            incoming.remove(req.reqId);
            // 先登记再发送：电脑回 TOKEN 可能比这行代码还快
            awaitingToken.put(req.reqId, Long.valueOf(System.currentTimeMillis() + REQ_TIMEOUT));
        }
        sendAsync(s, req.ip, req.srcPort, data, new Runnable() {
            @Override
            public void run() {
                boolean removed;
                synchronized (LanDiscovery.this) {
                    removed = awaitingToken.remove(req.reqId) != null;
                }
                if (removed) {
                    AppLog.add("回复电脑失败，请重试");
                    Listener l = listener;
                    if (l != null) {
                        l.onPcDenied(req.reqId, "回复电脑失败，请重试");
                    }
                }
            }
        });
    }

    /** 拒绝电脑的连接请求 */
    public void denyRequest(ConnectRequest req, String reason) {
        if (req == null) {
            return;
        }
        final DatagramSocket s = socket;
        synchronized (this) {
            incoming.remove(req.reqId);
        }
        if (s == null) {
            return;
        }
        final byte[] data;
        try {
            JSONObject o = new JSONObject();
            o.put("magic", MAGIC_CONNECT_DENY);
            o.put("v", 1);
            o.put("reqId", req.reqId);
            o.put("reason", reason == null ? "" : reason);
            data = o.toString().getBytes("UTF-8");
        } catch (Exception e) {
            return;
        }
        sendAsync(s, req.ip, req.srcPort, data, null);
    }

    // ==================== 收包 ====================

    private class RxLoop implements Runnable {
        @Override
        public void run() {
            byte[] buf = new byte[2048];
            while (running) {
                DatagramSocket s = socket;
                if (s == null) {
                    break;
                }
                DatagramPacket p = new DatagramPacket(buf, buf.length);
                try {
                    s.receive(p);
                } catch (SocketTimeoutException e) {
                    continue;
                } catch (Exception e) {
                    break;
                }
                String json;
                try {
                    json = new String(p.getData(), 0, p.getLength(), "UTF-8");
                } catch (Exception e) {
                    continue;
                }
                try {
                    handleMessage(json, p.getAddress(), p.getPort());
                } catch (Exception ignored) {
                }
            }
        }
    }

    private void handleMessage(String json, InetAddress from, int fromPort) {
        JSONObject o;
        try {
            o = new JSONObject(json);
        } catch (Exception e) {
            return;
        }
        String magic = o.optString("magic", "");
        if (MAGIC_OFFER.equals(magic)) {
            addComputer(o, from);
        } else if (MAGIC_CONNECT_REQ.equals(magic)) {
            onIncomingRequest(o, from, fromPort);
        } else if (MAGIC_CONNECT_ACCEPT.equals(magic)) {
            onAccepted(o, from);
        } else if (MAGIC_CONNECT_DENY.equals(magic)) {
            onDenied(o);
        } else if (MAGIC_CONNECT_TOKEN.equals(magic)) {
            onToken(o, from);
        }
    }

    private void addComputer(JSONObject o, InetAddress from) {
        String ip = from == null ? "" : from.getHostAddress();
        if (ip == null || ip.isEmpty()) {
            return;
        }
        Computer d = new Computer();
        d.name = o.optString("name", "");
        d.id = o.optString("id", "");
        d.port = o.optInt("port", 8080);
        d.needPair = o.optBoolean("needPair", true);
        d.ip = ip;
        d.lastSeen = System.currentTimeMillis();
        if (d.port <= 0 || d.port > 65535) {
            d.port = 8080;
        }
        String key = d.id == null || d.id.isEmpty() ? ip : d.id;
        synchronized (this) {
            computers.put(key, d);
        }
    }

    private void onIncomingRequest(JSONObject o, InetAddress from, int fromPort) {
        if (!"pc".equals(o.optString("from", ""))) {
            return;
        }
        String reqId = o.optString("reqId", "");
        String pcId = o.optString("id", "");
        String pcName = o.optString("name", "");
        String to = o.optString("to", "");
        String ip = from == null ? "" : from.getHostAddress();
        if (reqId.isEmpty() || pcId.isEmpty() || ip == null || ip.isEmpty()) {
            return;
        }
        if (!to.isEmpty() && !to.equals(phoneId)) {
            return;
        }
        if (LinkService.isConnected()) {
            // 已连接：不再弹框，直接告诉电脑被拒
            try {
                JSONObject r = new JSONObject();
                r.put("magic", MAGIC_CONNECT_DENY);
                r.put("v", 1);
                r.put("reqId", reqId);
                r.put("reason", "手机已连接");
                DatagramSocket s = socket;
                if (s != null) {
                    sendTo(s, ip, fromPort, r.toString().getBytes("UTF-8"));
                }
            } catch (Exception ignored) {
            }
            return;
        }
        ConnectRequest req = new ConnectRequest();
        req.reqId = reqId;
        req.pcId = pcId;
        req.pcName = pcName;
        req.ip = ip;
        req.httpPort = o.optInt("port", 8080);
        req.srcPort = fromPort;
        req.ts = System.currentTimeMillis();
        if (req.httpPort <= 0 || req.httpPort > 65535) {
            req.httpPort = 8080;
        }
        synchronized (this) {
            incoming.put(reqId, req);
        }
        Listener l = listener;
        if (l != null) {
            l.onPcConnectRequest(req);
        }
    }

    private void onAccepted(JSONObject o, InetAddress from) {
        String reqId = o.optString("reqId", "");
        boolean mine;
        synchronized (this) {
            mine = outgoing.remove(reqId) != null;
        }
        if (!mine) {
            return;
        }
        String token = o.optString("token", "");
        if (token.isEmpty()) {
            return;
        }
        Computer pc = new Computer();
        pc.name = o.optString("name", "");
        pc.id = o.optString("id", "");
        pc.port = o.optInt("port", 8080);
        pc.ip = from == null ? "" : from.getHostAddress();
        pc.needPair = false;
        pc.lastSeen = System.currentTimeMillis();
        if (pc.port <= 0 || pc.port > 65535) {
            pc.port = 8080;
        }
        Listener l = listener;
        if (l != null) {
            l.onPcReady(pc, token);
        }
    }

    private void onToken(JSONObject o, InetAddress from) {
        String reqId = o.optString("reqId", "");
        boolean expected;
        synchronized (this) {
            expected = awaitingToken.remove(reqId) != null;
        }
        if (!expected) {
            return;
        }
        String token = o.optString("token", "");
        if (token.isEmpty()) {
            return;
        }
        Computer pc = new Computer();
        pc.name = o.optString("name", "");
        pc.id = o.optString("id", "");
        pc.port = o.optInt("port", 8080);
        pc.ip = from == null ? "" : from.getHostAddress();
        pc.needPair = false;
        pc.lastSeen = System.currentTimeMillis();
        if (pc.port <= 0 || pc.port > 65535) {
            pc.port = 8080;
        }
        Listener l = listener;
        if (l != null) {
            l.onPcReady(pc, token);
        }
    }

    private void onDenied(JSONObject o) {
        String reqId = o.optString("reqId", "");
        String reason = o.optString("reason", "");
        boolean known;
        synchronized (this) {
            boolean out = outgoing.remove(reqId) != null;
            boolean wait = awaitingToken.remove(reqId) != null;
            incoming.remove(reqId);
            known = out || wait;
        }
        if (!known) {
            return;
        }
        Listener l = listener;
        if (l != null) {
            l.onPcDenied(reqId, reason.isEmpty() ? "电脑端拒绝了连接" : reason);
        }
    }

    // ==================== 发送 ====================

    private class TxLoop implements Runnable {
        @Override
        public void run() {
            while (running) {
                try {
                    DatagramSocket s = socket;
                    // 已连接时不再广播 ANNOUNCE，电脑 12 秒收不到就视为手机离线
                    if (s != null && !LinkService.isConnected()) {
                        sendBroadcasts(s, discoverPayload);
                        sendBroadcasts(s, announcePayload);
                    }
                    checkTimeouts();
                } catch (Exception ignored) {
                }
                synchronized (wakeLock) {
                    try {
                        wakeLock.wait(BROADCAST_INTERVAL);
                    } catch (InterruptedException e) {
                        break;
                    }
                }
            }
        }
    }

    private void checkTimeouts() {
        long now = System.currentTimeMillis();
        List<String> expired = null;
        synchronized (this) {
            Iterator<Map.Entry<String, Long>> it = outgoing.entrySet().iterator();
            while (it.hasNext()) {
                Map.Entry<String, Long> e = it.next();
                if (now > e.getValue().longValue()) {
                    it.remove();
                    if (expired == null) {
                        expired = new ArrayList<String>();
                    }
                    expired.add(e.getKey());
                }
            }
            Iterator<Map.Entry<String, Long>> it2 = awaitingToken.entrySet().iterator();
            while (it2.hasNext()) {
                Map.Entry<String, Long> e = it2.next();
                if (now > e.getValue().longValue()) {
                    it2.remove();
                    if (expired == null) {
                        expired = new ArrayList<String>();
                    }
                    expired.add(e.getKey());
                }
            }
            Iterator<Map.Entry<String, ConnectRequest>> it3 = incoming.entrySet().iterator();
            while (it3.hasNext()) {
                Map.Entry<String, ConnectRequest> e = it3.next();
                ConnectRequest req = e.getValue();
                if (req == null || now - req.ts > INCOMING_TTL) {
                    it3.remove();
                }
            }
        }
        if (expired == null) {
            return;
        }
        Listener l = listener;
        for (int i = 0; i < expired.size(); i++) {
            if (l != null) {
                l.onRequestTimeout(expired.get(i));
            }
        }
    }

    private byte[] buildDiscover() {
        try {
            JSONObject o = new JSONObject();
            o.put("magic", MAGIC_DISCOVER);
            o.put("v", 1);
            return o.toString().getBytes("UTF-8");
        } catch (Exception e) {
            return new byte[0];
        }
    }

    private byte[] buildAnnounce() {
        try {
            JSONObject o = new JSONObject();
            o.put("magic", MAGIC_ANNOUNCE);
            o.put("v", 1);
            o.put("role", "phone");
            o.put("id", phoneId);
            o.put("name", phoneName);
            o.put("port", localPort);
            return o.toString().getBytes("UTF-8");
        } catch (Exception e) {
            return new byte[0];
        }
    }

    /** 向所有网卡的广播地址发送（全局 + 各网卡定向广播） */
    private static void sendBroadcasts(DatagramSocket sock, byte[] payload) {
        if (sock == null || payload == null || payload.length == 0) {
            return;
        }
        sendTo(sock, "255.255.255.255", PORT, payload);
        try {
            Enumeration<NetworkInterface> nis = NetworkInterface.getNetworkInterfaces();
            if (nis == null) {
                return;
            }
            while (nis.hasMoreElements()) {
                NetworkInterface ni = nis.nextElement();
                try {
                    if (!ni.isUp() || ni.isLoopback()) {
                        continue;
                    }
                } catch (Exception e) {
                    continue;
                }
                for (InterfaceAddress ia : ni.getInterfaceAddresses()) {
                    if (ia == null) {
                        continue;
                    }
                    InetAddress bcast = ia.getBroadcast();
                    if (bcast == null) {
                        bcast = calcBroadcast(ia.getAddress(), ia.getNetworkPrefixLength());
                    }
                    if (bcast != null && bcast.getAddress().length == 4) {
                        sendTo(sock, bcast.getHostAddress(), PORT, payload);
                    }
                }
            }
        } catch (Exception ignored) {
            // 枚举网卡失败时至少已发过全局广播
        }
    }

    private static boolean sendTo(DatagramSocket sock, String host, int port, byte[] payload) {
        try {
            sock.send(new DatagramPacket(payload, payload.length,
                    InetAddress.getByName(host), port));
            return true;
        } catch (Exception e) {
            return false;
        }
    }

    /** 由 IP + 掩码长度推算定向广播地址 */
    private static InetAddress calcBroadcast(InetAddress addr, int prefix) {
        try {
            byte[] b = addr.getAddress();
            if (b == null || b.length != 4) {
                return null;
            }
            int ip = ((b[0] & 0xFF) << 24) | ((b[1] & 0xFF) << 16)
                    | ((b[2] & 0xFF) << 8) | (b[3] & 0xFF);
            int mask;
            if (prefix <= 0) {
                mask = 0;
            } else if (prefix >= 32) {
                mask = -1;
            } else {
                mask = ~0 << (32 - prefix);
            }
            int bc = ip | ~mask;
            return InetAddress.getByAddress(new byte[]{
                    (byte) (bc >> 24), (byte) (bc >> 16), (byte) (bc >> 8), (byte) bc});
        } catch (Exception e) {
            return null;
        }
    }

    private String randomHex(int bytes) {
        byte[] b = new byte[bytes];
        random.nextBytes(b);
        StringBuilder sb = new StringBuilder();
        for (int i = 0; i < b.length; i++) {
            int v = b[i] & 0xFF;
            if (v < 16) {
                sb.append('0');
            }
            sb.append(Integer.toHexString(v));
        }
        return sb.toString();
    }
}
