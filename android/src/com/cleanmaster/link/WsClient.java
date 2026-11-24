package com.cleanmaster.link;

import android.util.Base64;

import java.io.BufferedInputStream;
import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.InetSocketAddress;
import java.net.Socket;
import java.net.SocketTimeoutException;
import java.security.MessageDigest;
import java.security.SecureRandom;

/**
 * 极简 WebSocket 客户端（RFC 6455），只用 JDK / Android 原生 API，不依赖 OkHttp。
 *
 * 支持：
 *   · 标准握手与 Sec-WebSocket-Accept 校验
 *   · 客户端发帧强制加掩码（协议要求），接收服务端无掩码帧
 *   · 125 / 126 / 127 三种长度编码、分片重组、ping / pong / close
 *
 * 线程模型：
 *   connect() 与 readLoop() 是阻塞调用，需要放在子线程；
 *   send() 可以被任意线程调用（内部加锁）。
 */
public class WsClient {

    private static final String WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11";
    private static final int MAX_FRAME = 8 * 1024 * 1024;   // 单帧上限 8MB，防内存被打爆

    private static final int OP_CONT = 0x0;
    private static final int OP_TEXT = 0x1;
    private static final int OP_BINARY = 0x2;
    private static final int OP_CLOSE = 0x8;
    private static final int OP_PING = 0x9;
    private static final int OP_PONG = 0xA;

    public interface Listener {
        void onOpen();

        void onMessage(String text);

        void onClosed(String reason);
    }

    private final String host;
    private final int port;
    private final String path;
    private final Listener listener;
    private final SecureRandom random = new SecureRandom();
    private final Object writeLock = new Object();

    private Socket socket;
    private InputStream in;
    private OutputStream out;
    private volatile boolean closed = false;

    public WsClient(String host, int port, String path, Listener listener) {
        this.host = host;
        this.port = port;
        this.path = (path == null || path.isEmpty()) ? "/ws" : path;
        this.listener = listener;
    }

    public boolean isClosed() {
        return closed;
    }

    /** 发起 TCP 连接并完成 WebSocket 握手，失败抛 IOException */
    public void connect() throws IOException {
        Socket s = new Socket();
        s.connect(new InetSocketAddress(host, port), 8000);
        s.setSoTimeout(8000);          // 握手阶段的读超时
        s.setTcpNoDelay(true);
        socket = s;
        in = new BufferedInputStream(s.getInputStream());
        out = s.getOutputStream();

        byte[] keyBytes = new byte[16];
        random.nextBytes(keyBytes);
        String key = Base64.encodeToString(keyBytes, Base64.NO_WRAP);

        String request = "GET " + path + " HTTP/1.1\r\n"
                + "Host: " + host + ":" + port + "\r\n"
                + "Upgrade: websocket\r\n"
                + "Connection: Upgrade\r\n"
                + "Sec-WebSocket-Key: " + key + "\r\n"
                + "Sec-WebSocket-Version: 13\r\n"
                + "\r\n";
        out.write(request.getBytes("UTF-8"));
        out.flush();

        String status = readHttpLine();
        if (status == null) {
            throw new IOException("握手无响应");
        }
        if (!status.contains("101")) {
            throw new IOException("握手失败: " + status);
        }

        String accept = null;
        String line;
        while ((line = readHttpLine()) != null && !line.isEmpty()) {
            int idx = line.indexOf(':');
            if (idx > 0) {
                String k = line.substring(0, idx).trim().toLowerCase();
                if ("sec-websocket-accept".equals(k)) {
                    accept = line.substring(idx + 1).trim();
                }
            }
        }
        String expect = computeAccept(key);
        if (accept == null || !accept.equals(expect)) {
            closeQuietly();
            throw new IOException("Sec-WebSocket-Accept 校验失败");
        }

        // 握手完成后进入长连接：75 秒读超时，配合 30 秒心跳可以及时发现假死
        s.setSoTimeout(75000);
        listener.onOpen();
    }

    /**
     * 帧读取循环，连接断开时返回并通过 onClosed 回调。
     * 阻塞方法，必须放在子线程（LinkService 的读线程）。
     */
    public void readLoop() {
        String reason = "连接结束";
        ByteArrayOutputStream fragment = null;
        int fragmentOpcode = 0;

        try {
            while (!closed) {
                int b0 = in.read();
                if (b0 < 0) {
                    reason = "服务端断开";
                    break;
                }
                int b1 = readByte();

                boolean fin = (b0 & 0x80) != 0;
                int opcode = b0 & 0x0F;
                boolean masked = (b1 & 0x80) != 0;
                long len = b1 & 0x7F;

                if (len == 126) {
                    len = ((long) readByte() << 8) | readByte();
                } else if (len == 127) {
                    len = 0;
                    for (int i = 0; i < 8; i++) {
                        len = (len << 8) | readByte();
                    }
                }
                if (len < 0 || len > MAX_FRAME) {
                    reason = "帧过大";
                    break;
                }

                byte[] mask = null;
                if (masked) {                 // 服务端发的帧按协议不应带掩码，这里兼容处理
                    mask = new byte[4];
                    readFully(mask, 4);
                }
                byte[] payload = new byte[(int) len];
                if (len > 0) {
                    readFully(payload, (int) len);
                }
                if (mask != null) {
                    for (int i = 0; i < payload.length; i++) {
                        payload[i] ^= mask[i % 4];
                    }
                }

                if (opcode == OP_PING) {
                    sendFrame(OP_PONG, payload);
                    continue;
                }
                if (opcode == OP_PONG) {
                    continue;
                }
                if (opcode == OP_CLOSE) {
                    try {
                        sendFrame(OP_CLOSE, new byte[]{0x03, (byte) 0xE8});
                    } catch (Exception ignored) {
                    }
                    reason = "收到关闭帧";
                    break;
                }
                if (opcode == OP_CONT) {
                    if (fragment != null) {
                        fragment.write(payload);
                        if (fin) {
                            dispatch(fragmentOpcode, fragment.toByteArray());
                            fragment = null;
                        }
                    }
                    continue;
                }
                if (opcode == OP_TEXT || opcode == OP_BINARY) {
                    if (!fin) {
                        fragment = new ByteArrayOutputStream();
                        fragment.write(payload);
                        fragmentOpcode = opcode;
                    } else {
                        dispatch(opcode, payload);
                    }
                }
            }
        } catch (SocketTimeoutException e) {
            reason = "心跳超时";
        } catch (Exception e) {
            reason = closed ? "已关闭" : ("读取异常: " + e.getMessage());
        } finally {
            closed = true;
            closeQuietly();
            try {
                listener.onClosed(reason);
            } catch (Exception ignored) {
            }
        }
    }

    private void dispatch(int opcode, byte[] payload) {
        if (opcode != OP_TEXT) {
            return;   // 本项目业务都是文本 JSON，二进制帧忽略
        }
        try {
            listener.onMessage(new String(payload, "UTF-8"));
        } catch (Exception ignored) {
        }
    }

    /** 发送一条文本消息，失败返回 false */
    public boolean send(String text) {
        if (closed || text == null) {
            return false;
        }
        try {
            sendFrame(OP_TEXT, text.getBytes("UTF-8"));
            return true;
        } catch (Exception e) {
            return false;
        }
    }

    /**
     * 发送一条二进制消息（麦克风 PCM 数据），失败返回 false。
     * 与文本帧共用同一把写锁，保证两种帧不会在 TCP 流里交错。
     */
    public boolean sendBinary(byte[] data) {
        if (closed || data == null || data.length == 0) {
            return false;
        }
        try {
            sendFrame(OP_BINARY, data);
            return true;
        } catch (Exception e) {
            return false;
        }
    }

    /** 主动关闭 */
    public void close() {
        if (closed) {
            closeQuietly();
            return;
        }
        closed = true;
        try {
            sendFrame(OP_CLOSE, new byte[]{0x03, (byte) 0xE8});
        } catch (Exception ignored) {
        }
        closeQuietly();
    }

    // ==================== 底层实现 ====================

    /** 发送一帧，客户端必须加掩码 */
    private void sendFrame(int opcode, byte[] payload) throws IOException {
        if (out == null) {
            throw new IOException("连接未建立");
        }
        int len = payload == null ? 0 : payload.length;
        ByteArrayOutputStream frame = new ByteArrayOutputStream(len + 14);
        frame.write(0x80 | opcode);          // FIN=1
        if (len <= 125) {
            frame.write(0x80 | len);
        } else if (len <= 0xFFFF) {
            frame.write(0x80 | 126);
            frame.write((len >>> 8) & 0xFF);
            frame.write(len & 0xFF);
        } else {
            frame.write(0x80 | 127);
            long l = len;
            for (int i = 7; i >= 0; i--) {
                frame.write((int) ((l >>> (8 * i)) & 0xFF));
            }
        }
        byte[] mask = new byte[4];
        random.nextBytes(mask);
        frame.write(mask, 0, 4);
        for (int i = 0; i < len; i++) {
            frame.write(payload[i] ^ mask[i % 4]);
        }

        byte[] data = frame.toByteArray();
        synchronized (writeLock) {
            out.write(data);
            out.flush();
        }
    }

    private int readByte() throws IOException {
        int b = in.read();
        if (b < 0) {
            throw new IOException("连接已断开");
        }
        return b;
    }

    private void readFully(byte[] buf, int len) throws IOException {
        int off = 0;
        while (off < len) {
            int n = in.read(buf, off, len - off);
            if (n < 0) {
                throw new IOException("连接已断开");
            }
            off += n;
        }
    }

    /** 读取一行 HTTP 头（不含 CRLF）；流结束返回 null，空行返回 "" */
    private String readHttpLine() throws IOException {
        ByteArrayOutputStream buf = new ByteArrayOutputStream();
        int c;
        while ((c = in.read()) != -1) {
            if (c == '\n') {
                break;
            }
            if (c != '\r') {
                buf.write(c);
            }
        }
        if (c == -1 && buf.size() == 0) {
            return null;
        }
        return new String(buf.toByteArray(), "ISO-8859-1");
    }

    private static String computeAccept(String key) {
        try {
            MessageDigest sha1 = MessageDigest.getInstance("SHA-1");
            byte[] digest = sha1.digest((key + WS_GUID).getBytes("UTF-8"));
            return Base64.encodeToString(digest, Base64.NO_WRAP);
        } catch (Exception e) {
            return "";
        }
    }

    private void closeQuietly() {
        try {
            if (socket != null) {
                socket.close();
            }
        } catch (Exception ignored) {
        }
        socket = null;
        in = null;
        out = null;
    }
}
