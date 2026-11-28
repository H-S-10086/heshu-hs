package com.cleanmaster.link;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.Context;
import android.content.Intent;
import android.content.pm.ServiceInfo;
import android.net.ConnectivityManager;
import android.net.Network;
import android.net.wifi.WifiManager;
import android.os.Build;
import android.os.Handler;
import android.os.IBinder;
import android.os.Looper;
import android.os.PowerManager;

import org.json.JSONObject;

/**
 * 前台服务：维持与电脑的 WebSocket 长连接，并承担消息中转。
 *
 *   · 常驻通知栏（Android 8+ 需要 NotificationChannel），并显示当前连接状态
 *   · 断线自动重连，退避 2s / 4s / 8s / 16s / 30s（封顶）
 *   · 20 秒一次心跳；连续 2 个心跳周期收不到任何数据就主动断开重连（防假连接）
 *   · 网络恢复 / 切换时立即重连，不等退避计时
 *   · 与 Activity 的通信走应用内广播（AppLog + ACTION_STATE）
 *   · 收到电脑剪贴板 -> 写入手机剪贴板（ClipboardWatcher.applyRemote 负责防回环）
 *   · 麦克风：把 AudioRecord 采到的 PCM 以二进制帧转发给电脑
 */
public class LinkService extends Service {

    public static final String ACTION_STATE = "com.cleanmaster.link.action.STATE";
    public static final String EXTRA_NEED_PAIR = "needPair";
    /** 电脑端主动踢下线 */
    public static final String EXTRA_KICKED = "kicked";
    public static final String EXTRA_KICK_MSG = "kickMsg";

    /** 实时推来的文字（电脑或其它手机） */
    public static final String ACTION_TEXT = "com.cleanmaster.link.action.TEXT";
    public static final String EXTRA_TEXT = "text";
    public static final String EXTRA_TEXT_TIME = "textTime";
    public static final String EXTRA_TEXT_FROM = "textFrom";
    /** 发送方设备名 */
    public static final String EXTRA_TEXT_DEVICE = "textDevice";

    /** 电脑/其它手机发来文件（文件已在共享目录，界面收到后立即刷新） */
    public static final String ACTION_FILE = "com.cleanmaster.link.action.FILE";
    public static final String EXTRA_FILE_NAME = "fileName";
    public static final String EXTRA_FILE_SIZE = "fileSize";
    /** 发送方设备名 */
    public static final String EXTRA_FILE_DEVICE = "fileDevice";

    /** 连接状态：供界面用不同颜色展示 */
    public static final int STATE_IDLE = 0;          // 未连接
    public static final int STATE_CONNECTING = 1;    // 连接中…
    public static final int STATE_CONNECTED = 2;     // 已连接
    public static final int STATE_RECONNECTING = 3;  // 重连中(第N次, 倒计时)

    private static final String CHANNEL_ID = "cmlink_service";
    private static final int NOTIF_ID = 1001;

    /** 心跳间隔 20 秒；超过 2 个周期没收到任何数据判定为假连接 */
    private static final long HEARTBEAT_INTERVAL = 20000;
    private static final long HEARTBEAT_TIMEOUT = 42000;

    /** 供 Activity / 通知监听模块查询与发送消息 */
    private static volatile LinkService sInstance;
    private static volatile boolean sConnected = false;
    private static volatile String sServerName = "";
    private static volatile int sState = STATE_IDLE;
    private static volatile int sReconnectAttempt = 0;
    private static volatile int sReconnectRemain = 0;
    private static volatile String sLastError = "";

    /** 麦克风状态（界面 / MicSender 共享） */
    private static volatile boolean sMicActive = false;
    private static volatile boolean sMicAckPending = false;
    private static volatile boolean sMicAckOk = false;
    private static volatile String sMicMessage = "";
    private static volatile long sMicBytes = 0;

    private volatile boolean running = false;
    private volatile boolean authFailed = false;
    private volatile WsClient client;
    private Thread worker;
    private volatile Thread heartbeat;
    private volatile boolean heartbeatRun = false;

    /** 最近一次收到任何服务端数据的时间（心跳判断假连接用） */
    private volatile long lastRecv = 0;

    private final Object backoffLock = new Object();
    private volatile boolean backoffWake = false;
    private ConnectivityManager.NetworkCallback netCallback;
    private String lastNotifText = "";

    private final Handler mainHandler = new Handler(Looper.getMainLooper());

    /** 连接级息屏保活：CPU 与 Wi-Fi 都要锁住，否则锁屏后 Doze 会断网 */
    private PowerManager.WakeLock connWakeLock;
    private WifiManager.WifiLock wifiLock;

    // ==================== 静态入口 ====================

    public static boolean isRunning() {
        return sInstance != null;
    }

    public static boolean isConnected() {
        return sConnected;
    }

    public static String getServerName() {
        return sServerName;
    }

    public static int getState() {
        return sState;
    }

    public static int getReconnectAttempt() {
        return sReconnectAttempt;
    }

    public static int getReconnectRemainSec() {
        return sReconnectRemain;
    }

    public static String getLastError() {
        return sLastError;
    }

    public static boolean isMicActive() {
        return sMicActive;
    }

    public static String getMicMessage() {
        return sMicMessage;
    }

    public static long getMicBytes() {
        return sMicBytes;
    }

    public static void start(Context context) {
        Intent i = new Intent(context, LinkService.class);
        try {
            if (Build.VERSION.SDK_INT >= 26) {
                context.startForegroundService(i);
            } else {
                context.startService(i);
            }
        } catch (Exception e) {
            AppLog.add("启动服务失败：" + e.getMessage());
        }
    }

    public static void stop(Context context) {
        try {
            context.stopService(new Intent(context, LinkService.class));
        } catch (Exception ignored) {
        }
    }

    /**
     * 主动退出前先发 {"type":"bye"}，电脑会立刻把本机从「已连接设备」里移除。
     * 只在连接仍可用时发送，失败无所谓。
     */
    public static void sendBye() {
        LinkService s = sInstance;
        if (s == null) {
            return;
        }
        WsClient c = s.client;
        if (c == null || c.isClosed()) {
            return;
        }
        try {
            if (c.send("{\"type\":\"bye\"}")) {
                AppLog.add("→ 已发送 bye");
            }
        } catch (Exception ignored) {
        }
    }

    /** 手机剪贴板变化时调用（仅连接可用时真正发送） */
    public static void sendClipboard(String text) {
        LinkService s = sInstance;
        if (s == null || text == null || text.isEmpty()) {
            return;
        }
        WsClient c = s.client;
        if (c == null || c.isClosed() || !sConnected) {
            return;
        }
        try {
            JSONObject o = new JSONObject();
            o.put("type", "clipboard");
            o.put("text", text);
            o.put("ts", System.currentTimeMillis());
            if (c.send(o.toString())) {
                AppLog.add("→ 剪贴板：" + AppLog.preview(text));
            }
        } catch (Exception ignored) {
        }
    }

    /**
     * 「文字互传」：通过 WebSocket 发送一条文字。
     * 返回 false 表示当前没有可用连接（调用方应回退到 HTTP /api/text）。
     */
    public static boolean sendText(String text) {
        LinkService s = sInstance;
        if (s == null || text == null || text.isEmpty()) {
            return false;
        }
        WsClient c = s.client;
        if (c == null || c.isClosed() || !sConnected) {
            return false;
        }
        try {
            JSONObject o = new JSONObject();
            o.put("type", "text");
            o.put("text", text);
            o.put("ts", System.currentTimeMillis());
            if (!c.send(o.toString())) {
                return false;
            }
            AppLog.add("→ 文字：" + AppLog.preview(text));
            return true;
        } catch (Exception e) {
            return false;
        }
    }

    /** 通知监听模块调用 */
    public static void sendNotificationEvent(String app, String title, String text) {
        LinkService s = sInstance;
        if (s == null) {
            return;
        }
        WsClient c = s.client;
        if (c == null || c.isClosed() || !sConnected) {
            return;
        }
        try {
            JSONObject o = new JSONObject();
            o.put("type", "notification");
            o.put("app", app == null ? "" : app);
            o.put("title", title == null ? "" : title);
            o.put("text", text == null ? "" : text);
            o.put("ts", System.currentTimeMillis());
            if (c.send(o.toString())) {
                AppLog.add("→ 通知：" + app + " / " + AppLog.preview(title + " " + text));
            }
        } catch (Exception ignored) {
        }
    }

    // ==================== 麦克风 ====================

    /** 发送 mic_start，返回 false 表示当前连接不可用 */
    public static boolean startMicStream(int rate, int channels, int bits) {
        LinkService s = sInstance;
        if (s == null) {
            return false;
        }
        WsClient c = s.client;
        if (c == null || c.isClosed() || !sConnected) {
            return false;
        }
        sMicAckPending = true;
        sMicAckOk = false;
        sMicMessage = "";
        sMicBytes = 0;
        try {
            JSONObject o = new JSONObject();
            o.put("type", "mic_start");
            o.put("rate", rate);
            o.put("channels", channels);
            o.put("bits", bits);
            return c.send(o.toString());
        } catch (Exception e) {
            sMicAckPending = false;
            return false;
        }
    }

    /** 等待电脑端回 mic_ack，超时返回 false */
    public static boolean awaitMicAck(long timeoutMs) {
        long end = System.currentTimeMillis() + timeoutMs;
        while (System.currentTimeMillis() < end) {
            if (!sMicAckPending) {
                return sMicAckOk;
            }
            try {
                Thread.sleep(50);
            } catch (InterruptedException e) {
                return false;
            }
        }
        sMicAckPending = false;
        return false;
    }

    /** 发送 mic_stop */
    public static void stopMicStream() {
        LinkService s = sInstance;
        if (s == null) {
            return;
        }
        WsClient c = s.client;
        if (c != null && !c.isClosed() && c.send("{\"type\":\"mic_stop\"}")) {
            AppLog.add("→ 已发送 mic_stop");
        }
        sMicActive = false;
    }

    /** 发送一段 PCM 数据；失败返回 false（调用方据此结束录音） */
    public static boolean sendMicBinary(byte[] pcm) {
        LinkService s = sInstance;
        if (s == null || pcm == null || pcm.length == 0) {
            return false;
        }
        WsClient c = s.client;
        if (c == null || c.isClosed() || !sConnected) {
            return false;
        }
        if (!c.sendBinary(pcm)) {
            return false;
        }
        sMicBytes += pcm.length;
        return true;
    }

    // ==================== 生命周期 ====================

    /**
     * 连接级息屏保活。
     *
     * 前台服务只能防止进程被回收，不能阻止 CPU 休眠和 Wi-Fi 省电；
     * 锁屏后 Doze 会暂停网络，导致 WebSocket 心跳超时、整机断连。
     * 这里在服务运行期间持有 PARTIAL_WAKE_LOCK + WIFI_MODE_FULL_HIGH_PERF，
     * 代价是耗电增加，但换来锁屏后的持续连接与麦克风。
     */
    private void acquireConnectionLocks() {
        try {
            PowerManager pm = (PowerManager) getSystemService(POWER_SERVICE);
            if (pm != null) {
                if (connWakeLock == null) {
                    connWakeLock = pm.newWakeLock(
                            PowerManager.PARTIAL_WAKE_LOCK, "cmlink:conn");
                    connWakeLock.setReferenceCounted(false);
                }
                if (!connWakeLock.isHeld()) {
                    connWakeLock.acquire();
                }
            }
            WifiManager wm = (WifiManager) getApplicationContext()
                    .getSystemService(WIFI_SERVICE);
            if (wm != null) {
                if (wifiLock == null) {
                    wifiLock = wm.createWifiLock(
                            WifiManager.WIFI_MODE_FULL_HIGH_PERF, "cmlink:wifi");
                    wifiLock.setReferenceCounted(false);
                }
                if (!wifiLock.isHeld()) {
                    wifiLock.acquire();
                }
            }
            AppLog.add("已启用息屏保活（CPU + Wi-Fi 唤醒锁）");
        } catch (Exception e) {
            AppLog.add("息屏保活启用失败：" + e.getMessage());
        }
    }

    private void releaseConnectionLocks() {
        try {
            if (connWakeLock != null && connWakeLock.isHeld()) {
                connWakeLock.release();
            }
        } catch (Exception ignored) {
        }
        connWakeLock = null;
        try {
            if (wifiLock != null && wifiLock.isHeld()) {
                wifiLock.release();
            }
        } catch (Exception ignored) {
        }
        wifiLock = null;
    }

    @Override
    public void onCreate() {
        super.onCreate();
        sInstance = this;
        AppLog.init(this);
        sState = STATE_CONNECTING;
        sReconnectAttempt = 0;
        sReconnectRemain = 0;
        sLastError = "";

        createChannel();
        startForegroundCompat("正在连接电脑…");
        acquireConnectionLocks();

        registerNetworkCallback();

        running = true;
        worker = new Thread(new Runnable() {
            @Override
            public void run() {
                connectLoop();
            }
        }, "cmlink-ws");
        worker.setDaemon(true);
        worker.start();

        AppLog.add("后台服务已启动");
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        // START_STICKY：被系统回收后尽量重新拉起并恢复长连接
        return START_STICKY;
    }

    @Override
    public IBinder onBind(Intent intent) {
        return null;
    }

    @Override
    public void onTaskRemoved(Intent rootIntent) {
        // 用户从最近任务里划掉 App 时，服务继续保活，不主动退出
        AppLog.add("App 任务被移除，后台服务继续保活");
        super.onTaskRemoved(rootIntent);
    }

    @Override
    public void onDestroy() {
        // 先礼貌告别，电脑端会立刻把本机从设备列表里移除
        sendBye();
        running = false;
        releaseConnectionLocks();
        authFailed = true;          // 阻止重连循环继续
        sConnected = false;
        sServerName = "";
        sState = STATE_IDLE;
        sMicActive = false;
        sMicAckPending = false;
        stopHeartbeat();
        unregisterNetworkCallback();

        WsClient c = client;
        client = null;
        if (c != null) {
            c.close();
        }
        if (worker != null) {
            worker.interrupt();
            worker = null;
        }
        sInstance = null;

        AppLog.add("后台服务已停止");
        broadcastState();
        try {
            stopForeground(true);
        } catch (Exception ignored) {
        }
        super.onDestroy();
    }

    // ==================== 连接循环 ====================

    private void connectLoop() {
        int attempt = 0;
        int failStreak = 0;

        while (running && !authFailed) {
            String ip = Prefs.getIp(this);
            String token = Prefs.getToken(this);
            int port = Prefs.getPort(this);

            if (ip == null || ip.isEmpty()) {
                AppLog.add("未配置电脑地址，停止连接");
                sState = STATE_IDLE;
                broadcastNeedPair();
                stopSelfSafely();
                return;
            }
            if (token == null || token.isEmpty()) {
                // 电脑端要求配对时必须先拿到 token；不需要配对的电脑允许空 token 连接
                if (Prefs.isNeedPair(this)) {
                    AppLog.add("尚未配对，请在界面输入访问码");
                    sState = STATE_IDLE;
                    broadcastNeedPair();
                    stopSelfSafely();
                    return;
                }
                token = "";
            }

            // token 放请求头（不再拼进 URL，避免出现在日志/代理历史里）。
            // 电脑端在 WebSocket 握手阶段校验 X-Transfer-Token。
            WsClient c = new WsClient(ip, port, "/ws", token, new WsListener());
            client = c;
            setState(STATE_CONNECTING, attempt, 0);

            try {
                c.connect();
                attempt = 0;
                failStreak = 0;

                JSONObject hello = new JSONObject();
                hello.put("type", "hello");
                hello.put("token", token);
                hello.put("device", Prefs.getDeviceName(this));
                if (!c.send(hello.toString())) {
                    throw new java.io.IOException("hello 发送失败");
                }
                AppLog.add("已发出 hello，等待电脑确认…");

                startHeartbeat(c);
                c.readLoop();
            } catch (Exception e) {
                String raw = messageOf(e);
                // 403 = 电脑端认为 token 无效或尚未配对。
                // 这时不能傻等重连，要清掉本地 token 并让界面弹出配对框。
                if (raw != null && raw.contains("403")) {
                    Prefs.setToken(this, "");
                    HttpApi.setToken("");
                    authFailed = true;
                    sLastError = "配对已失效，请重新连接";
                    sState = STATE_IDLE;
                    AppLog.add("配对已失效：请重新搜索设备并请求连接；"
                            + "不同网络时手动输入电脑上的配对码");
                    broadcastNeedPair();
                } else if (!authFailed) {
                    failStreak++;
                    String msg = classifyError(raw);
                    sLastError = msg;
                    AppLog.add("连接失败：" + msg);
                    if (failStreak == 5) {
                        AppLog.add("连续失败 5 次，请检查：①电脑端是否点击了「开启协同服务」"
                                + " ②手机与电脑是否连接同一 WiFi ③Windows 防火墙是否拦截");
                    }
                }
            } finally {
                stopHeartbeat();
                // 连接断开时同步结束麦克风，避免录音线程对着死 socket 空转
                if (sMicActive || sMicAckPending) {
                    sMicActive = false;
                    sMicAckPending = false;
                    AppLog.add("连接断开，麦克风已中止");
                }
                if (client == c) {
                    client = null;
                }
                c.close();
                if (sConnected) {
                    sConnected = false;
                    AppLog.add("与电脑的连接已断开");
                    broadcastState();
                }
            }

            if (!running || authFailed) {
                break;
            }

            if (!sConnected) {
                // 走到这里说明本轮连接已结束，进入退避等待
                long delay = reconnectDelay(attempt);
                attempt++;
                AppLog.add((delay / 1000) + " 秒后重试连接…");
                setState(STATE_RECONNECTING, attempt, (int) (delay / 1000));
                sleepBackoff(delay, attempt);
            }
        }
    }

    /** 指数退避：2s, 4s, 8s, 16s，之后固定 30s */
    private static long reconnectDelay(int attempt) {
        switch (attempt) {
            case 0:
                return 2000;
            case 1:
                return 4000;
            case 2:
                return 8000;
            case 3:
                return 16000;
            default:
                return 30000;
        }
    }

    /**
     * 退避等待。每秒刷新一次剩余秒数到界面；
     * 网络恢复时 wakeBackoff() 会立刻把它叫醒，直接进入下一次连接。
     */
    private void sleepBackoff(long totalMs, final int attempt) {
        long end = System.currentTimeMillis() + totalMs;
        synchronized (backoffLock) {
            backoffWake = false;
        }
        while (running && !authFailed) {
            long left = end - System.currentTimeMillis();
            if (left <= 0) {
                return;
            }
            synchronized (backoffLock) {
                if (backoffWake) {
                    backoffWake = false;
                    return;
                }
                try {
                    backoffLock.wait(Math.min(500, left));
                } catch (InterruptedException e) {
                    return;
                }
            }
            int sec = (int) ((end - System.currentTimeMillis() + 999) / 1000);
            if (sec < 0) {
                sec = 0;
            }
            if (sec != sReconnectRemain) {
                setState(STATE_RECONNECTING, attempt, sec);
            }
        }
    }

    /** 网络恢复 / 切换时立即打断退避 */
    private void wakeBackoff() {
        synchronized (backoffLock) {
            backoffWake = true;
            backoffLock.notifyAll();
        }
    }

    /** 把底层网络异常翻译成用户能看懂的提示 */
    private static String classifyError(String msg) {
        if (msg == null || msg.isEmpty()) {
            return "未知错误";
        }
        String lower = msg.toLowerCase();
        if (msg.contains("403")) {
            return "需要配对（403）：请在电脑上查看访问码并输入";
        }
        if (lower.contains("connection refused") || lower.contains("econnrefused")
                || msg.contains("拒绝")) {
            return "电脑端服务未开启（Connection refused）";
        }
        if (lower.contains("timeout") || lower.contains("timed out")
                || lower.contains("sockettimeout")) {
            return "网络不通（连接超时）";
        }
        if (lower.contains("no route to host") || lower.contains("unreachable")) {
            return "网络不通（主机不可达）";
        }
        return msg;
    }

    private void stopSelfSafely() {
        running = false;
        mainHandler.post(new Runnable() {
            @Override
            public void run() {
                stopSelf();
            }
        });
    }

    // ==================== 网络变化监听 ====================

    private void registerNetworkCallback() {
        try {
            ConnectivityManager cm =
                    (ConnectivityManager) getSystemService(Context.CONNECTIVITY_SERVICE);
            if (cm == null) {
                return;
            }
            netCallback = new ConnectivityManager.NetworkCallback() {
                @Override
                public void onAvailable(Network network) {
                    // 断网恢复 / WiFi 切换后会走到这里：立即重连，不等退避计时
                    AppLog.add("网络可用，立即尝试连接…");
                    wakeBackoff();
                }

                @Override
                public void onLost(Network network) {
                    AppLog.add("网络已断开，等待恢复…");
                    WsClient c = client;
                    if (c != null) {
                        c.close();      // 触发读线程异常 -> 进入重连
                    }
                }
            };
            cm.registerDefaultNetworkCallback(netCallback);
        } catch (Exception e) {
            AppLog.add("网络监听注册失败：" + e.getMessage());
        }
    }

    private void unregisterNetworkCallback() {
        if (netCallback == null) {
            return;
        }
        try {
            ConnectivityManager cm =
                    (ConnectivityManager) getSystemService(Context.CONNECTIVITY_SERVICE);
            if (cm != null) {
                cm.unregisterNetworkCallback(netCallback);
            }
        } catch (Exception ignored) {
        }
        netCallback = null;
    }

    // ==================== 消息处理 ====================

    private class WsListener implements WsClient.Listener {
        @Override
        public void onOpen() {
            lastRecv = System.currentTimeMillis();
            AppLog.add("WebSocket 握手成功");
        }

        @Override
        public void onMessage(String text) {
            lastRecv = System.currentTimeMillis();
            handleMessage(text);
        }

        @Override
        public void onClosed(String reason) {
            if (!authFailed) {
                AppLog.add("连接关闭：" + reason);
            }
        }
    }

    private void handleMessage(String text) {
        try {
            JSONObject o = new JSONObject(text);
            String type = o.optString("type", "");

            if ("welcome".equals(type)) {
                sServerName = o.optString("server", "");
                sConnected = true;
                sLastError = "";
                sReconnectAttempt = 0;
                sReconnectRemain = 0;
                sState = STATE_CONNECTED;
                AppLog.add("已连接：" + (sServerName.isEmpty() ? "电脑" : sServerName));
                updateNotification("已连接 " + (sServerName.isEmpty() ? "电脑" : sServerName));
                broadcastState();
            } else if ("clipboard".equals(type)) {
                String content = o.optString("text", "");
                ClipboardWatcher.applyRemote(LinkService.this, content);
                AppLog.add("收到剪贴板：" + AppLog.preview(content));
            } else if ("text".equals(type)) {
                // 电脑 / 其它手机实时发来的文字：带上设备名转发给界面
                String content = o.optString("text", "");
                String from = o.optString("from", "pc");
                String time = o.optString("time", "");
                String device = o.optString("device", "");
                AppLog.add("收到文字" + (device.isEmpty() ? "" : "（" + device + "）")
                        + "：" + AppLog.preview(content));
                broadcastText(content, time, from, device);
            } else if ("text_ack".equals(type)) {
                boolean ok = o.optBoolean("ok", false);
                AppLog.add(ok ? "文字已送达电脑" : "电脑端未接收文字");
            } else if ("file".equals(type)) {
                // 电脑 / 其它手机发来的文件：文件已在共享目录里，通知界面立刻刷新
                String name = o.optString("name", "");
                long size = o.optLong("size", 0);
                String device = o.optString("device", "");
                AppLog.add("收到文件" + (device.isEmpty() ? "" : "（" + device + "）")
                        + "：" + name);
                broadcastFile(name, size, device);
            } else if ("kick".equals(type)) {
                // 电脑主动踢下线：停止重连，等用户手动「重新连接」
                String msg = o.optString("message", "");
                authFailed = true;
                sConnected = false;
                sState = STATE_IDLE;
                sReconnectAttempt = 0;
                sReconnectRemain = 0;
                sLastError = "已被电脑断开" + (msg.isEmpty() ? "" : "：" + msg);
                AppLog.add(sLastError);
                updateNotification("已被电脑断开");
                broadcastKick(msg);
                WsClient wk = client;
                if (wk != null) {
                    wk.close();
                }
                stopSelfSafely();
            } else if ("pong".equals(type)) {
                // 心跳回应，lastRecv 已在 onMessage 里更新，无需处理
            } else if ("mic_ack".equals(type)) {
                sMicAckPending = false;
                sMicAckOk = o.optBoolean("ok", false);
                sMicActive = sMicAckOk;
                sMicMessage = o.optString("message", "");
                AppLog.add("麦克风：" + (sMicAckOk ? "电脑端已开始接收" : "电脑端拒绝")
                        + (sMicMessage.isEmpty() ? "" : "（" + sMicMessage + "）"));
                updateNotification(lastNotifText);
                broadcastState();
            } else if ("mic_stopped".equals(type)) {
                sMicActive = false;
                sMicMessage = o.optString("message", "");
                AppLog.add("麦克风已停止：" + (sMicMessage.isEmpty() ? "电脑端已保存" : sMicMessage));
                updateNotification(lastNotifText);
                broadcastState();
            } else if ("error".equals(type)) {
                String err = o.optString("error", o.optString("message", "未知错误"));
                AppLog.add("电脑端提示：" + err);
                if (err.contains("token") || err.contains("pair") || err.contains("配对")
                        || err.contains("访问码")) {
                    authFailed = true;
                    sState = STATE_IDLE;
                    broadcastNeedPair();
                    if (client != null) {
                        client.close();
                    }
                }
            } else {
                AppLog.add("收到消息：" + type);
            }
        } catch (Exception e) {
            AppLog.add("消息解析失败：" + AppLog.preview(text));
        }
    }

    /**
     * 心跳：每 20 秒发一次 {"type":"ping"}。
     * 若连续 2 个周期（约 40 秒）没有收到任何数据（包括 pong），
     * 说明 TCP 可能还活着但对端应用已死 —— 主动关闭 socket 触发重连。
     */
    private void startHeartbeat(final WsClient c) {
        lastRecv = System.currentTimeMillis();
        heartbeatRun = true;
        Thread t = new Thread(new Runnable() {
            @Override
            public void run() {
                while (heartbeatRun && running && client == c) {
                    try {
                        Thread.sleep(HEARTBEAT_INTERVAL);
                    } catch (InterruptedException e) {
                        break;
                    }
                    if (!heartbeatRun || !running || client != c) {
                        break;
                    }
                    long idle = System.currentTimeMillis() - lastRecv;
                    if (idle >= HEARTBEAT_TIMEOUT) {
                        AppLog.add("心跳超时：已 " + (idle / 1000)
                                + " 秒无任何数据，判定为假连接，主动重连");
                        sLastError = "心跳超时，已主动重连";
                        c.close();
                        break;
                    }
                    c.send("{\"type\":\"ping\"}");
                }
            }
        }, "cmlink-heartbeat");
        t.setDaemon(true);
        heartbeat = t;
        t.start();
    }

    private void stopHeartbeat() {
        heartbeatRun = false;
        Thread t = heartbeat;
        heartbeat = null;
        if (t != null) {
            t.interrupt();
        }
    }

    /**
     * 更新连接状态并同步刷新通知栏文字。
     * 退避倒计时每秒调用一次，notification 文案只在状态切换时真正重设。
     */
    private void setState(int state, int attempt, int remainSec) {
        sState = state;
        sReconnectAttempt = attempt;
        sReconnectRemain = remainSec;
        String text;
        switch (state) {
            case STATE_CONNECTED:
                text = "已连接 " + (sServerName.isEmpty() ? "电脑" : sServerName);
                break;
            case STATE_CONNECTING:
                text = "正在连接电脑…";
                break;
            case STATE_RECONNECTING:
                text = "重连中（第 " + attempt + " 次）";
                break;
            default:
                text = "未连接";
                break;
        }
        if (!text.equals(lastNotifText)) {
            lastNotifText = text;
            updateNotification(text);
        }
        broadcastState();
    }

    private void broadcastState() {
        mainHandler.post(new Runnable() {
            @Override
            public void run() {
                try {
                    Intent i = new Intent(ACTION_STATE);
                    i.setPackage(getPackageName());
                    sendBroadcast(i);
                } catch (Exception ignored) {
                }
            }
        });
    }

    /** 文字消息不需要切主线程，直接从读线程发应用内广播即可 */
    private void broadcastText(String text, String time, String from, String device) {
        try {
            Intent i = new Intent(ACTION_TEXT);
            i.setPackage(getPackageName());
            i.putExtra(EXTRA_TEXT, text == null ? "" : text);
            i.putExtra(EXTRA_TEXT_TIME, time == null ? "" : time);
            i.putExtra(EXTRA_TEXT_FROM, from == null ? "" : from);
            i.putExtra(EXTRA_TEXT_DEVICE, device == null ? "" : device);
            sendBroadcast(i);
        } catch (Exception ignored) {
        }
    }

    /** 电脑 / 其它手机发来文件：让界面立刻刷新列表（不必等下一次轮询） */
    private void broadcastFile(String name, long size, String device) {
        try {
            Intent i = new Intent(ACTION_FILE);
            i.setPackage(getPackageName());
            i.putExtra(EXTRA_FILE_NAME, name == null ? "" : name);
            i.putExtra(EXTRA_FILE_SIZE, size);
            i.putExtra(EXTRA_FILE_DEVICE, device == null ? "" : device);
            sendBroadcast(i);
        } catch (Exception ignored) {
        }
    }

    /** 被电脑踢下线：单独广播，界面据此显示「重新连接」入口 */
    private void broadcastKick(String msg) {
        try {
            Intent i = new Intent(ACTION_STATE);
            i.setPackage(getPackageName());
            i.putExtra(EXTRA_KICKED, true);
            i.putExtra(EXTRA_KICK_MSG, msg == null ? "" : msg);
            sendBroadcast(i);
        } catch (Exception ignored) {
        }
    }

    private void broadcastNeedPair() {
        mainHandler.post(new Runnable() {
            @Override
            public void run() {
                try {
                    Intent i = new Intent(ACTION_STATE);
                    i.setPackage(getPackageName());
                    i.putExtra(EXTRA_NEED_PAIR, true);
                    sendBroadcast(i);
                } catch (Exception ignored) {
                }
            }
        });
    }

    private static String messageOf(Exception e) {
        String msg = e.getMessage();
        if (msg == null || msg.isEmpty()) {
            msg = e.getClass().getSimpleName();
        }
        return msg;
    }

    // ==================== 通知栏 ====================

    private void createChannel() {
        if (Build.VERSION.SDK_INT >= 26) {
            NotificationManager nm =
                    (NotificationManager) getSystemService(NOTIFICATION_SERVICE);
            if (nm != null) {
                NotificationChannel ch = new NotificationChannel(CHANNEL_ID,
                        "连接服务", NotificationManager.IMPORTANCE_LOW);
                ch.setDescription("保持与电脑的实时连接");
                ch.setShowBadge(false);
                nm.createNotificationChannel(ch);
            }
        }
    }

    private Notification buildNotification(String text) {
        return buildNotification(text, sMicActive);
    }

    /**
     * 麦克风录制期间用红色标题 + 明确文案，让用户随时知道正在采音。
     */
    private Notification buildNotification(String text, boolean micActive) {
        Intent launch = new Intent(this, MainActivity.class);
        int flags = PendingIntent.FLAG_UPDATE_CURRENT;
        if (Build.VERSION.SDK_INT >= 23) {
            flags |= PendingIntent.FLAG_IMMUTABLE;
        }
        PendingIntent pi = PendingIntent.getActivity(this, 0, launch, flags);

        Notification.Builder b;
        if (Build.VERSION.SDK_INT >= 26) {
            b = new Notification.Builder(this, CHANNEL_ID);
        } else {
            b = new Notification.Builder(this);
        }
        b.setSmallIcon(android.R.drawable.stat_notify_sync)
                .setContentTitle(micActive ? "手机互传 · 麦克风正在使用" : "手机互传")
                .setContentText(micActive ? "正在录音并实时发送到电脑，点此返回停止" : text)
                .setOngoing(true)
                .setOnlyAlertOnce(true)
                .setContentIntent(pi);
        if (Build.VERSION.SDK_INT >= 21) {
            b.setColor(micActive ? 0xFFFF4D4F : 0xFF1890FF);
        }
        return b.build();
    }

    private void startForegroundCompat(String text) {
        try {
            if (Build.VERSION.SDK_INT >= 29) {
                int type = ServiceInfo.FOREGROUND_SERVICE_TYPE_DATA_SYNC;
                if (Build.VERSION.SDK_INT >= 30) {
                    // 锁屏后继续采集麦克风，需要把前台服务声明为 microphone 类型
                    type |= ServiceInfo.FOREGROUND_SERVICE_TYPE_MICROPHONE;
                }
                startForeground(NOTIF_ID, buildNotification(text), type);
            } else {
                startForeground(NOTIF_ID, buildNotification(text));
            }
            lastNotifText = text;
        } catch (Exception e) {
            AppLog.add("前台通知创建失败：" + e.getMessage());
        }
    }

    private void updateNotification(String text) {
        lastNotifText = text == null ? "" : text;
        try {
            NotificationManager nm =
                    (NotificationManager) getSystemService(NOTIFICATION_SERVICE);
            if (nm != null) {
                nm.notify(NOTIF_ID, buildNotification(lastNotifText));
            }
        } catch (Exception ignored) {
        }
    }
}
