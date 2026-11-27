# -*- coding: utf-8 -*-
r"""
跨设备协同服务（PC 端）

在已有的 HTTP 文件传输之上，补齐"真正协同"需要的三件事：

  1. UDP 局域网自动发现 —— 手机不用手输 IP，广播一下就能找到电脑
  2. 配对机制            —— 首次连接输入 6 位访问码，之后用 token 免输
  3. WebSocket 实时通道  —— 剪贴板双向同步、手机通知转发

图省事的对比
------------
浏览器方案只能"打开网页手动传文件"，而 App + WebSocket 才能做到：
手机上复制一段话，电脑上直接 Ctrl+V；手机来消息，电脑弹通知。

Windows 剪贴板
--------------
用 ctypes 调 user32 的剪贴板 API（OpenClipboard / GetClipboardData）。
必须显式声明 argtypes/restype，否则 64 位下句柄会被截断（这个坑之前踩过）。
"""

import os
import re
import json
import time
import uuid
import socket
import base64
import ctypes
import secrets
import threading
from datetime import datetime
from ctypes import wintypes

from ws_server import WebSocketHub, WebSocketSession, OP_TEXT, OP_BINARY


# ==================== 协议常量 ====================

DISCOVERY_PORT = 47777
MAGIC_DISCOVER = "CMLINK_DISCOVER"
MAGIC_OFFER = "CMLINK_OFFER"
MAGIC_ANNOUNCE = "CMLINK_ANNOUNCE"
MAGIC_CONNECT_REQ = "CMLINK_CONNECT_REQ"
MAGIC_CONNECT_ACCEPT = "CMLINK_CONNECT_ACCEPT"
MAGIC_CONNECT_DENY = "CMLINK_CONNECT_DENY"
MAGIC_CONNECT_TOKEN = "CMLINK_CONNECT_TOKEN"
PROTOCOL_VERSION = 1

# 手机广播的有效期：超过这个时间没收到新广播就当作离线
PHONE_TTL = 12.0
# 连接请求等待对方确认的时长
CONNECT_REQ_TTL = 60.0
# 配对 token 有效期（天）。到期后必须重新配对 / 在电脑上同意。
TOKEN_TTL_DAYS = 30

# 剪贴板轮询间隔（毫秒）。太快费 CPU，太慢手感差，500ms 比较平衡。
CLIPBOARD_POLL_MS = 500


# ==================== Windows 剪贴板 ====================

CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002

_user32 = ctypes.windll.user32
_kernel32 = ctypes.windll.kernel32

# 必须声明类型：默认 c_int 会把 64 位句柄截断成 32 位
_user32.OpenClipboard.argtypes = [wintypes.HWND]
_user32.OpenClipboard.restype = wintypes.BOOL
_user32.CloseClipboard.restype = wintypes.BOOL
_user32.EmptyClipboard.restype = wintypes.BOOL
_user32.GetClipboardData.argtypes = [wintypes.UINT]
_user32.GetClipboardData.restype = wintypes.HANDLE
_user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
_user32.SetClipboardData.restype = wintypes.HANDLE
_kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
_kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
_kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
_kernel32.GlobalLock.restype = ctypes.c_void_p
_kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
_kernel32.GlobalFree.argtypes = [wintypes.HGLOBAL]


def get_clipboard_text():
    """读取 Windows 剪贴板文本，失败返回 None"""
    if not _user32.OpenClipboard(None):
        return None
    try:
        handle = _user32.GetClipboardData(CF_UNICODETEXT)
        if not handle:
            return None
        ptr = _kernel32.GlobalLock(handle)
        if not ptr:
            return None
        try:
            return ctypes.wstring_at(ptr)
        finally:
            _kernel32.GlobalUnlock(handle)
    except Exception:
        return None
    finally:
        _user32.CloseClipboard()


def set_clipboard_text(text):
    """写入 Windows 剪贴板，成功返回 True"""
    if text is None:
        return False
    data = str(text)
    try:
        # 关键：缓冲区大小必须按 UTF-16LE 的**字节数**算，不能按 Python 字符数算。
        # Python 的 len() 数的是码点，而 emoji 等在 UTF-16 里占两个码元（代理对），
        # 按 len()*2 开缓冲区会把内容截断（实测 "内容🎉" 变成 "内容\ud83c"）。
        raw = data.encode("utf-16-le", errors="surrogatepass") + b"\x00\x00"
        buf_size = len(raw)
        if not _user32.OpenClipboard(None):
            return False
        try:
            if not _user32.EmptyClipboard():
                return False
            h = _kernel32.GlobalAlloc(GMEM_MOVEABLE, buf_size)
            if not h:
                return False
            ptr = _kernel32.GlobalLock(h)
            if not ptr:
                _kernel32.GlobalFree(h)
                return False
            try:
                ctypes.memmove(ptr, raw, buf_size)
            finally:
                _kernel32.GlobalUnlock(h)
            if not _user32.SetClipboardData(CF_UNICODETEXT, h):
                _kernel32.GlobalFree(h)
                return False
            return True
        finally:
            _user32.CloseClipboard()
    except Exception:
        return False


# ==================== UDP 发现服务 ====================

class DiscoveryResponder:
    """
    监听 UDP 广播，收到手机的发现请求后单播回复本机信息。

    为什么用广播：手机不知道电脑 IP，但知道"局域网里喊一嗓子"。
    """

    def __init__(self, get_info, port=DISCOVERY_PORT):
        """
        get_info: 无参函数，返回 dict（要回复给手机的内容）
        """
        self.get_info = get_info
        self.port = port
        self._sock = None
        self._thread = None
        self._running = False
        # 扩展协议处理器：handler(msg, addr) -> True 表示已处理
        self._handler = None

    def set_handler(self, handler):
        """接管 DISCOVER 之外的扩展消息（手机广播 / 连接请求等）"""
        self._handler = handler

    def start(self):
        if self._running:
            return False, "发现服务已在运行"
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            # Windows 下要允许端口复用，否则多实例会冲突
            try:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            except Exception:
                pass
            sock.bind(("0.0.0.0", self.port))
            sock.settimeout(1.0)
        except OSError as e:
            return False, f"UDP {self.port} 端口绑定失败：{e}"

        self._sock = sock
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        return True, f"设备发现已开启（UDP {self.port}）"

    def _loop(self):
        while self._running:
            try:
                data, addr = self._sock.recvfrom(2048)
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                msg = json.loads(data.decode("utf-8", errors="ignore"))
            except Exception:
                continue
            if self._handler is not None:
                try:
                    if self._handler(msg, addr):
                        continue
                except Exception:
                    pass
            if msg.get("magic") != MAGIC_DISCOVER:
                continue
            try:
                reply = self.get_info()
                reply["magic"] = MAGIC_OFFER
                reply["v"] = PROTOCOL_VERSION
                payload = json.dumps(reply, ensure_ascii=False).encode("utf-8")
                self._sock.sendto(payload, addr)
            except Exception:
                continue

    def send(self, payload, addr):
        """从发现端口发送一条 JSON（保持源端口 47777，手机才能直接回包）"""
        sock = self._sock
        if sock is None:
            return False
        try:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            sock.sendto(data, addr)
            return True
        except Exception:
            return False

    def stop(self):
        self._running = False
        if self._sock:
            try:
                self._sock.close()
            except Exception:
                pass
            self._sock = None
        return True, "设备发现已停止"

    def is_running(self):
        return self._running


# ==================== 配对管理 ====================

class PairingManager:
    """
    管理访问码与 token。

    流程：PC 界面显示 6 位随机码 → 用户在手机输入 → 手机换来一个长期 token。
    之后手机每次连接只带 token，不再需要输码。
    """

    def __init__(self, state_file=None):
        self._lock = threading.Lock()
        self.code = self._new_code()
        self.tokens = {}          # token -> {"device":..., "paired_at":...}
        self.state_file = state_file or os.path.join(
            os.environ.get("USERPROFILE", os.environ.get("TEMP", ".")),
            "CleanMasterBackup", "link_paired.json")
        self._load()

    @staticmethod
    def _new_code():
        """6 位数字，避免以 0 开头引起输入框消失"""
        return str(secrets.randbelow(900000) + 100000)

    def refresh_code(self):
        with self._lock:
            self.code = self._new_code()
        return self.code

    def _load(self):
        if not os.path.exists(self.state_file):
            return
        try:
            with open(self.state_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                raw = data.get("tokens", {}) or {}
                # 旧版本 token 没有过期时间：按"已过期"处理，强制重新配对。
                now = time.time()
                kept = {}
                for tok, info in raw.items():
                    if not isinstance(info, dict):
                        continue
                    try:
                        exp = float(info.get("expires_at") or 0)
                    except Exception:
                        exp = 0
                    if exp > now:
                        kept[tok] = info
                self.tokens = kept
                if len(kept) != len(raw):
                    self._save()
        except Exception:
            self.tokens = {}

    def _save(self):
        try:
            os.makedirs(os.path.dirname(self.state_file), exist_ok=True)
            with open(self.state_file, "w", encoding="utf-8") as f:
                json.dump({"tokens": self.tokens}, f, ensure_ascii=False)
        except Exception:
            pass

    def verify_code(self, code, device="未知设备"):
        """
        校验访问码，成功返回 token（字符串），失败返回 None。
        """
        with self._lock:
            if not code or str(code).strip() != self.code:
                return None
            token = secrets.token_hex(16)
            now = time.time()
            self.tokens[token] = {
                "device": device,
                "paired_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "expires_at": now + TOKEN_TTL_DAYS * 86400,
            }
            # 配对成功后立即换新码，防止被重复利用
            self.code = self._new_code()
            self._save()
            return token

    def grant(self, device="未知设备"):
        """设备连线请求被同意后，直接签发一个 token（不需要输入访问码）"""
        with self._lock:
            token = secrets.token_hex(16)
            now = time.time()
            self.tokens[token] = {
                "device": device,
                "paired_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "expires_at": now + TOKEN_TTL_DAYS * 86400,
            }
            self._save()
            return token

    def check_token(self, token):
        if not token:
            return False
        now = time.time()
        with self._lock:
            info = self.tokens.get(token)
            if not info:
                return False
            try:
                exp = float(info.get("expires_at") or 0)
            except Exception:
                exp = 0
            if exp <= now:
                # 过期即删除，避免长期驻留
                del self.tokens[token]
                self._save()
                return False
            return True

    def device_of(self, token):
        with self._lock:
            info = self.tokens.get(token)
            return info.get("device", "未知设备") if info else ""

    def revoke(self, token):
        with self._lock:
            if token in self.tokens:
                del self.tokens[token]
                self._save()
                return True
        return False

    def revoke_all(self):
        with self._lock:
            self.tokens.clear()
            self._save()

    def revoke_by_prefix(self, token_id):
        """
        按界面上展示的 8 位前缀移除一条配对记录，返回完整 token（用于踢掉在线连接）。
        前缀太短直接拒绝，避免误删。
        """
        token_id = (token_id or "").strip()
        if len(token_id) < 6:
            return ""
        with self._lock:
            for tok in list(self.tokens.keys()):
                if tok.startswith(token_id):
                    del self.tokens[tok]
                    self._save()
                    return tok
        return ""

    def paired_devices(self):
        now = time.time()
        with self._lock:
            out = []
            for k, v in self.tokens.items():
                try:
                    exp = float(v.get("expires_at") or 0)
                except Exception:
                    exp = 0
                out.append({
                    "id": k[:8],
                    "device": v.get("device", ""),
                    "paired_at": v.get("paired_at", ""),
                    "expires_at": exp,
                    "remain_days": max(0, int((exp - now) // 86400)),
                })
            out.sort(key=lambda x: x.get("paired_at", ""), reverse=True)
            return out


# ==================== 协同服务主类 ====================

class LinkService:
    """
    跨设备协同服务。

    职责：
      · 启动/停止 UDP 发现
      · 管理 WebSocket 连接（剪贴板、通知、心跳）
      · Windows 剪贴板监听与双向同步
    """

    def __init__(self, transfer_engine=None, progress_callback=None,
                 event_callback=None):
        self.engine = transfer_engine
        self.progress_callback = progress_callback
        self.event_callback = event_callback

        self.hub = WebSocketHub()
        self.pairing = PairingManager()
        self.discovery = DiscoveryResponder(self._discovery_info)

        self.device_id = self._load_or_make_device_id()
        self.pc_name = socket.gethostname() or "这台电脑"

        self._running = False
        self._clipboard_thread = None
        self._clipboard_running = False
        self._keepalive_running = False
        self.clipboard_sync_enabled = True

        # 防回环：记录最近一次由"外部来源"写入剪贴板的内容
        self._last_from_phone = None
        self._last_seen = None

        self.notifications = []      # 收到的手机通知
        self.clipboard_log = []      # 剪贴板同步记录
        self.connected_clients = []  # 已连接设备信息

        # 局域网设备列表 / 连线确认（UDP 线程与界面线程共享，全部加锁）
        self._udp_lock = threading.Lock()
        self._discovered_phones = {}   # id -> {id,name,addr,last_seen}
        self._pending_connects = {}    # reqId -> {kind:"in"/"out", ...}
        self._connect_events = []      # 供界面轮询的提示消息
        self.discovery.set_handler(self._on_discovery_message)

        # 手机麦克风：PC 端接收与声卡输出
        self.mic = None
        self._mic_owner = None      # 当前占用麦克风的那条连接（多台手机只允许一台）
        try:
            import mic_receiver
            self.mic = mic_receiver.MicEngine()
        except Exception:
            self.mic = None

    # ---------- 基础 ----------

    def _load_or_make_device_id(self):
        path = os.path.join(os.environ.get("USERPROFILE", os.environ.get("TEMP", ".")),
                            "CleanMasterBackup", "link_device_id.txt")
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            if os.path.exists(path):
                with open(path, "r", encoding="utf-8") as f:
                    v = f.read().strip()
                    if v:
                        return v
            v = uuid.uuid4().hex
            with open(path, "w", encoding="utf-8") as f:
                f.write(v)
            return v
        except Exception:
            return uuid.uuid4().hex

    def _emit(self, percent, message):
        if self.progress_callback:
            try:
                self.progress_callback(percent, message)
            except Exception:
                pass

    def _notify(self, kind, payload=None):
        if self.event_callback:
            try:
                self.event_callback(kind, payload)
            except Exception:
                pass

    def _http_port(self):
        """HTTP 传输服务实际监听的端口（未启动时退回默认 8080）"""
        try:
            if self.engine and getattr(self.engine, "_server", None):
                return self.engine._server.server_address[1]
        except Exception:
            pass
        return 8080

    def _discovery_info(self):
        """回复给手机广播的信息"""
        return {
            "name": self.pc_name,
            "port": self._http_port(),
            "id": self.device_id,
            # 语义修正：这里应该表示"本机要求配对"，而不是"本机还没有任何配对记录"。
            # 用后者会让新手机误以为不用配对，直接连接然后被 403 挡在门外
            # （手机上表现为一直"连接中…"无限重试）。本机的 WebSocket 永远要求 token，
            # 所以这里恒为 True，由手机端根据自己的 token 是否为空来决定是否弹配对框。
            "needPair": True,
        }

    # ---------- 局域网设备列表与连线确认 ----------

    @staticmethod
    def _is_lan(ip):
        try:
            from file_transfer import is_private_ip
            return is_private_ip(ip)
        except Exception:
            return ip.startswith(("192.168.", "10.", "127."))

    def _push_connect_event(self, text, ok=True):
        with self._udp_lock:
            self._connect_events.append({"text": text, "ok": bool(ok), "ts": time.time()})
            if len(self._connect_events) > 30:
                self._connect_events = self._connect_events[-30:]

    def _on_discovery_message(self, msg, addr):
        """
        处理扩展 UDP 消息。

        返回 True 表示已消费；返回 False 交给默认的 DISCOVER 回复逻辑。
        只接受局域网来源，公网包一律丢弃。
        """
        magic = msg.get("magic")
        if magic == MAGIC_DISCOVER:
            return False          # 保持原有"手机找电脑"逻辑
        try:
            ip = addr[0]
        except Exception:
            return True
        if not self._is_lan(ip):
            return True

        if magic == MAGIC_ANNOUNCE:
            if msg.get("role") != "phone":
                return True
            did = str(msg.get("id") or "").strip()[:64]
            name = str(msg.get("name") or "").strip()[:60]
            if not did or not name:
                return True
            try:
                port = int(msg.get("port") or DISCOVERY_PORT)
            except Exception:
                port = DISCOVERY_PORT
            if not (0 < port < 65536):
                port = DISCOVERY_PORT
            with self._udp_lock:
                self._discovered_phones[did] = {
                    "id": did, "name": name, "addr": ip, "port": port,
                    "last_seen": time.time(),
                }
            return True

        if magic == MAGIC_CONNECT_REQ:
            # 手机过来请求连接：交给界面弹确认框
            if msg.get("from") != "phone":
                return True
            req_id = str(msg.get("reqId") or "").strip()[:64]
            did = str(msg.get("id") or "").strip()[:64]
            name = str(msg.get("name") or "").strip()[:60]
            target = str(msg.get("to") or "").strip()
            if not req_id or not did or not name:
                return True
            if target and target != self.device_id:
                return True
            now = time.time()
            with self._udp_lock:
                # 同一台手机的旧请求直接替换，避免确认框堆积
                for key, val in list(self._pending_connects.items()):
                    if val.get("kind") == "in" and val.get("device_id") == did:
                        self._pending_connects.pop(key, None)
                self._pending_connects[req_id] = {
                    "kind": "in", "reqId": req_id, "device_id": did,
                    "device": name, "addr": ip, "sport": addr[1], "ts": now,
                }
            self._notify("connect_request", {"reqId": req_id,
                                             "device": name, "addr": ip})
            return True

        if magic in (MAGIC_CONNECT_ACCEPT, MAGIC_CONNECT_DENY, MAGIC_CONNECT_TOKEN):
            req_id = str(msg.get("reqId") or "").strip()[:64]
            with self._udp_lock:
                pending = self._pending_connects.get(req_id)
            if not pending or pending.get("kind") != "out":
                return True
            if str(pending.get("addr")) != ip:
                return True
            if magic == MAGIC_CONNECT_DENY:
                with self._udp_lock:
                    self._pending_connects.pop(req_id, None)
                self._push_connect_event("「%s」拒绝了连接请求"
                                         % pending.get("device", "手机"), ok=False)
                return True
            if magic == MAGIC_CONNECT_ACCEPT:
                # 手机同意了电脑的请求：签发 token 回给它，手机拿 token 连 WebSocket
                token = self.pairing.grant(pending.get("device") or "手机")
                sent = self.discovery.send({
                    "magic": MAGIC_CONNECT_TOKEN,
                    "v": PROTOCOL_VERSION,
                    "reqId": req_id,
                    "token": token,
                    "port": self._http_port(),
                    "name": self.pc_name,
                    "id": self.device_id,
                }, (ip, addr[1]))
                with self._udp_lock:
                    self._pending_connects.pop(req_id, None)
                if sent:
                    self._push_connect_event("「%s」已同意，等待手机接入"
                                             % pending.get("device", "手机"), ok=True)
                else:
                    self.pairing.revoke(token)
                    self._push_connect_event("回复手机失败，请重试", ok=False)
                return True
            return True           # TOKEN 是电脑发出的，忽略对端发来的

        return True               # 未知消息：丢弃

    def discoverable_devices(self):
        """局域网内正在广播、且尚未连接的手机列表"""
        now = time.time()
        connected = {c.get("device") for c in self.connected_clients}
        out = []
        with self._udp_lock:
            for did, info in list(self._discovered_phones.items()):
                if now - float(info.get("last_seen", 0)) > PHONE_TTL:
                    self._discovered_phones.pop(did, None)
                    continue
                if info.get("name") in connected:
                    continue
                out.append(dict(info))
        out.sort(key=lambda x: (x.get("name") or "", x.get("addr") or ""))
        return out

    def request_connect(self, device_id):
        """电脑端向列表里的手机发起连接请求（手机确认后才下发 token）"""
        if not self._running or not self.discovery.is_running():
            return False, "请先开启协同服务"
        with self._udp_lock:
            info = self._discovered_phones.get(str(device_id))
            info = dict(info) if info else None
        if not info:
            return False, "该设备已离线，请刷新列表"
        req_id = secrets.token_hex(8)
        ok = self.discovery.send({
            "magic": MAGIC_CONNECT_REQ,
            "v": PROTOCOL_VERSION,
            "reqId": req_id,
            "from": "pc",
            "id": self.device_id,
            "name": self.pc_name,
            "port": self._http_port(),
            "to": info["id"],
        }, (str(info["addr"]), int(info.get("port") or DISCOVERY_PORT)))
        if not ok:
            return False, "发送请求失败，请重试"
        with self._udp_lock:
            self._pending_connects[req_id] = {
                "kind": "out", "reqId": req_id, "device_id": info["id"],
                "device": info["name"], "addr": info["addr"], "ts": time.time(),
            }
        return True, "已请求连接「%s」，等待对方确认" % info["name"]

    def pending_connect_requests(self):
        """待电脑确认的入站连接请求（界面轮询后弹确认框）"""
        now = time.time()
        out = []
        with self._udp_lock:
            for req_id, info in list(self._pending_connects.items()):
                if now - float(info.get("ts", 0)) > CONNECT_REQ_TTL:
                    self._pending_connects.pop(req_id, None)
                    if info.get("kind") == "out":
                        self._connect_events.append({
                            "text": "「%s」未确认，请求已超时"
                                    % info.get("device", "手机"),
                            "ok": False, "ts": now})
                    continue
                if info.get("kind") == "in":
                    out.append(dict(info))
        out.sort(key=lambda x: x.get("ts", 0))
        return out

    def respond_connect_request(self, req_id, allow):
        """电脑端同意 / 拒绝手机的连接请求"""
        with self._udp_lock:
            info = self._pending_connects.pop(str(req_id), None)
        if not info or info.get("kind") != "in":
            return False, "该请求已失效"
        addr = (str(info.get("addr")), int(info.get("sport") or DISCOVERY_PORT))
        if not allow:
            self.discovery.send({
                "magic": MAGIC_CONNECT_DENY, "v": PROTOCOL_VERSION,
                "reqId": req_id, "reason": "电脑端拒绝了连接",
            }, addr)
            self._push_connect_event("已拒绝「%s」的连接请求"
                                     % info.get("device", "手机"), ok=False)
            return True, "已拒绝"

        token = self.pairing.grant(info.get("device") or "手机")
        ok = self.discovery.send({
            "magic": MAGIC_CONNECT_ACCEPT, "v": PROTOCOL_VERSION,
            "reqId": req_id, "token": token,
            "port": self._http_port(), "name": self.pc_name,
            "id": self.device_id,
        }, addr)
        if not ok:
            self.pairing.revoke(token)
            return False, "回复手机失败，请重试"
        self._push_connect_event("已允许「%s」连接" % info.get("device", "手机"), ok=True)
        return True, "已允许"

    def pop_connect_events(self):
        """取出并清空连接状态提示"""
        with self._udp_lock:
            events = self._connect_events
            self._connect_events = []
        return events

    # ---------- 启停 ----------

    def start(self):
        if self._running:
            return False, "协同服务已在运行"
        ok, msg = self.discovery.start()
        if not ok:
            return False, msg
        self._running = True
        self.start_clipboard_watch()
        self._start_keepalive()
        return True, msg

    def stop(self):
        self._running = False
        self._keepalive_running = False
        self.stop_clipboard_watch()
        self.discovery.stop()
        with self._udp_lock:
            self._discovered_phones.clear()
            self._pending_connects.clear()
        try:
            self.hub.close_all()
        except Exception:
            pass
        return True, "协同服务已停止"

    # ---------- 连接保活 ----------

    def _start_keepalive(self):
        """
        定期给所有连接发 ping，并清理"看着还连着、其实早断了"的半开连接。

        手机锁屏/切 Wi-Fi 时 TCP 不会有 FIN，PC 侧会一直以为还连着，
        剪贴板推送全写进黑洞。这里用 WS 控制帧探测：
          · 空闲 15 秒就 ping 一次（浏览器/OkHttp 都会自动回 pong）
          · 50 秒没有任何数据往来判定为死连接，主动关闭，让手机端立刻走重连
        """
        if self._keepalive_running:
            return
        self._keepalive_running = True

        def loop():
            while self._keepalive_running and self._running:
                time.sleep(5)
                now = time.time()
                for sid, s in self.hub.all():
                    # token 过期 / 被移除：立刻踢下线，避免长连接绕过有效期
                    tok = s.meta.get("token") or ""
                    if tok and not self.pairing.check_token(tok):
                        try:
                            s.send_json({"type": "kick",
                                         "message": "连接凭据已过期，请重新连接"})
                            s.close(4001, "token expired")
                        except Exception:
                            pass
                        self.hub.remove(sid)
                        self.connected_clients = [
                            c for c in self.connected_clients
                            if c.get("sid") != sid]
                        self._notify("disconnected", s.meta.get("device") or "设备")
                        continue
                    idle = now - getattr(s, "last_active", now)
                    if idle > 50:
                        try:
                            s.close(1001, "idle timeout")
                        except Exception:
                            pass
                        self.hub.remove(sid)
                    elif idle > 15:
                        try:
                            s.ping()
                        except Exception:
                            pass

        t = threading.Thread(target=loop, name="cmlink-keepalive", daemon=True)
        t.start()

    def is_running(self):
        return self._running

    # ---------- 配对 ----------

    def get_pair_code(self):
        return self.pairing.code

    def refresh_pair_code(self):
        return self.pairing.refresh_code()

    def pair(self, code, device="未知设备"):
        token = self.pairing.verify_code(code, device)
        if not token:
            return None
        return token

    # ---------- WebSocket ----------

    def attach_session(self, session: WebSocketSession, token=""):
        """
        接管一条刚完成握手的 WebSocket 连接。

        重要：这个方法会**阻塞**直到连接断开。
        原因是 BaseHTTPRequestHandler 在 do_GET 返回后会关闭 socket，
        如果只把消息循环丢到别的线程、自己立刻返回，连接就会马上被掐断
        （实测表现为：握手成功 → 连接数瞬间变 0 → 剪贴板全收不到）。
        ThreadingHTTPServer 本身给每个请求分配独立线程，所以阻塞在这里是安全的。
        """
        device = self.pairing.device_of(token) or "未知设备"
        sid = self.hub.add(session)
        session.meta["device"] = device
        session.meta["token"] = token
        close_reason = ""
        try:
            session.send_json({"type": "welcome", "server": self.pc_name})
            self.connected_clients.append({
                "sid": sid,
                "device": device,
                "addr": "%s:%s" % session.client_addr[:2],
                "since": datetime.now().strftime("%H:%M:%S"),
                "ts": time.time(),
            })
            self._notify("connected", device)
            while True:
                op, data = session.recv()
                if op is None:
                    break
                if op == OP_BINARY:
                    # 二进制帧 = 手机麦克风的 PCM 音频流。
                    # 多台手机同时连时，只接收真正持有麦克风的那一台。
                    if (self.mic is not None and self.mic.is_active()
                            and session is self._mic_owner):
                        self.mic.feed(data)
                    continue
                if op != OP_TEXT:
                    continue
                try:
                    msg = json.loads(data.decode("utf-8", errors="ignore"))
                except Exception:
                    continue
                try:
                    action = self._handle_message(msg, session)
                except Exception:
                    # 单条消息处理失败不能把整条连接带走
                    import traceback
                    traceback.print_exc()
                    action = None
                if action == "close":
                    close_reason = "bye"
                    break
        finally:
            self.hub.remove(sid)
            # 手机是主动退出（bye）：立刻吊销它的 token，
            # 否则断开后它还能用旧 token 走 HTTP 收发消息。
            # 网络掉线不会走 bye，token 保留，方便锁屏/切网后自动重连。
            if close_reason == "bye" and token:
                try:
                    self.pairing.revoke(token)
                except Exception:
                    pass
            # 用 sid 精确移除：两台手机可能同名，按名字过滤会误删另一台
            self.connected_clients = [c for c in self.connected_clients
                                      if c.get("sid") != sid]
            if session is self._mic_owner:
                self._mic_owner = None
            # 录音中途掉线（手机锁屏/切网）时必须收尾：
            # 否则 WAV 文件句柄一直不关，音频数据也永远写不进去。
            if session.meta.get("mic") and self.mic is not None and self.mic.is_active():
                try:
                    ok, text = self.mic.stop()
                    text = "连接断开，录音已自动结束。" + text
                    self._notify("mic", {"state": "stop", "ok": ok, "message": text})
                except Exception:
                    pass
            self._notify("disconnected", device)
        return True

    def _handle_message(self, msg, session):
        """处理手机发来的消息"""
        mtype = msg.get("type")

        if mtype == "ping":
            session.send_json({"type": "pong"})
            return

        if mtype == "sheet_hello":
            return

        if mtype == "clipboard":
            text = msg.get("text")
            if not text:
                return
            self._apply_clipboard_from_phone(text, session)
            return

        if mtype == "text":
            # 手机发来的「文字互传」内容（不是剪贴板同步）
            text = (msg.get("text") or "").strip()
            if not text:
                return
            device = session.meta.get("device") or "手机"
            item = {
                "text": text,
                "time": datetime.now().strftime("%m-%d %H:%M:%S"),
                "ts": time.time(),
                "from": "phone",
                "device": device,
            }
            eng = self.engine
            if eng is not None:
                eng.received_texts.append(item)
                if len(eng.received_texts) > 200:
                    eng.received_texts = eng.received_texts[-200:]
            self._notify("text", item)
            # 转发给其它手机：手机之间也能互相发消息
            self.push_peer_text(text, device,
                                exclude_token=session.meta.get("token", ""))
            session.send_json({"type": "text_ack", "ok": True})
            return

        if mtype == "bye":
            # 手机端主动退出：立刻结束会话，避免"已退出还收到消息"
            return "close"

        if mtype == "notification":
            item = {
                "app": msg.get("app", ""),
                "title": msg.get("title", ""),
                "text": msg.get("text", ""),
                "time": datetime.now().strftime("%H:%M:%S"),
                "raw_ts": msg.get("ts", 0),
            }
            self.notifications.append(item)
            if len(self.notifications) > 300:
                self.notifications = self.notifications[-300:]
            self._notify("notification", item)
            return

        # ---------- 手机麦克风 ----------
        if mtype == "mic_start":
            if self.mic is None:
                session.send_json({"type": "mic_ack", "ok": False,
                                   "error": "PC 端音频模块不可用"})
                return
            # 多台手机同时连时，麦克风只能被一台占用
            if self.mic.is_active() and session is not self._mic_owner:
                session.send_json({
                    "type": "mic_ack", "ok": False,
                    "error": "电脑的麦克风正被另一台手机使用，请先在那边停止",
                    "message": "麦克风已被其它设备占用",
                })
                return
            rate = int(msg.get("rate", 16000))
            channels = int(msg.get("channels", 1))
            bits = int(msg.get("bits", 16))
            ok, text = self.mic.start(rate=rate, channels=channels, bits=bits)
            if ok:
                session.meta["mic"] = True
                self._mic_owner = session
            # 告诉手机：可以用指定格式开始推 PCM 了
            session.send_json({
                "type": "mic_ack", "ok": bool(ok),
                "rate": rate, "channels": channels, "bits": bits,
                "playing": bool(self.mic.session and self.mic.session.playing),
                "message": text,
            })
            self._notify("mic", {"state": "start", "ok": ok, "message": text})
            return

        if mtype == "mic_stop":
            if self.mic is None:
                return
            if self._mic_owner is not None and session is not self._mic_owner:
                session.send_json({"type": "mic_stopped", "ok": False,
                                   "message": "麦克风不在你这边，无需停止"})
                return
            ok, text = self.mic.stop()
            session.meta["mic"] = False
            self._mic_owner = None
            session.send_json({"type": "mic_stopped", "ok": bool(ok), "message": text})
            self._notify("mic", {"state": "stop", "ok": ok, "message": text})
            return

    def _apply_clipboard_from_phone(self, text, session=None):
        """把手机剪贴板写入电脑，并防止回环"""
        if not self.clipboard_sync_enabled:
            return
        if text == self._last_seen:
            return
        self._last_from_phone = text
        ok = set_clipboard_text(text)
        self._last_seen = text
        item = {
            "dir": "手机→电脑",
            "ok": ok,
            "preview": text[:80].replace("\n", " "),
            "time": datetime.now().strftime("%H:%M:%S"),
        }
        self.clipboard_log.append(item)
        if len(self.clipboard_log) > 200:
            self.clipboard_log = self.clipboard_log[-200:]
        self._notify("clipboard", item)

    # ---------- 剪贴板监听 ----------

    def start_clipboard_watch(self):
        if self._clipboard_running:
            return
        # 记录当前剪贴板作为基线：否则手机一连上，就会立刻收到电脑上
        # 早已存在的那条旧内容，用户会觉得很突兀。
        try:
            self._last_seen = get_clipboard_text()
        except Exception:
            self._last_seen = None
        self._clipboard_running = True
        self._clipboard_thread = threading.Thread(
            target=self._clipboard_loop, daemon=True)
        self._clipboard_thread.start()

    def stop_clipboard_watch(self):
        self._clipboard_running = False

    def _clipboard_loop(self):
        """
        轮询电脑剪贴板，有变化就推给手机。

        注意防回环：如果当前内容正是"刚从手机写进来的"，就跳过，
        否则会形成 手机→电脑→手机→电脑… 的死循环。
        """
        while self._clipboard_running:
            try:
                if self.clipboard_sync_enabled and self.hub.count() > 0:
                    cur = get_clipboard_text()
                    if cur and cur != self._last_seen:
                        if cur == self._last_from_phone:
                            # 这是刚才从手机写进来的，不往回推
                            self._last_seen = cur
                        else:
                            self._last_seen = cur
                            self.hub.broadcast({
                                "type": "clipboard",
                                "text": cur,
                            })
                            item = {
                                "dir": "电脑→手机",
                                "ok": True,
                                "preview": cur[:80].replace("\n", " "),
                                "time": datetime.now().strftime("%H:%M:%S"),
                            }
                            self.clipboard_log.append(item)
                            if len(self.clipboard_log) > 200:
                                self.clipboard_log = self.clipboard_log[-200:]
                            self._notify("clipboard", item)
            except Exception:
                pass
            time.sleep(CLIPBOARD_POLL_MS / 1000.0)

    def push_clipboard_to_phones(self, text):
        """桌面端主动把内容推到手机"""
        if not text:
            return False, "内容为空"
        self._last_seen = text
        n = self.hub.broadcast({"type": "clipboard", "text": text})
        if n == 0:
            return False, "没有已连接的手机"
        item = {
            "dir": "电脑→手机",
            "ok": True,
            "preview": text[:80].replace("\n", " "),
            "time": datetime.now().strftime("%H:%M:%S"),
        }
        self.clipboard_log.append(item)
        if len(self.clipboard_log) > 200:
            self.clipboard_log = self.clipboard_log[-200:]
        self._notify("clipboard", item)
        return True, f"已推送到 {n} 台设备"

    def push_text(self, text):
        """把电脑发出的文字实时推给手机（App 端直接进「文字」列表）"""
        if not text:
            return 0
        return self.hub.broadcast({
            "type": "text",
            "text": text,
            "from": "pc",
            "ts": int(time.time() * 1000),
        })

    def push_file(self, name, size=0, from_device="", exclude_token=""):
        """文件已放进共享目录，通知其它手机（exclude_token 用于排除上传者自己）"""
        if not name:
            return 0
        payload = {
            "type": "file",
            "name": name,
            "size": int(size or 0),
            "from": "peer" if from_device else "pc",
            "device": from_device or "",
            "ts": int(time.time() * 1000),
        }
        return self._broadcast_excluding_token(payload, exclude_token)

    def push_peer_text(self, text, device="", exclude_token=""):
        """把某台手机发的文字转发给其它手机（手机之间互发）"""
        if not text:
            return 0
        return self._broadcast_excluding_token({
            "type": "text",
            "text": text,
            "from": "phone",
            "device": device or "手机",
            "ts": int(time.time() * 1000),
        }, exclude_token)

    def _broadcast_excluding_token(self, payload, token):
        n = 0
        for _sid, s in self.hub.all():
            if s.closed:
                continue
            if token and s.meta.get("token") == token:
                continue          # 不回发给发送者自己
            if s.send_json(payload):
                n += 1
        return n

    def send_to_session(self, sid, payload):
        """只给某一台设备发消息（调试用）"""
        for s_sid, s in self.hub.all():
            if s_sid == sid and not s.closed:
                return bool(s.send_json(payload))
        return False

    def kick_session(self, sid):
        """从电脑端强制断开某台设备"""
        for s_sid, s in self.hub.all():
            if s_sid != sid:
                continue
            token = s.meta.get("token") or ""
            try:
                s.send_json({"type": "kick", "message": "电脑端已断开此设备"})
            except Exception:
                pass
            try:
                s.close(4000, "kicked by pc")
            except Exception:
                pass
            self.hub.remove(s_sid)
            # 断开后立即吊销 token：旧 token 不能再走 HTTP 收发消息
            if token:
                try:
                    self.pairing.revoke(token)
                except Exception:
                    pass
            self.connected_clients = [c for c in self.connected_clients
                                      if c.get("sid") != s_sid]
            if getattr(self, "_mic_owner", None) is s:
                try:
                    if self.mic is not None and self.mic.is_active():
                        self.mic.stop()
                except Exception:
                    pass
                self._mic_owner = None
            self._notify("disconnected", "已断开")
            return True
        return False

    def revoke_paired(self, token_id):
        """从「已配对设备」里移除一条，并踢掉使用该 token 的在线连接"""
        token = self.pairing.revoke_by_prefix(token_id)
        if not token:
            return False, "该配对记录已不存在"
        for sid, s in list(self.hub.all()):
            if s.meta.get("token") == token:
                self.kick_session(sid)
        return True, "已移除该设备"

    def revoke_all_paired(self):
        """移除全部配对设备，并断开所有在线连接"""
        self.pairing.revoke_all()
        for sid, _s in list(self.hub.all()):
            self.kick_session(sid)
        return True, "已移除全部配对设备"

    # ---------- 状态 ----------

    def get_status(self):
        return {
            "running": self._running,
            "discovery": self.discovery.is_running(),
            "clients": self.hub.count(),
            "devices": list(self.connected_clients),
            "pair_code": self.pairing.code,
            "paired": self.pairing.paired_devices(),
            "clipboard_sync": self.clipboard_sync_enabled,
            "notifications": len(self.notifications),
        }

    def recent_notifications(self, limit=50):
        return list(reversed(self.notifications[-limit:]))

    def recent_clipboard(self, limit=50):
        return list(reversed(self.clipboard_log[-limit:]))
