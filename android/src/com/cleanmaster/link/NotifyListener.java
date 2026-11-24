package com.cleanmaster.link;

import android.app.Notification;
import android.content.pm.ApplicationInfo;
import android.content.pm.PackageManager;
import android.os.Bundle;
import android.service.notification.NotificationListenerService;
import android.service.notification.StatusBarNotification;

import java.util.HashMap;
import java.util.Map;

/**
 * 通知转发：继承系统的 NotificationListenerService。
 *
 * 用户在系统「通知使用权」里授权后，本服务会收到所有应用的通知，
 * 这里提取 应用名 / 标题 / 正文，经 WebSocket 发给电脑。
 *
 * 过滤规则：
 *   · 自己的通知不转发（避免把「手机互传」本身发出去）
 *   · 常驻通知（FLAG_ONGOING_EVENT）与媒体通知不转发
 *   · 分组汇总（group summary）不转发，否则同一条会重复两遍
 */
public class NotifyListener extends NotificationListenerService {

    /** 包名 -> 应用名 缓存，避免每条通知都查一次 PackageManager */
    private final Map<String, String> appNameCache = new HashMap<>();

    @Override
    public void onNotificationPosted(StatusBarNotification sbn) {
        try {
            if (sbn == null || !Prefs.isNotifyOn(this)) {
                return;
            }
            Notification n = sbn.getNotification();
            if (n == null) {
                return;
            }
            String pkg = sbn.getPackageName();
            if (pkg == null || pkg.isEmpty() || getPackageName().equals(pkg)) {
                return;
            }
            // 分组汇总会把它包含的子通知再重复一次，直接丢弃
            if ((n.flags & Notification.FLAG_GROUP_SUMMARY) != 0) {
                return;
            }
            // 常驻通知（下载进度、音乐播放器等）没转发价值
            if ((n.flags & Notification.FLAG_ONGOING_EVENT) != 0) {
                return;
            }
            // 媒体通知：系统用 CATEGORY_TRANSPORT 或 extras 里的 mediaSession 标记
            if (Notification.CATEGORY_TRANSPORT.equals(n.category)) {
                return;
            }
            Bundle extras = n.extras;
            if (extras == null) {
                return;
            }
            if (extras.containsKey("android.mediaSession")) {
                return;
            }

            String title = charSeq(extras.getCharSequence(Notification.EXTRA_TITLE));
            String text = charSeq(extras.getCharSequence(Notification.EXTRA_TEXT));
            if (text.isEmpty()) {
                CharSequence[] lines =
                        extras.getCharSequenceArray(Notification.EXTRA_TEXT_LINES);
                if (lines != null && lines.length > 0) {
                    text = charSeq(lines[0]);
                }
            }
            if (title.isEmpty() && text.isEmpty()) {
                return;
            }

            LinkService.sendNotificationEvent(appName(pkg), title, text);
        } catch (Exception ignored) {
            // 任何异常都不能让监听服务崩溃，否则会被系统停用
        }
    }

    private static String charSeq(CharSequence cs) {
        return cs == null ? "" : cs.toString();
    }

    /** 包名映射成用户看得懂的应用名 */
    private String appName(String pkg) {
        String cached = appNameCache.get(pkg);
        if (cached != null) {
            return cached;
        }
        String name = pkg;
        try {
            PackageManager pm = getPackageManager();
            ApplicationInfo info = pm.getApplicationInfo(pkg, 0);
            CharSequence label = pm.getApplicationLabel(info);
            if (label != null && label.length() > 0) {
                name = label.toString();
            }
        } catch (Exception ignored) {
        }
        appNameCache.put(pkg, name);
        return name;
    }
}
