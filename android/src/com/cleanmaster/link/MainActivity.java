package com.cleanmaster.link;

import android.Manifest;
import android.app.Activity;
import android.app.AlertDialog;
import android.app.Dialog;
import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.content.BroadcastReceiver;
import android.content.ClipData;
import android.content.Context;
import android.content.DialogInterface;
import android.content.Intent;
import android.content.IntentFilter;
import android.content.pm.PackageManager;
import android.graphics.Bitmap;
import android.graphics.BitmapFactory;
import android.graphics.Typeface;
import android.graphics.drawable.GradientDrawable;
import android.media.MediaPlayer;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.os.Message;
import android.provider.Settings;
import android.text.InputType;
import android.view.Gravity;
import android.view.MotionEvent;
import android.view.ScaleGestureDetector;
import android.view.SurfaceHolder;
import android.view.SurfaceView;
import android.view.View;
import android.view.ViewGroup;
import android.widget.Button;
import android.widget.CompoundButton;
import android.widget.EditText;
import android.widget.FrameLayout;
import android.widget.HorizontalScrollView;
import android.widget.ImageView;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.Switch;
import android.widget.TextView;
import android.widget.Toast;

import java.io.File;
import java.util.ArrayList;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Locale;
import java.util.Set;

/**
 * 主界面：连接卡片 / 配对 / 两个同步开关 / 文件卡片 / 手机麦克风 / 事件日志。
 *
 * 全部用代码构建布局（ScrollView + LinearLayout），
 * 不依赖 RecyclerView、AndroidX、Material 等任何第三方库。
 *
 * 监听器说明：
 *   按钮点击、筛选切换、文件卡片操作都统一交给本 Activity 自己处理，
 *   通过 View 的 tag 携带参数（筛选类型 / 文件对象 / 要关闭的 Dialog），
 *   这样能避免产生几十个匿名内部类，构建时 d8 的命令行也不会超长。
 */
public class MainActivity extends Activity
        implements View.OnClickListener, CompoundButton.OnCheckedChangeListener,
        View.OnLongClickListener, View.OnTouchListener,
        DialogInterface.OnClickListener, Handler.Callback, LanDiscovery.Listener {

    private static final int REQ_PICK_FILES = 2001;
    private static final int REQ_NOTIF_PERM = 2002;
    private static final int REQ_RECORD_AUDIO = 2003;

    /** 通知点击后直接定位到「会话」列表 */
    public static final String EXTRA_OPEN_FILES = "open_files";

    private static final int MSG_POLL = 1;
    private static final int MSG_UI_POLL = 2;
    private static final int MSG_UI_SEND = 3;
    private static final int MSG_SCROLL = 4;
    /** 局域网回调（LanDiscovery 在后台线程触发，统一切到主线程处理） */
    private static final int MSG_LAN_REQ = 5;
    private static final int MSG_LAN_READY = 6;
    private static final int MSG_LAN_DENY = 7;
    private static final int MSG_LAN_TIMEOUT = 8;
    /** 搜索等待结束，读取 LanDiscovery 的设备列表 */
    private static final int MSG_SEARCH_DONE = 9;
    private static final long POLL_INTERVAL = 5000;
    private static final long SEARCH_WAIT_MS = 2500;

    private static final String MSG_CHANNEL_ID = "cmlink_msg";
    private static final int NOTIF_MSG_ID = 1002;

    private static final int COLOR_TEXT = 0xFF262626;
    private static final int COLOR_SUB = 0xFF8C8C8C;
    private static final int COLOR_OK = 0xFF1890FF;
    private static final int COLOR_OK_GREEN = 0xFF52C41A;
    private static final int COLOR_WARN = 0xFFFA8C16;
    private static final int COLOR_ERR = 0xFFF5222D;

    private ScrollView page;
    private TextView tvNewBanner;
    private TextView tvStatus;
    private TextView tvPcName;
    private TextView tvAddr;
    private TextView tvLog;
    private ScrollView logScroll;
    private LinearLayout pairBox;
    private EditText etCode;
    private Button btnPair;
    private Button btnDiscover;
    private Button btnReconnect;
    private Switch swClip;
    private Switch swNotify;

    // 会话卡片（文件 + 文字合并成一条时间线）
    private LinearLayout convCard;
    private ScrollView msgScroll;
    private LinearLayout msgBox;
    private TextView tvMsgHint;
    private EditText etInput;
    /** 全屏编辑对话框里的控件（同时只存在一个） */
    private EditText etFullEdit;
    private TextView tvFullCount;
    /** 服务端会话（正序，旧→新） */
    private final List<HttpApi.MsgItem> messages = new ArrayList<>();
    /** 已发送但服务端尚未回传的本地消息（乐观插入） */
    private final List<HttpApi.MsgItem> localPending = new ArrayList<>();

    // 新内容提示（轮询 / WS 对比）
    private Handler pollHandler;
    private boolean polling = false;
    private boolean pollInFlight = false;
    private boolean pollQueued = false;
    private volatile List<HttpApi.MsgItem> pendingMsgs;
    private volatile boolean pendingMsgsOk = false;
    private volatile boolean sendOk = false;
    private volatile String sendError = "";
    private volatile String sendingText = "";
    private boolean sendInFlight = false;
    private final Set<String> seenMsgs = new LinkedHashSet<>();
    private boolean msgsSeenReady = false;
    private int bannerFiles = 0;
    private int bannerTexts = 0;

    /** 当前弹出操作对话框的消息（同一时刻只会有一个） */
    private HttpApi.MsgItem dialogMsg;

    /** 等待用户在弹框里允许 / 拒绝的电脑连接请求 */
    private LanDiscovery.ConnectRequest pendingConnectReq;

    // 手机麦克风卡片
    private Button btnMic;
    private TextView tvMicStatus;
    private TextView tvMicStat;
    private MicSender micSender;

    private ClipboardWatcher watcher;
    private boolean resumed = false;
    private boolean receiverRegistered = false;

    private final BroadcastReceiver evtReceiver = new BroadcastReceiver() {
        @Override
        public void onReceive(Context context, Intent intent) {
            String action = intent == null ? null : intent.getAction();
            if (AppLog.ACTION_LOG.equals(action)) {
                refreshLog();
            } else if (LinkService.ACTION_STATE.equals(action)) {
                refreshState();
                if (intent.getBooleanExtra(LinkService.EXTRA_NEED_PAIR, false)) {
                    showPairBox(false);
                }
                if (intent.getBooleanExtra(LinkService.EXTRA_KICKED, false)) {
                    onKicked(intent.getStringExtra(LinkService.EXTRA_KICK_MSG));
                }
                if (LinkService.isConnected()) {
                    // 连接成功后立刻拉一次会话，保证一进来就能看到消息
                    schedulePoll(0);
                } else {
                    // 断开 / 被踢 / 服务停止：取消已排队的轮询，禁止继续请求 /api/messages
                    stopPolling();
                }
            } else if (LinkService.ACTION_TEXT.equals(action)
                    || LinkService.ACTION_FILE.equals(action)) {
                // 电脑刚发来文字 / 文件：立刻拉一次统一会话流
                schedulePoll(0);
            }
        }
    };

    // ==================== 生命周期 ====================

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        AppLog.init(this);
        // 所有 HTTP 请求带上配对 token，电脑端即使设了浏览器访问码也不会被挡
        HttpApi.setToken(Prefs.getToken(this));
        pollHandler = new Handler(Looper.getMainLooper(), this);
        polling = true;
        loadSeenState();
        buildUi();
        ensureMsgChannel();

        watcher = new ClipboardWatcher(this, new ClipboardWatcher.Listener() {
            @Override
            public void onLocalClipboard(String text) {
                if (Prefs.isClipboardOn(MainActivity.this)) {
                    LinkService.sendClipboard(text);
                }
            }
        });

        // 未配对时直接把配对卡片显示出来
        if (!Prefs.getIp(this).isEmpty() && Prefs.getToken(this).isEmpty()
                && Prefs.isNeedPair(this)) {
            showPairBox(false);
        }

        requestNotificationPermission();
        refreshState();
        refreshLog();
        handleLaunchIntent(getIntent());
    }

    @Override
    protected void onStart() {
        super.onStart();
        IntentFilter f = new IntentFilter();
        f.addAction(AppLog.ACTION_LOG);
        f.addAction(LinkService.ACTION_STATE);
        f.addAction(LinkService.ACTION_TEXT);
        f.addAction(LinkService.ACTION_FILE);
        try {
            if (Build.VERSION.SDK_INT >= 33) {
                registerReceiver(evtReceiver, f, Context.RECEIVER_NOT_EXPORTED);
            } else {
                registerReceiver(evtReceiver, f);
            }
            receiverRegistered = true;
        } catch (Exception ignored) {
        }
        refreshState();
        refreshLog();

        polling = true;
        schedulePoll(400);

        // Activity 可见期间才做局域网广播；onStop 立即停止
        LanDiscovery lan = LanDiscovery.get(this);
        lan.setListener(this);
        lan.start(this);
        lan.searchNow();
    }

    @Override
    protected void onNewIntent(Intent intent) {
        super.onNewIntent(intent);
        setIntent(intent);
        handleLaunchIntent(intent);
    }

    @Override
    protected void onResume() {
        super.onResume();
        resumed = true;
        syncWatcher();
        refreshState();
    }

    @Override
    protected void onPause() {
        resumed = false;
        syncWatcher();       // 退到后台立即摘掉监听：Android 10+ 后台读不到剪贴板
        super.onPause();
    }

    @Override
    protected void onStop() {
        // 轮询不停：退到后台后仍要靠它发现电脑新传来的文件并发系统通知。
        // 真正停止放在 onDestroy（Activity 被回收时）。
        if (receiverRegistered) {
            try {
                unregisterReceiver(evtReceiver);
            } catch (Exception ignored) {
            }
            receiverRegistered = false;
        }
        // 不可见就不再广播 ANNOUNCE（电脑 12 秒收不到即视为离线）
        LanDiscovery lan = LanDiscovery.get(this);
        lan.setListener(null);
        lan.stop();
        super.onStop();
    }

    @Override
    protected void onDestroy() {
        polling = false;
        pendingConnectReq = null;
        if (pollHandler != null) {
            pollHandler.removeCallbacksAndMessages(null);
            pollHandler = null;
        }
        LanDiscovery lan = LanDiscovery.get(this);
        lan.setListener(null);
        lan.stop();
        // Activity 销毁后录音线程不能继续跑，否则麦克风会一直被占用
        if (micSender != null) {
            try {
                micSender.stop();
            } catch (Exception ignored) {
            }
            micSender = null;
        }
        super.onDestroy();
    }

    @Override
    protected void onActivityResult(int requestCode, int resultCode, Intent data) {
        super.onActivityResult(requestCode, resultCode, data);
        if (requestCode != REQ_PICK_FILES || resultCode != RESULT_OK || data == null) {
            return;
        }
        List<Uri> uris = new ArrayList<>();
        ClipData clip = data.getClipData();
        if (clip != null) {
            for (int i = 0; i < clip.getItemCount(); i++) {
                Uri u = clip.getItemAt(i).getUri();
                if (u != null) {
                    uris.add(u);
                }
            }
        } else if (data.getData() != null) {
            uris.add(data.getData());
        }
        if (!uris.isEmpty()) {
            uploadFiles(uris);
        }
    }

    @Override
    public void onRequestPermissionsResult(int requestCode, String[] permissions,
                                           int[] grantResults) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults);
        if (requestCode == REQ_NOTIF_PERM && grantResults.length > 0
                && grantResults[0] != PackageManager.PERMISSION_GRANTED) {
            AppLog.add("通知权限被拒绝，前台服务通知可能不显示");
        } else if (requestCode == REQ_RECORD_AUDIO) {
            if (grantResults.length > 0
                    && grantResults[0] == PackageManager.PERMISSION_GRANTED) {
                startMic();
            } else {
                AppLog.add("麦克风权限被拒绝，手机麦克风不可用");
                dialog("需要录音权限",
                        "手机麦克风功能需要使用手机的「录音」权限。\n"
                                + "如果刚才选择了「不再询问」，请到：\n"
                                + "系统设置 → 应用 → 手机互传 → 权限 → 麦克风，手动开启。");
            }
        }
    }

    // ==================== UI 构建 ====================

    private void buildUi() {
        page = new ScrollView(this);
        page.setBackgroundColor(0xFFF0F4F8);

        LinearLayout root = new LinearLayout(this);
        root.setOrientation(LinearLayout.VERTICAL);
        root.setPadding(dp(14), dp(14), dp(14), dp(28));
        page.addView(root, new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT,
                ViewGroup.LayoutParams.WRAP_CONTENT));

        TextView title = label("合枢 HS · 手机互传", 20);
        title.setTypeface(null, Typeface.BOLD);
        title.setTextColor(COLOR_OK);
        title.setGravity(Gravity.CENTER_HORIZONTAL);
        root.addView(title, new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT));

        // ---- 新内容提示条 ----
        tvNewBanner = new TextView(this);
        tvNewBanner.setTextSize(13);
        tvNewBanner.setTextColor(0xFFFFFFFF);
        tvNewBanner.setGravity(Gravity.CENTER_VERTICAL);
        tvNewBanner.setPadding(dp(12), dp(9), dp(12), dp(9));
        tvNewBanner.setBackground(roundBg(COLOR_WARN));
        tvNewBanner.setVisibility(View.GONE);
        tvNewBanner.setOnClickListener(this);
        LinearLayout.LayoutParams bannerLp = new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT);
        bannerLp.bottomMargin = dp(10);
        root.addView(tvNewBanner, bannerLp);

        // ---- 连接卡片 ----
        LinearLayout connCard = card();
        tvStatus = label("状态：未连接", 17);
        tvStatus.setTypeface(null, Typeface.BOLD);
        tvStatus.setTextColor(COLOR_SUB);
        tvPcName = label("电脑：—", 14);
        tvAddr = label("地址：—", 14);
        connCard.addView(tvStatus);
        connCard.addView(tvPcName);
        connCard.addView(tvAddr);

        // 被电脑踢下线后才出现；点击重新走一遍连接流程
        btnReconnect = button("重新连接");
        btnReconnect.setOnClickListener(this);
        btnReconnect.setVisibility(View.GONE);
        LinearLayout.LayoutParams recLp = new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT);
        recLp.topMargin = dp(6);
        connCard.addView(btnReconnect, recLp);
        root.addView(connCard);

        // ---- 第一排按钮 ----
        LinearLayout row1 = new LinearLayout(this);
        row1.setOrientation(LinearLayout.HORIZONTAL);
        LinearLayout.LayoutParams rowLp = new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT);
        rowLp.bottomMargin = dp(10);
        row1.setLayoutParams(rowLp);

        btnDiscover = button("自动发现");
        btnDiscover.setOnClickListener(this);
        row1.addView(btnDiscover, weight(1));

        Button btnManual = button("手动输入 IP");
        btnManual.setOnClickListener(this);
        row1.addView(btnManual, weightWithLeft(1));

        Button btnDisconnect = button("断开");
        btnDisconnect.setOnClickListener(this);
        row1.addView(btnDisconnect, weightWithLeft(1));

        root.addView(row1);

        // ---- 配对卡片 ----
        pairBox = card();
        pairBox.setVisibility(View.GONE);
        pairBox.addView(label("输入电脑上显示的 6 位配对码（同网络可改用「自动发现」免输码）",
                13));
        LinearLayout pairRow = new LinearLayout(this);
        pairRow.setOrientation(LinearLayout.HORIZONTAL);
        etCode = new EditText(this);
        etCode.setHint("6 位访问码");
        etCode.setInputType(InputType.TYPE_CLASS_NUMBER);
        etCode.setTextSize(16);
        pairRow.addView(etCode, weight(1));
        btnPair = button("配对");
        btnPair.setOnClickListener(this);
        LinearLayout.LayoutParams pairBtnLp = new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.WRAP_CONTENT, ViewGroup.LayoutParams.WRAP_CONTENT);
        pairBtnLp.leftMargin = dp(8);
        pairRow.addView(btnPair, pairBtnLp);
        pairBox.addView(pairRow);
        root.addView(pairBox);

        // ---- 会话卡片（文件 + 文字合并，像微信一样的单条时间线） ----
        convCard = card();

        LinearLayout convHead = new LinearLayout(this);
        convHead.setOrientation(LinearLayout.HORIZONTAL);
        convHead.setGravity(Gravity.CENTER_VERTICAL);
        TextView convTitle = label("💬 会话", 16);
        convTitle.setTypeface(null, Typeface.BOLD);
        convHead.addView(convTitle, weight(1));
        TextView btnConvRefresh = smallButton("刷新");
        btnConvRefresh.setOnClickListener(this);
        convHead.addView(btnConvRefresh);
        convCard.addView(convHead);

        TextView convHint = label("点文字复制 · 点文件预览 · 长按更多", 11);
        convHint.setTextColor(COLOR_SUB);
        convCard.addView(convHint);

        // 消息区固定 380dp 高、内部滚动，避免把下面的输入栏顶走
        msgScroll = new ScrollView(this);
        msgScroll.setBackgroundColor(0xFFF5F7FA);
        msgScroll.setVerticalScrollBarEnabled(true);
        // 让消息区优先响应用户拖动，避免外层页面把滚动抢走
        msgScroll.setOnTouchListener(this);
        msgBox = new LinearLayout(this);
        msgBox.setOrientation(LinearLayout.VERTICAL);
        msgBox.setPadding(dp(6), dp(6), dp(6), dp(6));
        msgScroll.addView(msgBox, new FrameLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT));
        LinearLayout.LayoutParams msgLp = new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, dp(380));
        msgLp.topMargin = dp(6);
        convCard.addView(msgScroll, msgLp);

        tvMsgHint = label("暂无消息，发一条试试", 12);
        tvMsgHint.setTextColor(COLOR_SUB);
        tvMsgHint.setGravity(Gravity.CENTER);
        tvMsgHint.setPadding(0, dp(20), 0, dp(20));
        msgBox.addView(tvMsgHint, new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT));

        // 输入栏：多行输入 + 发送 + 选择文件
        LinearLayout inputBar = new LinearLayout(this);
        inputBar.setOrientation(LinearLayout.HORIZONTAL);
        inputBar.setGravity(Gravity.BOTTOM);
        LinearLayout.LayoutParams inputLp = new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT);
        inputLp.topMargin = dp(8);

        etInput = new EditText(this);
        etInput.setHint("输入消息…");
        etInput.setTextSize(14);
        etInput.setMinLines(2);
        etInput.setMaxLines(6);
        etInput.setGravity(Gravity.TOP | Gravity.START);
        etInput.setVerticalScrollBarEnabled(true);
        etInput.setInputType(InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_FLAG_MULTI_LINE);
        inputBar.addView(etInput, weight(1));

        Button btnSendMsg = button("发送");
        btnSendMsg.setOnClickListener(this);
        LinearLayout.LayoutParams sendLp = new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.WRAP_CONTENT, dp(44));
        sendLp.leftMargin = dp(6);
        inputBar.addView(btnSendMsg, sendLp);

        TextView btnAttach = smallButton("📎");
        btnAttach.setTextSize(18);
        btnAttach.setPadding(dp(10), dp(8), dp(10), dp(8));
        btnAttach.setOnClickListener(this);
        LinearLayout.LayoutParams attachLp = new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.WRAP_CONTENT, ViewGroup.LayoutParams.WRAP_CONTENT);
        attachLp.leftMargin = dp(6);
        inputBar.addView(btnAttach, attachLp);

        TextView btnFull = smallButton("全屏");
        btnFull.setOnClickListener(this);
        LinearLayout.LayoutParams fullLp = new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.WRAP_CONTENT, ViewGroup.LayoutParams.WRAP_CONTENT);
        fullLp.leftMargin = dp(6);
        inputBar.addView(btnFull, fullLp);

        convCard.addView(inputBar, inputLp);
        root.addView(convCard);

        // ---- 手机麦克风卡片 ----
        LinearLayout micCard = card();
        TextView micTitle = label("🎤 手机麦克风", 16);
        micTitle.setTypeface(null, Typeface.BOLD);
        micCard.addView(micTitle);

        btnMic = button("🎤 开启手机麦克风");
        btnMic.setTextSize(17);
        btnMic.setOnClickListener(this);
        LinearLayout.LayoutParams micBtnLp = new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, dp(52));
        micBtnLp.topMargin = dp(6);
        micCard.addView(btnMic, micBtnLp);

        tvMicStatus = label("未开启", 14);
        tvMicStatus.setTextColor(COLOR_SUB);
        tvMicStat = label("已传输 0.0 秒 · 0 B", 12);
        tvMicStat.setTextColor(COLOR_SUB);
        micCard.addView(tvMicStatus);
        micCard.addView(tvMicStat);
        micCard.addView(label("说明：声音实时送到电脑，不保存文件。电脑上其它软件把麦克风"
                + "选成对应设备即可（无需装虚拟驱动）。", 11));
        root.addView(micCard);

        // ---- 同步开关卡片（放在会话下方，保持原有能力） ----
        LinearLayout swCard = card();
        swClip = new Switch(this);
        swClip.setText("剪贴板同步");
        swClip.setTextSize(15);
        swClip.setChecked(Prefs.isClipboardOn(this));
        swClip.setOnCheckedChangeListener(this);
        swCard.addView(swClip);
        swCard.addView(label("说明：Android 10 起系统禁止后台应用读取剪贴板，"
                + "只有本 App 停留在前台时才能自动同步手机剪贴板。", 11));

        swNotify = new Switch(this);
        swNotify.setText("通知转发");
        swNotify.setTextSize(15);
        swNotify.setChecked(Prefs.isNotifyOn(this));
        swNotify.setOnCheckedChangeListener(this);
        swCard.addView(swNotify);

        Button btnNotifSetting = button("打开「通知使用权」设置");
        btnNotifSetting.setOnClickListener(this);
        LinearLayout.LayoutParams nslp = new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT);
        nslp.topMargin = dp(4);
        swCard.addView(btnNotifSetting, nslp);
        root.addView(swCard);

        // ---- 日志卡片 ----
        LinearLayout logCard = card();
        logCard.addView(label("事件日志（最近 50 条）", 13));
        logScroll = new ScrollView(this);
        LinearLayout.LayoutParams logLp = new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, dp(180));
        tvLog = new TextView(this);
        tvLog.setTextSize(11);
        tvLog.setTextColor(0xFF444444);
        tvLog.setTypeface(Typeface.MONOSPACE);
        tvLog.setTextIsSelectable(true);
        tvLog.setPadding(dp(6), dp(6), dp(6), dp(6));
        logScroll.setBackgroundColor(0xFFF7F9FC);
        logScroll.addView(tvLog, new FrameLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT));
        logCard.addView(logScroll, logLp);
        root.addView(logCard);

        setContentView(page);
        renderMessages();
    }

    // ==================== 统一事件分发 ====================

    @Override
    public void onClick(View v) {
        Object tag = v.getTag();
        // 关闭按钮：tag 是它要关闭的 Dialog
        if (tag instanceof Dialog) {
            try {
                ((Dialog) tag).dismiss();
            } catch (Exception ignored) {
            }
            return;
        }
        // 会话气泡：文字→复制，文件→预览
        if (tag instanceof HttpApi.MsgItem) {
            openMessage((HttpApi.MsgItem) tag);
            return;
        }
        if (v == tvNewBanner) {
            openConversationFromBanner();
            return;
        }
        if (v == btnDiscover) {
            doDiscover();
        } else if (v == btnPair) {
            doPair();
        } else if (v == btnMic) {
            toggleMic();
        } else if (v instanceof Button) {
            // 其余无 tag 的普通按钮按文字区分，避免为每个按钮写匿名类
            CharSequence text = ((Button) v).getText();
            String s = text == null ? "" : text.toString();
            if ("手动输入 IP".equals(s)) {
                showManualDialog();
            } else if ("断开".equals(s)) {
                doDisconnect();
            } else if ("重新连接".equals(s)) {
                btnReconnect.setVisibility(View.GONE);
                connectNow();
            } else if ("发送".equals(s)) {
                sendTextNow();
            } else if ("打开「通知使用权」设置".equals(s)) {
                openNotificationAccess();
            }
        } else if (v instanceof TextView) {
            CharSequence text = ((TextView) v).getText();
            String s = text == null ? "" : text.toString();
            if ("刷新".equals(s)) {
                schedulePoll(0);
            } else if ("📎".equals(s)) {
                pickFiles();
            } else if ("全屏".equals(s)) {
                showFullEditor();
            }
        }
    }

    @Override
    public void onCheckedChanged(CompoundButton buttonView, boolean isChecked) {
        if (buttonView == swClip) {
            Prefs.setClipboardOn(this, isChecked);
            syncWatcher();
            AppLog.add(isChecked ? "剪贴板同步已开启（仅 App 在前台时生效）" : "剪贴板同步已关闭");
        } else if (buttonView == swNotify) {
            Prefs.setNotifyOn(this, isChecked);
            if (isChecked && !hasNotificationAccess()) {
                toast("请先授予「通知使用权」");
                openNotificationAccess();
            }
            AppLog.add(isChecked ? "通知转发已开启" : "通知转发已关闭");
        }
    }

    /** 长按：文件气泡弹「预览 / 下载到手机 / 取消」，文字气泡弹「复制 / 取消」 */
    @Override
    public boolean onLongClick(View v) {
        Object tag = v.getTag();
        if (tag instanceof HttpApi.MsgItem) {
            showMessageActions((HttpApi.MsgItem) tag);
            return true;
        }
        return false;
    }

    /** 消息区触摸时禁止外层页面拦截手势，保证内层可以独立滚动 */
    @Override
    public boolean onTouch(View v, MotionEvent event) {
        if (v == msgScroll && v.getParent() != null) {
            v.getParent().requestDisallowInterceptTouchEvent(true);
        }
        return false;
    }

    /** 全屏编辑 / 会话气泡操作对话框 / 电脑连接请求的选项回调 */
    @Override
    public void onClick(DialogInterface dialog, int which) {
        if (pendingConnectReq != null
                && (which == DialogInterface.BUTTON_POSITIVE
                || which == DialogInterface.BUTTON_NEGATIVE)) {
            final LanDiscovery.ConnectRequest req = pendingConnectReq;
            pendingConnectReq = null;
            LanDiscovery lan = LanDiscovery.get(this);
            if (which == DialogInterface.BUTTON_POSITIVE) {
                lan.acceptRequest(req);
                if (tvStatus != null) {
                    tvStatus.setText("状态：等待电脑下发凭据…");
                    tvStatus.setTextColor(COLOR_WARN);
                }
                AppLog.add("已允许电脑「" + req.pcName + "」连接，等待电脑确认…");
            } else {
                lan.denyRequest(req, "手机端拒绝了连接");
                AppLog.add("已拒绝电脑「" + req.pcName + "」的连接请求");
                toast("已拒绝");
            }
            return;
        }
        if (etFullEdit != null) {
            EditText ed = etFullEdit;
            etFullEdit = null;
            tvFullCount = null;
            if (which == DialogInterface.BUTTON_POSITIVE && etInput != null) {
                // 只写回输入框，不自动发送
                etInput.setText(ed.getText().toString());
                etInput.setSelection(etInput.getText().length());
            }
            return;
        }
        if (dialogMsg == null) {
            return;
        }
        HttpApi.MsgItem m = dialogMsg;
        dialogMsg = null;
        if ("file".equals(m.kind)) {
            if (which == 0) {
                previewFile(toFileItem(m));
            } else if (which == 1) {
                downloadFile(m.name);
            }
        } else if (which == 0) {
            copyMessage(m);
        }
    }

    @Override
    public boolean handleMessage(Message msg) {
        if (msg.what == MSG_POLL) {
            startPoll();
            return true;
        }
        if (msg.what == MSG_UI_POLL) {
            pollInFlight = false;
            if (!LinkService.isConnected()) {
                // 已经断开：丢弃这一轮结果，也不再排队
                pendingMsgs = null;
                pendingMsgsOk = false;
                stopPolling();
                return true;
            }
            applyPollResult();
            if (pollQueued) {
                pollQueued = false;
                schedulePoll(0);
            } else {
                schedulePoll(POLL_INTERVAL);
            }
            return true;
        }
        if (msg.what == MSG_UI_SEND) {
            applySendResult();
            return true;
        }
        if (msg.what == MSG_SCROLL) {
            try {
                msgScroll.fullScroll(View.FOCUS_DOWN);
            } catch (Exception ignored) {
            }
            return true;
        }
        if (msg.what == MSG_LAN_REQ) {
            Object o = msg.obj;
            if (o instanceof LanDiscovery.ConnectRequest) {
                showConnectRequest((LanDiscovery.ConnectRequest) o);
            }
            return true;
        }
        if (msg.what == MSG_LAN_READY) {
            Object o = msg.obj;
            if (o instanceof LanDiscovery.Computer) {
                String token = msg.getData() == null
                        ? "" : msg.getData().getString("token", "");
                applyPcReady((LanDiscovery.Computer) o, token);
            }
            return true;
        }
        if (msg.what == MSG_LAN_DENY) {
            applyPcDenied(msg.obj == null ? "" : msg.obj.toString());
            return true;
        }
        if (msg.what == MSG_LAN_TIMEOUT) {
            applyRequestTimeout();
            return true;
        }
        if (msg.what == MSG_SEARCH_DONE) {
            onSearchFinished();
            return true;
        }
        return false;
    }

    // ==================== LanDiscovery 回调（后台线程 -> 主线程） ====================

    @Override
    public void onPcConnectRequest(LanDiscovery.ConnectRequest request) {
        Handler h = pollHandler;
        if (h == null) {
            return;
        }
        Message m = h.obtainMessage(MSG_LAN_REQ);
        m.obj = request;
        h.sendMessage(m);
    }

    @Override
    public void onPcReady(LanDiscovery.Computer pc, String token) {
        Handler h = pollHandler;
        if (h == null) {
            return;
        }
        Message m = h.obtainMessage(MSG_LAN_READY);
        m.obj = pc;
        Bundle b = new Bundle();
        b.putString("token", token == null ? "" : token);
        m.setData(b);
        h.sendMessage(m);
    }

    @Override
    public void onPcDenied(String reqId, String reason) {
        Handler h = pollHandler;
        if (h == null) {
            return;
        }
        Message m = h.obtainMessage(MSG_LAN_DENY);
        m.obj = reason;
        h.sendMessage(m);
    }

    @Override
    public void onRequestTimeout(String reqId) {
        Handler h = pollHandler;
        if (h == null) {
            return;
        }
        h.sendEmptyMessage(MSG_LAN_TIMEOUT);
    }

    // ==================== 视图小工具 ====================

    private int dp(int value) {
        return (int) (value * getResources().getDisplayMetrics().density + 0.5f);
    }

    private TextView label(String text, int sp) {
        TextView tv = new TextView(this);
        tv.setText(text);
        tv.setTextSize(sp);
        tv.setTextColor(COLOR_TEXT);
        tv.setPadding(0, dp(4), 0, dp(4));
        tv.setLineSpacing(dp(2), 1f);
        return tv;
    }

    private Button button(String text) {
        Button b = new Button(this);
        b.setText(text);
        b.setAllCaps(false);
        b.setTextSize(14);
        return b;
    }

    /** 白底圆角卡片 */
    private LinearLayout card() {
        LinearLayout l = new LinearLayout(this);
        l.setOrientation(LinearLayout.VERTICAL);
        l.setPadding(dp(14), dp(10), dp(14), dp(12));
        GradientDrawable bg = new GradientDrawable();
        bg.setColor(0xFFFFFFFF);
        bg.setCornerRadius(dp(10));
        l.setBackground(bg);
        LinearLayout.LayoutParams lp = new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT);
        lp.bottomMargin = dp(10);
        l.setLayoutParams(lp);
        return l;
    }

    private LinearLayout.LayoutParams weight(float w) {
        return new LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, w);
    }

    private LinearLayout.LayoutParams weightWithLeft(float w) {
        LinearLayout.LayoutParams lp = new LinearLayout.LayoutParams(
                0, ViewGroup.LayoutParams.WRAP_CONTENT, w);
        lp.leftMargin = dp(8);
        return lp;
    }

    /** 浅蓝底的小号文字按钮（比系统 Button 紧凑很多，适合卡片内的操作） */
    private TextView smallButton(String text) {
        TextView tv = new TextView(this);
        tv.setText(text);
        tv.setTextSize(12);
        tv.setTextColor(COLOR_OK);
        tv.setPadding(dp(10), dp(5), dp(10), dp(5));
        tv.setGravity(Gravity.CENTER);
        tv.setBackground(roundBg(0xFFE6F7FF));
        tv.setClickable(true);
        return tv;
    }

    private GradientDrawable roundBg(int color) {
        GradientDrawable g = new GradientDrawable();
        g.setColor(color);
        g.setCornerRadius(dp(6));
        return g;
    }

    // ==================== 文件类型辅助 ====================

    private static String typeOf(HttpApi.FileItem f) {
        return (f == null || f.type == null || f.type.isEmpty()) ? "other" : f.type;
    }

    private static String typeLabel(String type) {
        if ("image".equals(type)) {
            return "图片";
        }
        if ("video".equals(type)) {
            return "视频";
        }
        if ("audio".equals(type)) {
            return "音频";
        }
        if ("text".equals(type)) {
            return "文本";
        }
        if ("pdf".equals(type)) {
            return "PDF";
        }
        return "其他";
    }

    private static String sizeOf(HttpApi.FileItem f) {
        if (f.sizeText != null && !f.sizeText.isEmpty()) {
            return f.sizeText;
        }
        return formatSize(f.size);
    }

    private static String formatSize(long bytes) {
        if (bytes < 1024) {
            return bytes + " B";
        }
        if (bytes < 1024 * 1024) {
            return String.format(Locale.US, "%.1f KB", bytes / 1024.0);
        }
        if (bytes < 1024L * 1024 * 1024) {
            return String.format(Locale.US, "%.1f MB", bytes / (1024.0 * 1024.0));
        }
        return String.format(Locale.US, "%.2f GB", bytes / (1024.0 * 1024.0 * 1024.0));
    }

    /** 中间省略的超长文件名，保证能看到扩展名 */
    private static String ellipsizeMiddle(String name, int max) {
        if (name == null) {
            return "";
        }
        if (name.length() <= max) {
            return name;
        }
        int head = max * 2 / 3;
        int tail = max - head - 1;
        if (tail < 1) {
            tail = 1;
        }
        return name.substring(0, head) + "…" + name.substring(name.length() - tail);
    }

    // ==================== 状态刷新 ====================

    private void refreshState() {
        if (tvStatus == null) {
            return;
        }
        int st = LinkService.getState();

        if (LinkService.isConnected()) {
            tvStatus.setText("状态：已连接");
            tvStatus.setTextColor(COLOR_OK_GREEN);
        } else if (st == LinkService.STATE_RECONNECTING) {
            int n = LinkService.getReconnectAttempt();
            int sec = LinkService.getReconnectRemainSec();
            tvStatus.setText("状态：重连中(第" + n + "次"
                    + (sec > 0 ? "，" + sec + " 秒后重试" : "") + ")");
            tvStatus.setTextColor(COLOR_ERR);
        } else if (st == LinkService.STATE_CONNECTING || LinkService.isRunning()) {
            tvStatus.setText("状态：连接中…");
            tvStatus.setTextColor(COLOR_WARN);
        } else {
            tvStatus.setText("状态：未连接");
            tvStatus.setTextColor(COLOR_SUB);
        }

        String pc = LinkService.getServerName();
        if (pc == null || pc.isEmpty()) {
            pc = Prefs.getPcName(this);
        }
        tvPcName.setText("电脑：" + (pc == null || pc.isEmpty() ? "—" : pc));

        String ip = Prefs.getIp(this);
        String addr = "地址：" + (ip.isEmpty() ? "—" : ip + ":" + Prefs.getPort(this));
        String err = LinkService.getLastError();
        if (!LinkService.isConnected() && err != null && !err.isEmpty()) {
            addr += "\n最近错误：" + err;
        }
        tvAddr.setText(addr);

        // 已连上就收起「重新连接」；被踢下线的状态则重新显示出来
        if (btnReconnect != null) {
            if (LinkService.isConnected()) {
                btnReconnect.setVisibility(View.GONE);
            } else if (err != null && err.contains("已被电脑断开")) {
                btnReconnect.setVisibility(View.VISIBLE);
            }
        }
    }

    private void refreshLog() {
        if (tvLog == null) {
            return;
        }
        List<String> lines = AppLog.snapshot();
        if (lines.isEmpty()) {
            tvLog.setText("暂无事件");
            return;
        }
        StringBuilder sb = new StringBuilder();
        for (int i = 0; i < lines.size(); i++) {
            sb.append(lines.get(i));
            if (i != lines.size() - 1) {
                sb.append('\n');
            }
        }
        tvLog.setText(sb.toString());
        logScroll.post(new Runnable() {
            @Override
            public void run() {
                logScroll.fullScroll(View.FOCUS_DOWN);
            }
        });
    }

    // ==================== 剪贴板 ====================

    /**
     * 只在 Activity 处于前台时挂载监听。
     * 这是 Android 10+ 的系统限制：后台 App 无法读取剪贴板，
     * 前台服务也不豁免，所以不要试图在 Service 里一直挂着。
     */
    private void syncWatcher() {
        if (watcher == null) {
            return;
        }
        if (resumed && Prefs.isClipboardOn(this)) {
            watcher.attach();
        } else {
            watcher.detach();
        }
    }

    // ==================== 发现 / 连接 ====================

    /**
     * 搜索按钮：触发 LanDiscovery 立即广播，2.5 秒后读它的电脑列表。
     * 同一时间只有 LanDiscovery 一套绑定 47777 的实现。
     */
    private void doDiscover() {
        if (LinkService.isConnected()) {
            toast("已连接电脑，如需切换请先断开");
            return;
        }
        btnDiscover.setEnabled(false);
        tvStatus.setText("状态：正在搜索同一 Wi-Fi 的电脑…");
        tvStatus.setTextColor(COLOR_WARN);
        AppLog.add("开始局域网搜索…");

        LanDiscovery lan = LanDiscovery.get(this);
        lan.setListener(this);
        lan.start(this);
        lan.searchNow();
        if (pollHandler != null) {
            pollHandler.removeMessages(MSG_SEARCH_DONE);
            pollHandler.sendEmptyMessageDelayed(MSG_SEARCH_DONE, SEARCH_WAIT_MS);
        }
    }

    /** 搜索等待结束 */
    private void onSearchFinished() {
        if (btnDiscover != null) {
            btnDiscover.setEnabled(true);
        }
        refreshState();
        List<LanDiscovery.Computer> found = LanDiscovery.get(this).getComputers();
        if (found.isEmpty()) {
            AppLog.add("未发现电脑");
            dialog("未发现电脑",
                    "请确认：\n1. 电脑端已启动「手机互传」服务\n"
                            + "2. 手机与电脑连接同一个 Wi-Fi（同一网络免输入配对码）\n"
                            + "3. 路由器未开启 AP 隔离/访客网络\n\n"
                            + "如果手机和电脑不在同一个网络，请改用「手动输入 IP」，"
                            + "并输入电脑上显示的配对码。");
        } else {
            AppLog.add("发现 " + found.size() + " 台电脑");
            showDeviceDialog(found);
        }
    }

    private void showDeviceDialog(final List<LanDiscovery.Computer> devices) {
        String[] names = new String[devices.size()];
        for (int i = 0; i < devices.size(); i++) {
            names[i] = devices.get(i).label();
        }
        new AlertDialog.Builder(this)
                .setTitle("选择要连接的电脑（同一 Wi-Fi 免输码）")
                .setItems(names, new DialogInterface.OnClickListener() {
                    @Override
                    public void onClick(DialogInterface d, int which) {
                        connectToPc(devices.get(which));
                    }
                })
                .setNegativeButton("取消", null)
                .show();
    }

    /** 选中列表里的电脑：发 CONNECT_REQ，等电脑确认后自动连接 */
    private void connectToPc(LanDiscovery.Computer pc) {
        if (pc == null) {
            return;
        }
        LanDiscovery lan = LanDiscovery.get(this);
        lan.setListener(this);
        boolean ok = lan.requestConnect(pc);
        if (ok) {
            tvStatus.setText("状态：等待电脑确认…");
            tvStatus.setTextColor(COLOR_WARN);
            AppLog.add("已请求连接「" + (pc.name == null || pc.name.isEmpty() ? pc.ip : pc.name)
                    + "」，等待电脑确认…");
            toast("等待电脑确认…");
        } else if (lan.isWaiting()) {
            toast("正在等待电脑确认…");
        } else {
            AppLog.add("连接请求发送失败");
            toast("连接请求发送失败，请重试");
        }
    }

    /** 电脑在弹框里允许 / 拒绝后，电脑回 ACCEPT（手机发起）或 TOKEN（电脑发起） */
    private void showConnectRequest(final LanDiscovery.ConnectRequest req) {
        if (req == null) {
            return;
        }
        if (LinkService.isConnected()) {
            LanDiscovery.get(this).denyRequest(req, "手机已连接");
            return;
        }
        if (pendingConnectReq != null) {
            // 同一时间只处理一个确认框
            LanDiscovery.get(this).denyRequest(req, "手机端正在处理其它连接请求");
            return;
        }
        pendingConnectReq = req;
        try {
            new AlertDialog.Builder(this)
                    .setTitle("连接请求")
                    .setMessage("电脑「" + (req.pcName == null || req.pcName.isEmpty()
                            ? req.ip : req.pcName) + "」请求连接，是否允许？")
                    .setPositiveButton("允许", this)
                    .setNegativeButton("拒绝", this)
                    .setCancelable(false)
                    .show();
        } catch (Exception e) {
            pendingConnectReq = null;
            toast("无法弹出连接确认框");
            return;
        }
        AppLog.add("电脑「" + req.pcName + "」请求连接，等待确认…");
    }

    /** 拿到 token：保存配置后直接连接（不再弹配对码） */
    private void applyPcReady(LanDiscovery.Computer pc, String token) {
        if (pc == null || token == null || token.isEmpty()) {
            return;
        }
        Prefs.setIp(this, pc.ip);
        Prefs.setPort(this, pc.port);
        Prefs.setToken(this, token);
        Prefs.setPcName(this, pc.name == null ? "" : pc.name);
        Prefs.setNeedPair(this, false);
        HttpApi.setToken(token);
        AppLog.add("电脑「" + (pc.name == null || pc.name.isEmpty() ? pc.ip : pc.name)
                + "」已同意连接，正在建立长连接…");
        toast("电脑已同意，正在连接…");
        refreshState();
        connectNow();
    }

    /** 电脑拒绝了本机的请求 */
    private void applyPcDenied(String reason) {
        refreshState();
        String msg = reason == null || reason.isEmpty() ? "电脑端拒绝了连接" : reason;
        AppLog.add(msg);
        toast(msg);
        if (tvStatus != null && !LinkService.isConnected()) {
            tvStatus.setText("状态：未连接");
            tvStatus.setTextColor(COLOR_SUB);
        }
        if (btnDiscover != null) {
            btnDiscover.setEnabled(true);
        }
    }

    /** 45 秒内电脑没确认 */
    private void applyRequestTimeout() {
        refreshState();
        AppLog.add("等待电脑确认超时，请重试");
        toast("等待电脑确认超时，请重试");
        if (btnDiscover != null) {
            btnDiscover.setEnabled(true);
        }
        if (tvStatus != null && !LinkService.isConnected()) {
            tvStatus.setText("状态：未连接");
            tvStatus.setTextColor(COLOR_SUB);
        }
    }

    private void showPairBox(boolean focus) {
        pairBox.setVisibility(View.VISIBLE);
        if (focus) {
            etCode.requestFocus();
        }
    }

    private void connectNow() {
        String ip = Prefs.getIp(this);
        if (ip == null || ip.isEmpty()) {
            toast("请先选择或输入电脑地址");
            return;
        }
        pairBox.setVisibility(View.GONE);
        AppLog.add("正在连接 " + ip + ":" + Prefs.getPort(this) + " …");
        if (LinkService.isRunning()) {
            // 已经在连别的电脑：先停掉旧实例，稍后按新配置重新拉起
            LinkService.stop(this);
            pairBox.postDelayed(new Runnable() {
                @Override
                public void run() {
                    LinkService.start(MainActivity.this);
                    refreshState();
                }
            }, 800);
        } else {
            LinkService.start(this);
        }
        refreshState();
    }

    private void doDisconnect() {
        // 先停轮询，避免服务销毁过程中还去请求 /api/messages
        stopPolling();
        pollQueued = false;
        LinkService.stop(this);
        AppLog.add("已断开连接");
        if (tvStatus != null) {
            tvStatus.setText("状态：未连接");
            tvStatus.setTextColor(COLOR_SUB);
        }
        if (btnReconnect != null) {
            btnReconnect.setVisibility(View.GONE);
        }
        if (btnDiscover != null) {
            btnDiscover.setEnabled(true);
        }
        messages.clear();
        localPending.clear();
        renderMessages();
        refreshState();
        dismissLater();
    }

    /** 服务销毁是异步的，稍后再刷一次状态 */
    private void dismissLater() {
        if (tvStatus == null) {
            return;
        }
        tvStatus.postDelayed(new Runnable() {
            @Override
            public void run() {
                refreshState();
            }
        }, 600);
    }

    private void showManualDialog() {
        final EditText et = new EditText(this);
        et.setHint("例如 192.168.1.8 或 192.168.1.8:8080");
        String ip = Prefs.getIp(this);
        if (!ip.isEmpty()) {
            et.setText(ip + ":" + Prefs.getPort(this));
        }
        et.setInputType(InputType.TYPE_CLASS_TEXT);
        et.setSelection(et.getText().length());

        new AlertDialog.Builder(this)
                .setTitle("手动输入电脑 IP（不同网络）")
                .setMessage("同一 Wi-Fi 请直接用「自动发现」，选中电脑后由电脑点「允许」即可，"
                        + "免输配对码。\n不同网络时才在这里输入电脑 IP，再用电脑上显示的"
                        + " 6 位配对码配对。")
                .setView(et)
                .setPositiveButton("确定", new DialogInterface.OnClickListener() {
                    @Override
                    public void onClick(DialogInterface d, int which) {
                        applyManualAddress(et);
                    }
                })
                .setNegativeButton("取消", null)
                .show();
    }

    /** 解析「IP 或 IP:端口」并更新配置 */
    private void applyManualAddress(EditText et) {
        String raw = et.getText().toString().trim();
        if (raw.isEmpty()) {
            return;
        }
        String ipPart = raw;
        int port = 8080;
        int idx = raw.lastIndexOf(':');
        if (idx > 0) {
            ipPart = raw.substring(0, idx).trim();
            try {
                port = Integer.parseInt(raw.substring(idx + 1).trim());
            } catch (Exception e) {
                port = 8080;
            }
        }
        if (ipPart.isEmpty() || port <= 0 || port > 65535) {
            toast("地址格式不正确");
            return;
        }
        boolean same = ipPart.equals(Prefs.getIp(this))
                && port == Prefs.getPort(this);
        if (!same) {
            Prefs.setToken(this, "");
            HttpApi.setToken("");
            Prefs.setNeedPair(this, true);
            Prefs.setPcName(this, "");
        }
        Prefs.setIp(this, ipPart);
        Prefs.setPort(this, port);
        refreshState();
        if (Prefs.getToken(this).isEmpty()) {
            showPairBox(true);
        } else {
            connectNow();
        }
    }

    // ==================== 配对 ====================

    private void doPair() {
        final String code = etCode.getText().toString().trim();
        if (code.length() < 4) {
            toast("请输入电脑上显示的访问码");
            return;
        }
        final String ip = Prefs.getIp(this);
        final int port = Prefs.getPort(this);
        if (ip.isEmpty()) {
            toast("请先选择或输入电脑地址");
            return;
        }
        btnPair.setEnabled(false);
        AppLog.add("正在配对…");

        runBg(new Runnable() {
            @Override
            public void run() {
                final HttpApi.PairResult result;
                try {
                    result = HttpApi.pair(ip, port, code, Prefs.getDeviceName(MainActivity.this));
                } catch (final Exception e) {
                    runOnUiThread(new Runnable() {
                        @Override
                        public void run() {
                            btnPair.setEnabled(true);
                            AppLog.add("配对失败：" + e.getMessage());
                            toast("配对失败，请检查网络");
                        }
                    });
                    return;
                }
                runOnUiThread(new Runnable() {
                    @Override
                    public void run() {
                        btnPair.setEnabled(true);
                        if (result.ok) {
                            Prefs.setToken(MainActivity.this, result.token);
                            HttpApi.setToken(result.token);
                            Prefs.setNeedPair(MainActivity.this, false);
                            etCode.setText("");
                            pairBox.setVisibility(View.GONE);
                            AppLog.add("配对成功");
                            connectNow();
                        } else {
                            AppLog.add("配对失败：" + result.error);
                            toast(result.error);
                        }
                    }
                });
            }
        });
    }

    // ==================== 文件 ====================

    private void pickFiles() {
        Intent intent = new Intent(Intent.ACTION_GET_CONTENT);
        intent.setType("*/*");
        intent.addCategory(Intent.CATEGORY_OPENABLE);
        intent.putExtra(Intent.EXTRA_ALLOW_MULTIPLE, true);
        try {
            startActivityForResult(Intent.createChooser(intent, "选择要发送的文件"),
                    REQ_PICK_FILES);
        } catch (Exception e) {
            toast("没有可用的文件选择器");
        }
    }

    private void uploadFiles(final List<Uri> uris) {
        final String ip = Prefs.getIp(this);
        final int port = Prefs.getPort(this);
        if (ip == null || ip.isEmpty()) {
            toast("请先连接电脑");
            return;
        }
        AppLog.add("开始上传 " + uris.size() + " 个文件…");
        runBg(new Runnable() {
            @Override
            public void run() {
                try {
                    int count = HttpApi.upload(MainActivity.this, ip, port, uris,
                            new HttpApi.Progress() {
                                private int last = -10;

                                @Override
                                public void onProgress(int percent, String message) {
                                    if (percent >= last + 10 || percent == 100) {
                                        last = percent;
                                        AppLog.add("上传进度 " + percent + "%");
                                    }
                                }
                            });
                    final int n = count;
                    runOnUiThread(new Runnable() {
                        @Override
                        public void run() {
                            AppLog.add("上传完成，共 " + n + " 个文件");
                            toast("已发送到电脑");
                            // 上传完成后立刻刷新会话，让右侧出现文件气泡
                            schedulePoll(0);
                        }
                    });
                } catch (final Exception e) {
                    runOnUiThread(new Runnable() {
                        @Override
                        public void run() {
                            AppLog.add("上传失败：" + e.getMessage());
                            toast("上传失败");
                        }
                    });
                }
            }
        });
    }

    // ==================== 会话渲染 ====================

    /** 重建消息列表：服务端消息 + 尚未被服务端确认的本地乐观消息 */
    private void renderMessages() {
        if (msgBox == null) {
            return;
        }
        msgBox.removeAllViews();
        if (messages.isEmpty() && localPending.isEmpty()) {
            if (tvMsgHint.getParent() == null) {
                msgBox.addView(tvMsgHint, new LinearLayout.LayoutParams(
                        ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT));
            }
            return;
        }
        for (HttpApi.MsgItem m : messages) {
            msgBox.addView(messageRow(m));
        }
        for (HttpApi.MsgItem m : localPending) {
            msgBox.addView(messageRow(m));
        }
        scrollMessagesToBottom();
    }

    /**
     * 一条消息：右侧（我，浅蓝） / 左侧（电脑或其它手机，白底细边框），
     * 宽度最多约 78% 屏宽。
     */
    private View messageRow(final HttpApi.MsgItem m) {
        boolean mine = isMine(m);
        String who = senderLabel(m);
        LinearLayout row = new LinearLayout(this);
        row.setOrientation(LinearLayout.HORIZONTAL);
        row.setGravity(mine ? Gravity.END : Gravity.START);
        LinearLayout.LayoutParams rlp = new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT);
        rlp.topMargin = dp(4);
        row.setLayoutParams(rlp);

        int maxW = (int) (getResources().getDisplayMetrics().widthPixels * 0.78);

        LinearLayout bubble = new LinearLayout(this);
        bubble.setOrientation(LinearLayout.VERTICAL);
        bubble.setPadding(dp(10), dp(7), dp(10), dp(7));
        bubble.setBackground(mine ? roundBg(0xFFD6E9FF)
                : bubbleBg(0xFFFFFFFF, 0xFFDDDDDD));

        if ("file".equals(m.kind)) {
            TextView name = new TextView(this);
            name.setText("📎 " + m.name);
            name.setTextSize(13);
            name.setTextColor(COLOR_TEXT);
            name.setMaxLines(2);
            name.setEllipsize(android.text.TextUtils.TruncateAt.END);
            name.setMaxWidth(maxW - dp(24));
            bubble.addView(name);

            TextView meta = new TextView(this);
            meta.setText(fileMeta(m, who));
            meta.setTextSize(10);
            meta.setTextColor(COLOR_SUB);
            meta.setGravity(mine ? Gravity.END : Gravity.START);
            meta.setPadding(0, dp(3), 0, 0);
            bubble.addView(meta);
        } else {
            TextView body = new TextView(this);
            body.setText(m.text);
            body.setTextSize(13);
            body.setTextColor(COLOR_TEXT);
            body.setLineSpacing(dp(2), 1f);
            body.setMaxWidth(maxW - dp(24));
            bubble.addView(body);

            TextView meta = new TextView(this);
            meta.setText(who + (m.time == null || m.time.isEmpty() ? "" : " · " + m.time));
            meta.setTextSize(10);
            meta.setTextColor(COLOR_SUB);
            meta.setGravity(mine ? Gravity.END : Gravity.START);
            meta.setPadding(0, dp(3), 0, 0);
            bubble.addView(meta);
        }

        bubble.setTag(m);
        bubble.setOnClickListener(this);
        bubble.setOnLongClickListener(this);
        row.addView(bubble);
        return row;
    }

    private static String fileMeta(HttpApi.MsgItem m, String who) {
        String size = (m.sizeText == null || m.sizeText.isEmpty())
                ? formatSize(m.size) : m.sizeText;
        return who + " · " + size
                + (m.time == null || m.time.isEmpty() ? "" : " · " + m.time);
    }

    /**
     * 是不是「我」发的：dir=phone->pc 且 device 等于本机设备名。
     * device 为空视为历史遗留（旧版没有该字段），按本机处理，避免旧消息全跑到左边。
     */
    private boolean isMine(HttpApi.MsgItem m) {
        if (m == null || !"phone->pc".equals(m.dir)) {
            return false;
        }
        String mine = Prefs.getDeviceName(this);
        return m.device == null || m.device.isEmpty() || m.device.equals(mine);
    }

    /** 气泡上方的发送方标签 */
    private String senderLabel(HttpApi.MsgItem m) {
        if (m == null) {
            return "";
        }
        if ("pc->phone".equals(m.dir)) {
            return "💻 电脑";
        }
        if (isMine(m)) {
            return "我";
        }
        return "📱 " + (m.device == null || m.device.isEmpty() ? "手机" : m.device);
    }

    /**
     * 是否需要提示的新内容：
     *   · 电脑发的（pc->phone）
     *   · 其它手机发的（phone->pc 且 device 不是本机）
     * 自己发的不算；device 为空的旧数据也不算，避免升级后一启动就刷屏。
     */
    private boolean isIncoming(HttpApi.MsgItem m) {
        if (m == null) {
            return false;
        }
        if ("pc->phone".equals(m.dir)) {
            return true;
        }
        if ("phone->pc".equals(m.dir)) {
            return m.device != null && !m.device.isEmpty()
                    && !m.device.equals(Prefs.getDeviceName(this));
        }
        return false;
    }

    /** 白底 + 细边框气泡 */
    private GradientDrawable bubbleBg(int fill, int stroke) {
        GradientDrawable g = new GradientDrawable();
        g.setColor(fill);
        g.setCornerRadius(dp(10));
        g.setStroke(dp(1), stroke);
        return g;
    }

    private void scrollMessagesToBottom() {
        if (pollHandler == null) {
            return;
        }
        pollHandler.removeMessages(MSG_SCROLL);
        pollHandler.sendEmptyMessageDelayed(MSG_SCROLL, 60);
    }

    /** 单击气泡：文字复制，文件预览 */
    private void openMessage(HttpApi.MsgItem m) {
        if (m == null) {
            return;
        }
        if ("file".equals(m.kind)) {
            previewFile(toFileItem(m));
        } else {
            copyMessage(m);
        }
    }

    private void copyMessage(HttpApi.MsgItem m) {
        if (m == null || m.text == null || m.text.isEmpty()) {
            return;
        }
        // 用 applyRemote 写入剪贴板，登记为「电脑内容」，避免剪贴板同步再发回电脑
        ClipboardWatcher.applyRemote(this, m.text);
        toast("已复制到剪贴板");
    }

    private void showMessageActions(HttpApi.MsgItem m) {
        // 同时只允许一个对话框：清掉全屏编辑的残留引用
        etFullEdit = null;
        tvFullCount = null;
        dialogMsg = m;
        boolean file = "file".equals(m.kind);
        try {
            new AlertDialog.Builder(this)
                    .setTitle(file ? ellipsizeMiddle(m.name, 30) : "文字消息")
                    .setItems(file ? new String[]{"预览", "下载到手机", "取消"}
                            : new String[]{"复制", "取消"}, this)
                    .show();
        } catch (Exception e) {
            dialogMsg = null;
            toast("无法打开操作菜单");
        }
    }

    /** 会话里的文件消息转成预览 / 下载所需的 FileItem（类型按扩展名推断） */
    private static HttpApi.FileItem toFileItem(HttpApi.MsgItem m) {
        HttpApi.FileItem f = new HttpApi.FileItem();
        f.name = m.name;
        f.size = m.size;
        f.sizeText = m.sizeText;
        f.type = typeFromName(m.name);
        return f;
    }

    /** 与电脑端 _file_kind 一致：按扩展名判断预览方式 */
    private static String typeFromName(String name) {
        String n = name == null ? "" : name.toLowerCase(Locale.US);
        if (n.endsWith(".jpg") || n.endsWith(".jpeg") || n.endsWith(".png")
                || n.endsWith(".gif") || n.endsWith(".bmp") || n.endsWith(".webp")
                || n.endsWith(".heic") || n.endsWith(".heif")) {
            return "image";
        }
        if (n.endsWith(".mp4") || n.endsWith(".mkv") || n.endsWith(".avi")
                || n.endsWith(".mov") || n.endsWith(".3gp") || n.endsWith(".webm")
                || n.endsWith(".m4v")) {
            return "video";
        }
        if (n.endsWith(".mp3") || n.endsWith(".wav") || n.endsWith(".flac")
                || n.endsWith(".aac") || n.endsWith(".ogg") || n.endsWith(".m4a")
                || n.endsWith(".amr")) {
            return "audio";
        }
        if (n.endsWith(".txt") || n.endsWith(".md") || n.endsWith(".log")
                || n.endsWith(".json") || n.endsWith(".xml") || n.endsWith(".csv")
                || n.endsWith(".ini") || n.endsWith(".py") || n.endsWith(".js")
                || n.endsWith(".java") || n.endsWith(".c") || n.endsWith(".cpp")
                || n.endsWith(".h") || n.endsWith(".html") || n.endsWith(".css")
                || n.endsWith(".bat")) {
            return "text";
        }
        if (n.endsWith(".pdf")) {
            return "pdf";
        }
        return "other";
    }

    // ==================== 文件预览 ====================

    /** 按类型分流预览 */
    private void previewFile(final HttpApi.FileItem f) {
        String type = typeOf(f);
        if ("text".equals(type)) {
            previewText(f);
        } else if ("image".equals(type)) {
            previewToCache(f, false);
        } else if ("video".equals(type) || "audio".equals(type)) {
            previewToCache(f, true);
        } else {
            toast("该类型不支持预览，长按气泡可下载");
        }
    }

    /** 文本：走 /api/preview，只展示前 8KB */
    private void previewText(final HttpApi.FileItem f) {
        final String ip = Prefs.getIp(this);
        final int port = Prefs.getPort(this);
        AppLog.add("预览文本：" + f.name);
        runBg(new Runnable() {
            @Override
            public void run() {
                try {
                    final HttpApi.PreviewResult pr = HttpApi.preview(ip, port, f.name);
                    runOnUiThread(new Runnable() {
                        @Override
                        public void run() {
                            showTextDialog(f, pr);
                        }
                    });
                } catch (final Exception e) {
                    runOnUiThread(new Runnable() {
                        @Override
                        public void run() {
                            AppLog.add("文本预览失败：" + e.getMessage());
                            toast("预览失败：" + e.getMessage());
                        }
                    });
                }
            }
        });
    }

    private void showTextDialog(final HttpApi.FileItem f, HttpApi.PreviewResult pr) {
        try {
            LinearLayout box = new LinearLayout(this);
            box.setOrientation(LinearLayout.VERTICAL);
            box.setPadding(dp(16), dp(8), dp(16), dp(4));

            TextView info = new TextView(this);
            info.setTextSize(11);
            info.setTextColor(pr.truncated ? COLOR_WARN : COLOR_SUB);
            info.setText(sizeOf(f) + " · " + typeLabel(typeOf(f)) + " · "
                    + (pr.truncated ? "内容过长，仅显示前 8KB，完整内容请下载" : "完整内容"));
            box.addView(info);

            TextView content = new TextView(this);
            content.setText(pr.content == null || pr.content.isEmpty() ? "（空文件）" : pr.content);
            content.setTextSize(13);
            content.setTextColor(COLOR_TEXT);
            content.setTextIsSelectable(true);
            content.setPadding(dp(2), dp(6), dp(2), dp(6));
            ScrollView sv = new ScrollView(this);
            sv.addView(content);
            box.addView(sv, new LinearLayout.LayoutParams(
                    ViewGroup.LayoutParams.MATCH_PARENT, dp(340)));

            new AlertDialog.Builder(this)
                    .setTitle(f.name)
                    .setView(box)
                    .setPositiveButton("下载", new DialogInterface.OnClickListener() {
                        @Override
                        public void onClick(DialogInterface d, int which) {
                            downloadFile(f.name);
                        }
                    })
                    .setNegativeButton("关闭", null)
                    .show();
        } catch (Exception e) {
            toast("预览失败：" + e.getMessage());
        }
    }

    /** 图片 / 音视频：先下载到缓存目录，再本地预览 */
    private void previewToCache(final HttpApi.FileItem f, final boolean media) {
        final String ip = Prefs.getIp(this);
        final int port = Prefs.getPort(this);
        AppLog.add("准备预览：" + f.name + "（先下载到缓存）");
        runBg(new Runnable() {
            @Override
            public void run() {
                try {
                    final File file = HttpApi.downloadToCache(MainActivity.this, ip, port, f.name);
                    runOnUiThread(new Runnable() {
                        @Override
                        public void run() {
                            if (media) {
                                showPlayerDialog(file, "video".equals(typeOf(f)), f.name);
                            } else {
                                showImageDialog(file);
                            }
                        }
                    });
                } catch (final Exception e) {
                    runOnUiThread(new Runnable() {
                        @Override
                        public void run() {
                            AppLog.add("预览下载失败：" + e.getMessage());
                            toast("预览失败：" + e.getMessage());
                        }
                    });
                }
            }
        });
    }

    /** 全屏图片预览，支持双指缩放 */
    private void showImageDialog(final File file) {
        final Bitmap bm;
        try {
            BitmapFactory.Options opts = new BitmapFactory.Options();
            opts.inJustDecodeBounds = true;
            BitmapFactory.decodeFile(file.getAbsolutePath(), opts);
            int sample = 1;
            while (opts.outWidth / sample > 2048 || opts.outHeight / sample > 2048) {
                sample *= 2;
            }
            BitmapFactory.Options real = new BitmapFactory.Options();
            real.inSampleSize = sample;
            bm = BitmapFactory.decodeFile(file.getAbsolutePath(), real);
        } catch (Exception e) {
            toast("图片解码失败：" + e.getMessage());
            return;
        }
        if (bm == null) {
            toast("图片解码失败，请下载后用其它应用查看");
            return;
        }
        try {
            final Dialog d = new Dialog(this, android.R.style.Theme_Black_NoTitleBar_Fullscreen);
            FrameLayout root = new FrameLayout(this);
            root.setBackgroundColor(0xFF000000);

            final ImageView iv = new ImageView(this);
            iv.setImageBitmap(bm);
            iv.setScaleType(ImageView.ScaleType.FIT_CENTER);
            root.addView(iv, new FrameLayout.LayoutParams(
                    ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT));

            final float[] scale = {1f};
            final ScaleGestureDetector sgd = new ScaleGestureDetector(this,
                    new ScaleGestureDetector.SimpleOnScaleGestureListener() {
                        @Override
                        public boolean onScale(ScaleGestureDetector detector) {
                            scale[0] *= detector.getScaleFactor();
                            if (scale[0] < 0.5f) {
                                scale[0] = 0.5f;
                            }
                            if (scale[0] > 6f) {
                                scale[0] = 6f;
                            }
                            iv.setScaleX(scale[0]);
                            iv.setScaleY(scale[0]);
                            return true;
                        }
                    });
            iv.setOnTouchListener(new View.OnTouchListener() {
                @Override
                public boolean onTouch(View v, MotionEvent event) {
                    sgd.onTouchEvent(event);
                    return true;
                }
            });

            TextView close = smallButton("关闭");
            close.setTag(d);
            close.setOnClickListener(this);
            FrameLayout.LayoutParams clp = new FrameLayout.LayoutParams(
                    ViewGroup.LayoutParams.WRAP_CONTENT, ViewGroup.LayoutParams.WRAP_CONTENT,
                    Gravity.TOP | Gravity.RIGHT);
            clp.topMargin = dp(28);
            clp.rightMargin = dp(16);
            root.addView(close, clp);

            d.setContentView(root);
            d.show();
        } catch (Exception e) {
            toast("图片预览失败：" + e.getMessage());
        }
    }

    /** 音视频：用 MediaPlayer 内嵌播放（不依赖第三方库 / FileProvider） */
    private void showPlayerDialog(final File file, boolean video, String name) {
        try {
            final Dialog d = new Dialog(this);
            LinearLayout box = new LinearLayout(this);
            box.setOrientation(LinearLayout.VERTICAL);
            box.setPadding(dp(12), dp(10), dp(12), dp(10));

            TextView title = label(name, 14);
            title.setTypeface(null, Typeface.BOLD);
            box.addView(title);

            final TextView status = label("正在准备播放…", 12);
            status.setTextColor(COLOR_SUB);
            final MediaPlayer mp = new MediaPlayer();
            mp.setOnPreparedListener(new MediaPlayer.OnPreparedListener() {
                @Override
                public void onPrepared(MediaPlayer player) {
                    try {
                        player.start();
                        status.setText("正在播放…");
                    } catch (Exception e) {
                        status.setText("播放失败：" + e.getMessage());
                    }
                }
            });
            mp.setOnErrorListener(new MediaPlayer.OnErrorListener() {
                @Override
                public boolean onError(MediaPlayer player, int what, int extra) {
                    status.setText("播放失败（格式可能不受支持）");
                    return true;
                }
            });

            if (video) {
                final SurfaceView sv = new SurfaceView(this);
                box.addView(sv, new LinearLayout.LayoutParams(
                        ViewGroup.LayoutParams.MATCH_PARENT, dp(220)));
                box.addView(status);
                sv.getHolder().addCallback(new SurfaceHolder.Callback() {
                    @Override
                    public void surfaceCreated(SurfaceHolder holder) {
                        try {
                            mp.setDataSource(file.getAbsolutePath());
                            mp.setDisplay(holder);
                            mp.prepareAsync();
                        } catch (Exception e) {
                            status.setText("播放失败：" + e.getMessage());
                        }
                    }

                    @Override
                    public void surfaceChanged(SurfaceHolder holder, int format, int width, int height) {
                    }

                    @Override
                    public void surfaceDestroyed(SurfaceHolder holder) {
                    }
                });
            } else {
                box.addView(status);
                try {
                    mp.setDataSource(file.getAbsolutePath());
                    mp.prepareAsync();
                } catch (Exception e) {
                    status.setText("播放失败：" + e.getMessage());
                }
            }

            TextView close = smallButton("关闭播放");
            close.setTag(d);
            close.setOnClickListener(this);
            LinearLayout.LayoutParams clp = new LinearLayout.LayoutParams(
                    ViewGroup.LayoutParams.WRAP_CONTENT, ViewGroup.LayoutParams.WRAP_CONTENT);
            clp.topMargin = dp(8);
            box.addView(close, clp);

            d.setOnDismissListener(new DialogInterface.OnDismissListener() {
                @Override
                public void onDismiss(DialogInterface dialog) {
                    try {
                        mp.release();
                    } catch (Exception ignored) {
                    }
                }
            });
            d.setContentView(box);
            d.show();
        } catch (Exception e) {
            toast("无法播放：" + e.getMessage());
        }
    }

    // ==================== 手机麦克风 ====================

    private void toggleMic() {
        if (micSender != null && micSender.isRecording()) {
            btnMic.setEnabled(false);
            btnMic.setText("正在停止…");
            tvMicStatus.setText("正在关闭麦克风…");
            tvMicStatus.setTextColor(COLOR_WARN);
            micSender.stop();
            return;
        }
        if (!LinkService.isConnected()) {
            toast("请先连接电脑，麦克风需要与电脑保持连接");
            return;
        }
        if (checkSelfPermission(Manifest.permission.RECORD_AUDIO)
                != PackageManager.PERMISSION_GRANTED) {
            requestPermissions(new String[]{Manifest.permission.RECORD_AUDIO}, REQ_RECORD_AUDIO);
            return;
        }
        startMic();
    }

    private void startMic() {
        if (btnMic == null) {
            return;
        }
        btnMic.setEnabled(true);
        btnMic.setText("⏹ 停止麦克风");
        tvMicStatus.setText("正在开启麦克风…");
        tvMicStatus.setTextColor(COLOR_WARN);
        tvMicStat.setText("已传输 0.0 秒 · 0 B");

        micSender = new MicSender();
        micSender.start(this, new MicSender.Listener() {
            @Override
            public void onReady() {
                runOnUiThread(new Runnable() {
                    @Override
                    public void run() {
                        tvMicStatus.setText("麦克风已开启 · 说话即可");
                        tvMicStatus.setTextColor(COLOR_OK_GREEN);
                        AppLog.add("手机麦克风已开始采集并发送");
                    }
                });
            }

            @Override
            public void onProgress(final long millis, final long bytes) {
                runOnUiThread(new Runnable() {
                    @Override
                    public void run() {
                        tvMicStat.setText("已传输 "
                                + String.format(Locale.US, "%.1f", millis / 1000.0)
                                + " 秒 · " + formatSize(bytes));
                    }
                });
            }

            @Override
            public void onStopped(final String message) {
                runOnUiThread(new Runnable() {
                    @Override
                    public void run() {
                        resetMicUi("麦克风已停止"
                                + (message == null || message.isEmpty() ? "" : "，" + message));
                        AppLog.add("手机麦克风已停止");
                    }
                });
            }

            @Override
            public void onError(final String message) {
                runOnUiThread(new Runnable() {
                    @Override
                    public void run() {
                        resetMicUi("录音失败");
                        toast(message);
                        AppLog.add("麦克风错误：" + message);
                    }
                });
            }
        });
    }

    private void resetMicUi(String status) {
        if (btnMic == null) {
            return;
        }
        tvMicStatus.setText(status);
        tvMicStatus.setTextColor(COLOR_SUB);
        btnMic.setEnabled(true);
        btnMic.setText("🎤 开启手机麦克风");
    }

    private void downloadFile(final String name) {
        final String ip = Prefs.getIp(this);
        final int port = Prefs.getPort(this);
        AppLog.add("开始下载：" + name);
        runBg(new Runnable() {
            @Override
            public void run() {
                try {
                    final String where = HttpApi.download(MainActivity.this, ip, port, name);
                    runOnUiThread(new Runnable() {
                        @Override
                        public void run() {
                            AppLog.add("下载完成：" + where);
                            toast("已保存到 " + where);
                        }
                    });
                } catch (final Exception e) {
                    runOnUiThread(new Runnable() {
                        @Override
                        public void run() {
                            AppLog.add("下载失败：" + e.getMessage());
                            toast("下载失败");
                        }
                    });
                }
            }
        });
    }

    // ==================== 文字互传 ====================

    /** 点击「发送」：只在已连接时发；WS 失败才允许 HTTP 回退 */
    private void sendTextNow() {
        if (sendInFlight) {
            toast("正在发送，请稍候");
            return;
        }
        if (etInput == null) {
            return;
        }
        String text = etInput.getText().toString().trim();
        if (text.isEmpty()) {
            toast("请输入消息内容");
            return;
        }
        if (LinkService.sendText(text)) {
            etInput.setText("");
            addLocalMessage(text);
            toast("已发送到电脑");
            // 稍后拉一次，让服务端数据替换本地乐观气泡
            schedulePoll(800);
            return;
        }
        if (!LinkService.isConnected()) {
            // 断开状态下禁止任何发送（包括 HTTP 回退）
            toast("未连接电脑，请先连接");
            return;
        }
        // 已连接但 WS 发送失败：走 HTTP 回退
        String ip = Prefs.getIp(this);
        if (ip == null || ip.isEmpty()) {
            toast("未连接电脑，请先连接");
            return;
        }
        sendInFlight = true;
        sendingText = text;
        sendOk = false;
        sendError = "";
        new Thread(new NetTask(this, NetTask.SEND_TEXT, text), "cmlink-send-text").start();
    }

    /** NetTask 在后台线程调用：HTTP 回退发送 */
    void sendTextInBg(String text) {
        String ip = Prefs.getIp(this);
        int port = Prefs.getPort(this);
        String token = Prefs.getToken(this);
        try {
            sendOk = HttpApi.sendText(ip, port, token, text);
            if (!sendOk) {
                sendError = "电脑端未确认收到";
            }
        } catch (Exception e) {
            sendOk = false;
            String m = e.getMessage();
            sendError = (m == null || m.isEmpty()) ? "网络错误" : m;
        }
        Handler h = pollHandler;
        if (h != null) {
            h.sendEmptyMessage(MSG_UI_SEND);
        }
    }

    private void applySendResult() {
        sendInFlight = false;
        if (sendOk) {
            if (etInput != null && sendingText.equals(etInput.getText().toString().trim())) {
                etInput.setText("");
            }
            addLocalMessage(sendingText);
            toast("已发送到电脑");
            AppLog.add("文字已通过 HTTP 发送");
            schedulePoll(800);
        } else {
            toast("发送失败" + (sendError.isEmpty() ? "" : "：" + sendError));
            AppLog.add("文字发送失败：" + sendError);
        }
    }

    /** 发送成功后立刻在右侧插入一条本地气泡（下一次轮询用服务端数据替换） */
    private void addLocalMessage(String text) {
        if (text == null || text.isEmpty()) {
            return;
        }
        HttpApi.MsgItem m = new HttpApi.MsgItem();
        m.kind = "text";
        m.dir = "phone->pc";
        m.device = Prefs.getDeviceName(this);
        m.text = text;
        m.time = new java.text.SimpleDateFormat("HH:mm:ss", Locale.US)
                .format(new java.util.Date());
        m.ts = System.currentTimeMillis() / 1000.0;
        localPending.add(m);
        renderMessages();
    }

    // ==================== 全屏编辑 ====================

    /** 全屏编辑：大号输入框 + 字数统计；确定后只写回输入框，不自动发送 */
    private void showFullEditor() {
        if (etInput == null) {
            return;
        }
        dialogMsg = null;
        String current = etInput.getText().toString();

        LinearLayout box = new LinearLayout(this);
        box.setOrientation(LinearLayout.VERTICAL);
        box.setPadding(dp(16), dp(6), dp(16), dp(4));

        etFullEdit = new EditText(this);
        etFullEdit.setText(current);
        etFullEdit.setSelection(current.length());
        etFullEdit.setTextSize(15);
        etFullEdit.setGravity(Gravity.TOP | Gravity.START);
        etFullEdit.setMinLines(10);
        etFullEdit.setVerticalScrollBarEnabled(true);
        etFullEdit.setInputType(InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_FLAG_MULTI_LINE);
        box.addView(etFullEdit, new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, dp(300)));

        tvFullCount = label("", 11);
        tvFullCount.setTextColor(COLOR_SUB);
        tvFullCount.setGravity(Gravity.END);
        tvFullCount.setText(current.length() + " 字");
        box.addView(tvFullCount, new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT));

        etFullEdit.addTextChangedListener(new android.text.TextWatcher() {
            @Override
            public void beforeTextChanged(CharSequence s, int start, int count, int after) {
            }

            @Override
            public void onTextChanged(CharSequence s, int start, int before, int count) {
            }

            @Override
            public void afterTextChanged(android.text.Editable s) {
                if (tvFullCount != null) {
                    int n = s == null ? 0 : s.length();
                    tvFullCount.setText(n + " 字");
                }
            }
        });

        try {
            new AlertDialog.Builder(this)
                    .setTitle("编辑文字")
                    .setView(box)
                    .setPositiveButton("确定", this)
                    .setNegativeButton("取消", this)
                    .show();
        } catch (Exception e) {
            etFullEdit = null;
            tvFullCount = null;
            toast("无法打开编辑框");
        }
    }

    /** 被电脑踢下线：显示「重新连接」入口并提示 */
    private void onKicked(String msg) {
        stopPolling();
        if (btnReconnect != null) {
            btnReconnect.setVisibility(View.VISIBLE);
        }
        if (tvStatus != null) {
            tvStatus.setText("状态：已被电脑断开");
            tvStatus.setTextColor(COLOR_ERR);
        }
        toast("已被电脑断开" + (msg == null || msg.isEmpty() ? "" : "：" + msg));
    }

    // ==================== 新内容轮询 / 提示 ====================

    private void loadSeenState() {
        seenMsgs.addAll(Prefs.getSeenMessages(this));
        msgsSeenReady = Prefs.isMessagesSeenReady(this);
    }

    private void saveSeenMsgs() {
        trimSeen(seenMsgs, 400);
        Prefs.setSeenMessages(this, seenMsgs);
    }

    /** 集合只保留最近 max 条，避免 SharedPreferences 无限膨胀 */
    private static void trimSeen(Set<String> set, int max) {
        while (set.size() > max) {
            java.util.Iterator<String> it = set.iterator();
            if (!it.hasNext()) {
                return;
            }
            it.remove();
        }
    }

    /** 新内容指纹：kind + dir + ts + 内容；服务端 ts 稳定，重复内容也能区分 */
    private static String msgFingerprint(HttpApi.MsgItem m) {
        if (m == null) {
            return "";
        }
        StringBuilder sb = new StringBuilder();
        sb.append(m.kind).append('|').append(m.dir).append('|')
                .append(m.ts).append('|');
        if ("file".equals(m.kind)) {
            sb.append(m.name);
        } else {
            sb.append(m.text);
        }
        return sb.toString();
    }

    private void schedulePoll(long delay) {
        if (!polling || pollHandler == null) {
            return;
        }
        pollHandler.removeMessages(MSG_POLL);
        // 只有真正连着电脑才允许轮询；断开 / 被踢 / 服务停止后不再请求 /api/messages
        if (!LinkService.isConnected()) {
            return;
        }
        pollHandler.sendEmptyMessageDelayed(MSG_POLL, delay);
    }

    /** 取消已排队的轮询（断开、被踢、onDestroy 时调用） */
    private void stopPolling() {
        pollQueued = false;
        if (pollHandler != null) {
            pollHandler.removeMessages(MSG_POLL);
        }
    }

    private void startPoll() {
        if (!polling) {
            return;
        }
        if (!LinkService.isConnected()) {
            stopPolling();
            return;
        }
        String ip = Prefs.getIp(this);
        if (ip == null || ip.isEmpty()) {
            stopPolling();
            return;
        }
        if (pollInFlight) {
            // 正在拉取：等这一轮结束后立刻再拉一次（WS 实时消息用）
            pollQueued = true;
            return;
        }
        pollInFlight = true;
        new Thread(new NetTask(this, NetTask.POLL, null), "cmlink-poll").start();
    }

    /** NetTask 在后台线程调用：拉统一会话流 */
    void pollInBg() {
        String ip = Prefs.getIp(this);
        int port = Prefs.getPort(this);
        pendingMsgsOk = false;
        pendingMsgs = null;
        if (ip == null || ip.isEmpty()) {
            postUiPoll();
            return;
        }
        try {
            pendingMsgs = HttpApi.listMessages(ip, port);
            pendingMsgsOk = true;
        } catch (Exception ignored) {
            // 电脑端暂时不可用：静默失败，下一轮再试
        }
        postUiPoll();
    }

    private void postUiPoll() {
        Handler h = pollHandler;
        if (h != null) {
            h.sendEmptyMessage(MSG_UI_POLL);
        }
    }

    private void applyPollResult() {
        if (!LinkService.isConnected()) {
            // 断开后到达的旧结果直接丢弃，避免界面继续出现新消息
            pendingMsgs = null;
            pendingMsgsOk = false;
            return;
        }
        List<HttpApi.MsgItem> list = pendingMsgs;
        if (pendingMsgsOk && list != null) {
            // 1) 乐观插入去重：服务端回传的同内容消息替换本地气泡
            boolean[] used = new boolean[list.size()];
            boolean pendingChanged = false;
            for (int i = localPending.size() - 1; i >= 0; i--) {
                HttpApi.MsgItem local = localPending.get(i);
                int best = -1;
                double bestDiff = 0;
                for (int j = 0; j < list.size(); j++) {
                    if (used[j]) {
                        continue;
                    }
                    HttpApi.MsgItem s = list.get(j);
                    if (!"text".equals(s.kind) || !"phone->pc".equals(s.dir)) {
                        continue;
                    }
                    // 服务端返回的设备名必须与本机一致（旧版无 device 时容忍为空）
                    if (s.device != null && !s.device.isEmpty()
                            && !s.device.equals(local.device)) {
                        continue;
                    }
                    if (!s.text.equals(local.text)) {
                        continue;
                    }
                    double diff = Math.abs(s.ts - local.ts);
                    // 同一段文字 + 发送方向 + 10 分钟内，视为服务端已确认这条乐观消息
                    if (diff <= 600 && (best < 0 || diff < bestDiff)) {
                        best = j;
                        bestDiff = diff;
                    }
                }
                if (best >= 0) {
                    used[best] = true;
                    localPending.remove(i);
                    pendingChanged = true;
                }
            }

            // 2) 新内容判断：电脑发来的 + 其它手机发来的；自己发的不算
            int newFiles = 0;
            int newTexts = 0;
            String newFileDevice = "";
            if (!msgsSeenReady) {
                // 首次启动：现有内容全部记为已见，不提示
                for (HttpApi.MsgItem m : list) {
                    if (isIncoming(m)) {
                        seenMsgs.add(msgFingerprint(m));
                    }
                }
                msgsSeenReady = true;
                Prefs.setMessagesSeenReady(this, true);
                saveSeenMsgs();
            } else {
                for (HttpApi.MsgItem m : list) {
                    if (!isIncoming(m)) {
                        continue;
                    }
                    if (seenMsgs.add(msgFingerprint(m))) {
                        if ("file".equals(m.kind)) {
                            newFiles++;
                            newFileDevice = m.device == null ? "" : m.device;
                        } else {
                            newTexts++;
                        }
                    }
                }
                if (newFiles + newTexts > 0) {
                    saveSeenMsgs();
                }
            }

            // 3) 更新会话列表
            boolean changed = !sameMsgs(messages, list);
            messages.clear();
            messages.addAll(list);
            if (changed || pendingChanged) {
                renderMessages();
            }

            if (newFiles > 0 || newTexts > 0) {
                showNewBanner(newFiles, newTexts);
                notifyNewContent(newFiles, newTexts, newFileDevice);
            }
        }
        pendingMsgs = null;
    }

    private static boolean sameMsgs(List<HttpApi.MsgItem> a, List<HttpApi.MsgItem> b) {
        if (a.size() != b.size()) {
            return false;
        }
        for (int i = 0; i < a.size(); i++) {
            HttpApi.MsgItem x = a.get(i);
            HttpApi.MsgItem y = b.get(i);
            if (!x.kind.equals(y.kind) || !x.dir.equals(y.dir) || x.ts != y.ts
                    || !x.device.equals(y.device)
                    || !x.text.equals(y.text) || !x.name.equals(y.name)
                    || x.size != y.size) {
                return false;
            }
        }
        return true;
    }

    private void showNewBanner(int files, int texts) {
        if (tvNewBanner == null) {
            return;
        }
        bannerFiles += files;
        bannerTexts += texts;
        tvNewBanner.setText(bannerText(bannerFiles, bannerTexts) + "，点击查看");
        tvNewBanner.setVisibility(View.VISIBLE);
    }

    private static String bannerText(int files, int texts) {
        if (files > 0 && texts > 0) {
            return "收到 " + files + " 个新文件，" + texts + " 条新文字";
        }
        if (files > 0) {
            return "收到 " + files + " 个新文件";
        }
        return "收到 " + texts + " 条新文字";
    }

    /** 点提示条 / 通知：清掉提示并定位到会话列表底部 */
    private void openConversationFromBanner() {
        bannerFiles = 0;
        bannerTexts = 0;
        if (tvNewBanner != null) {
            tvNewBanner.setVisibility(View.GONE);
        }
        if (page != null && convCard != null) {
            page.smoothScrollTo(0, convCard.getTop());
        }
        scrollMessagesToBottom();
    }

    /** 通知点击进入会话（从 onCreate / onNewIntent 读取） */
    private void handleLaunchIntent(Intent intent) {
        if (intent == null) {
            return;
        }
        try {
            if (intent.getBooleanExtra(EXTRA_OPEN_FILES, false)) {
                intent.removeExtra(EXTRA_OPEN_FILES);
                openConversationFromBanner();
            }
        } catch (Exception ignored) {
        }
    }

    private void ensureMsgChannel() {
        if (Build.VERSION.SDK_INT < 26) {
            return;
        }
        try {
            NotificationManager nm =
                    (NotificationManager) getSystemService(Context.NOTIFICATION_SERVICE);
            if (nm == null) {
                return;
            }
            NotificationChannel ch = new NotificationChannel(MSG_CHANNEL_ID,
                    "新内容提醒", NotificationManager.IMPORTANCE_DEFAULT);
            ch.setDescription("电脑传来新文件或文字时提醒");
            nm.createNotificationChannel(ch);
        } catch (Exception ignored) {
        }
    }

    /** 通知正文：文件优先按发送方设备名描述 */
    private static String notifyText(int files, int texts, String device) {
        if (files > 0) {
            String who = (device == null || device.isEmpty()) ? "电脑" : device;
            String s = who + " 发来" + (files > 1 ? " " + files + " 个文件" : "文件");
            if (texts > 0) {
                s += "，另有 " + texts + " 条新文字";
            }
            return s;
        }
        return "收到 " + texts + " 条新文字";
    }

    private void notifyNewContent(int files, int texts, String fileDevice) {
        try {
            NotificationManager nm =
                    (NotificationManager) getSystemService(Context.NOTIFICATION_SERVICE);
            if (nm == null) {
                return;
            }
            Intent launch = new Intent(this, MainActivity.class);
            launch.putExtra(EXTRA_OPEN_FILES, true);
            launch.setFlags(Intent.FLAG_ACTIVITY_CLEAR_TOP | Intent.FLAG_ACTIVITY_SINGLE_TOP);
            int flags = PendingIntent.FLAG_UPDATE_CURRENT;
            if (Build.VERSION.SDK_INT >= 23) {
                flags |= PendingIntent.FLAG_IMMUTABLE;
            }
            PendingIntent pi = PendingIntent.getActivity(this, 1, launch, flags);

            Notification.Builder b;
            if (Build.VERSION.SDK_INT >= 26) {
                b = new Notification.Builder(this, MSG_CHANNEL_ID);
            } else {
                b = new Notification.Builder(this);
            }
            b.setSmallIcon(android.R.drawable.stat_sys_download_done)
                    .setContentTitle("手机互传")
                    .setContentText(notifyText(files, texts, fileDevice))
                    .setAutoCancel(true)
                    .setContentIntent(pi);
            nm.notify(NOTIF_MSG_ID, b.build());
        } catch (Exception ignored) {
        }
    }

    // ==================== 通知授权 / 权限 ====================

    private boolean hasNotificationAccess() {
        try {
            String flat = Settings.Secure.getString(getContentResolver(),
                    "enabled_notification_listeners");
            return flat != null && flat.contains(getPackageName());
        } catch (Exception e) {
            return false;
        }
    }

    private void openNotificationAccess() {
        try {
            startActivity(new Intent(Settings.ACTION_NOTIFICATION_LISTENER_SETTINGS));
        } catch (Exception e) {
            try {
                startActivity(new Intent(
                        "android.settings.ACTION_NOTIFICATION_LISTENER_SETTINGS"));
            } catch (Exception e2) {
                toast("无法打开系统设置页，请手动到「通知使用权」里开启");
            }
        }
    }

    private void requestNotificationPermission() {
        try {
            if (Build.VERSION.SDK_INT >= 33
                    && checkSelfPermission(android.Manifest.permission.POST_NOTIFICATIONS)
                    != PackageManager.PERMISSION_GRANTED) {
                requestPermissions(
                        new String[]{android.Manifest.permission.POST_NOTIFICATIONS},
                        REQ_NOTIF_PERM);
            }
        } catch (Exception ignored) {
        }
    }

    // ==================== 杂项 ====================

    private void runBg(final Runnable task) {
        Thread t = new Thread(task, "cmlink-ui-bg");
        t.setDaemon(true);
        t.start();
    }

    private void toast(String msg) {
        Toast.makeText(this, msg, Toast.LENGTH_SHORT).show();
    }

    private void dialog(String title, String message) {
        try {
            new AlertDialog.Builder(this)
                    .setTitle(title)
                    .setMessage(message)
                    .setPositiveButton("知道了", null)
                    .show();
        } catch (Exception ignored) {
        }
    }
}
