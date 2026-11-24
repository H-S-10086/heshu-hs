# -*- coding: utf-8 -*-
"""
C盘清理大师 - 主程序
一款面向 Windows 系统盘空间清理与性能优化的工具。

已整合的引擎模块：
    scanner            垃圾扫描（系统垃圾 / 微信 / QQ / 隐私痕迹 / 大文件）
    cleaner            清理执行（目录清空 / 回收站 / 休眠 / 虚拟内存 / 碎片整理 / 压缩）
    diskmap            磁盘空间分析（Treemap / 目录树 / 文件类型 / 重复文件）
    sysreclaim         深层空间回收（Windows.old / WinSxS / 旧驱动 / 还原点 / 空文件夹 / 右键菜单）
    software_manager   软件管理（卸载 / 残留扫描 / 软件搬家）
    registry_cleaner   注册表清理（无效项 / 备份还原 / 还原点 / 隐私开关）
    startup_manager    启动项与服务管理（自启项 / 服务 / 计划任务 / 开机耗时）
    hardware_info      硬件信息与磁盘健康（温度 / SMART / TRIM / 电池）
    network_tools      网络工具（测速 / DNS 优选 / LSP 修复 / hosts）
    shredder           文件粉碎与空闲空间擦除
    popup_blocker      弹窗拦截
    health             一键体检评分

UI 页面模块（各自独立，通过 build(app, parent) 挂载）：
    page_diskmap / page_software / page_startup / page_registry /
    page_shredder / page_popup / page_systeminfo
"""

import os
import sys
import importlib
import threading
import tkinter as tk
from tkinter import ttk, messagebox

from scanner import ScanEngine, format_size, ScanResult
from cleaner import CleanEngine, is_admin, run_as_admin


# ==================== 可选引擎的容错导入 ====================
# 各引擎模块互相独立，任一模块不可用时不影响主程序启动

def _try_import(module_name):
    """尝试导入模块，失败时返回 None（不抛异常）"""
    try:
        return importlib.import_module(module_name)
    except Exception:
        return None


_diskmap_mod = _try_import("diskmap")
_disktrend_mod = _try_import("disktrend")
_sysreclaim_mod = _try_import("sysreclaim")
_software_mod = _try_import("software_manager")
_registry_mod = _try_import("registry_cleaner")
_startup_mod = _try_import("startup_manager")
_hardware_mod = _try_import("hardware_info")
_drivers_mod = _try_import("drivers")
_virtual_mod = _try_import("virtual_devices")
_network_mod = _try_import("network_tools")
_shredder_mod = _try_import("shredder")
_popup_mod = _try_import("popup_blocker")
_health_mod = _try_import("health")


# ==================== 颜色主题 ====================

class Theme:
    BG = "#f0f4f8"
    CARD_BG = "#ffffff"
    PRIMARY = "#1890ff"
    PRIMARY_DARK = "#096dd9"
    ACCENT = "#52c41a"
    WARNING = "#faad14"
    DANGER = "#ff4d4f"
    PURPLE = "#722ed1"
    TEXT = "#262626"
    TEXT_LIGHT = "#8c8c8c"
    SIDEBAR = "#001529"
    SIDEBAR_ITEM = "#d9d9d9"
    SIDEBAR_HOVER = "#12263f"
    SIDEBAR_ACTIVE = "#1890ff"
    BORDER = "#e8e8e8"
    GHOST_BG = "#e6f7ff"


# ==================== 侧边栏导航结构 ====================

NAV_GROUPS = [
    ("清理", [
        ("🏠  首页", "home"),
        ("⚡  一键扫描", "quick_scan"),
        ("🩺  电脑体检", "checkup"),
        ("🧹  C盘清理", "c_clean"),
        ("💾  C盘瘦身", "c_slim"),
        ("🔍  深度清理", "deep_clean"),
        ("📦  深层空间回收", "deep_reclaim"),
    ]),
    ("空间", [
        ("🗺️  磁盘空间分析", "page:page_diskmap"),
        ("♻️  重复文件", "page:page_diskmap_dup"),
        ("📈  磁盘占用趋势", "page:page_disktrend"),
        ("💬  微信专清", "wechat"),
        ("🐧  QQ专清", "qq"),
    ]),
    ("软件与系统", [
        ("📋  软件管理", "page:page_software"),
        ("🚀  启动项管理", "page:page_startup"),
        ("🗂️  注册表清理", "page:page_registry"),
        ("🗜️  软件压缩", "compress"),
    ]),
    ("磁盘维护", [
        ("💿  磁盘碎片整理", "defrag"),
        ("🔄  虚拟内存转移", "pagefile"),
        ("🧠  内存清理", "memory"),
    ]),
    ("隐私与安全", [
        ("🔥  文件粉碎", "page:page_shredder"),
        ("🔒  隐私清理", "privacy"),
        ("🛡️  弹窗拦截", "page:page_popup"),
    ]),
    ("系统", [
        ("🖥️  系统信息与网络", "page:page_systeminfo"),
        ("🎛️  虚拟设备管理", "page:page_virtual"),
        ("🔧  内核驱动服务", "page:page_drivers"),
    ]),
    ("工具", [
        ("📱  手机互传", "page:page_transfer"),
        ("🔗  设备协同", "page:page_link"),
    ]),
]


# ==================== 扫描任务定义 ====================
# 每个任务：key -> (标题, 说明, 执行函数)
# 执行函数签名：fn(progress_cb) -> list[ScanResult]

def _job_deep(progress_cb):
    """深度清理 = 常规深度扫描 + 深层空间回收"""
    engine = ScanEngine(progress_cb)
    results = engine.scan_deep()
    results = list(results)
    if _sysreclaim_mod:
        try:
            sr = _sysreclaim_mod.SysReclaimEngine(progress_cb)
            results += sr.scan_all_deep()
        except Exception:
            pass
    return results


def _job_deep_reclaim(progress_cb):
    if not _sysreclaim_mod:
        return []
    return _sysreclaim_mod.SysReclaimEngine(progress_cb).scan_all_deep()


def _job_registry(progress_cb):
    if not _registry_mod:
        return []
    return _registry_mod.RegistryEngine(progress_cb).scan_registry_junk()


def _job_software_leftover(progress_cb):
    if not _software_mod:
        return []
    return _software_mod.SoftwareManager(progress_cb).scan_software_leftovers()


def _job_startup_junk(progress_cb):
    if not _startup_mod:
        return []
    return _startup_mod.StartupEngine(progress_cb).scan_startup_junk()


def _job_hardware_issues(progress_cb):
    if not _hardware_mod:
        return []
    return _hardware_mod.HardwareEngine(progress_cb).scan_hardware_issues()


def _job_driver_issues(progress_cb):
    if not _drivers_mod:
        return []
    return _drivers_mod.DriverEngine(progress_cb).scan_driver_issues()


def _job_virtual_issues(progress_cb):
    if not _virtual_mod:
        return []
    return _virtual_mod.VirtualDeviceEngine(progress_cb).scan_virtual_device_issues()


def _job_network_issues(progress_cb):
    if not _network_mod:
        return []
    return _network_mod.NetworkEngine(progress_cb).scan_network_issues()


def _job_shred_targets(progress_cb):
    if not _shredder_mod:
        return []
    return _shredder_mod.ShredEngine(progress_cb).scan_privacy_shred_targets()


def _job_popup_report(progress_cb):
    if not _popup_mod:
        return []
    return _popup_mod.PopupBlocker().scan_popup_report()


def _job_disk_report(progress_cb):
    if not _diskmap_mod:
        return []
    return _diskmap_mod.DiskMapEngine(progress_cb).scan_disk_report()


SCAN_JOBS = {
    "quick": ("一键快速扫描", "快速扫描系统垃圾、临时文件、缓存、回收站等，耗时最短",
              lambda cb: ScanEngine(cb).scan_quick()),
    "all": ("C盘瘦身", "全方位扫描 C 盘：系统垃圾、缓存、社交软件缓存、隐私痕迹等",
            lambda cb: ScanEngine(cb).scan_all()),
    "deep": ("深度清理", "深度扫描全部垃圾，并额外检查旧系统备份、组件存储、还原点等深层占用",
             _job_deep),
    "deep_reclaim": ("深层空间回收", "回收普通清理工具够不到的空间：Windows.old、WinSxS 组件存储、历史驱动、还原点、空文件夹",
                     _job_deep_reclaim),
    "wechat": ("微信专清", "清理微信聊天缓存、接收文件、图片视频等",
               lambda cb: _single_scan(cb, lambda e: e.scan_wechat())),
    "qq": ("QQ专清", "清理 QQ 聊天图片、文件、视频缓存等",
           lambda cb: _single_scan(cb, lambda e: e.scan_qq())),
    "privacy": ("隐私清理", "清理最近文档、剪贴板历史、图标缓存等隐私痕迹",
                lambda cb: _single_scan(cb, lambda e: e.scan_privacy_traces())),
    "large": ("系统大文件清理", "扫描 C 盘大容量文件（大于 100MB），供确认后搬家或删除",
              lambda cb: _single_scan(cb, lambda e: e.scan_large_files(min_size_mb=100))),
    "registry": ("注册表清理", "扫描无效的注册表项（卸载残留、失效路径引用、无效共享计数等）",
                 _job_registry),
    "software_leftover": ("卸载残留扫描", "比对注册表记录与磁盘目录，找出已卸载软件遗留的文件与失效记录",
                          _job_software_leftover),
    "startup": ("启动项问题扫描", "找出失效与可疑的开机自启项，加快开机速度",
                _job_startup_junk),
    "hardware": ("硬件健康检查", "检查硬盘健康、SSD TRIM 状态、电池健康与磁盘剩余空间",
                 _job_hardware_issues),
    "driver_issues": ("驱动残留检查", "检查内核驱动服务：驱动文件丢失的残留记录、未签名的第三方驱动",
                      _job_driver_issues),
    "virtual_issues": ("虚拟设备检查", "检查虚拟麦克风/声卡、虚拟网卡、虚拟摄像头等无真实硬件的设备是否残留",
                       _job_virtual_issues),
    "network": ("网络优化检查", "检查 DNS、hosts 映射、Winsock 目录等网络配置问题",
                _job_network_issues),
    "shred_targets": ("隐私文件粉碎", "扫描建议粉碎而非普通删除的敏感文件",
                      _job_shred_targets),
    "popup": ("弹窗扫描", "扫描当前屏幕上疑似广告的窗口",
              _job_popup_report),
    "disk_report": ("磁盘空间体检", "检查各磁盘剩余空间是否充足",
                    _job_disk_report),
}


def _single_scan(progress_cb, fn):
    """只跑单个扫描方法的小工具"""
    engine = ScanEngine(progress_cb)
    engine.reset()
    fn(engine)
    return engine.results


# ==================== 主应用 ====================

class CleanerApp:
    """主应用窗口"""

    def __init__(self, root):
        self.root = root
        self.root.title("合枢 HS - 电脑清理与设备互联")
        self.root.geometry("1200x800")
        self.root.minsize(1050, 700)
        self.root.configure(bg=Theme.BG)

        # 扫描/清理状态
        self.scan_engine = ScanEngine()
        self.clean_engine = CleanEngine()
        self.scan_results = []
        self.scanning = False
        self.cleaning = False
        self.scan_job_key = "quick"
        self._progress_value = 0

        # 供页面模块使用的接口别名
        self.THEME = Theme
        self.format_size = staticmethod(format_size)

        self._build_ui()

        # 关闭窗口时释放后台资源（如手机互传占用的端口）
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        # 启动后在后台记录一次磁盘占用快照（用于"磁盘占用趋势"功能）
        self.root.after(800, lambda: self._record_disk_snapshot("启动"))

    def _on_close(self):
        """退出前清理后台资源"""
        try:
            import page_transfer
            page_transfer.stop_shared_engine()
        except Exception:
            pass
        try:
            self.root.destroy()
        except Exception:
            pass

    def _record_disk_snapshot(self, reason="启动", force=False):
        """在后台记录一次系统盘占用快照（不影响启动速度）"""
        if not _disktrend_mod:
            return

        def work(_progress_cb):
            try:
                engine = _disktrend_mod.DiskTrendEngine()
                return engine.record_snapshot("C", force=force, reason=reason)
            except Exception:
                return None

        def on_done(result):
            if result and result.get("recorded"):
                self.set_status(result.get("message", ""))

        self.run_async(work, on_done=on_done)

    # ==================== 界面骨架 ====================

    def _build_ui(self):
        self._build_header()
        self._build_sidebar()
        self._build_content()
        self._build_statusbar()
        self._setup_styles()
        self._show_home()

    def _build_header(self):
        header = tk.Frame(self.root, bg=Theme.PRIMARY, height=68)
        header.pack(side=tk.TOP, fill=tk.X)
        header.pack_propagate(False)

        tk.Label(header, text="🧹  合枢 HS", bg=Theme.PRIMARY, fg="white",
                 font=("Microsoft YaHei", 17, "bold")).pack(side=tk.LEFT, padx=20)

        tk.Label(header, text="清理系统盘 · 释放空间 · 手机互联",
                 bg=Theme.PRIMARY, fg="#b3d9ff",
                 font=("Microsoft YaHei", 10)).pack(side=tk.LEFT, pady=(10, 0))

        self.admin_label = tk.Label(header, text="", bg=Theme.PRIMARY, fg="#b3d9ff",
                                    font=("Microsoft YaHei", 9))
        self.admin_label.pack(side=tk.RIGHT, padx=20)
        self._update_admin_status()

    def _update_admin_status(self):
        if is_admin():
            self.admin_label.config(text="✓ 已获得管理员权限", fg="#95de64")
        else:
            self.admin_label.config(text="⚠ 未获得管理员权限（部分功能受限）", fg="#ffd666")

    def _build_sidebar(self):
        """可滚动的侧边导航"""
        outer = tk.Frame(self.root, bg=Theme.SIDEBAR, width=196)
        outer.pack(side=tk.LEFT, fill=tk.Y)
        outer.pack_propagate(False)

        canvas = tk.Canvas(outer, bg=Theme.SIDEBAR, highlightthickness=0, width=196)
        scrollbar = ttk.Scrollbar(outer, orient="vertical", command=canvas.yview)
        inner = tk.Frame(canvas, bg=Theme.SIDEBAR)

        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        canvas.configure(yscrollcommand=scrollbar.set)

        inner_id = canvas.create_window((0, 0), window=inner, anchor="nw")

        def _on_config(_e):
            canvas.configure(scrollregion=canvas.bbox("all"))
            canvas.itemconfig(inner_id, width=canvas.winfo_width())

        inner.bind("<Configure>", _on_config)
        canvas.bind("<Configure>", _on_config)

        def _on_wheel(event):
            canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

        canvas.bind("<Enter>", lambda e: canvas.bind_all("<MouseWheel>", _on_wheel))
        canvas.bind("<Leave>", lambda e: canvas.unbind_all("<MouseWheel>"))

        self.nav_buttons = {}
        for group_name, items in NAV_GROUPS:
            tk.Label(inner, text=group_name, bg=Theme.SIDEBAR, fg="#5a6b7c",
                     font=("Microsoft YaHei", 8), anchor=tk.W).pack(
                fill=tk.X, padx=18, pady=(10, 2))
            for text, key in items:
                btn = tk.Button(inner, text=text, bg=Theme.SIDEBAR, fg=Theme.SIDEBAR_ITEM,
                                font=("Microsoft YaHei", 9), bd=0, anchor=tk.W,
                                padx=16, pady=6, activebackground=Theme.SIDEBAR_HOVER,
                                activeforeground="white", cursor="hand2",
                                command=lambda k=key: self._switch_view(k))
                btn.pack(fill=tk.X)
                # 悬停高亮
                btn.bind("<Enter>", lambda e, b=btn: self._nav_hover(b, True))
                btn.bind("<Leave>", lambda e, b=btn: self._nav_hover(b, False))
                self.nav_buttons[key] = btn

        # 底部：本工具默认启动即获取管理员权限，因此这里显示版本与权限状态；
        # 若因故未获取权限（例如打包未生效或被安全软件拦截），保留重启按钮作为兜底。
        footer = tk.Frame(inner, bg=Theme.SIDEBAR)
        footer.pack(side=tk.BOTTOM, fill=tk.X, padx=10, pady=12)
        if is_admin():
            tk.Label(footer, text="合枢 HS  v2.0.0", bg=Theme.SIDEBAR, fg="#5a6b7c",
                     font=("Microsoft YaHei", 8), anchor=tk.W).pack(fill=tk.X)
            tk.Label(footer, text="✓ 已获得管理员权限", bg=Theme.SIDEBAR, fg="#73d13d",
                     font=("Microsoft YaHei", 8), anchor=tk.W).pack(fill=tk.X, pady=(2, 0))
        else:
            tk.Button(footer, text="🔑 以管理员身份重启", bg="#1d2b3a", fg="#8c8c8c",
                      font=("Microsoft YaHei", 9), bd=0, padx=14, pady=8, cursor="hand2",
                      command=self._restart_as_admin).pack(fill=tk.X)

    def _nav_hover(self, btn, entering):
        """悬停效果：仅对未选中的项生效"""
        key = None
        for k, b in self.nav_buttons.items():
            if b is btn:
                key = k
                break
        if key is None or key == getattr(self, "current_key", None):
            return
        btn.config(bg=Theme.SIDEBAR_HOVER if entering else Theme.SIDEBAR)

    def _highlight_nav(self, key):
        for k, btn in self.nav_buttons.items():
            if k == key:
                btn.config(bg=Theme.SIDEBAR_ACTIVE, fg="white")
            else:
                btn.config(bg=Theme.SIDEBAR, fg=Theme.SIDEBAR_ITEM)

    def _build_content(self):
        self.content = tk.Frame(self.root, bg=Theme.BG)
        self.content.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

    def _build_statusbar(self):
        bar = tk.Frame(self.root, bg="#e6e6e6", height=30)
        bar.pack(side=tk.BOTTOM, fill=tk.X)
        bar.pack_propagate(False)

        self.status_label = tk.Label(bar, text="就绪", bg="#e6e6e6", fg=Theme.TEXT,
                                     font=("Microsoft YaHei", 9), anchor=tk.W)
        self.status_label.pack(side=tk.LEFT, padx=14)

        self.size_label = tk.Label(bar, text="", bg="#e6e6e6", fg=Theme.PRIMARY,
                                   font=("Microsoft YaHei", 9, "bold"))
        self.size_label.pack(side=tk.RIGHT, padx=14)

    def _setup_styles(self):
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except Exception:
            pass
        style.configure("Treeview", font=("Microsoft YaHei", 9), rowheight=26,
                        background="white", fieldbackground="white")
        style.configure("Treeview.Heading", font=("Microsoft YaHei", 9, "bold"))
        style.configure("TNotebook.Tab", font=("Microsoft YaHei", 9), padding=(14, 6))
        style.configure("TProgressbar", thickness=12)

    # ==================== 供页面模块使用的公共接口 ====================

    def set_status(self, text):
        """更新底部状态栏"""
        try:
            self.status_label.config(text=text)
            self.root.update_idletasks()
        except Exception:
            pass

    def set_size_hint(self, text):
        """更新右下角提示文字"""
        try:
            self.size_label.config(text=text)
        except Exception:
            pass

    def run_async(self, work, on_done=None, on_progress=None):
        """
        在后台线程执行 work(progress_cb)，完成后在主线程调用 on_done(result)。
        on_progress 为空时，进度只更新状态栏；页面可传入自己的回调同时更新页面进度条。
        """
        def _progress(percent, msg):
            self._progress_value = percent
            def _apply():
                self.set_status(msg)
                if on_progress:
                    try:
                        on_progress(percent, msg)
                    except Exception:
                        pass
            self.root.after(0, _apply)

        def _runner():
            try:
                result = work(_progress)
            except Exception as e:
                self.root.after(0, lambda err=e: self.show_error("操作失败", str(err)))
                return
            if on_done:
                self.root.after(0, lambda r=result: on_done(r))

        threading.Thread(target=_runner, daemon=True).start()

    def card(self, parent, **kw):
        """白色卡片容器（默认已 pack，可用 fill/padx/pady 覆盖）"""
        fill = kw.pop("fill", tk.X)
        padx = kw.pop("padx", 20)
        pady = kw.pop("pady", (0, 10))
        frame = tk.Frame(parent, bg=Theme.CARD_BG, bd=0,
                         highlightbackground=Theme.BORDER, highlightthickness=1, **kw)
        try:
            frame.pack(fill=fill, padx=padx, pady=pady)
        except Exception:
            pass
        return frame

    def make_table(self, parent, columns, widths, headings=None, height=10):
        """创建带滚动条的表格，返回 (tree, container)"""
        container = tk.Frame(parent, bg=Theme.BG)
        container.pack(fill=tk.BOTH, expand=True)

        tree = ttk.Treeview(container, columns=list(columns), show="headings", height=height)
        for i, c in enumerate(columns):
            text = headings[i] if headings and i < len(headings) else c
            tree.heading(c, text=text)
            width = widths[i] if i < len(widths) else 120
            tree.column(c, width=width, anchor=tk.W)
        if columns:
            # 第一列通常是勾选框或序号，居中显示
            tree.column(columns[0], anchor=tk.CENTER, width=widths[0] if widths else 60)

        vsb = ttk.Scrollbar(container, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=vsb.set)
        tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)
        return tree, container

    def button(self, parent, text, command, kind="primary", width=None):
        """统一样式的按钮"""
        palette = {
            "primary": (Theme.PRIMARY, "white"),
            "success": (Theme.ACCENT, "white"),
            "danger": (Theme.DANGER, "white"),
            "warning": (Theme.WARNING, "white"),
            "ghost": (Theme.GHOST_BG, Theme.PRIMARY),
        }
        bg, fg = palette.get(kind, palette["primary"])
        btn = tk.Button(parent, text=text, command=command, bg=bg, fg=fg,
                        relief=tk.FLAT, font=("Microsoft YaHei", 9),
                        padx=14, pady=5, cursor="hand2",
                        activebackground=bg, activeforeground=fg)
        if width:
            btn.config(width=width)
        return btn

    def section_title(self, parent, title, desc=""):
        """页面标题区"""
        frame = tk.Frame(parent, bg=Theme.BG)
        frame.pack(fill=tk.X, padx=20, pady=(14, 8))
        tk.Label(frame, text=title, bg=Theme.BG, fg=Theme.TEXT,
                 font=("Microsoft YaHei", 15, "bold")).pack(anchor=tk.W)
        if desc:
            tk.Label(frame, text=desc, bg=Theme.BG, fg=Theme.TEXT_LIGHT,
                     font=("Microsoft YaHei", 9), wraplength=940,
                     justify=tk.LEFT).pack(anchor=tk.W, pady=(2, 0))
        return frame

    def progress_row(self, parent):
        """创建进度条 + 百分比标签，返回 (progressbar, label)"""
        frame = tk.Frame(parent, bg=Theme.BG)
        frame.pack(fill=tk.X, padx=20, pady=(0, 6))
        bar = ttk.Progressbar(frame, mode="determinate")
        bar.pack(side=tk.LEFT, fill=tk.X, expand=True)
        label = tk.Label(frame, text="0%", bg=Theme.BG, fg=Theme.PRIMARY,
                         font=("Microsoft YaHei", 9, "bold"), width=5)
        label.pack(side=tk.LEFT, padx=8)
        return bar, label

    def hint(self, parent, text, color=None):
        """一行提示文字"""
        label = tk.Label(parent, text=text, bg=Theme.BG, fg=color or Theme.TEXT_LIGHT,
                         font=("Microsoft YaHei", 9), wraplength=940, justify=tk.LEFT)
        label.pack(anchor=tk.W, padx=20, pady=(0, 6))
        return label

    def show_info(self, title, msg):
        messagebox.showinfo(title, msg)

    def show_error(self, title, msg):
        messagebox.showerror(title, msg)

    def show_warning(self, title, msg):
        messagebox.showwarning(title, msg)

    def ask_yes_no(self, title, msg):
        return messagebox.askyesno(title, msg)

    # ==================== 视图路由 ====================

    def _clear_content(self):
        for w in self.content.winfo_children():
            w.destroy()

    def _switch_view(self, key):
        self.current_key = key
        self._highlight_nav(key)
        self._clear_content()
        self.set_size_hint("")

        # 页面模块
        if key.startswith("page:"):
            module_name = key.split(":", 1)[1]
            self._show_page_module(module_name)
            return

        # 内置视图
        if key == "home":
            self._show_home()
        elif key == "checkup":
            self._show_checkup()
        elif key == "c_clean":
            self._show_scan_view("quick")
        elif key == "c_slim":
            self._show_scan_view("all")
        elif key == "quick_scan":
            self._show_scan_view("quick")
        elif key == "deep_clean":
            self._show_scan_view("deep")
        elif key == "deep_reclaim":
            self._show_scan_view("deep_reclaim")
        elif key == "wechat":
            self._show_scan_view("wechat")
        elif key == "qq":
            self._show_scan_view("qq")
        elif key == "privacy":
            self._show_scan_view("privacy")
        elif key == "compress":
            self._show_compress_view()
        elif key == "defrag":
            self._show_defrag_view()
        elif key == "pagefile":
            self._show_pagefile_view()
        elif key == "memory":
            self._show_memory_view()
        else:
            self._show_home()

    def _show_page_module(self, module_name):
        """动态挂载界面页面模块"""
        # 重复文件页指向同一模块的另一个标签页
        target = "page_diskmap" if module_name == "page_diskmap_dup" else module_name
        mod = _try_import(target)
        if mod is None or not hasattr(mod, "build"):
            self._show_missing_module(target)
            return
        try:
            mod.build(self, self.content)
        except Exception as e:
            self._show_missing_module(target, str(e))

        # 重复文件：渲染后自动切到第 2 个标签页
        if module_name == "page_diskmap_dup":
            try:
                for child in self.content.winfo_children():
                    if isinstance(child, ttk.Notebook) and len(child.tabs()) >= 2:
                        child.select(1)
            except Exception:
                pass

    def _show_missing_module(self, module_name, error=""):
        """页面模块不可用时的占位提示"""
        self.section_title(self.content, "模块暂不可用",
                           f"页面模块 {module_name}.py 未能加载。")
        card = self.card(self.content, pady=(6, 10))
        tk.Label(card, text=f"缺失或加载失败的模块：{module_name}.py",
                 bg=Theme.CARD_BG, fg=Theme.DANGER,
                 font=("Microsoft YaHei", 11, "bold")).pack(padx=16, pady=(14, 6))
        if error:
            tk.Label(card, text=f"错误信息：{error}", bg=Theme.CARD_BG, fg=Theme.TEXT_LIGHT,
                     font=("Microsoft YaHei", 9), wraplength=860,
                     justify=tk.LEFT).pack(padx=16, pady=(0, 14))
        tk.Label(card, text="该模块的文件应位于程序同目录下。请确认文件未丢失、未被安全软件拦截。",
                 bg=Theme.CARD_BG, fg=Theme.TEXT_LIGHT,
                 font=("Microsoft YaHei", 9)).pack(padx=16, pady=(0, 14))

    # ==================== 首页 ====================

    def _show_home(self):
        self.current_key = "home"
        self._highlight_nav("home")

        frame = tk.Frame(self.content, bg=Theme.BG)
        frame.pack(fill=tk.BOTH, expand=True)

        # 欢迎区
        card = self.card(frame, pady=(16, 10))
        tk.Label(card, text="欢迎使用 合枢 HS", bg=Theme.CARD_BG, fg=Theme.TEXT,
                 font=("Microsoft YaHei", 18, "bold")).pack(pady=(16, 4))
        tk.Label(card,
                 text="清理系统盘空间，解决系统盘空间不足、磁盘碎片、垃圾堆积、虚拟内存占用等问题，"
                      "提升电脑运行速度。\n"
                      "所有清理项都会标注来源与影响说明，清理前可逐项确认。",
                 bg=Theme.CARD_BG, fg=Theme.TEXT_LIGHT, font=("Microsoft YaHei", 9),
                 justify=tk.CENTER).pack(pady=(0, 14))

        btn_row = tk.Frame(card, bg=Theme.CARD_BG)
        btn_row.pack(pady=(0, 16))
        self.button(btn_row, "⚡ 一键扫描", lambda: self._switch_view("quick_scan")).pack(side=tk.LEFT, padx=6)
        self.button(btn_row, "🩺 电脑体检", lambda: self._switch_view("checkup"),
                    kind="success").pack(side=tk.LEFT, padx=6)
        self.button(btn_row, "🗺️ 磁盘空间分析", lambda: self._switch_view("page:page_diskmap"),
                    kind="ghost").pack(side=tk.LEFT, padx=6)

        # 功能网格
        grid_holder = tk.Frame(frame, bg=Theme.BG)
        grid_holder.pack(fill=tk.BOTH, expand=True, padx=14, pady=(0, 12))

        features = [
            ("⚡ 一键扫描", "快速找出可清理垃圾", "quick_scan", Theme.PRIMARY),
            ("🩺 电脑体检", "百秒健康评分与建议", "checkup", Theme.ACCENT),
            ("🧹 C盘清理", "系统缓存/临时/回收站", "c_clean", "#13c2c2"),
            ("🔍 深度清理", "垃圾+深层占用一次扫", "deep_clean", Theme.WARNING),
            ("📦 深层回收", "WinSxS/旧系统/还原点", "deep_reclaim", "#2f54eb"),
            ("🗺️ 磁盘空间分析", "树图看清空间去向", "page:page_diskmap", "#fa541c"),
            ("♻️ 重复文件", "哈希比对找出重复", "page:page_diskmap_dup", "#a0d911"),
            ("💬 微信专清", "按图片/视频/文件分类", "wechat", Theme.PURPLE),
            ("🐧 QQ专清", "QQ 缓存与接收文件", "qq", "#eb2f96"),
            ("📋 软件管理", "卸载/残留/软件搬家", "page:page_software", "#1890ff"),
            ("🚀 启动项管理", "自启项/服务/计划任务", "page:page_startup", "#fadb14"),
            ("🗂️ 注册表清理", "无效项+备份还原", "page:page_registry", "#ff7a45"),
            ("💿 碎片整理", "优化文件存储布局", "defrag", "#b37feb"),
            ("🔄 虚拟内存转移", "把 pagefile 移出C盘", "pagefile", "#36cfc9"),
            ("🔥 文件粉碎", "不可恢复删除敏感文件", "page:page_shredder", Theme.DANGER),
            ("🔒 隐私清理", "使用痕迹与缓存痕迹", "privacy", "#597ef7"),
            ("🛡️ 弹窗拦截", "让电脑安静下来", "page:page_popup", "#ffc53d"),
            ("🖥️ 系统信息", "硬件/硬盘健康/网络", "page:page_systeminfo", "#8c8c8c"),
        ]

        cols = 6
        for i, (title, desc, key, color) in enumerate(features):
            r, c = divmod(i, cols)
            f = tk.Frame(grid_holder, bg=Theme.CARD_BG, bd=0,
                         highlightbackground=Theme.BORDER, highlightthickness=1,
                         cursor="hand2")
            f.grid(row=r, column=c, padx=6, pady=6, sticky="nsew")
            grid_holder.columnconfigure(c, weight=1)
            tk.Label(f, text=title, bg=Theme.CARD_BG, fg=color,
                     font=("Microsoft YaHei", 10, "bold")).pack(pady=(12, 2))
            tk.Label(f, text=desc, bg=Theme.CARD_BG, fg=Theme.TEXT_LIGHT,
                     font=("Microsoft YaHei", 8)).pack(pady=(0, 12))
            for widget in (f,) + tuple(f.winfo_children()):
                widget.bind("<Button-1>", lambda e, k=key: self._switch_view(k))

    # ==================== 电脑体检 ====================

    def _show_checkup(self):
        self.section_title(self.content, "电脑体检",
                           "从空间、内存、垃圾、启动项、硬盘健康、温度、系统维护七个维度综合评分，"
                           "给出可执行的优化建议。")

        top = self.card(self.content, pady=(0, 10))
        inner = tk.Frame(top, bg=Theme.CARD_BG)
        inner.pack(fill=tk.X, padx=16, pady=14)

        # 评分圆环
        self.gauge_canvas = tk.Canvas(inner, width=170, height=170, bg=Theme.CARD_BG,
                                      highlightthickness=0)
        self.gauge_canvas.pack(side=tk.LEFT, padx=(0, 24))
        self._draw_gauge(0, "#cccccc", "待体检")

        right = tk.Frame(inner, bg=Theme.CARD_BG)
        right.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.checkup_summary = tk.Label(right, text="点击「开始体检」检查电脑状态",
                                        bg=Theme.CARD_BG, fg=Theme.TEXT,
                                        font=("Microsoft YaHei", 12), justify=tk.LEFT,
                                        wraplength=700, anchor=tk.W)
        self.checkup_summary.pack(anchor=tk.W, pady=(6, 12))

        btn_row = tk.Frame(right, bg=Theme.CARD_BG)
        btn_row.pack(anchor=tk.W)
        self.checkup_btn = self.button(btn_row, "🩺 开始体检", self._run_checkup)
        self.checkup_btn.pack(side=tk.LEFT)
        self.button(btn_row, "仅快速体检（跳过温度与硬盘）",
                    lambda: self._run_checkup(include_slow=False),
                    kind="ghost").pack(side=tk.LEFT, padx=8)

        prog_holder = tk.Frame(right, bg=Theme.CARD_BG)
        prog_holder.pack(fill=tk.X, pady=(12, 0))
        prog_frame = tk.Frame(prog_holder, bg=Theme.CARD_BG)
        prog_frame.pack(fill=tk.X)
        self.checkup_progress = ttk.Progressbar(prog_frame, mode="determinate")
        self.checkup_progress.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.checkup_progress_label = tk.Label(prog_frame, text="0%", bg=Theme.CARD_BG,
                                                fg=Theme.PRIMARY,
                                                font=("Microsoft YaHei", 9, "bold"), width=5)
        self.checkup_progress_label.pack(side=tk.LEFT, padx=8)

        # 结果表格
        self.section_title(self.content, "检查明细", "")
        table_holder = tk.Frame(self.content, bg=Theme.BG)
        table_holder.pack(fill=tk.BOTH, expand=True, padx=20, pady=(0, 10))
        self.checkup_tree, _ = self.make_table(
            table_holder,
            ("name", "status", "detail", "suggestion"),
            (130, 80, 420, 340),
            headings=("检查项", "状态", "详情", "建议"),
            height=8)
        try:
            self.checkup_tree.tag_configure("bad", foreground=Theme.DANGER)
            self.checkup_tree.tag_configure("warn", foreground=Theme.WARNING)
            self.checkup_tree.tag_configure("ok", foreground=Theme.ACCENT)
        except Exception:
            pass

    def _draw_gauge(self, score, color, grade):
        """绘制评分圆环"""
        c = self.gauge_canvas
        c.delete("all")
        c.create_oval(16, 16, 154, 154, outline="#eef2f6", width=14)
        if score > 0:
            extent = -359.9 * min(score, 100) / 100.0
            c.create_arc(16, 16, 154, 154, start=90, extent=extent,
                         style="arc", outline=color, width=14)
        c.create_text(85, 76, text=str(score), fill=color,
                      font=("Microsoft YaHei", 30, "bold"))
        c.create_text(85, 112, text=grade, fill=Theme.TEXT_LIGHT,
                      font=("Microsoft YaHei", 11))

    def _run_checkup(self, include_slow=True):
        if not _health_mod:
            self.show_error("模块缺失", "health.py 未能加载，无法执行体检。")
            return
        self.checkup_btn.config(state=tk.DISABLED, text="体检中...")

        def work(progress_cb):
            engine = _health_mod.HealthEngine(progress_cb)
            return engine.run_checkup(include_slow=include_slow)

        def on_progress(percent, _msg):
            self.checkup_progress["value"] = percent
            self.checkup_progress_label.config(text=f"{percent}%")

        def on_done(result):
            self.checkup_btn.config(state=tk.NORMAL, text="🩺 重新体检")
            self._render_checkup(result)

        self.run_async(work, on_done=on_done, on_progress=on_progress)

    def _render_checkup(self, result):
        score = result.get("score", 0)
        grade = result.get("grade", "未知")
        color = (Theme.ACCENT if score >= 75 else
                 Theme.PRIMARY if score >= 60 else
                 Theme.WARNING if score >= 40 else Theme.DANGER)
        self._draw_gauge(score, color, grade)

        items = result.get("items", [])
        actions = result.get("actions", [])
        text = f"健康评分 {score} 分（{grade}）　检查时间 {result.get('checked_at', '')}\n"
        if actions:
            text += "需要优先处理：" + "、".join(a["name"] for a in actions[:4])
        else:
            text += "各项指标均处于健康范围，继续保持。"
        self.checkup_summary.config(text=text)

        for row in self.checkup_tree.get_children():
            self.checkup_tree.delete(row)
        for it in items:
            ratio = it["score"] / max(it["max_score"], 1)
            tag = "ok" if ratio >= 0.75 else ("warn" if ratio >= 0.45 else "bad")
            self.checkup_tree.insert("", tk.END,
                                     values=(it["name"], it["status"], it["detail"],
                                             it["suggestion"]),
                                     tags=(tag,))

    # ==================== 通用扫描视图 ====================

    def _show_scan_view(self, job_key):
        if job_key not in SCAN_JOBS:
            self._show_home()
            return
        title, desc, _fn = SCAN_JOBS[job_key]
        self.scan_job_key = job_key

        self.section_title(self.content, title, desc)

        bar_card = self.card(self.content, pady=(0, 8))
        inner = tk.Frame(bar_card, bg=Theme.CARD_BG)
        inner.pack(fill=tk.X, padx=16, pady=12)

        self.scan_btn = self.button(inner, "🔍 开始扫描", self._start_scan)
        self.scan_btn.pack(side=tk.LEFT)

        self.clean_btn = self.button(inner, "🧹 一键清理", self._start_clean,
                                     kind="success")
        self.clean_btn.config(state=tk.DISABLED)
        self.clean_btn.pack(side=tk.LEFT, padx=8)

        self.select_all_var = tk.BooleanVar(value=True)
        tk.Checkbutton(inner, text="全选", variable=self.select_all_var,
                       bg=Theme.CARD_BG, fg=Theme.TEXT,
                       font=("Microsoft YaHei", 9),
                       activebackground=Theme.CARD_BG,
                       command=self._toggle_select_all).pack(side=tk.LEFT, padx=10)

        self.scan_summary = tk.Label(inner, text="尚未扫描", bg=Theme.CARD_BG,
                                     fg=Theme.TEXT_LIGHT,
                                     font=("Microsoft YaHei", 9))
        self.scan_summary.pack(side=tk.LEFT, padx=12)

        # 表格（勾选列 + 来源备注 + 说明）
        table_holder = tk.Frame(self.content, bg=Theme.BG)
        table_holder.pack(fill=tk.BOTH, expand=True, padx=20, pady=(0, 6))
        self.tree, _ = self.make_table(
            table_holder,
            ("sel", "category", "source", "path", "size", "desc"),
            (50, 90, 230, 300, 90, 380),
            headings=("选择", "分类", "来源", "路径 / 位置", "大小", "说明与影响"),
            height=12)
        self.tree.bind("<Button-1>", self._on_tree_click)

        self._scan_rows = []

    def _on_tree_click(self, event):
        """点击第一列切换勾选"""
        try:
            region = self.tree.identify_region(event.x, event.y)
            col = self.tree.identify_column(event.x)
            if region == "cell" and col == "#1":
                item = self.tree.identify_row(event.y)
                if item:
                    cur = self.tree.set(item, "sel")
                    self.tree.set(item, "sel", "" if cur == "✓" else "✓")
        except Exception:
            pass

    def _toggle_select_all(self):
        val = "✓" if self.select_all_var.get() else ""
        for item in self.tree.get_children():
            self.tree.set(item, "sel", val)

    # ---------- 扫描 ----------

    def _start_scan(self):
        if self.scanning:
            return
        self.scanning = True
        self.scan_btn.config(text="⏹ 停止扫描", bg=Theme.DANGER, command=self._stop_scan)
        self.clean_btn.config(state=tk.DISABLED)
        for row in self.tree.get_children():
            self.tree.delete(row)
        self.scan_summary.config(text="扫描中...")

        def work(progress_cb):
            _title, _desc, fn = SCAN_JOBS.get(self.scan_job_key, SCAN_JOBS["quick"])
            return fn(progress_cb)

        def on_progress(percent, _msg):
            self.set_status(_msg)
            self.scan_summary.config(text=f"扫描中 {percent}%　{_msg}")

        def on_done(results):
            self.scanning = False
            self.scan_btn.config(text="🔍 重新扫描", bg=Theme.PRIMARY, command=self._start_scan)
            self._render_scan_results(results or [])

        self.run_async(work, on_done=on_done, on_progress=on_progress)

    def _stop_scan(self):
        try:
            self.scan_engine.stop()
        except Exception:
            pass
        self.scanning = False
        self.scan_btn.config(text="🔍 开始扫描", bg=Theme.PRIMARY, command=self._start_scan)
        self.set_status("已请求停止扫描")

    def _render_scan_results(self, results):
        self.scan_results = list(results)
        total_size = sum(getattr(r, "size", 0) for r in self.scan_results)

        if not self.scan_results:
            self.scan_summary.config(text="未发现可清理项目")
            self.set_size_hint("")
            self.set_status("扫描完成，未发现问题")
            return

        sorted_results = sorted(self.scan_results, key=lambda x: getattr(x, "size", 0),
                                reverse=True)
        self._scan_rows = sorted_results
        for r in sorted_results:
            size = getattr(r, "size", 0)
            self.tree.insert("", tk.END, values=(
                "✓",
                getattr(r, "category", ""),
                getattr(r, "source", ""),
                getattr(r, "path", ""),
                format_size(size) if size else "—",
                getattr(r, "description", ""),
            ))

        counts = {}
        for r in self.scan_results:
            counts[getattr(r, "category", "其他")] = counts.get(getattr(r, "category", "其他"), 0) + 1
        detail = "、".join(f"{k} {v} 项" for k, v in counts.items())
        self.scan_summary.config(text=f"共 {len(self.scan_results)} 项：{detail}")
        self.set_size_hint(f"可清理: {format_size(total_size)}")
        self.set_status(f"扫描完成，共 {len(self.scan_results)} 项，合计 {format_size(total_size)}")
        self.clean_btn.config(state=tk.NORMAL)

    # ---------- 清理 ----------

    def _selected_rows(self):
        """返回被勾选的 ScanResult 列表"""
        selected = []
        for i, item in enumerate(self.tree.get_children()):
            if self.tree.set(item, "sel") == "✓":
                if i < len(self._scan_rows):
                    selected.append(self._scan_rows[i])
        return selected

    def _start_clean(self):
        if self.cleaning or not self.scan_results:
            return
        to_clean = self._selected_rows()
        if not to_clean:
            self.show_warning("提示", "请先勾选要清理的项目")
            return

        total_size = sum(getattr(r, "size", 0) for r in to_clean)
        if not self.ask_yes_no(
                "确认清理",
                f"即将处理 {len(to_clean)} 个项目，预计释放约 {format_size(total_size)}。\n\n"
                "· 部分操作不可恢复（如回收站、文件删除）\n"
                "· 注册表类操作会先自动备份\n"
                "· 提示类项目（如硬件告警）不会被执行删除\n\n是否继续？"):
            return

        self.cleaning = True
        self.clean_btn.config(state=tk.DISABLED, text="清理中...")

        def work(progress_cb):
            return self._dispatch_clean(to_clean, progress_cb)

        def on_progress(percent, _msg):
            self.scan_summary.config(text=f"清理中 {percent}%　{_msg}")

        def on_done(result):
            self.cleaning = False
            self.clean_btn.config(state=tk.NORMAL, text="🧹 一键清理")
            freed, logs = result
            self.set_status(f"清理完成，释放 {format_size(freed)}")
            # 清理完成后强制记录一次快照，让趋势图能体现本次清理释放了多少空间
            self._record_disk_snapshot("清理后", force=True)
            summary = f"清理完成！\n\n释放空间：{format_size(freed)}\n"
            if logs:
                summary += "\n执行记录：\n" + "\n".join(f"· {line}" for line in logs[:12])
                if len(logs) > 12:
                    summary += f"\n… 其余 {len(logs) - 12} 条略"
            summary += "\n\n部分更改（如休眠、虚拟内存相关）需重启后生效。"
            self.show_info("清理完成", summary)
            self._start_scan()

        self.run_async(work, on_done=on_done, on_progress=on_progress)

    def _dispatch_clean(self, items, progress_cb):
        """
        按分类把清理任务分派给对应的引擎。
        返回 (释放字节数, 日志列表)
        """
        freed = 0
        logs = []
        total = len(items) or 1

        # 分组收集
        generic = []          # 走通用目录/文件删除
        registry_items = []   # 注册表项
        ghost_items = []      # 失效软件注册记录
        empty_folders = []    # 空文件夹
        empty_leftovers = []  # 空壳残留目录（需删除目录本身，不能走通用清理）
        shortcut_files = []   # 失效快捷方式（按文件删）
        windows_old = False
        component_store = False
        restore_points = False
        skipped_hints = 0

        # 只有明确"可安全删除"的分类才进入通用文件删除分支。
        # 采用白名单机制而非黑名单：任何未列入白名单的分类一律只做提示，
        # 这样即使将来新增了扫描类型，也不会因为遗漏判断而误删重要数据
        # （例如驱动仓库 DriverStore、WinSxS 组件存储这类绝对不能直接删除的目录）。
        ACTIONABLE_CATEGORIES = {
            "系统垃圾", "微信专清", "QQ专清", "隐私清理", "深度清理",
            "系统清理", "深度回收", "注册表垃圾", "软件残留",
        }

        for r in items:
            cat = getattr(r, "category", "")
            src = getattr(r, "source", "")
            path = getattr(r, "path", "")

            if cat not in ACTIONABLE_CATEGORIES:
                # 提示类项目：大文件、硬件告警、磁盘优化建议、弹窗、网络建议、文件粉碎建议等
                skipped_hints += 1
                continue

            if cat == "注册表垃圾":
                registry_items.append(r)
            elif cat == "软件残留" and "幽灵软件" in src:
                ghost_items.append(r)
            elif cat == "软件残留" and "空壳" in src:
                empty_leftovers.append(path)
            elif cat == "系统清理" and src == "空文件夹":
                empty_folders.append(path)
            elif cat == "系统清理" and "失效快捷方式" in src:
                shortcut_files.append(path)
            elif cat == "深度回收":
                if "Windows.old" in src:
                    windows_old = True
                elif "组件存储" in src:
                    component_store = True
                elif "还原点" in src:
                    restore_points = True
                else:
                    generic.append(r)
            else:
                generic.append(r)

        step = 0
        step_total = sum(1 for x in (bool(registry_items), bool(ghost_items),
                                     bool(empty_leftovers), bool(empty_folders),
                                     bool(shortcut_files),
                                     windows_old, component_store, restore_points,
                                     bool(generic)) if x) or 1

        def _bump(msg):
            nonlocal step
            step += 1
            progress_cb(int(step / step_total * 100), msg)

        # 1) 注册表
        if registry_items:
            if _registry_mod:
                try:
                    engine = _registry_mod.RegistryEngine()
                    ok, backup_path = engine.backup_registry()
                    if ok:
                        logs.append(f"注册表已备份到 {backup_path}")
                    else:
                        logs.append(f"注册表备份失败（{backup_path}），仍继续清理已确认安全的类别")
                    cnt, reg_logs = engine.clean_registry_items(registry_items)
                    logs.append(f"注册表清理：成功 {cnt} 项")
                    logs.extend(reg_logs[:6])
                except Exception as e:
                    logs.append(f"注册表清理失败：{e}")
            else:
                logs.append("注册表引擎不可用，已跳过注册表清理")
            _bump("注册表清理完成")

        # 2) 失效软件注册记录
        if ghost_items:
            if _software_mod:
                try:
                    cnt, failures = _software_mod.SoftwareManager().clean_ghost_registry_entries(ghost_items)
                    logs.append(f"清理失效软件记录：{cnt} 条")
                    for p, reason in failures[:4]:
                        logs.append(f"  {p}：{reason}")
                except Exception as e:
                    logs.append(f"清理失效软件记录失败：{e}")
            else:
                logs.append("软件管理引擎不可用，已跳过")
            _bump("失效软件记录清理完成")

        # 2.5) 空壳残留目录（要删除目录本身，不能用通用目录清理）
        if empty_leftovers:
            if _software_mod:
                try:
                    cnt, failures = _software_mod.SoftwareManager().remove_empty_leftover_folders(empty_leftovers)
                    logs.append(f"删除空壳残留目录：{cnt} 个")
                    for p, reason in failures[:4]:
                        logs.append(f"  {p}：{reason}")
                except Exception as e:
                    logs.append(f"删除空壳残留目录失败：{e}")
            else:
                logs.append("软件管理引擎不可用，已跳过空壳残留目录")
            _bump("空壳残留目录清理完成")

        # 3) 空文件夹
        if empty_folders:
            if _sysreclaim_mod:
                try:
                    cnt, failures = _sysreclaim_mod.SysReclaimEngine().clean_empty_folders(empty_folders)
                    logs.append(f"删除空文件夹：{cnt} 个")
                    for p, reason in failures[:3]:
                        logs.append(f"  {p}：{reason}")
                except Exception as e:
                    logs.append(f"删除空文件夹失败：{e}")
            else:
                logs.append("系统回收引擎不可用，已跳过")
            _bump("空文件夹清理完成")

        # 4) 失效快捷方式
        if shortcut_files:
            ok_count = 0
            for p in shortcut_files:
                try:
                    if os.path.isfile(p):
                        os.remove(p)
                        ok_count += 1
                except (OSError, PermissionError):
                    pass
            logs.append(f"删除失效快捷方式：{ok_count} 个")
            _bump("失效快捷方式清理完成")

        # 5) Windows.old
        if windows_old:
            if _sysreclaim_mod:
                try:
                    before = get_size_safe("C:\\Windows.old")
                    ok, msg = _sysreclaim_mod.SysReclaimEngine().delete_windows_old()
                    logs.append(f"Windows.old：{msg}")
                    if ok:
                        freed += before
                except Exception as e:
                    logs.append(f"删除 Windows.old 失败：{e}")
            else:
                logs.append("系统回收引擎不可用，已跳过 Windows.old")
            _bump("旧系统备份处理完成")

        # 6) 组件存储
        if component_store:
            if _sysreclaim_mod:
                try:
                    engine = _sysreclaim_mod.SysReclaimEngine()
                    info_before = engine.analyze_component_store()
                    before = info_before.get("component_store_size", 0) if info_before.get("success") else 0
                    ok, msg = engine.clean_component_store(reset_base=False)
                    logs.append(f"组件存储清理：{msg}")
                    if ok and before:
                        # DISM 清理后释放量无法精确获取，此处仅记录已执行的清理
                        logs.append("（组件存储清理的精确释放量需系统重新统计后显示）")
                except Exception as e:
                    logs.append(f"组件存储清理失败：{e}")
            else:
                logs.append("系统回收引擎不可用，已跳过组件存储")
            _bump("组件存储清理完成")

        # 7) 还原点
        if restore_points:
            if _sysreclaim_mod:
                try:
                    ok, msg = _sysreclaim_mod.SysReclaimEngine().delete_restore_points("oldest")
                    logs.append(f"系统还原点：{msg}（仅删除最旧的一个，保留回滚能力）")
                except Exception as e:
                    logs.append(f"删除还原点失败：{e}")
            else:
                logs.append("系统回收引擎不可用，已跳过还原点")
            _bump("还原点处理完成")

        # 8) 通用删除
        if generic:
            try:
                sub_results = [r for r in generic if getattr(r, "size", 0) > 0 or True]
                # 借用现有清理引擎的分派逻辑；它已处理临时目录/回收站/浏览器缓存/休眠文件等
                freed_generic = self.clean_engine.clean_results(sub_results)
                freed += freed_generic
                logs.append(f"文件清理：释放 {format_size(freed_generic)}")
            except Exception as e:
                logs.append(f"文件清理失败：{e}")
            _bump("文件清理完成")

        if skipped_hints:
            logs.append(
                f"已跳过 {skipped_hints} 个提示类项目（如大文件、硬件告警、驱动仓库、SSD 优化建议、"
                "文件粉碎建议等）。这些项目需要您确认后在对应功能页手动处理，"
                "本工具不会自动删除它们，以免造成不可逆损失。")

        return freed, logs

    # ==================== 软件压缩 ====================

    def _show_compress_view(self):
        self.section_title(self.content, "软件压缩",
                           "使用 Windows NTFS 压缩功能压缩软件目录，在不影响使用的前提下节省磁盘空间。需要管理员权限。")

        card = self.card(self.content, pady=(0, 10))
        inner = tk.Frame(card, bg=Theme.CARD_BG)
        inner.pack(fill=tk.X, padx=16, pady=14)

        tk.Label(inner, text="选择要压缩的软件目录：", bg=Theme.CARD_BG, fg=Theme.TEXT,
                 font=("Microsoft YaHei", 10)).pack(anchor=tk.W)

        row = tk.Frame(inner, bg=Theme.CARD_BG)
        row.pack(fill=tk.X, pady=(6, 10))
        self.compress_path = tk.StringVar()
        tk.Entry(row, textvariable=self.compress_path, font=("Microsoft YaHei", 9),
                 width=64).pack(side=tk.LEFT, padx=(0, 8))
        self.button(row, "浏览...", self._browse_compress_dir,
                    kind="ghost").pack(side=tk.LEFT)

        quick = tk.Frame(inner, bg=Theme.CARD_BG)
        quick.pack(fill=tk.X, pady=(0, 8))
        tk.Label(quick, text="快捷选择：", bg=Theme.CARD_BG, fg=Theme.TEXT_LIGHT,
                 font=("Microsoft YaHei", 9)).pack(side=tk.LEFT)
        for prog_dir in (r"C:\Program Files", r"C:\Program Files (x86)"):
            if not os.path.isdir(prog_dir):
                continue
            try:
                dirs = sorted([d for d in os.listdir(prog_dir)
                               if os.path.isdir(os.path.join(prog_dir, d))])[:10]
            except (OSError, PermissionError):
                dirs = []
            for d in dirs:
                full = os.path.join(prog_dir, d)
                self.button(quick, d[:18],
                            lambda p=full: self.compress_path.set(p),
                            kind="ghost").pack(side=tk.LEFT, padx=2, pady=2)

        self.compress_progress = ttk.Progressbar(inner, mode="determinate")
        self.compress_progress.pack(fill=tk.X, pady=(4, 6))
        self.compress_status = tk.Label(inner, text="就绪", bg=Theme.CARD_BG,
                                        fg=Theme.TEXT_LIGHT,
                                        font=("Microsoft YaHei", 9), anchor=tk.W)
        self.compress_status.pack(anchor=tk.W)

        self.button(inner, "🗜️ 开始压缩", self._start_compress).pack(anchor=tk.W, pady=(10, 0))

        self.hint(self.content,
                  "说明：压缩后文件仍可正常读写，但 CPU 占用会略有增加。"
                  "对已在使用中的软件压缩，建议先关闭该软件。")

    def _browse_compress_dir(self):
        from tkinter import filedialog
        d = filedialog.askdirectory(title="选择要压缩的软件目录")
        if d:
            self.compress_path.set(d)

    def _start_compress(self):
        path = self.compress_path.get().strip()
        if not path or not os.path.isdir(path):
            self.show_warning("提示", "请选择有效的目录")
            return
        if not is_admin():
            self.show_warning("需要管理员权限",
                              "软件压缩需要管理员权限，请点击左下角「以管理员身份重启」后再试。")
            return

        self.compress_progress["value"] = 0

        def work(progress_cb):
            self.clean_engine.progress_callback = progress_cb
            return self.clean_engine.compress_software(path)

        def on_progress(percent, msg):
            self.compress_progress["value"] = percent
            self.compress_status.config(text=msg)

        def on_done(ok):
            self.compress_status.config(
                text="压缩完成" if ok else "压缩未完成，请检查权限或目录占用情况")

        self.run_async(work, on_done=on_done, on_progress=on_progress)

    # ==================== 磁盘碎片整理 ====================

    def _show_defrag_view(self):
        self.section_title(self.content, "磁盘碎片整理",
                           "整理磁盘碎片、优化文件存储布局。需要管理员权限，机械硬盘效果明显，SSD 一般无需整理。")

        card = self.card(self.content, pady=(0, 10))
        inner = tk.Frame(card, bg=Theme.CARD_BG)
        inner.pack(fill=tk.X, padx=16, pady=14)

        tk.Label(inner, text="选择盘符：", bg=Theme.CARD_BG, fg=Theme.TEXT,
                 font=("Microsoft YaHei", 10)).pack(anchor=tk.W)
        row = tk.Frame(inner, bg=Theme.CARD_BG)
        row.pack(anchor=tk.W, pady=(6, 10))
        self.defrag_drive = tk.StringVar(value="C")
        for drv in ("C", "D", "E", "F"):
            if os.path.exists(drv + ":\\"):
                tk.Radiobutton(row, text=f"{drv}: 盘", variable=self.defrag_drive,
                               value=drv, bg=Theme.CARD_BG, fg=Theme.TEXT,
                               font=("Microsoft YaHei", 9),
                               activebackground=Theme.CARD_BG).pack(side=tk.LEFT, padx=12)

        self.defrag_progress = ttk.Progressbar(inner, mode="indeterminate")
        self.defrag_progress.pack(fill=tk.X, pady=(4, 6))
        self.defrag_status = tk.Label(inner, text="就绪", bg=Theme.CARD_BG,
                                      fg=Theme.TEXT_LIGHT,
                                      font=("Microsoft YaHei", 9), anchor=tk.W)
        self.defrag_status.pack(anchor=tk.W)

        self.button(inner, "💿 开始整理", self._start_defrag).pack(anchor=tk.W, pady=(10, 0))

        self.hint(self.content,
                  "⚠ 碎片整理耗时较长（机械盘可能数十分钟），过程中请勿断电或强制关机。"
                  "若为固态硬盘，Windows 会自动执行 TRIM 而非传统整理。",
                  color=Theme.WARNING)

    def _start_defrag(self):
        if not is_admin():
            self.show_warning("需要管理员权限",
                              "碎片整理需要管理员权限，请点击左下角「以管理员身份重启」后再试。")
            return
        drive = self.defrag_drive.get()
        if not self.ask_yes_no("确认", f"即将整理 {drive}: 盘碎片，耗时可能较长。是否继续？"):
            return

        self.defrag_progress.start(10)
        self.defrag_status.config(text=f"正在整理 {drive}: 盘...", fg=Theme.PRIMARY)

        def work(progress_cb):
            self.clean_engine.progress_callback = progress_cb
            return self.clean_engine.defragment(drive)

        def on_progress(_percent, msg):
            self.defrag_status.config(text=msg)

        def on_done(ok):
            self.defrag_progress.stop()
            self.defrag_status.config(text="整理完成" if ok else "整理未完成或超时",
                                      fg=Theme.ACCENT if ok else Theme.DANGER)
            self.show_info("结果", "碎片整理完成！" if ok else "碎片整理未完成，请稍后重试。")

        self.run_async(work, on_done=on_done, on_progress=on_progress)

    # ==================== 虚拟内存转移 ====================

    def _show_pagefile_view(self):
        self.section_title(self.content, "虚拟内存转移",
                           "把 pagefile.sys 从系统盘转移到其他盘，直接释放与物理内存同等量级的 C 盘空间。")

        card = self.card(self.content, pady=(0, 10))
        inner = tk.Frame(card, bg=Theme.CARD_BG)
        inner.pack(fill=tk.X, padx=16, pady=14)

        pagefile = "C:\\pagefile.sys"
        size = get_size_safe(pagefile)
        info = (f"C 盘虚拟内存文件当前大小：{format_size(size)}" if size > 0
                else "未检测到 C 盘的 pagefile.sys（可能已被转移到其他盘或已禁用）")
        tk.Label(inner, text=info, bg=Theme.CARD_BG, fg=Theme.PRIMARY,
                 font=("Microsoft YaHei", 11, "bold")).pack(anchor=tk.W, pady=(0, 12))

        tk.Label(inner, text="选择转移目标盘：", bg=Theme.CARD_BG, fg=Theme.TEXT,
                 font=("Microsoft YaHei", 10)).pack(anchor=tk.W)
        row = tk.Frame(inner, bg=Theme.CARD_BG)
        row.pack(anchor=tk.W, pady=(6, 12))
        self.pagefile_drive = tk.StringVar(value="D")
        for drv in ("D", "E", "F"):
            if os.path.exists(drv + ":\\"):
                tk.Radiobutton(row, text=f"{drv}: 盘", variable=self.pagefile_drive,
                               value=drv, bg=Theme.CARD_BG, fg=Theme.TEXT,
                               font=("Microsoft YaHei", 9),
                               activebackground=Theme.CARD_BG).pack(side=tk.LEFT, padx=12)
        if all(not os.path.exists(d + ":\\") for d in ("D", "E", "F")):
            tk.Label(row, text="未检测到其他可用磁盘，无法转移", bg=Theme.CARD_BG,
                     fg=Theme.DANGER, font=("Microsoft YaHei", 9)).pack(side=tk.LEFT)

        self.button(inner, "🔄 转移虚拟内存", self._do_move_pagefile).pack(anchor=tk.W)

        self.hint(self.content,
                  "⚠ 转移后需要重启电脑才会生效。建议目标盘剩余空间大于物理内存容量。"
                  "修改前请确认已保存工作内容。",
                  color=Theme.WARNING)

    def _do_move_pagefile(self):
        if not is_admin():
            self.show_warning("需要管理员权限", "请点击左下角「以管理员身份重启」后再试。")
            return
        target = self.pagefile_drive.get()
        if not self.ask_yes_no("确认", f"即将把虚拟内存转移到 {target}: 盘，重启后生效。是否继续？"):
            return

        def work(progress_cb):
            self.clean_engine.progress_callback = progress_cb
            return self.clean_engine.move_pagefile(target)

        def on_done(ok):
            if ok:
                self.show_info("完成", f"虚拟内存已设置转移到 {target}: 盘，请重启电脑使设置生效。")
            else:
                self.show_error("失败", "转移失败。可尝试在「系统属性 > 高级 > 性能设置 > 高级 > 虚拟内存」中手动修改。")

        self.run_async(work, on_done=on_done)

    # ==================== 内存清理 ====================

    def _show_memory_view(self):
        self.section_title(self.content, "内存清理",
                           "释放被后台程序占用的物理内存，缓解卡顿。")

        card = self.card(self.content, pady=(0, 10))
        inner = tk.Frame(card, bg=Theme.CARD_BG)
        inner.pack(fill=tk.X, padx=16, pady=20)

        self.mem_status = tk.Label(inner, text="点击下方按钮开始清理内存",
                                   bg=Theme.CARD_BG, fg=Theme.TEXT,
                                   font=("Microsoft YaHei", 12))
        self.mem_status.pack(pady=(0, 16))

        self.button(inner, "🧠 一键清理内存", self._clean_memory,
                    kind="primary").pack()

        self.hint(self.content,
                  "说明：内存清理通过释放进程空闲工作集来实现，效果是暂时的。"
                  "若内存长期占用过高，建议在「启动项管理」中减少开机自启程序。")

    def _clean_memory(self):
        self.mem_status.config(text="正在清理内存...", fg=Theme.PRIMARY)

        def work(progress_cb):
            self.clean_engine.progress_callback = progress_cb
            return self.clean_engine.clean_memory()

        def on_done(result):
            ok = result.get("success")
            freed = result.get("freed_bytes", 0)
            procs = result.get("processes", 0)
            if ok:
                self.mem_status.config(
                    text=f"已回收 {procs} 个进程的工作集，可用内存增加约 {format_size(freed)}",
                    fg=Theme.ACCENT)
            else:
                self.mem_status.config(text=result.get("message", "内存清理未完成"),
                                       fg=Theme.DANGER)

        self.run_async(work, on_done=on_done)

    # ==================== 管理员重启 ====================

    def _restart_as_admin(self):
        if is_admin():
            self.show_info("提示", "当前已拥有管理员权限")
            return
        if self.ask_yes_no("确认", "将以管理员身份重新启动本程序，当前窗口会关闭。是否继续？"):
            try:
                run_as_admin()
                self.root.destroy()
            except Exception as e:
                self.show_error("失败", f"无法获取管理员权限：{e}")


# ==================== 辅助函数 ====================

def get_size_safe(path):
    """安全获取路径体积"""
    try:
        if not os.path.exists(path):
            return 0
        if os.path.isfile(path):
            return os.path.getsize(path)
        total = 0
        for dirpath, _dirnames, filenames in os.walk(path, onerror=lambda e: None):
            for f in filenames:
                try:
                    total += os.path.getsize(os.path.join(dirpath, f))
                except (OSError, PermissionError):
                    pass
        return total
    except (OSError, PermissionError):
        return 0


def main():
    # 无人值守模式：计划任务在打包为 exe 后同样可用
    #   "C盘清理大师.exe" --auto-clean         快速清理
    #   "C盘清理大师.exe" --auto-clean --full  完整清理（含微信/QQ 缓存）
    if "--auto-clean" in sys.argv:
        full = "--full" in sys.argv
        try:
            import auto_clean_runner
            return auto_clean_runner.run(mode="full" if full else "quick", quiet=True)
        except Exception as e:
            # 无控制台环境下无法打印，写入日志文件便于排查
            try:
                log_dir = os.path.join(os.environ.get("USERPROFILE", "."), "CleanMasterBackup")
                os.makedirs(log_dir, exist_ok=True)
                with open(os.path.join(log_dir, "auto_clean.log"), "a", encoding="utf-8") as f:
                    from datetime import datetime
                    f.write(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] 自动清理启动失败：{e}\n")
            except Exception:
                pass
            return 1

    root = tk.Tk()
    try:
        # 让界面在高分屏上更清晰
        from ctypes import windll
        windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    CleanerApp(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
