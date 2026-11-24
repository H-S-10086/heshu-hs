# -*- coding: utf-8 -*-
"""
弹窗拦截引擎
用 ctypes 调用 Win32 API 枚举顶层窗口，按用户维护的规则匹配并关闭广告弹窗。

设计原则（重要）：
  - 默认工作在"仅记录"模式，不会自动关闭任何窗口，避免误关用户正在使用的程序
  - 规则完全由用户维护，默认只给出少量示例规则且默认关闭
  - 所有操作都有日志，用户可清楚看到"拦截了什么、为什么拦截"
  - 只对"可见的顶层窗口"发 WM_CLOSE（温和关闭，等价于点右上角×），不强制杀进程
"""

import os
import json
import time
import ctypes
import threading
from ctypes import wintypes
from datetime import datetime

from scanner import ScanResult


# ==================== Win32 接口 ====================

_user32 = ctypes.windll.user32
_kernel32 = ctypes.windll.kernel32

EnumWindowsProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

WM_CLOSE = 0x0010

# 默认规则：仅示例，默认全部禁用，避免误伤
DEFAULT_RULES = [
    {"keyword": "今日热点", "match": "title", "enabled": False,
     "note": "示例：不少国产软件用「今日热点」作为广告弹窗标题"},
    {"keyword": "游戏中心", "match": "title", "enabled": False,
     "note": "示例：捆绑安装的游戏推广窗口"},
    {"keyword": "软件推广", "match": "title", "enabled": False,
     "note": "示例：第三方软件推广窗口"},
    {"keyword": "MiniNews", "match": "class", "enabled": False,
     "note": "示例：部分资讯类弹窗的窗口类名"},
    {"keyword": "TXNews", "match": "class", "enabled": False,
     "note": "示例：资讯弹窗类名"},
]


def _get_window_title(hwnd):
    """读取窗口标题"""
    length = _user32.GetWindowTextLengthW(hwnd)
    if length <= 0:
        return ""
    buf = ctypes.create_unicode_buffer(length + 2)
    _user32.GetWindowTextW(hwnd, buf, length + 2)
    return buf.value or ""


def _get_window_class(hwnd):
    """读取窗口类名"""
    buf = ctypes.create_unicode_buffer(256)
    _user32.GetClassNameW(hwnd, buf, 256)
    return buf.value or ""


def _get_window_pid(hwnd):
    """获取窗口所属进程 ID"""
    pid = wintypes.DWORD()
    _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value


def _process_name(pid):
    """根据 PID 取进程名（失败返回空串）"""
    try:
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        handle = _kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return ""
        try:
            buf = ctypes.create_unicode_buffer(512)
            size = wintypes.DWORD(512)
            if _kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
                return os.path.basename(buf.value)
        finally:
            _kernel32.CloseHandle(handle)
    except Exception:
        pass
    return ""


class PopupBlocker:
    """弹窗拦截：规则维护 + 窗口枚举 + 日志"""

    def __init__(self, log_callback=None, state_callback=None):
        """
        log_callback: 有新拦截记录时回调，参数为一条日志 dict
        state_callback: 状态变化回调（如"已启动"/"已停止"）
        """
        self.log_callback = log_callback
        self.state_callback = state_callback
        self.rules = []
        self.logs = []
        self.blocked_count = 0
        self._thread = None
        self._stop_event = threading.Event()
        self._running = False
        self.mode = "record"       # record = 仅记录；close = 自动关闭
        self._interval = 2.0
        self.load_rules()

    # ---------- 规则持久化 ----------

    def _rules_file(self):
        base = os.path.join(os.environ.get("USERPROFILE", ""), "CleanMasterBackup")
        try:
            os.makedirs(base, exist_ok=True)
        except Exception:
            base = os.environ.get("TEMP", "")
        return os.path.join(base, "popup_rules.json")

    def load_rules(self):
        """加载规则；文件不存在时使用默认示例规则"""
        path = self._rules_file()
        self.rules = []
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, list):
                    self.rules = [r for r in data if isinstance(r, dict) and r.get("keyword")]
                    return
            except Exception:
                pass
        self.rules = [dict(r) for r in DEFAULT_RULES]

    def save_rules(self):
        """保存规则"""
        try:
            with open(self._rules_file(), "w", encoding="utf-8") as f:
                json.dump(self.rules, f, ensure_ascii=False, indent=2)
            return True, "规则已保存"
        except Exception as e:
            return False, f"规则保存失败：{e}"

    def get_rules(self):
        """返回规则列表副本"""
        return [dict(r) for r in self.rules]

    def add_rule(self, keyword, match="title", note=""):
        """新增规则"""
        keyword = (keyword or "").strip()
        if not keyword:
            return False, "关键词不能为空"
        if any(r["keyword"].lower() == keyword.lower() for r in self.rules):
            return False, "该关键词已存在"
        self.rules.append({"keyword": keyword, "match": match,
                           "enabled": True, "note": note or "用户添加"})
        self.save_rules()
        return True, f"已添加规则：{keyword}"

    def remove_rule(self, keyword):
        """删除规则"""
        before = len(self.rules)
        self.rules = [r for r in self.rules if r["keyword"] != keyword]
        if len(self.rules) == before:
            return False, "未找到该规则"
        self.save_rules()
        return True, f"已删除规则：{keyword}"

    def set_rule_enabled(self, keyword, enabled):
        """启用/禁用规则"""
        for r in self.rules:
            if r["keyword"] == keyword:
                r["enabled"] = bool(enabled)
                self.save_rules()
                return True, "已更新规则状态"
        return False, "未找到该规则"

    # ---------- 窗口枚举 ----------

    def enum_windows(self, skip_own_title="合枢 HS"):
        """
        枚举当前所有可见顶层窗口。
        返回 [{"hwnd","title","class","pid","process"}]
        """
        windows = []

        def _callback(hwnd, _lparam):
            try:
                if not _user32.IsWindowVisible(hwnd):
                    return True
                title = _get_window_title(hwnd)
                if not title:
                    return True
                if skip_own_title and skip_own_title in title:
                    return True
                cls = _get_window_class(hwnd)
                # 跳过系统外壳窗口
                if cls in ("Progman", "WorkerW", "Shell_TrayWnd", "Button",
                           "TaskListThumbnailWnd", "Windows.UI.Core.CoreWindow"):
                    return True
                pid = _get_window_pid(hwnd)
                windows.append({
                    "hwnd": hwnd,
                    "title": title,
                    "class": cls,
                    "pid": pid,
                    "process": _process_name(pid),
                })
            except Exception:
                pass
            return True

        try:
            _user32.EnumWindows(EnumWindowsProc(_callback), 0)
        except Exception:
            pass
        return windows

    def match_window(self, win):
        """
        判断窗口是否命中启用中的规则。
        返回命中的规则 dict，或 None。
        """
        title = (win.get("title") or "").lower()
        cls = (win.get("class") or "").lower()
        for rule in self.rules:
            if not rule.get("enabled"):
                continue
            kw = str(rule.get("keyword", "")).lower()
            if not kw:
                continue
            m = rule.get("match", "title")
            if m == "title" and kw in title:
                return rule
            if m == "class" and kw in cls:
                return rule
            if m == "both" and (kw in title or kw in cls):
                return rule
        return None

    def close_window(self, hwnd):
        """向窗口发送 WM_CLOSE（温和关闭，等价于点×）"""
        try:
            return bool(_user32.PostMessageW(hwnd, WM_CLOSE, 0, 0))
        except Exception:
            return False

    # ---------- 监控循环 ----------

    def scan_once(self):
        """
        执行一次扫描。
        返回 {"scanned":int,"matched":[{"title","class","process","rule","action"}]}
        """
        windows = self.enum_windows()
        matched = []
        for win in windows:
            rule = self.match_window(win)
            if not rule:
                continue
            action = "已关闭" if self.mode == "close" else "仅记录"
            if self.mode == "close":
                if not self.close_window(win["hwnd"]):
                    action = "关闭失败"
                elif self.blocked_count >= 0:
                    self.blocked_count += 1
            entry = {
                "time": datetime.now().strftime("%H:%M:%S"),
                "title": win["title"],
                "class": win["class"],
                "process": win["process"],
                "rule": rule["keyword"],
                "action": action,
            }
            matched.append(entry)
            self.logs.append(entry)
            if len(self.logs) > 500:
                self.logs = self.logs[-500:]
            if self.log_callback:
                try:
                    self.log_callback(entry)
                except Exception:
                    pass
        return {"scanned": len(windows), "matched": matched}

    def _loop(self):
        while not self._stop_event.is_set():
            try:
                self.scan_once()
            except Exception:
                pass
            # 用 wait 代替 sleep，保证能及时响应停止
            self._stop_event.wait(self._interval)

    def start(self, mode=None, interval=None):
        """启动监控线程"""
        if self._running:
            return False, "监控已在运行"
        if mode:
            self.mode = mode
        if interval:
            try:
                self._interval = max(0.5, float(interval))
            except Exception:
                self._interval = 2.0
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        self._running = True
        msg = ("弹窗监控已启动（仅记录模式，不会关闭窗口）" if self.mode == "record"
               else "弹窗监控已启动（自动关闭模式）")
        if self.state_callback:
            try:
                self.state_callback(msg)
            except Exception:
                pass
        return True, msg

    def stop(self):
        """停止监控线程"""
        if not self._running:
            return False, "监控未在运行"
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5)
        self._running = False
        if self.state_callback:
            try:
                self.state_callback("弹窗监控已停止")
            except Exception:
                pass
        return True, "弹窗监控已停止"

    def is_running(self):
        """是否正在监控"""
        return self._running

    def get_logs(self, limit=100):
        """返回最近的日志"""
        return self.logs[-limit:]

    def clear_logs(self):
        """清空日志"""
        self.logs = []
        self.blocked_count = 0

    # ---------- 汇总给主程序 ----------

    def scan_popup_report(self):
        """
        生成弹窗拦截的可疑窗口清单（当前正在显示、疑似广告的窗口）。
        返回 ScanResult 列表；path 写窗口标题（无法删除，仅提示）。
        """
        results = []
        try:
            windows = self.enum_windows()
        except Exception:
            return results

        # 用一套启发式关键词做"疑似广告"判断（不是规则，仅用于提示）
        hints = ("广告", "推广", "热点", "资讯", "新闻", "抽奖", "领取", "福利",
                 "游戏", "下载器", "推荐", "弹窗")
        for win in windows:
            title = win.get("title", "")
            low = title.lower()
            if any(h in low for h in hints):
                results.append(ScanResult(
                    category="弹窗拦截",
                    source=f"疑似广告窗口（进程 {win.get('process') or '未知'}）",
                    path=title,
                    size=0,
                    description=("当前屏幕上存在标题含推广/资讯类关键词的窗口。"
                                 "可在「弹窗拦截」页把它加入规则，之后该窗口出现时会自动关闭。"
                                 f"窗口类名：{win.get('class')}"),
                ))
        return results
