# -*- coding: utf-8 -*-
r"""
手机麦克风 · PC 端接收与播放引擎

做一件事
--------
把手机传上来的 PCM 音频**实时**播放到电脑的音频输出设备，不落盘。

如何变成"麦克风"
----------------
Windows 不允许用户态凭空造一个麦克风设备（WO Mic 靠的是自带内核驱动）。
所以这里自动找一条现成通路：
  · 优先虚拟声卡：播放到 "CABLE Input"，其它软件选 "CABLE Output"
  · 否则立体声混音：播放到与该混音同厂商的扬声器，其它软件选「立体声混音」
两者都不存在时提示用户装 VB-Cable 或启用立体声混音。

技术实现
--------
用 ctypes 调 Windows 的 winmm（waveOut 系列）做流式播放。
之所以不用 WASAPI：WASAPI 需要 COM 接口定义，在纯标准库 + ctypes 下代码量会翻几倍，
而 waveOut 对"低延迟播放一段 PCM 流"这个需求完全够用。

关键细节
--------
  · WAVEHDR 在 64 位下的字段顺序与填充必须准确，否则 waveOutWrite 会失败
  · 缓冲区写完后必须 waveOutUnprepareHeader 才能释放，否则内存泄漏
  · 用一个"待回收列表"定期清理已播放完的 header（WHDR_DONE）
"""

import re
import ctypes
import threading
from ctypes import wintypes
from datetime import datetime


# ==================== winmm 绑定 ====================

_winmm = ctypes.windll.winmm

WAVE_MAPPER = -1
WAVE_FORMAT_PCM = 0x0001
WHDR_DONE = 0x00000001


class WAVEFORMATEX(ctypes.Structure):
    _pack_ = 1
    _fields_ = [
        ("wFormatTag", wintypes.WORD),
        ("nChannels", wintypes.WORD),
        ("nSamplesPerSec", wintypes.DWORD),
        ("nAvgBytesPerSec", wintypes.DWORD),
        ("nBlockAlign", wintypes.WORD),
        ("wBitsPerSample", wintypes.WORD),
        ("cbSize", wintypes.WORD),
    ]


class WAVEHDR(ctypes.Structure):
    _fields_ = [
        ("lpData", ctypes.c_void_p),
        ("dwBufferLength", wintypes.DWORD),
        ("dwBytesRecorded", wintypes.DWORD),
        ("dwUser", ctypes.c_void_p),
        ("dwFlags", wintypes.DWORD),
        ("dwLoops", wintypes.DWORD),
        ("lpNext", ctypes.c_void_p),
        ("reserved", ctypes.c_void_p),
    ]


class WAVEOUTCAPS(ctypes.Structure):
    _fields_ = [
        ("wMid", wintypes.WORD),
        ("wPid", wintypes.WORD),
        ("vDriverVersion", wintypes.DWORD),
        ("szPname", wintypes.WCHAR * 32),
        ("dwFormats", wintypes.DWORD),
        ("wChannels", wintypes.WORD),
        ("wReserved1", wintypes.WORD),
        ("dwSupport", wintypes.DWORD),
    ]


_winmm.waveOutGetNumDevs.restype = wintypes.UINT
_winmm.waveOutGetDevCapsW.argtypes = [ctypes.c_void_p,
                                      ctypes.POINTER(WAVEOUTCAPS),
                                      wintypes.UINT]
_winmm.waveOutOpen.argtypes = [ctypes.POINTER(ctypes.c_void_p),
                               wintypes.UINT,
                               ctypes.POINTER(WAVEFORMATEX),
                               ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD]
_winmm.waveOutPrepareHeader.argtypes = [ctypes.c_void_p,
                                        ctypes.POINTER(WAVEHDR), wintypes.UINT]
_winmm.waveOutUnprepareHeader.argtypes = [ctypes.c_void_p,
                                          ctypes.POINTER(WAVEHDR), wintypes.UINT]
_winmm.waveOutWrite.argtypes = [ctypes.c_void_p, ctypes.POINTER(WAVEHDR),
                                wintypes.UINT]
_winmm.waveOutReset.argtypes = [ctypes.c_void_p]
_winmm.waveOutClose.argtypes = [ctypes.c_void_p]


def list_output_devices():
    """
    枚举音频输出设备。
    返回 [{"index": i, "name": "扬声器 (Realtek)"}]
    说明：设备索引会随插拔变化，因此界面上要显示名称让人选。
    """
    devices = []
    try:
        count = _winmm.waveOutGetNumDevs()
    except Exception:
        return devices
    for i in range(count):
        caps = WAVEOUTCAPS()
        try:
            rc = _winmm.waveOutGetDevCapsW(ctypes.c_void_p(i),
                                           ctypes.byref(caps),
                                           ctypes.sizeof(caps))
            if rc == 0:
                devices.append({"index": i, "name": caps.szPname})
        except Exception:
            continue
    return devices


def find_device_index(keyword):
    """按名称关键字查找设备索引，找不到返回 None"""
    if not keyword:
        return None
    low = str(keyword).lower()
    for d in list_output_devices():
        if low in d["name"].lower():
            return d["index"]
    return None


# ==================== 输入设备 / 虚拟麦克风通路 ====================

class WAVEINCAPS(ctypes.Structure):
    _fields_ = [
        ("wMid", wintypes.WORD),
        ("wPid", wintypes.WORD),
        ("vDriverVersion", wintypes.DWORD),
        ("szPname", wintypes.WCHAR * 32),
        ("dwFormats", wintypes.DWORD),
        ("wChannels", wintypes.WORD),
        ("wReserved1", wintypes.WORD),
    ]


_winmm.waveInGetNumDevs.restype = wintypes.UINT
_winmm.waveInGetDevCapsW.argtypes = [ctypes.c_void_p,
                                     ctypes.POINTER(WAVEINCAPS),
                                     wintypes.UINT]


def list_input_devices():
    """枚举录音（麦克风）设备。返回 [{"index": i, "name": "..."}]"""
    devices = []
    try:
        count = _winmm.waveInGetNumDevs()
    except Exception:
        return devices
    for i in range(count):
        caps = WAVEINCAPS()
        try:
            rc = _winmm.waveInGetDevCapsW(ctypes.c_void_p(i),
                                          ctypes.byref(caps),
                                          ctypes.sizeof(caps))
            if rc == 0:
                devices.append({"index": i, "name": caps.szPname})
        except Exception:
            continue
    return devices


# 虚拟声卡（能直接当麦克风用的驱动型设备）
CABLE_RENDER_HINTS = ("cable input", "vb-audio", "voicemeeter input",
                      "virtual audio cable", "virtual cable", "line 1 (virtual")
CABLE_MIC_HINTS = ("cable output", "vb-audio", "voicemeeter output",
                   "virtual audio cable", "virtual cable", "line 1 (virtual")
# 立体声混音：录的是电脑正在播放的声音，零安装就能当麦克风
STEREO_MIX_HINTS = ("立体声混音", "stereo mix", "what u hear", "wave out mix",
                    "混音 (", "混音(")


def _vendor_of(name):
    """取名字最后一对括号里的厂商标识：'立体声混音 (Realtek(R) Audio)' -> 'Realtek(R) Audio'"""
    s = str(name or "")
    m = re.search(r"\(([^()]*(?:\([^()]*\)[^()]*)*)\)\s*$", s)
    return m.group(1).strip() if m else ""


def detect_mic_route():
    """
    找出"把手机声音变成麦克风"的最佳通路。

    返回
    ----
    {
      "kind": "cable" | "stereo_mix" | "none",
      "render": {"index": i, "name": "..."} | None,   # 声音该播到哪个输出设备
      "mic": "CABLE Output (...)" | "立体声混音 (...)",  # 其它软件该选哪个麦克风
      "note": "给用户看的说明"
    }

    优先级
    ------
    1. 虚拟声卡（VB-Cable / VoiceMeeter 等）：声音只在软件间流动，不外放、不混入系统其它声音
    2. 立体声混音：把声音播到同厂商的扬声器，其它软件选「立体声混音」采集
       （零安装，但会把电脑上的其它声音一起录进去，并且会外放）
    """
    outs = list_output_devices()
    ins = list_input_devices()

    render = None
    for d in outs:
        low = d["name"].lower()
        if any(h in low for h in CABLE_RENDER_HINTS):
            render = d
            break
    mic_name = ""
    for d in ins:
        low = d["name"].lower()
        if any(h in low for h in CABLE_MIC_HINTS):
            mic_name = d["name"]
            break
    if render and mic_name:
        return {
            "kind": "cable",
            "render": render,
            "mic": mic_name,
            "note": "声音直接进入虚拟声卡。其它软件把麦克风选成「%s」即可，不会外放。" % mic_name,
        }

    # 立体声混音
    mix = None
    for d in ins:
        low = d["name"].lower()
        if any(h in low for h in STEREO_MIX_HINTS):
            mix = d
            break
    if mix:
        vendor = _vendor_of(mix["name"])
        target = None
        if vendor:
            for d in outs:
                if vendor.lower() in d["name"].lower():
                    target = d
                    if "扬声器" in d["name"] or "speaker" in d["name"].lower():
                        break
        if target is None:
            for d in outs:
                if "扬声器" in d["name"] or "speaker" in d["name"].lower():
                    target = d
                    break
        if target is None and outs:
            target = outs[0]
        if target:
            return {
                "kind": "stereo_mix",
                "render": target,
                "mic": mix["name"],
                "note": ("声音会播到「%s」，其它软件把麦克风选成「%s」即可。"
                         "注意：这条路会把电脑上其它声音一起录进去，建议外放音量调小。"
                         % (target["name"], mix["name"])),
            }

    womic = [d["name"] for d in ins if "wo mic" in d["name"].lower()]
    note = "没有找到虚拟声卡或「立体声混音」。"
    if womic:
        note += ("检测到「%s」，但它只能由 WO Mic 客户端写入，本程序无法直接使用；"
                 "请安装 VB-Cable 虚拟声卡，或启用「立体声混音」。" % womic[0])
    else:
        note += "可安装 VB-Cable 虚拟声卡（免费），或在声音设置里启用「立体声混音」。"
    return {"kind": "none", "render": None, "mic": "", "note": note}


# ==================== 流式播放器 ====================

class _WaveOutPlayer:
    """
    极简流式播放器：把 PCM 分块塞给 waveOut。

    waveOut 的模型是"提交缓冲区 → 播完置 WHDR_DONE"，
    所以这里维护 pending 列表，每次写新数据前先回收已完成的。
    """

    def __init__(self, device_index, rate, channels, bits=16):
        self.ok = False
        self.handle = ctypes.c_void_p()
        self.rate = rate
        self.channels = channels
        self.bits = bits
        self.block_align = channels * bits // 8
        self._pending = []
        self._lock = threading.Lock()
        self._error = ""

        wfx = WAVEFORMATEX()
        wfx.wFormatTag = WAVE_FORMAT_PCM
        wfx.nChannels = channels
        wfx.nSamplesPerSec = rate
        wfx.wBitsPerSample = bits
        wfx.nBlockAlign = self.block_align
        wfx.nAvgBytesPerSec = rate * self.block_align
        wfx.cbSize = 0

        dev = WAVE_MAPPER if device_index is None else int(device_index)
        rc = _winmm.waveOutOpen(ctypes.byref(self.handle), dev,
                                ctypes.byref(wfx), None, None, 0)
        if rc != 0:
            self._error = "waveOutOpen 失败，错误码 %d（设备可能被独占）" % rc
            return
        self.ok = True

    def _recycle(self):
        """回收已播放完的缓冲区"""
        alive = []
        for (hdr, buf) in self._pending:
            if hdr.dwFlags & WHDR_DONE:
                try:
                    _winmm.waveOutUnprepareHeader(self.handle,
                                                  ctypes.byref(hdr),
                                                  ctypes.sizeof(hdr))
                except Exception:
                    pass
            else:
                alive.append((hdr, buf))
        self._pending = alive

    def write(self, pcm: bytes):
        if not self.ok or not pcm:
            return False
        try:
            with self._lock:
                self._recycle()
                # 缓冲区过多说明播放端跟不上，丢弃最旧的避免内存堆积
                while len(self._pending) > 12:
                    hdr, buf = self._pending.pop(0)
                    try:
                        _winmm.waveOutUnprepareHeader(self.handle,
                                                      ctypes.byref(hdr),
                                                      ctypes.sizeof(hdr))
                    except Exception:
                        pass
                buf = ctypes.create_string_buffer(pcm, len(pcm))
                hdr = WAVEHDR()
                hdr.lpData = ctypes.cast(buf, ctypes.c_void_p)
                hdr.dwBufferLength = len(pcm)
                if _winmm.waveOutPrepareHeader(self.handle, ctypes.byref(hdr),
                                               ctypes.sizeof(hdr)) != 0:
                    return False
                if _winmm.waveOutWrite(self.handle, ctypes.byref(hdr),
                                       ctypes.sizeof(hdr)) != 0:
                    return False
                self._pending.append((hdr, buf))
            return True
        except Exception:
            return False

    def close(self):
        if not self.ok:
            return
        self.ok = False
        try:
            _winmm.waveOutReset(self.handle)
        except Exception:
            pass
        with self._lock:
            for (hdr, buf) in self._pending:
                try:
                    _winmm.waveOutUnprepareHeader(self.handle,
                                                  ctypes.byref(hdr),
                                                  ctypes.sizeof(hdr))
                except Exception:
                    pass
            self._pending = []
        try:
            _winmm.waveOutClose(self.handle)
        except Exception:
            pass

    def error(self):
        return self._error


# ==================== 接收会话 ====================

class MicSession:
    """一次"手机麦克风"录音会话"""

    def __init__(self, rate=16000, channels=1, bits=16,
                 save_dir=None, output_device=None):
        self.rate = int(rate or 16000)
        self.channels = int(channels or 1)
        self.bits = int(bits or 16)
        self.output_device = output_device

        self.started_at = datetime.now()
        self.bytes_received = 0
        self.frames = 0
        self.playing = False
        self.player = None
        self._lock = threading.Lock()
        self._error = ""

    def open(self):
        """只做实时播放：声音不落盘"""
        if self.output_device is not None:
            idx = self.output_device
            if isinstance(idx, str):
                idx = find_device_index(idx)
            if idx is None:
                self._error = "找不到指定的输出设备"
            else:
                p = _WaveOutPlayer(idx, self.rate, self.channels, self.bits)
                if p.ok:
                    self.player = p
                    self.playing = True
                else:
                    self._error = p.error()
        return True

    def feed(self, pcm):
        """喂入一段 PCM 数据，直接送去声卡"""
        if not pcm:
            return
        with self._lock:
            self.bytes_received += len(pcm)
            self.frames += len(pcm) // max(1, self.channels * self.bits // 8)
            if self.player:
                self.player.write(pcm)

    def close(self):
        with self._lock:
            if self.player:
                self.player.close()
                self.player = None
                self.playing = False
        return self.summary()

    def audio_seconds(self):
        """按已收到的音频字节数换算时长（不能用墙上时间：网络抖动会造成误判）"""
        bps = max(1, self.rate * self.channels * (self.bits // 8))
        return round(self.bytes_received / float(bps), 1)

    def summary(self):
        return {
            "seconds": self.audio_seconds(),
            "bytes": self.bytes_received,
            "playing": self.playing,
            "error": self._error,
        }


class MicEngine:
    """
    手机麦克风总管：管理会话、提供设备列表。
    LinkService 收到 mic_start / 二进制帧 / mic_stop 时调用它。
    """

    def __init__(self, progress_callback=None):
        self.progress_callback = progress_callback
        self.session = None
        self.history = []           # 完成的录音记录
        self.output_device = "auto"  # "auto" / None(仅保存) / 设备名 / 索引
        self.route = {"kind": "none", "render": None, "mic": "", "note": ""}
        self.route = detect_mic_route()

    def devices(self):
        return list_output_devices()

    def mic_targets(self):
        """其它软件可以选来当麦克风的设备名（供界面提示）"""
        return [d["name"] for d in list_input_devices()]

    def refresh_route(self):
        self.route = detect_mic_route()
        return self.route

    def set_output_device(self, device):
        """
        device 取值：
          "auto"  自动选最佳虚拟麦克风通路（默认）
          None    不播放，仅保存 WAV
          名称/索引 指定输出设备
        """
        self.output_device = device
        if device == "auto":
            return True, self.refresh_route()["note"]
        if device is None:
            return True, "不播放，仅保存 WAV"
        return True, "已设置输出设备：%s" % device

    def _resolve_render(self):
        """把 output_device 解析成 waveOut 设备索引；None 表示不播放"""
        dev = self.output_device
        if dev is None:
            return None
        if dev == "auto":
            if not self.route or self.route.get("kind") == "none":
                self.refresh_route()
            r = (self.route or {}).get("render")
            return r["index"] if r else None
        if isinstance(dev, str):
            return find_device_index(dev)
        return int(dev)

    def start(self, rate=16000, channels=1, bits=16):
        if self.session is not None:
            self.stop()
        idx = self._resolve_render()
        s = MicSession(rate=rate, channels=channels, bits=bits,
                       output_device=idx)
        if not s.open():
            return False, s._error
        self.session = s
        msg = "手机麦克风已接入"
        if s.playing:
            target = ""
            if self.output_device == "auto":
                target = (self.route or {}).get("mic", "")
            if target:
                msg += "：其它软件把麦克风选成「%s」即可" % target
            else:
                msg += "：声音实时输出到声卡"
        elif s._error:
            msg += "（%s）" % s._error
        else:
            note = (self.route or {}).get("note", "")
            msg += "；但声音没有输出" + ("：" + note if note else "")
        return True, msg

    def feed(self, pcm):
        if self.session is None:
            return False
        self.session.feed(pcm)
        return True

    def stop(self):
        if self.session is None:
            return False, "当前没有进行中的麦克风会话"
        info = self.session.close()
        self.history.append(info)
        if len(self.history) > 100:
            self.history = self.history[-100:]
        self.session = None
        return True, "麦克风已断开，共 %.1f 秒" % info["seconds"]

    def is_active(self):
        return self.session is not None

    def status(self):
        if self.session is None:
            return {"active": False, "devices": self.devices(),
                    "route": self.route, "mic_targets": self.mic_targets()}
        s = self.session
        return {
            "active": True,
            "seconds": s.audio_seconds(),
            "bytes": s.bytes_received,
            "playing": s.playing,
            "rate": s.rate,
            "devices": self.devices(),
            "route": self.route,
            "mic_targets": self.mic_targets(),
            "error": s._error,
        }
