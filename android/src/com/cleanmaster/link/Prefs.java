package com.cleanmaster.link;

import android.content.Context;
import android.content.SharedPreferences;
import android.os.Build;

import java.util.Collections;
import java.util.LinkedHashSet;
import java.util.Set;
import java.util.UUID;

/**
 * 设置存储：连接信息、设备名、两个同步开关。
 * 全部落在 SharedPreferences 里，卸载前一直有效。
 */
public final class Prefs {

    private static final String FILE = "cmlink";

    private Prefs() {
    }

    private static SharedPreferences sp(Context c) {
        return c.getApplicationContext().getSharedPreferences(FILE, Context.MODE_PRIVATE);
    }

    public static String getIp(Context c) {
        return sp(c).getString("ip", "");
    }

    public static void setIp(Context c, String ip) {
        sp(c).edit().putString("ip", ip == null ? "" : ip).apply();
    }

    public static int getPort(Context c) {
        return sp(c).getInt("port", 8080);
    }

    public static void setPort(Context c, int port) {
        sp(c).edit().putInt("port", port <= 0 ? 8080 : port).apply();
    }

    public static String getToken(Context c) {
        return sp(c).getString("token", "");
    }

    public static void setToken(Context c, String token) {
        sp(c).edit().putString("token", token == null ? "" : token).apply();
    }

    /** 电脑名称（用于界面展示） */
    public static String getPcName(Context c) {
        return sp(c).getString("pc_name", "");
    }

    public static void setPcName(Context c, String name) {
        sp(c).edit().putString("pc_name", name == null ? "" : name).apply();
    }

    /** 电脑端是否要求配对（来自 UDP OFFER 的 needPair） */
    public static boolean isNeedPair(Context c) {
        return sp(c).getBoolean("need_pair", true);
    }

    public static void setNeedPair(Context c, boolean need) {
        sp(c).edit().putBoolean("need_pair", need).apply();
    }

    /** 手机设备名，首次使用时用 厂商 + 型号 自动生成 */
    public static String getDeviceName(Context c) {
        String name = sp(c).getString("device", "");
        if (name == null || name.isEmpty()) {
            name = (Build.MANUFACTURER == null ? "" : Build.MANUFACTURER)
                    + " " + (Build.MODEL == null ? "" : Build.MODEL);
            name = name.trim();
            if (name.isEmpty()) {
                name = "Android";
            }
            sp(c).edit().putString("device", name).apply();
        }
        return name;
    }

    public static void setDeviceName(Context c, String name) {
        sp(c).edit().putString("device", name == null ? "" : name).apply();
    }

    /** 手机唯一 id（UDP ANNOUNCE / CONNECT_REQ 用），首次使用时生成并持久化 */
    public static String getDeviceId(Context c) {
        String id = sp(c).getString("device_id", "");
        if (id == null || id.isEmpty()) {
            id = UUID.randomUUID().toString().replace("-", "");
            sp(c).edit().putString("device_id", id).apply();
        }
        return id;
    }

    public static boolean isClipboardOn(Context c) {
        return sp(c).getBoolean("clipboard", true);
    }

    public static void setClipboardOn(Context c, boolean on) {
        sp(c).edit().putBoolean("clipboard", on).apply();
    }

    public static boolean isNotifyOn(Context c) {
        return sp(c).getBoolean("notify", false);
    }

    public static void setNotifyOn(Context c, boolean on) {
        sp(c).edit().putBoolean("notify", on).apply();
    }

    // ==================== 新内容提示（已见过的会话消息） ====================

    public static Set<String> getSeenMessages(Context c) {
        return new LinkedHashSet<>(sp(c).getStringSet(
                "seen_msgs", Collections.<String>emptySet()));
    }

    public static void setSeenMessages(Context c, Set<String> fingerprints) {
        sp(c).edit().putStringSet("seen_msgs", new LinkedHashSet<>(fingerprints)).apply();
    }

    /** 首次成功拉到会话列表后置位：之后新增的「电脑发来」内容才算新内容 */
    public static boolean isMessagesSeenReady(Context c) {
        return sp(c).getBoolean("seen_msgs_ready", false);
    }

    public static void setMessagesSeenReady(Context c, boolean ready) {
        sp(c).edit().putBoolean("seen_msgs_ready", ready).apply();
    }
}
