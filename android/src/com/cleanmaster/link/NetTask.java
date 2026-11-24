package com.cleanmaster.link;

/**
 * 界面发起的后台网络任务。
 *
 * 单独拎成顶层类，是为了避免在 MainActivity 里再加匿名 Runnable
 * （手工构建时 d8 走命令行传 class 文件列表，Windows 有 8191 字符上限）。
 */
final class NetTask implements Runnable {

    /** 轮询 /api/files 与 /api/texts */
    static final int POLL = 1;
    /** WebSocket 不可用时，走 HTTP /api/text 发送文字 */
    static final int SEND_TEXT = 2;

    private final MainActivity activity;
    private final int mode;
    private final String text;

    NetTask(MainActivity activity, int mode, String text) {
        this.activity = activity;
        this.mode = mode;
        this.text = text;
    }

    @Override
    public void run() {
        if (mode == POLL) {
            activity.pollInBg();
        } else if (mode == SEND_TEXT) {
            activity.sendTextInBg(text);
        }
    }
}
