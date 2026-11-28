package com.cleanmaster.link;

import android.content.Context;
import android.media.AudioFormat;
import android.media.AudioRecord;
import android.media.MediaRecorder;
import android.os.PowerManager;
import android.os.SystemClock;

import java.util.Arrays;

/**
 * 手机麦克风：用 AudioRecord 采集 16kHz / 单声道 / PCM 16bit，
 * 每 3200 字节（约 0.1 秒）作为一帧通过 WebSocket 二进制帧发到电脑。
 *
 * 流程：
 *   1. 发送 {"type":"mic_start",...}
 *   2. 等电脑回 mic_ack（ok:true 才真正开始采集）
 *   3. 子线程循环 read -> sendBinary
 *   4. 停止时先停采集，再发送 {"type":"mic_stop"}
 *
 * 线程模型：start() 立即返回，实际采集在 "cmlink-mic" 子线程里跑，
 * 不会阻塞 UI；所有回调都在子线程，界面需自行切回主线程。
 */
public class MicSender {

    /** 录音状态回调（均发生在子线程） */
    public interface Listener {
        /** 电脑端已确认，开始采集 */
        void onReady();

        /** 采集进度：已录毫秒数 / 已发送字节数 */
        void onProgress(long millis, long bytes);

        /** 正常停止 */
        void onStopped(String message);

        /** 启动或运行失败 */
        void onError(String message);
    }

    private static final int RATE = 16000;
    private static final int CHANNELS = 1;
    private static final int BITS = 16;
    /**
     * 16kHz * 单声道 * 2 字节 * 0.04 秒 = 1280 字节。
     * 用 40ms 而不是 100ms：当麦克风用时延迟越低越像"实时"，
     * 40ms 在 Wi-Fi 上只有 25 帧/秒，开销可以忽略。
     */
    private static final int CHUNK = 1280;

    private volatile boolean recording = false;
    private volatile boolean stopRequested = false;
    private volatile boolean failed = false;
    private volatile long sentBytes = 0;
    private volatile Listener listener;
    private PowerManager.WakeLock wakeLock;

    public boolean isRecording() {
        return recording;
    }

    public void start(Context context, Listener l) {
        if (recording) {
            return;
        }
        this.listener = l;
        this.recording = true;
        this.stopRequested = false;
        this.failed = false;
        this.sentBytes = 0;
        acquireWakeLock(context);
        Thread t = new Thread(new Runnable() {
            @Override
            public void run() {
                runSession();
            }
        }, "cmlink-mic");
        t.setDaemon(true);
        t.start();
    }

    /**
     * 息屏后系统会让 CPU 休眠，AudioRecord 的读取线程拿不到时间片，
     * 表现就是"手机一锁屏麦克风就停"。这里持一个 CPU 唤醒锁，跑完再释放。
     */
    private void acquireWakeLock(Context context) {
        try {
            if (context == null || wakeLock != null) {
                return;
            }
            PowerManager pm = (PowerManager) context.getApplicationContext()
                    .getSystemService(Context.POWER_SERVICE);
            if (pm == null) {
                return;
            }
            wakeLock = pm.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "cmlink:mic");
            wakeLock.setReferenceCounted(false);
            wakeLock.acquire();
        } catch (Exception ignored) {
        }
    }

    private void releaseWakeLock() {
        try {
            if (wakeLock != null && wakeLock.isHeld()) {
                wakeLock.release();
            }
        } catch (Exception ignored) {
        }
        wakeLock = null;
    }

    /** 请求停止：采集循环会在下一个 read 之后退出 */
    public void stop() {
        stopRequested = true;
    }

    private void runSession() {
        AudioRecord rec = null;
        long startMs = 0;
        boolean handshaked = false;
        boolean readyNotified = false;
        try {
            int minBuf = AudioRecord.getMinBufferSize(
                    RATE, AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT);
            if (minBuf <= 0) {
                fail("设备不支持 16kHz 单声道录音");
                return;
            }
            int bufSize = Math.max(minBuf * 2, CHUNK * 4);

            try {
                rec = new AudioRecord(MediaRecorder.AudioSource.MIC, RATE,
                        AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT, bufSize);
            } catch (Exception e) {
                fail("创建录音器失败：" + e.getMessage());
                return;
            }
            if (rec.getState() != AudioRecord.STATE_INITIALIZED) {
                fail("麦克风不可用（可能被其它应用占用或被系统限制）");
                return;
            }
            rec.startRecording();

            byte[] buf = new byte[CHUNK];
            long lastReport = 0;
            // 注意：连接断开时不要退出线程。锁屏 / Wi-Fi 抖动会导致 WS 断开，
            // 只要用户没主动停止，就保持录音并在重连后自动重新握手。
            while (!stopRequested) {
                if (!LinkService.isConnected()) {
                    handshaked = false;
                    // 读出来丢弃，防止 AudioRecord 内部缓冲堆积
                    int n = rec.read(buf, 0, buf.length);
                    if (n < 0) {
                        fail("录音读取失败（" + n + "）");
                        return;
                    }
                    sleepQuiet(120);
                    continue;
                }

                if (!handshaked) {
                    if (!LinkService.startMicStream(RATE, CHANNELS, BITS)) {
                        sleepQuiet(500);
                        continue;
                    }
                    if (!LinkService.awaitMicAck(5000)) {
                        // 电脑端可能还占着上一段会话的麦克风，稍后重试
                        sleepQuiet(800);
                        continue;
                    }
                    handshaked = true;
                    if (!readyNotified) {
                        readyNotified = true;
                        startMs = SystemClock.elapsedRealtime();
                        ready();
                    }
                }

                int n = rec.read(buf, 0, buf.length);
                if (n < 0) {
                    fail("录音读取失败（" + n + "）");
                    return;
                }
                if (n == 0) {
                    continue;
                }
                byte[] chunk = (n == buf.length) ? buf : Arrays.copyOf(buf, n);
                if (!LinkService.sendMicBinary(chunk)) {
                    // 发送失败通常是连接刚断；不结束录音，等重连后重新握手
                    handshaked = false;
                    AppLog.add("麦克风发送中断，等待重连后自动恢复…");
                } else {
                    sentBytes += n;
                }
                long now = SystemClock.elapsedRealtime();
                if (now - lastReport >= 200) {
                    lastReport = now;
                    progress(now - startMs, sentBytes);
                }
            }
        } catch (Exception e) {
            fail("录音异常：" + e.getMessage());
        } finally {
            // 无论正常结束还是异常，都必须停止并释放 AudioRecord
            recording = false;
            try {
                if (rec != null && rec.getRecordingState() == AudioRecord.RECORDSTATE_RECORDING) {
                    rec.stop();
                }
            } catch (Exception ignored) {
            }
            try {
                if (rec != null) {
                    rec.release();
                }
            } catch (Exception ignored) {
            }
            releaseWakeLock();
            try {
                LinkService.stopMicStream();
            } catch (Exception ignored) {
            }
        }

        if (failed) {
            return;
        }
        if (startMs > 0) {
            progress(SystemClock.elapsedRealtime() - startMs, sentBytes);
        }
        Listener l = listener;
        if (l != null) {
            try {
                l.onStopped(LinkService.getMicMessage());
            } catch (Exception ignored) {
            }
        }
    }

    private static void sleepQuiet(long ms) {
        try {
            Thread.sleep(ms);
        } catch (InterruptedException ignored) {
        }
    }

    private void fail(String msg) {
        failed = true;
        stopRequested = true;
        recording = false;
        Listener l = listener;
        if (l != null) {
            try {
                l.onError(msg);
            } catch (Exception ignored) {
            }
        }
    }

    private void ready() {
        Listener l = listener;
        if (l != null) {
            try {
                l.onReady();
            } catch (Exception ignored) {
            }
        }
    }

    private void progress(long millis, long bytes) {
        Listener l = listener;
        if (l != null) {
            try {
                l.onProgress(millis, bytes);
            } catch (Exception ignored) {
            }
        }
    }
}
