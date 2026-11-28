# -*- coding: utf-8 -*-
"""
PC ↔ 手机 传输页面

手机上不用装任何 App：电脑起一个局域网服务，手机浏览器扫码或输入地址即可。
支持：手机上传文件到电脑、从电脑下载文件、双向发送文字。
"""

import os
import re
import shutil
import socket
import struct
import ctypes
import threading
import urllib.request
import urllib.parse
import tkinter as tk
from ctypes import wintypes
from tkinter import filedialog, ttk

from file_transfer import TransferEngine, get_local_ips, safe_filename
from upnp_nat import map_port, unmap_port, UpnpError


def fetch_public_ip(timeout=4):
    """尽力探测本机公网出口 IP；失败返回空串（不阻塞主流程）"""
    for url in ("https://api.ipify.org", "https://ifconfig.me/ip", "http://ip.3322.net"):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                ip = r.read(64).decode("utf-8", "ignore").strip()
            if re.match(r"^\d{1,3}(\.\d{1,3}){3}$", ip):
                return ip
        except Exception:
            continue
    return ""


def local_ipv6_addresses():
    """列出本机非链路本地 IPv6（用于跨网络直连）"""
    out = []
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET6):
            ip = info[4][0].split("%")[0]
            if ip.startswith("fe80") or ip in ("::1",):
                continue
            if ip not in out:
                out.append(ip)
    except Exception:
        pass
    return out


# 全局共享的传输引擎：让服务在页面切换后继续运行，
# 否则用户一旦离开本页再回来，服务就断了，手机那边会突然失效。
_shared_engine = None
_shared_link = None


def get_shared_engine():
    global _shared_engine
    if _shared_engine is None:
        _shared_engine = TransferEngine()
    return _shared_engine


def get_shared_link():
    """
    获取全局协同服务（WebSocket + UDP 发现 + 剪贴板同步）。
    「设备协同」页与「手机互传」页共用同一份，避免重复占端口。
    """
    global _shared_link
    if _shared_link is None:
        import link_service
        engine = get_shared_engine()
        _shared_link = link_service.LinkService(transfer_engine=engine)
        engine.link = _shared_link
    return _shared_link


def copy_files_to_clipboard(paths):
    """
    把文件本体放进 Windows 剪贴板（CF_HDROP），效果等同资源管理器里 Ctrl+C：
    到别的文件夹/微信/邮件里可以直接 Ctrl+V 粘贴出这些文件。
    注意：文本复制用 tkinter 的 clipboard_append，这里必须走 Win32，
    因为 tkinter 只支持文本，不支持文件列表。
    """
    paths = [os.path.abspath(p) for p in (paths or []) if p and os.path.exists(p)]
    if not paths:
        return False

    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32

    user32.OpenClipboard.argtypes = [wintypes.HWND]
    user32.OpenClipboard.restype = wintypes.BOOL
    user32.EmptyClipboard.restype = wintypes.BOOL
    user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
    user32.SetClipboardData.restype = wintypes.HANDLE
    user32.CloseClipboard.restype = wintypes.BOOL
    kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
    kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
    kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalFree.argtypes = [wintypes.HGLOBAL]

    GMEM_MOVEABLE = 0x0002
    CF_HDROP = 15

    # DROPFILES 头（20 字节）+ 以两个 \0 结尾的 UTF-16LE 路径列表
    file_list = "".join(p + "\x00" for p in paths) + "\x00"
    header = struct.pack("<IiiII", 20, 0, 0, 0, 1)   # pFiles, pt, fNC, fWide=True
    blob = header + file_list.encode("utf-16-le")

    if not user32.OpenClipboard(None):
        return False
    try:
        user32.EmptyClipboard()
        handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(blob))
        if not handle:
            return False
        ptr = kernel32.GlobalLock(handle)
        if not ptr:
            kernel32.GlobalFree(handle)
            return False
        ctypes.memmove(ptr, blob, len(blob))
        kernel32.GlobalUnlock(handle)
        if not user32.SetClipboardData(CF_HDROP, handle):
            kernel32.GlobalFree(handle)
            return False
        return True          # 成功后句柄归系统所有，不能自己释放
    finally:
        user32.CloseClipboard()


def stop_shared_engine():
    """供主程序在退出时调用，确保端口被释放"""
    global _shared_engine, _shared_link
    if _shared_link is not None:
        try:
            _shared_link.stop()
        except Exception:
            pass
        _shared_link = None
    if _shared_engine is not None:
        try:
            _shared_engine.stop()
        except Exception:
            pass
        _shared_engine = None


def build(app, parent):
    return TransferPage(app, parent).build()


class TransferPage:
    def __init__(self, app, parent):
        self.app = app
        self.parent = parent
        self.engine = get_shared_engine()
        self._refresh_job = None
        self._alive = True
        self._qr_matrix = None
        self._last_msg_count = -1

    # ==================== 界面 ====================

    def build(self):
        T = self.app.THEME

        self.app.section_title(
            self.parent, "手机互传",
            "手机不用装 App：电脑开启服务后，手机浏览器扫描二维码或输入地址，"
            "即可传文件、收发文字。仅在局域网内可用，数据不经过任何服务器。")

        # ============ 服务控制 + 二维码 ============
        top = self.app.card(self.parent, pady=(0, 10))
        inner = tk.Frame(top, bg=T.CARD_BG)
        inner.pack(fill=tk.X, padx=16, pady=14)

        # 左：二维码
        qr_holder = tk.Frame(inner, bg=T.CARD_BG)
        qr_holder.pack(side=tk.LEFT, padx=(0, 20))
        self.qr_canvas = tk.Canvas(qr_holder, width=200, height=200, bg="#ffffff",
                                   highlightthickness=1,
                                   highlightbackground=T.BORDER)
        self.qr_canvas.pack()
        self._draw_qr_placeholder()

        # 右：控制区
        right = tk.Frame(inner, bg=T.CARD_BG)
        right.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        self.status_label = tk.Label(right, text="服务未启动", bg=T.CARD_BG,
                                     fg=T.TEXT_LIGHT, font=("Microsoft YaHei", 11, "bold"),
                                     anchor=tk.W)
        self.status_label.pack(fill=tk.X, pady=(0, 6))

        self.url_label = tk.Label(right, text="点击下方按钮开启服务",
                                  bg=T.CARD_BG, fg=T.PRIMARY,
                                  font=("Consolas", 16, "bold"), anchor=tk.W)
        self.url_label.pack(fill=tk.X, pady=(0, 4))

        self.pin_label = tk.Label(right, text="", bg=T.CARD_BG, fg=T.WARNING,
                                  font=("Microsoft YaHei", 10), anchor=tk.W)
        self.pin_label.pack(fill=tk.X, pady=(0, 8))

        btn_row = tk.Frame(right, bg=T.CARD_BG)
        btn_row.pack(fill=tk.X, pady=(0, 8))
        self.start_btn = self.app.button(btn_row, "▶ 开启服务", self.toggle_service)
        self.start_btn.pack(side=tk.LEFT)
        self.app.button(btn_row, "📋 复制地址", self.copy_url, kind="ghost").pack(
            side=tk.LEFT, padx=8)
        self.app.button(btn_row, "📂 打开接收目录", self.open_folder, kind="ghost").pack(
            side=tk.LEFT)

        # App 扫码配对 / 多网卡地址（USB 网络共享、热点、蓝牙 PAN）
        app_row = tk.Frame(right, bg=T.CARD_BG)
        app_row.pack(fill=tk.X, pady=(0, 8))
        self.app.button(app_row, "📱 App 扫码配对", self.show_app_pair_qr,
                        kind="ghost").pack(side=tk.LEFT)
        self.app.button(app_row, "🌐 所有地址", self.show_addresses,
                        kind="ghost").pack(side=tk.LEFT, padx=8)

        # 设置区
        set_row = tk.Frame(right, bg=T.CARD_BG)
        set_row.pack(fill=tk.X, pady=(4, 0))

        tk.Label(set_row, text="访问码：", bg=T.CARD_BG, fg=T.TEXT,
                 font=("Microsoft YaHei", 9)).pack(side=tk.LEFT)
        self.pin_var = tk.StringVar(value="")
        tk.Entry(set_row, textvariable=self.pin_var, width=10,
                 font=("Microsoft YaHei", 9)).pack(side=tk.LEFT, padx=(0, 12))

        tk.Label(set_row, text="保存目录：", bg=T.CARD_BG, fg=T.TEXT,
                 font=("Microsoft YaHei", 9)).pack(side=tk.LEFT)
        self.dir_var = tk.StringVar(value=self.engine.save_dir)
        tk.Entry(set_row, textvariable=self.dir_var, width=34,
                 font=("Microsoft YaHei", 8)).pack(side=tk.LEFT)
        self.app.button(set_row, "浏览", self.browse_dir, kind="ghost").pack(
            side=tk.LEFT, padx=6)

        # 跨网络访问（默认关闭；开启必须设置访问码）
        wan_row = tk.Frame(right, bg=T.CARD_BG)
        wan_row.pack(fill=tk.X, pady=(6, 0))
        self.wan_var = tk.BooleanVar(value=bool(getattr(self.engine, "allow_wan", False)))
        tk.Checkbutton(
            wan_row, text="允许跨网络访问（必须设置访问码）", variable=self.wan_var,
            command=self.on_wan_toggle, bg=T.CARD_BG, fg=T.TEXT,
            activebackground=T.CARD_BG, selectcolor=T.CARD_BG,
            font=("Microsoft YaHei", 9),
        ).pack(side=tk.LEFT)
        self.app.button(wan_row, "🔌 自动映射", self.auto_map_port,
                        kind="ghost").pack(side=tk.LEFT, padx=(8, 0))
        self.app.button(wan_row, "🌐 公网地址", self.show_public_address,
                        kind="ghost").pack(side=tk.LEFT, padx=8)

        # 使用提示
        self.app.hint(
            self.parent,
            "使用步骤：① 手机和电脑连同一个 WiFi　② 手机扫左侧二维码（或手动输入地址）"
            "　③ 在网页中选择上传文件 / 发送文字。若设了访问码，手机上需输入对应数字。",
            color=T.TEXT_LIGHT)

        # ============ 会话区（像微信一样：文字和文件在同一条时间线上） ============
        chat_card = tk.Frame(self.parent, bg=T.CARD_BG, bd=0,
                             highlightbackground=T.BORDER, highlightthickness=1)
        chat_card.pack(fill=tk.BOTH, expand=True, padx=20, pady=(0, 4))

        head = tk.Frame(chat_card, bg=T.CARD_BG)
        head.pack(fill=tk.X, padx=12, pady=(8, 4))
        tk.Label(head, text="💬 和手机的消息", bg=T.CARD_BG, fg=T.TEXT,
                 font=("Microsoft YaHei", 10, "bold")).pack(side=tk.LEFT)
        tk.Label(head, text="左边是手机发来的，右边是你发的；点消息旁的「复制」即可复制",
                 bg=T.CARD_BG, fg=T.TEXT_LIGHT,
                 font=("Microsoft YaHei", 8)).pack(side=tk.LEFT, padx=10)
        self.app.button(head, "📂 接收目录", self.open_folder,
                        kind="ghost").pack(side=tk.RIGHT)

        chat_holder = tk.Frame(chat_card, bg="#f7f9fc")
        chat_holder.pack(fill=tk.BOTH, expand=True, padx=10, pady=(0, 6))
        chat_scroll = tk.Scrollbar(chat_holder)
        chat_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.chat = tk.Text(chat_holder, wrap=tk.WORD, relief=tk.FLAT, bd=0,
                            bg="#f7f9fc", fg=T.TEXT, padx=14, pady=8,
                            font=("Microsoft YaHei", 9),
                            state=tk.DISABLED, cursor="arrow",
                            yscrollcommand=chat_scroll.set)
        self.chat.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        chat_scroll.config(command=self.chat.yview)
        self._chat_file_seq = 0
        self._init_chat_tags()

        # 输入区
        bar = tk.Frame(chat_card, bg=T.CARD_BG)
        bar.pack(fill=tk.X, padx=10, pady=(0, 6))
        self.send_text = tk.Text(bar, height=3, font=("Microsoft YaHei", 9),
                                 wrap=tk.WORD, relief=tk.FLAT,
                                 highlightthickness=1,
                                 highlightbackground=T.BORDER,
                                 highlightcolor=T.PRIMARY)
        self.send_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.send_text.bind("<Control-Return>", self._on_ctrl_enter)
        btns = tk.Frame(bar, bg=T.CARD_BG)
        btns.pack(side=tk.LEFT, padx=(8, 0), fill=tk.Y)
        self.app.button(btns, "发送", self.do_send_text).pack(fill=tk.X, pady=(0, 4))
        self.app.button(btns, "📎 只发文件", self.send_files_to_phone,
                        kind="ghost").pack(fill=tk.X)

        tk.Label(chat_card,
                 text="Enter 换行 · Ctrl+Enter 发送 · 文字和文件可以分开发送；双击文件消息可打开",
                 bg=T.CARD_BG, fg=T.TEXT_LIGHT, font=("Microsoft YaHei", 8)).pack(
            anchor=tk.W, padx=12, pady=(0, 6))

        # 启动自动刷新；页面被销毁时停止定时器，避免回调越积越多
        self.parent.bind("<Destroy>", self._on_destroy, add="+")
        self._schedule_refresh()

        # 若服务已在运行（从别的页面切回来），恢复显示
        if self.engine.is_running():
            self._update_service_ui(True, self.engine.get_url())
        return self

    def _on_destroy(self, event):
        # 只处理自己的销毁事件（子控件也会冒泡上来）
        if event.widget is not self.parent:
            return
        self._alive = False
        if self._refresh_job:
            try:
                self.app.root.after_cancel(self._refresh_job)
            except Exception:
                pass
            self._refresh_job = None

    # ==================== 二维码绘制 ====================

    def _render_qr_to(self, canvas, text, size=240):
        """把任意文本画成二维码（App 扫码配对弹窗用）"""
        canvas.delete("all")
        try:
            import qrcode_gen
            matrix = qrcode_gen.make_qr_matrix(text, ecc="M", border=2)
        except Exception:
            matrix = None
        if not matrix:
            canvas.create_rectangle(0, 0, size, size, fill="#fafafa", outline="")
            canvas.create_text(size / 2, size / 2, text="二维码生成失败",
                               fill="#faad14", font=("Microsoft YaHei", 10))
            return
        n = len(matrix)
        margin = 8
        cell = (size - margin * 2) / float(n)
        canvas.create_rectangle(0, 0, size, size, fill="#ffffff", outline="")
        for r in range(n):
            row = matrix[r]
            for c in range(n):
                if row[c]:
                    x0 = margin + c * cell
                    y0 = margin + r * cell
                    canvas.create_rectangle(x0, y0, x0 + cell + 0.5,
                                            y0 + cell + 0.5,
                                            fill="#000000", outline="")

    @staticmethod
    def _pair_deeplink(ip, port, code, name):
        query = urllib.parse.urlencode({
            "ip": ip, "port": int(port), "code": str(code), "name": name or "",
        })
        return "cmlink://pair?" + query

    def show_app_pair_qr(self):
        """显示 App 深链配对二维码，支持多网卡地址切换"""
        link = self._try_link()
        if link is None or not link.is_running() or not self.engine.is_running():
            self.app.show_warning("提示", "请先开启服务，再让手机扫码配对")
            return
        ips = get_local_ips()
        if not ips:
            self.app.show_warning("提示", "未获取到本机局域网地址，请检查网络连接")
            return

        T = self.app.THEME
        port = self._listen_port()
        code = link.get_pair_code()
        name = socket.gethostname() or "电脑"

        win = tk.Toplevel(self.app.root)
        win.title("App 扫码配对")
        win.configure(bg=T.CARD_BG)
        win.transient(self.app.root)
        win.resizable(False, False)

        tk.Label(win, text="选择手机能访问的电脑地址：", bg=T.CARD_BG, fg=T.TEXT,
                 font=("Microsoft YaHei", 10)).pack(anchor=tk.W, padx=16, pady=(14, 4))
        ip_var = tk.StringVar(value=ips[0])
        combo = ttk.Combobox(win, textvariable=ip_var, values=ips, state="readonly",
                             width=32, font=("Consolas", 10))
        combo.pack(anchor=tk.W, padx=16, pady=(0, 8))

        canvas = tk.Canvas(win, width=260, height=260, bg="#ffffff",
                           highlightthickness=1, highlightbackground=T.BORDER)
        canvas.pack(padx=16, pady=(4, 8))

        info = tk.Label(win, text="", bg=T.CARD_BG, fg=T.PRIMARY,
                        font=("Microsoft YaHei", 9), justify=tk.LEFT)
        info.pack(anchor=tk.W, padx=16, pady=(0, 6))

        tk.Label(win,
                 text="用手机系统相机扫码，App 会自动填入地址并配对。\n"
                      "若相机不能直接打开 App，请在 App 里手动输入地址和配对码。\n"
                      "USB 网络共享 / 手机热点 / 蓝牙 PAN 下，请选择对应网段的地址。",
                 bg=T.CARD_BG, fg=T.TEXT_LIGHT, font=("Microsoft YaHei", 9),
                 justify=tk.LEFT).pack(anchor=tk.W, padx=16, pady=(0, 10))

        def current_link():
            return self._pair_deeplink(ip_var.get().strip(), port, code, name)

        def render(*_args):
            deep = current_link()
            self._render_qr_to(canvas, deep, 260)
            info.config(text="配对码：%s    地址：%s:%d"
                             % (code, ip_var.get().strip(), port))

        def copy_link():
            try:
                self.app.root.clipboard_clear()
                self.app.root.clipboard_append(current_link())
                self.app.set_status("已复制 App 配对链接")
            except Exception:
                pass

        combo.bind("<<ComboboxSelected>>", render)
        bottom = tk.Frame(win, bg=T.CARD_BG)
        bottom.pack(fill=tk.X, padx=16, pady=(0, 14))
        self.app.button(bottom, "复制配对链接", copy_link,
                        kind="ghost").pack(side=tk.LEFT)
        self.app.button(bottom, "关闭", win.destroy,
                        kind="ghost").pack(side=tk.RIGHT)
        render()

    def show_addresses(self):
        """列出所有网卡地址，便于 USB/热点/蓝牙 PAN 场景选择"""
        ips = get_local_ips()
        port = self._listen_port()
        if not ips:
            self.app.show_warning("提示", "未获取到本机局域网地址，请检查网络连接")
            return
        lines = ["本机监听端口：%d" % port, "", "手机能访问的地址："]
        for ip in ips:
            lines.append("  http://%s:%d" % (ip, port))
        lines += [
            "",
            "USB 网络共享 / 手机热点 / 蓝牙 PAN 建立后，这里会出现对应网段的地址。",
            "选择手机所在网段的地址，再用「App 扫码配对」生成二维码。",
            "没有 Wi-Fi 时：手机开启 USB 网络共享或热点，电脑连上后回到 App 点「自动发现」。",
        ]
        self.app.show_info("所有地址", "\n".join(lines))

    def _draw_qr_placeholder(self):
        c = self.qr_canvas
        c.delete("all")
        c.create_rectangle(0, 0, 200, 200, fill="#fafafa", outline="")
        c.create_text(100, 92, text="📱", font=("Segoe UI Emoji", 30))
        c.create_text(100, 130, text="开启服务后显示二维码",
                      fill="#bfbfbf", font=("Microsoft YaHei", 9))

    def _draw_qr(self, url):
        """生成并绘制二维码"""
        c = self.qr_canvas
        c.delete("all")

        if not url:
            self._draw_qr_placeholder()
            return

        matrix = None
        try:
            import qrcode_gen
            matrix = qrcode_gen.make_qr_matrix(url, ecc="M", border=2)
        except Exception:
            matrix = None

        if not matrix:
            # 二维码模块不可用时降级：显示醒目文字地址
            c.create_rectangle(0, 0, 200, 200, fill="#fafafa", outline="")
            c.create_text(100, 76, text="二维码不可用", fill="#faad14",
                          font=("Microsoft YaHei", 10, "bold"))
            c.create_text(100, 106, text="请在手机浏览器手动输入\n下方显示的地址",
                          fill="#8c8c8c", font=("Microsoft YaHei", 8),
                          justify=tk.CENTER)
            return

        n = len(matrix)
        if n <= 0:
            return
        size = 200
        margin = 6
        cell = (size - margin * 2) / float(n)
        c.create_rectangle(0, 0, size, size, fill="#ffffff", outline="")
        for r in range(n):
            row = matrix[r]
            for col in range(n):
                if not row[col]:
                    continue
                x0 = margin + col * cell
                y0 = margin + r * cell
                c.create_rectangle(x0, y0, x0 + cell + 0.6, y0 + cell + 0.6,
                                   fill="#000000", outline="")
        self._qr_matrix = matrix

    # ==================== 服务控制 ====================

    def toggle_service(self):
        if self.engine.is_running():
            ok, msg = self.engine.stop()
            # 传输服务和协同服务是一体的：关就一起关
            link = self._try_link()
            if link is not None and link.is_running():
                try:
                    link.stop()
                except Exception:
                    pass
            self._update_service_ui(False, "")
            self.app.set_status(msg)
            return

        # 应用访问码与目录设置
        pin = self.pin_var.get().strip()
        if pin and not pin.isdigit():
            self.app.show_warning("提示", "访问码请使用数字，方便手机输入")
            return
        self.engine.pin = pin

        target_dir = self.dir_var.get().strip()
        if target_dir and os.path.normcase(target_dir) != os.path.normcase(self.engine.save_dir):
            ok, res = self.engine.set_save_dir(target_dir)
            if not ok:
                self.app.show_error("目录无效", res)
                return

        ok, msg = self.engine.start()
        if not ok:
            self.app.show_error("启动失败", msg)
            return

        # 同时拉起协同服务（WebSocket / UDP 发现 / 配对），
        # 否则手机 App 只能访问网页接口，连不上实时通道。
        link = self._try_link()
        if link is not None and not link.is_running():
            ok2, msg2 = link.start()
            if not ok2:
                self.app.set_status("文件服务已启动，但协同服务失败：" + msg2)

        url = self.engine.get_url()
        self._update_service_ui(True, url)
        self.app.set_status(msg)

    def _try_link(self):
        """拿到共享的协同服务实例；audio/协议模块异常时不拖垮传输服务"""
        try:
            return get_shared_link()
        except Exception:
            return None

    def _update_service_ui(self, running, url):
        T = self.app.THEME
        if running:
            self.status_label.config(text="✓ 服务运行中，请用手机扫码或输入地址",
                                     fg=T.ACCENT)
            self.url_label.config(text=url or "未获取到局域网地址", fg=T.PRIMARY)
            self.start_btn.config(text="⏹ 停止服务", bg=T.DANGER, fg="white")
            if self.engine.pin:
                self.pin_label.config(text=f"访问码：{self.engine.pin}")
            else:
                self.pin_label.config(text="未设置访问码（同一局域网内可直接访问）")
            self._draw_qr(url)
        else:
            self.status_label.config(text="服务未启动", fg=T.TEXT_LIGHT)
            self.url_label.config(text="点击下方按钮开启服务", fg=T.TEXT_LIGHT)
            self.start_btn.config(text="▶ 开启服务", bg=T.PRIMARY, fg="white")
            self.pin_label.config(text="")
            self._draw_qr_placeholder()

    def copy_url(self):
        url = self.engine.get_url() if self.engine.is_running() else ""
        if not url:
            self.app.show_warning("提示", "请先开启服务")
            return
        try:
            self.app.root.clipboard_clear()
            self.app.root.clipboard_append(url)
            self.app.set_status(f"已复制：{url}")
        except Exception:
            pass

    def open_folder(self):
        try:
            os.makedirs(self.engine.save_dir, exist_ok=True)
            os.startfile(self.engine.save_dir)
        except Exception as e:
            self.app.show_error("打开失败", str(e))

    def browse_dir(self):
        from tkinter import filedialog
        d = filedialog.askdirectory(title="选择接收文件的保存目录",
                                    initialdir=self.engine.save_dir)
        if not d:
            return
        # 立即应用并持久化：否则切页/重开后会回到默认目录
        ok, res = self.engine.set_save_dir(d)
        if ok:
            self.dir_var.set(self.engine.save_dir)
            self.app.set_status("接收目录已改为：" + self.engine.save_dir)
        else:
            self.app.show_error("目录无效", res)

    # ---------- 跨网络访问 ----------

    def on_wan_toggle(self):
        enabled = bool(self.wan_var.get())
        if enabled:
            pin = self.pin_var.get().strip()
            if not pin.isdigit() or len(pin) < 4:
                self.app.show_warning(
                    "需要访问码",
                    "跨网络访问必须设置 4 位以上「数字访问码」，\n"
                    "否则互联网上任何人都能访问这台电脑。\n\n"
                    "请先在上方「访问码」里填写，再回来勾选。")
                self.wan_var.set(False)
                return
            port = self._listen_port()
            if not self.app.ask_yes_no(
                    "开放公网访问",
                    "开启后，知道公网地址的人可以访问传输服务；\n"
                    "读写文件和文字仍需正确访问码或配对 token。\n\n"
                    "请确认：\n"
                    "① 路由器已把 TCP %d 映射到本机（可用下方「自动映射」一键完成）\n"
                    "② 只在可信网络下使用\n\n继续吗？" % port):
                self.wan_var.set(False)
                return
            # 立即生效，避免服务已启动时还沿用旧访问码
            self.engine.pin = pin
        else:
            # 关闭跨网络时顺手撤销之前的自动映射，避免路由器上留着一个开放端口
            port = self._listen_port()
            threading.Thread(target=unmap_port, args=(port,), daemon=True).start()
        self.engine.allow_wan = enabled
        self.app.set_status("已" + ("开启" if enabled else "关闭") + "跨网络访问")
        self._update_service_ui(self.engine.is_running(), self.engine.get_url())

    def _listen_port(self):
        try:
            if self.engine._server is not None:
                return self.engine._server.server_address[1]
        except Exception:
            pass
        return self.engine.port or 8080

    def auto_map_port(self):
        """UPnP 一键把外网端口映射到本机（失败时给出替代方案）"""
        pin = self.pin_var.get().strip()
        if not pin.isdigit() or len(pin) < 4:
            self.app.show_warning(
                "需要访问码",
                "自动映射会把服务暴露到公网，必须先设置 4 位以上「数字访问码」。\n\n"
                "请先在上方「访问码」里填写，再点「自动映射」。")
            return
        port = self._listen_port()

        def work(_progress):
            try:
                return map_port(port)
            except UpnpError as e:
                return {"error": str(e)}
            except Exception as e:
                return {"error": "未预期错误：%s" % e}

        def done(result):
            result = result or {}
            if result.get("error"):
                self.app.show_warning(
                    "自动映射失败",
                    "%s\n\n可以改用下面的方式：\n"
                    "① 手动在路由器里把 TCP %d 映射到本机\n"
                    "② 两端都有 IPv6 时用 IPv6 地址直连\n"
                    "③ ZeroTier / Tailscale 组网后按局域网地址连接"
                    % (result["error"], port))
                return
            public_ip = result.get("public_ip") or ""
            local_ip = result.get("local_ip") or ""
            lines = ["路由器已把外网 TCP %d 转发到本机 %s。"
                     % (port, local_ip or "?"), ""]
            if public_ip and not public_ip.startswith(("100.", "0.0.0.0")):
                lines += [
                    "外网访问地址：",
                    "  http://%s:%d" % (public_ip, port),
                    "",
                    "手机 App 里手动填这个地址即可；网页访问需输入访问码。",
                ]
            elif public_ip:
                lines += [
                    "网关报告的外网 IP 是 %s，这属于运营商大内网（CGNAT），" % public_ip,
                    "端口映射对它无效。建议改用 ZeroTier / Tailscale 组网，",
                    "或两端走 IPv6 直连（可点「公网地址」查看 IPv6）。",
                ]
            else:
                lines += [
                    "映射已建立，但读取不到外网 IP。",
                    "可点「公网地址」查看本机公网 IP；若它与路由器 WAN 口",
                    "地址不一致，说明是运营商大内网。",
                ]
            lines += [
                "",
                "当前「允许跨网络访问」：%s" % ("已开启" if self.engine.allow_wan else "未开启"),
                "关闭跨网络访问时会自动撤销这条映射。",
            ]
            self.app.show_info("自动映射结果", "\n".join(lines))

        self.app.set_status("正在向路由器申请端口映射…")
        self.app.run_async(work, on_done=done)

    def show_public_address(self):
        port = self._listen_port()
        locals_ = get_local_ips()
        v6 = local_ipv6_addresses()

        def done(ip):
            lines = ["本机监听端口：%d" % port, "", "局域网地址（同一 WiFi 可用）："]
            if locals_:
                for a in locals_:
                    lines.append("  http://%s:%d" % (a, port))
            else:
                lines.append("  未获取到局域网 IP")
            lines += ["", "公网地址（需要路由器端口映射）："]
            if ip:
                lines.append("  http://%s:%d" % (ip, port))
            else:
                lines.append("  获取失败（可能没有公网 IP，或被网络拦截）")
            if v6:
                lines += ["", "IPv6 地址（两端都有 IPv6 时可直连）："]
                for a in v6:
                    lines.append("  http://[%s]:%d" % (a, port))
            lines += [
                "",
                "使用前提：",
                "  1. 勾选「允许跨网络访问」并设置数字访问码",
                "  2. 点「🔌 自动映射」让路由器自动转发（或手动把 TCP %d 映射到本机）" % port,
                "  3. 手机 App 里手动输入公网地址（或浏览器打开）",
                "",
                "没有公网 IP 时：可用 ZeroTier / Tailscale 等组网工具，",
                "把电脑和手机放进同一个虚拟局域网，再按局域网地址连接。",
            ]
            self.app.show_info("公网地址", "\n".join(lines))

        self.app.set_status("正在检测公网 IP…")
        self.app.run_async(fetch_public_ip, on_done=done)

    # ==================== 记录刷新 ====================

    def _schedule_refresh(self):
        if not self._alive:
            return
        try:
            self._refresh_records()
        except Exception:
            pass
        self._refresh_job = self.app.root.after(1500, self._schedule_refresh)

    def _refresh_records(self):
        msgs = self.engine.get_messages(limit=300)
        if len(msgs) != self._last_msg_count:
            self._last_msg_count = len(msgs)
            self._render_chat(msgs)

    # ---------- 会话气泡 ----------

    def _init_chat_tags(self):
        self.chat.tag_configure("meta_l", justify=tk.LEFT, foreground="#8c8c8c",
                                font=("Microsoft YaHei", 8), spacing1=8)
        self.chat.tag_configure("meta_r", justify=tk.RIGHT, foreground="#8c8c8c",
                                font=("Microsoft YaHei", 8), spacing1=8)
        self.chat.tag_configure("copy", foreground="#1890ff", underline=True,
                                font=("Microsoft YaHei", 8))
        self.chat.tag_configure("bubble_l", justify=tk.LEFT, background="#ffffff",
                                lmargin1=6, lmargin2=6, rmargin=180,
                                spacing1=2, spacing3=6)
        self.chat.tag_configure("bubble_r", justify=tk.RIGHT, background="#d6e9ff",
                                lmargin1=180, lmargin2=180, rmargin=6,
                                spacing1=2, spacing3=6)
        self.chat.tag_configure("file_l", justify=tk.LEFT, background="#eef6ff",
                                lmargin1=6, lmargin2=6, rmargin=180,
                                spacing1=2, spacing3=6, foreground="#0958d9")
        self.chat.tag_configure("file_r", justify=tk.RIGHT, background="#c7e0ff",
                                lmargin1=180, lmargin2=180, rmargin=6,
                                spacing1=2, spacing3=6, foreground="#003a8c")

    def _render_chat(self, messages):
        if not hasattr(self, "chat"):
            return
        try:
            at_bottom = self.chat.yview()[1] >= 0.999
        except Exception:
            at_bottom = True
        self.chat.configure(state=tk.NORMAL)
        self.chat.delete("1.0", tk.END)
        self._chat_copy_map = {}
        if not messages:
            self.chat.insert(tk.END,
                             "还没有消息。\n在下面输入文字，或点「📎 只发文件」发给手机。",
                             "meta_l")
        for idx, m in enumerate(messages):
            mine = m.get("dir") == "pc->phone"
            who = "我 · " if mine else ("%s · " % (m.get("device") or "手机"))
            meta = who + m.get("time", "")
            align = "meta_r" if mine else "meta_l"
            self.chat.insert(tk.END, meta, align)

            # 文字：复制正文；文件：复制文件本体 + 另存为
            if m.get("kind") == "file":
                ctag = "cp%d" % idx
                self.chat.insert(tk.END, "   [复制文件]", (align, "copy", ctag))
                self._bind_click(ctag, lambda mm=m: self._copy_file_message(mm))
                stag = "sv%d" % idx
                self.chat.insert(tk.END, "  [另存为]", (align, "copy", stag))
                self._bind_click(stag, lambda mm=m: self.save_message_file(mm))
            else:
                ctag = "cp%d" % idx
                self._chat_copy_map[ctag] = m.get("text", "")
                self.chat.insert(tk.END, "   [复制]", (align, "copy", ctag))
                self._bind_click(ctag, lambda c=ctag: self._copy_message(c))
            self.chat.insert(tk.END, "\n", align)

            if m.get("kind") == "text":
                self.chat.insert(tk.END, (m.get("text") or "") + "\n",
                                 "bubble_r" if mine else "bubble_l")
            else:
                label_text = "📎 %s\n%s" % (m.get("name", ""), m.get("size_text", ""))
                start = self.chat.index(tk.INSERT)
                self.chat.insert(tk.END, label_text + "\n",
                                 "file_r" if mine else "file_l")
                end = self.chat.index(tk.INSERT)
                path = m.get("path", "")
                if path:
                    tag = "f%d" % self._chat_file_seq
                    self._chat_file_seq += 1
                    self.chat.tag_add(tag, start, end)
                    self.chat.tag_bind(
                        tag, "<Double-1>",
                        lambda _e, p=path: self._open_chat_file(p))
        self.chat.configure(state=tk.DISABLED)
        if at_bottom:
            try:
                self.chat.see(tk.END)
            except Exception:
                pass

    def _bind_click(self, tag, handler):
        """给会话里的文字链接触发点击事件，并处理手型光标"""
        self.chat.tag_bind(tag, "<Button-1>", lambda _e: handler())
        self.chat.tag_bind(tag, "<Enter>",
                           lambda _e: self.chat.config(cursor="hand2"))
        self.chat.tag_bind(tag, "<Leave>",
                           lambda _e: self.chat.config(cursor="arrow"))

    def _copy_message(self, tag):
        """点击文字消息旁的「复制」：复制正文"""
        payload = (getattr(self, "_chat_copy_map", {}) or {}).get(tag, "")
        if not payload:
            return
        try:
            self.app.root.clipboard_clear()
            self.app.root.clipboard_append(payload)
            self.app.set_status("已复制文字到剪贴板")
        except Exception:
            pass

    def _copy_file_message(self, m):
        """点击文件消息旁的「复制文件」：复制文件本体（可到其它地方 Ctrl+V）"""
        path = m.get("path", "")
        if not path or not os.path.exists(path):
            self.app.show_warning("提示", "文件不存在，无法复制（可能已被移动或删除）")
            return
        if copy_files_to_clipboard([path]):
            self.app.set_status("已复制文件，可直接在文件夹/聊天窗口里粘贴")
        else:
            self.app.show_error("复制失败", "无法写入剪贴板，可能被其它程序占用，请重试")

    def save_message_file(self, m):
        """点击「另存为」：把文件另存到用户选择的位置"""
        src = m.get("path", "")
        if not src or not os.path.exists(src):
            self.app.show_warning("提示", "文件不存在，无法另存为")
            return
        name = os.path.basename(src)
        dst = filedialog.asksaveasfilename(
            title="另存为", initialfile=name,
            defaultextension=os.path.splitext(name)[1])
        if not dst:
            return

        def worker():
            try:
                shutil.copy2(src, dst)
            except Exception as e:
                err = str(e)
                self.app.root.after(0, lambda: self.app.show_error("另存为失败", err))
                return
            self.app.root.after(0, lambda: self.app.set_status("已另存为：" + dst))

        threading.Thread(target=worker, daemon=True).start()

    def _open_chat_file(self, path):
        if not path or not os.path.exists(path):
            self.app.show_warning("提示", "文件不存在（可能已被移动或删除）")
            return
        try:
            os.startfile(path)
        except Exception as e:
            self.app.show_error("打开失败", str(e))

    def _on_ctrl_enter(self, _event=None):
        self.do_send_text()
        return "break"

    # ---------- 电脑发文件给手机 ----------

    def send_files_to_phone(self):
        """
        选文件 → 复制进共享目录 → 通过 WebSocket 通知手机。
        手机端收到 {"type":"file"} 会立刻提示，并在「电脑文件」列表里出现。
        """
        paths = filedialog.askopenfilenames(title="选择要发送到手机的文件")
        if not paths:
            return
        if not self.engine.is_running():
            self.app.show_warning("提示", "请先开启服务")
            return

        def worker():
            sent = []
            failed = []
            for src in paths:
                try:
                    name = safe_filename(os.path.basename(src))
                    dst = self._unique_target(name)
                    shutil.copy2(src, dst)
                    size = os.path.getsize(dst)
                    sent.append((os.path.basename(dst), size, dst))
                    self.engine.record_sent_file(os.path.basename(dst), size, dst)
                except Exception as e:
                    failed.append("%s（%s）" % (os.path.basename(src), e))
            link = self._try_link()
            for name, size, _path in sent:
                if link is not None:
                    try:
                        link.push_file(name, size)
                    except Exception:
                        pass

            def report():
                if sent:
                    self._last_msg_count = -1      # 立刻重绘会话
                    self._refresh_records()
                    self.app.set_status(
                        "已发送 %d 个文件到共享目录，手机端会收到提示" % len(sent))
                if failed:
                    self.app.show_error("部分文件发送失败", "\n".join(failed))
            try:
                self.app.root.after(0, report)
            except Exception:
                pass

        threading.Thread(target=worker, daemon=True).start()

    def _unique_target(self, name):
        """同名文件自动加序号，避免覆盖之前发给手机的文件"""
        save_dir = self.engine.save_dir
        base, ext = os.path.splitext(name)
        target = os.path.join(save_dir, name)
        i = 1
        while os.path.exists(target):
            target = os.path.join(save_dir, "%s (%d)%s" % (base, i, ext))
            i += 1
        return target

    def do_send_text(self):
        text = self.send_text.get("1.0", tk.END).strip()
        if not text:
            self.app.show_warning("提示", "请输入要发送的文字")
            return
        ok, msg = self.engine.add_outgoing_text(text)
        if ok:
            self.send_text.delete("1.0", tk.END)
            self.app.set_status("已发送")
            self._last_msg_count = -1  # 立刻重绘会话
            self._refresh_records()
        else:
            self.app.show_warning("提示", msg)

    # ---------- 电脑发文件给手机 ----------
