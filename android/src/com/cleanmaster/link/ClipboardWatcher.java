package com.cleanmaster.link;

import android.content.ClipData;
import android.content.ClipboardManager;
import android.content.Context;

/**
 * 剪贴板监听。
 *
 * 重要限制（如实说明）：
 *   Android 10（API 29）起，系统禁止后台应用读取剪贴板。
 *   只有应用处于前台（拥有窗口焦点）时 addPrimaryClipChangedListener
 *   才能拿到内容，所以本类只在 Activity 的 onResume / onPause 之间挂载。
 *   App 退到后台时剪贴板同步会暂停 —— 这是系统限制，不是实现缺陷。
 *
 * 防回环：
 *   记录「最后一次由电脑写入手机剪贴板」的文本 lastRemote，
 *   监听到变化时若内容与它相同就不回传，避免 手机↔电脑 无限往返。
 */
public class ClipboardWatcher {

    public interface Listener {
        void onLocalClipboard(String text);
    }

    /** 最后一次由电脑写入的内容（静态，Service 写入 / Activity 监听到时共享） */
    private static volatile String lastRemote = "";

    private final Context appContext;
    private final ClipboardManager clipboard;
    private final Listener listener;
    private boolean attached;
    private String lastSent = "";

    private final ClipboardManager.OnPrimaryClipChangedListener callback =
            new ClipboardManager.OnPrimaryClipChangedListener() {
                @Override
                public void onPrimaryClipChanged() {
                    handleChange();
                }
            };

    public ClipboardWatcher(Context context, Listener listener) {
        this.appContext = context.getApplicationContext();
        this.clipboard = (ClipboardManager) this.appContext.getSystemService(Context.CLIPBOARD_SERVICE);
        this.listener = listener;
    }

    public synchronized void attach() {
        if (attached || clipboard == null) {
            return;
        }
        try {
            clipboard.addPrimaryClipChangedListener(callback);
            attached = true;
        } catch (Exception ignored) {
        }
    }

    public synchronized void detach() {
        if (!attached || clipboard == null) {
            return;
        }
        try {
            clipboard.removePrimaryClipChangedListener(callback);
        } catch (Exception ignored) {
        }
        attached = false;
    }

    private void handleChange() {
        try {
            ClipData clip = clipboard.getPrimaryClip();
            if (clip == null || clip.getItemCount() == 0) {
                return;
            }
            CharSequence cs = clip.getItemAt(0).coerceToText(appContext);
            if (cs == null) {
                return;
            }
            String text = cs.toString();
            if (text == null || text.isEmpty()) {
                return;
            }
            // 防回环：电脑刚写进来的内容 / 上次已经发过的内容，都不再回传
            if (text.equals(lastRemote) || text.equals(lastSent)) {
                return;
            }
            lastSent = text;
            if (listener != null) {
                listener.onLocalClipboard(text);
            }
        } catch (Exception ignored) {
            // 部分 ROM 在无焦点时会抛 SecurityException，直接忽略
        }
    }

    /**
     * 把电脑发来的文本写入手机剪贴板，并登记为「远程内容」用于防回环。
     * 任意线程可调用。
     */
    public static void applyRemote(Context context, String text) {
        if (text == null) {
            return;
        }
        lastRemote = text;
        try {
            ClipboardManager cm = (ClipboardManager) context.getApplicationContext()
                    .getSystemService(Context.CLIPBOARD_SERVICE);
            if (cm != null) {
                cm.setPrimaryClip(ClipData.newPlainText("电脑剪贴板", text));
            }
        } catch (Exception ignored) {
            // Android 10+ 后台写剪贴板在部分机型上也会被拦截，尽力而为
        }
    }

    public static String getLastRemote() {
        return lastRemote;
    }
}
