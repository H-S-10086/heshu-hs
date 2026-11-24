# -*- coding: utf-8 -*-
r"""
纯标准库 WebSocket 服务端（RFC 6455）

为什么自己实现
--------------
项目用 PyInstaller 单文件打包，不能引入 websockets / aiohttp 等第三方库。
而 WebSocket 是实现"实时双向同步"（剪贴板、通知）的必要通道：
HTTP 只能一问一答，做不到服务端主动推送。

实现范围
--------
  · 握手（Sec-WebSocket-Accept 计算）
  · 帧解析：支持分片、扩展长度（126/127）、掩码
  · 帧发送：text / binary / ping / pong / close
  · 心跳、连接保活
  · 广播给多个客户端

不支持（本项目用不到）：压缩扩展、二进制分片重组以外的协议扩展。

关键细节（容易踩坑的地方）
--------------------------
  1. 客户端发来的帧**必须**带掩码，服务端发出去的**必须**不带掩码
  2. payload 长度：<=125 直接用 7 位；126 用后 2 字节；127 用后 8 字节
  3. Sec-WebSocket-Accept = base64(sha1(key + 固定 GUID))
  4. 握手成功后必须先发 101 响应，再进入帧循环
"""

import os
import json
import base64
import hashlib
import struct
import socket
import threading
import time

# WebSocket 魔术字符串（RFC 6455 固定值）
WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

# 操作码
OP_CONT = 0x0
OP_TEXT = 0x1
OP_BINARY = 0x2
OP_CLOSE = 0x8
OP_PING = 0x9
OP_PONG = 0xA

# 单帧最大载荷（防止恶意超大帧撑爆内存）
MAX_FRAME_PAYLOAD = 32 * 1024 * 1024


def compute_accept(key):
    """计算 Sec-WebSocket-Accept"""
    raw = (key.strip() + WS_GUID).encode("utf-8")
    return base64.b64encode(hashlib.sha1(raw).digest()).decode("ascii")


def is_websocket_request(headers):
    """判断一个 HTTP 请求是不是 WebSocket 升级请求"""
    upgrade = (headers.get("Upgrade") or "").lower()
    connection = (headers.get("Connection") or "").lower()
    return upgrade == "websocket" and "upgrade" in connection


class WebSocketSession:
    """
    一条 WebSocket 连接。

    用法：
        session = WebSocketSession(sock, rfile)
        session.send_json({"type": "hello"})
        while True:
            op, data = session.recv()
            if op is None: break
    """

    def __init__(self, sock, rfile, client_addr=None):
        self.sock = sock
        self.rfile = rfile
        self.client_addr = client_addr or ("?", 0)
        self.closed = False
        self.last_active = time.time()
        self._send_lock = threading.Lock()
        self.meta = {}          # 业务层可挂任意信息（设备名、token 等）

    # ---------- 底层 ----------

    def _recv_exact(self, n):
        """精确读取 n 字节，失败返回 None"""
        if n <= 0:
            return b""
        try:
            data = self.rfile.read(n)
        except Exception:
            return None
        if not data or len(data) < n:
            return None
        return data

    def recv(self):
        """
        读取一帧，返回 (opcode, payload_bytes)。
        连接关闭或出错返回 (None, None)。
        会自动处理 ping（回 pong）与 close。
        """
        if self.closed:
            return None, None
        try:
            head = self._recv_exact(2)
            if head is None:
                return None, None
            b0, b1 = head[0], head[1]
            fin = (b0 & 0x80) != 0
            opcode = b0 & 0x0F
            masked = (b1 & 0x80) != 0
            length = b1 & 0x7F

            if length == 126:
                ext = self._recv_exact(2)
                if ext is None:
                    return None, None
                length = struct.unpack(">H", ext)[0]
            elif length == 127:
                ext = self._recv_exact(8)
                if ext is None:
                    return None, None
                length = struct.unpack(">Q", ext)[0]

            if length > MAX_FRAME_PAYLOAD:
                self.close(1009, "message too big")
                return None, None

            mask_key = b""
            if masked:
                mask_key = self._recv_exact(4)
                if mask_key is None:
                    return None, None

            payload = self._recv_exact(length) if length else b""
            if payload is None:
                return None, None

            # 解掩码
            if masked and mask_key:
                payload = bytes(b ^ mask_key[i % 4] for i, b in enumerate(payload))

            self.last_active = time.time()

            # 控制帧处理
            if opcode == OP_PING:
                self._send_frame(OP_PONG, payload)
                # 继续读下一帧
                return self.recv()
            if opcode == OP_PONG:
                return self.recv()
            if opcode == OP_CLOSE:
                try:
                    self._send_frame(OP_CLOSE, payload[:2] if payload else b"")
                except Exception:
                    pass
                self.closed = True
                return None, None

            # 分片重组（简化：把后续 continuation 拼接起来）
            if not fin and opcode in (OP_TEXT, OP_BINARY):
                chunks = [payload]
                while True:
                    h2 = self._recv_exact(2)
                    if h2 is None:
                        return None, None
                    b0c, b1c = h2[0], h2[1]
                    fin_c = (b0c & 0x80) != 0
                    op_c = b0c & 0x0F
                    masked_c = (b1c & 0x80) != 0
                    len_c = b1c & 0x7F
                    if len_c == 126:
                        e = self._recv_exact(2)
                        if e is None:
                            return None, None
                        len_c = struct.unpack(">H", e)[0]
                    elif len_c == 127:
                        e = self._recv_exact(8)
                        if e is None:
                            return None, None
                        len_c = struct.unpack(">Q", e)[0]
                    if len_c > MAX_FRAME_PAYLOAD:
                        self.close(1009, "message too big")
                        return None, None
                    mk = self._recv_exact(4) if masked_c else b""
                    if masked_c and mk is None:
                        return None, None
                    part = self._recv_exact(len_c) if len_c else b""
                    if part is None:
                        return None, None
                    if masked_c and mk:
                        part = bytes(b ^ mk[i % 4] for i, b in enumerate(part))
                    chunks.append(part)
                    if op_c == OP_CLOSE or fin_c:
                        break
                payload = b"".join(chunks)

            return opcode, payload
        except Exception:
            self.closed = True
            return None, None

    def _send_frame(self, opcode, payload):
        """发送一帧（服务端不加掩码）"""
        if self.closed and opcode != OP_CLOSE:
            return False
        header = bytearray()
        header.append(0x80 | opcode)   # FIN=1
        n = len(payload)
        if n <= 125:
            header.append(n)
        elif n <= 0xFFFF:
            header.append(126)
            header += struct.pack(">H", n)
        else:
            header.append(127)
            header += struct.pack(">Q", n)
        try:
            with self._send_lock:
                self.sock.sendall(bytes(header) + payload)
            return True
        except Exception:
            self.closed = True
            return False

    # ---------- 对外 ----------

    def send_text(self, text):
        if isinstance(text, str):
            text = text.encode("utf-8")
        return self._send_frame(OP_TEXT, text)

    def send_json(self, obj):
        try:
            body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        except Exception:
            return False
        return self._send_frame(OP_TEXT, body)

    def send_binary(self, data):
        return self._send_frame(OP_BINARY, data)

    def ping(self):
        return self._send_frame(OP_PING, b"")

    def close(self, code=1000, reason=""):
        if self.closed and self.sock is None:
            return
        try:
            payload = struct.pack(">H", code) + reason.encode("utf-8")[:120]
        except Exception:
            payload = b""
        try:
            self._send_frame(OP_CLOSE, payload)
        except Exception:
            pass
        self.closed = True
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except Exception:
            pass
        try:
            self.sock.close()
        except Exception:
            pass

    def recv_json(self):
        """读取并解析一条 JSON 消息；失败返回 None"""
        op, data = self.recv()
        if op is None:
            return None
        if op not in (OP_TEXT,):
            return None
        try:
            return json.loads(data.decode("utf-8", errors="ignore"))
        except Exception:
            return None


class WebSocketHub:
    """
    连接管理器：持有所有活动连接，支持广播。
    业务层（剪贴板/通知/文件）通过它推送消息。
    """

    def __init__(self):
        self._sessions = {}            # id -> WebSocketSession
        self._lock = threading.Lock()
        self._counter = 0

    def add(self, session):
        with self._lock:
            self._counter += 1
            sid = self._counter
            self._sessions[sid] = session
        return sid

    def remove(self, sid):
        with self._lock:
            self._sessions.pop(sid, None)

    def all(self):
        with self._lock:
            return list(self._sessions.items())

    def count(self):
        with self._lock:
            return len(self._sessions)

    def broadcast(self, obj, exclude=None):
        """向所有连接广播一条 JSON 消息，返回成功数"""
        ok = 0
        for sid, s in self.all():
            if exclude is not None and sid == exclude:
                continue
            if s.closed:
                self.remove(sid)
                continue
            if s.send_json(obj):
                ok += 1
        return ok

    def close_all(self):
        for sid, s in self.all():
            try:
                s.close(1001, "server shutdown")
            except Exception:
                pass
        with self._lock:
            self._sessions.clear()


def perform_handshake(handler):
    """
    在 BaseHTTPRequestHandler 上完成 WebSocket 升级握手。

    返回 WebSocketSession；失败返回 None（此时已发送错误响应）。

    说明：调用后需要自行进入帧循环，并且循环结束时设置
    handler.close_connection = True，否则 HTTPServer 会继续复用连接。
    """
    key = handler.headers.get("Sec-WebSocket-Key")
    if not key:
        try:
            handler.send_error(400, "Missing Sec-WebSocket-Key")
        except Exception:
            pass
        return None

    accept = compute_accept(key)
    try:
        handler.send_response(101, "Switching Protocols")
        handler.send_header("Upgrade", "websocket")
        handler.send_header("Connection", "Upgrade")
        handler.send_header("Sec-WebSocket-Accept", accept)
        handler.end_headers()
        handler.wfile.flush()
    except Exception:
        return None

    return WebSocketSession(handler.connection, handler.rfile,
                            handler.client_address)
