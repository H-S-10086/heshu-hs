package com.cleanmaster.link;

import android.content.Context;
import android.content.Intent;

import java.text.SimpleDateFormat;
import java.util.ArrayDeque;
import java.util.ArrayList;
import java.util.Date;
import java.util.List;
import java.util.Locale;

/**
 * 全局事件日志：
 *   · 内存里保留最近 50 条，界面可随时取全量快照
 *   · 每次新增用广播通知前台界面刷新（App 内广播，setPackage 限定本应用）
 *
 * 之所以不用回调，是因为日志的来源很杂（Service 线程、通知监听、UI 线程），
 * 广播能让这些模块彼此解耦。
 */
public final class AppLog {

    public static final String ACTION_LOG = "com.cleanmaster.link.action.LOG";

    private static final int MAX_LINES = 50;
    private static final ArrayDeque<String> LINES = new ArrayDeque<>();
    private static volatile Context appContext;

    private AppLog() {
    }

    public static void init(Context c) {
        if (c != null) {
            appContext = c.getApplicationContext();
        }
    }

    /** 追加一条日志，任意线程可调用 */
    public static void add(String msg) {
        String time = new SimpleDateFormat("HH:mm:ss", Locale.US).format(new Date());
        String line = time + "  " + (msg == null ? "" : msg);

        synchronized (LINES) {
            LINES.addLast(line);
            while (LINES.size() > MAX_LINES) {
                LINES.removeFirst();
            }
        }

        Context ctx = appContext;
        if (ctx != null) {
            try {
                Intent i = new Intent(ACTION_LOG);
                i.setPackage(ctx.getPackageName());   // 只发给本 App，避免广播外泄
                ctx.sendBroadcast(i);
            } catch (Exception ignored) {
                // 广播失败不影响日志本身
            }
        }
    }

    /** 按时间顺序返回日志快照（旧的在前） */
    public static List<String> snapshot() {
        synchronized (LINES) {
            return new ArrayList<>(LINES);
        }
    }

    public static void clear() {
        synchronized (LINES) {
            LINES.clear();
        }
    }

    /**
     * 把可能很长的文本压成一行，便于日志展示。
     * 只保留首尾各 8 个字符，避免剪贴板 / 通知正文 / 验证码整段留在日志里。
     */
    public static String preview(String text) {
        if (text == null) {
            return "";
        }
        String s = text.replace("\r", " ").replace("\n", " ").trim();
        if (s.length() <= 16) {
            return s;
        }
        return s.substring(0, 8) + "…" + s.substring(s.length() - 8);
    }
}
