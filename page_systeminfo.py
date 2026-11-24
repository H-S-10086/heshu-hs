# -*- coding: utf-8 -*-
"""
系统信息与网络页面（page_systeminfo）

对标鲁大师 / Speccy 的硬件总览，叠加硬盘健康、实时占用、网络工具四块能力：
  标签页 1「硬件总览」   ：分组卡片展示 CPU / 内存 / 硬盘 / 显卡 / 主板 / 操作系统 / 电池
  标签页 2「实时占用与温度」：每 2 秒刷新 CPU/内存/磁盘读写，并可读取温度传感器
  标签页 3「硬盘健康」    ：SMART 健康、SSD/TRIM、磁盘错误检查、电池信息
  标签页 4「网络工具」    ：网络信息、测速、DNS 优选、LSP / hosts / 连通性诊断

设计约束：
  - 仅使用标准库 + tkinter，以及 scanner.format_size、hardware_info.HardwareEngine、
    network_tools.NetworkEngine，不引入任何第三方依赖。
  - 本模块 import 时不产生任何副作用（不创建窗口、不执行任何检测）。
  - 所有耗时操作统一走 app.run_async 放到后台线程执行，绝不在主线程直接跑。
  - 所有引擎调用均以 try/except 包裹，异常通过 app.show_error 提示，绝不向界面冒泡。
  - 读不到的数据一律如实显示"未获取到"/"不可用"，绝不显示占位假数据。
"""

import tkinter as tk
from tkinter import ttk

from scanner import format_size
from hardware_info import HardwareEngine

# 网络引擎可能尚未就绪，容错导入：失败时页面仍可正常构建，网络标签页给出提示
try:
    from network_tools import NetworkEngine
except Exception:
    NetworkEngine = None


def _pack_if_needed(widget, **kw):
    """若控件尚未被几何管理器接管，则按给定参数 pack，避免重复布局"""
    try:
        if not widget.winfo_manager():
            widget.pack(**kw)
    except Exception:
        pass


class SystemInfoPage:
    """系统信息与网络页面（由 build(app, parent) 实例化）"""

    def __init__(self, app, parent):
        self.app = app
        self.parent = parent
        self.TH = app.THEME

        # 运行态标记
        self._last_error = ""
        self._hw_busy = False
        self._disk_busy = False
        self._net_busy = False

        # 实时刷新相关
        self._alive = False
        self._page_dead = False
        self._refreshing = False
        self._live_frame = None

        # 控件引用（在 _build 中创建）
        self._hw_inner = None
        self._hw_progress = None
        self._hw_progress_label = None
        self._hw_refresh_btn = None

        self._live_labels = {}
        self._live_detail = None
        self._temp_tree = None
        self._temp_btn = None

        self._disk_tree = None
        self._ssd_tree = None
        self._disk_scan_area = None
        self._disk_action_area = None
        self._battery_area = None

        self._net_info_area = None
        self._speed_value_label = None
        self._speed_buttons = []
        self._speed_detail_label = None
        self._dns_tree = None
        self._dns_ip_map = {}
        self._lsp_area = None
        self._hosts_tree = None
        self._diag_tree = None

        self._build()

        # 页面被切走时标记，避免后台任务回调再操作已销毁的控件
        try:
            self.parent.bind("<Destroy>", self._on_page_destroy, add="+")
        except Exception:
            pass

    # ==================== 页面构建 ====================

    def _build(self):
        self.app.section_title(
            self.parent, "系统信息与网络",
            "硬件状态监测、硬盘健康检查与网络优化工具。",
        )
        nb = ttk.Notebook(self.parent)
        nb.pack(fill=tk.BOTH, expand=True, padx=12, pady=(0, 10))

        tab1 = tk.Frame(nb, bg=self.TH.BG)
        tab2 = tk.Frame(nb, bg=self.TH.BG)
        tab3 = tk.Frame(nb, bg=self.TH.BG)
        tab4 = tk.Frame(nb, bg=self.TH.BG)
        nb.add(tab1, text="硬件总览")
        nb.add(tab2, text="实时占用与温度")
        nb.add(tab3, text="硬盘健康")
        nb.add(tab4, text="网络工具")

        self._build_hardware_tab(tab1)
        self._build_live_tab(tab2)
        self._build_disk_tab(tab3)
        self._build_network_tab(tab4)

        # 初始加载
        self._load_hardware()

    # ---------- 通用小工具 ----------

    def _card(self, parent, fill=tk.X, padx=14, pady=(0, 10)):
        """创建白色卡片（app.card 默认已 pack，这里按需覆盖边距）"""
        try:
            return self.app.card(parent, fill=fill, padx=padx, pady=pady)
        except Exception:
            frame = tk.Frame(parent, bg=self.TH.CARD_BG)
            frame.pack(fill=fill, padx=padx, pady=pady)
            return frame

    def _card_body(self, card, padx=15, pady=(12, 6)):
        body = tk.Frame(card, bg=self.TH.CARD_BG)
        body.pack(fill=tk.X, padx=padx, pady=pady)
        return body

    def _card_title(self, card, text):
        tk.Label(
            card, text=text, bg=self.TH.CARD_BG, fg=self.TH.TEXT,
            font=("Microsoft YaHei", 11, "bold"), anchor=tk.W,
        ).pack(anchor=tk.W, padx=15, pady=(12, 2))

    def _hint(self, parent, text, color=None):
        tk.Label(
            parent, text=text, bg=self.TH.CARD_BG, fg=color or self.TH.TEXT_LIGHT,
            font=("Microsoft YaHei", 9), justify=tk.LEFT, anchor=tk.W, wraplength=940,
        ).pack(anchor=tk.W, padx=15, pady=(0, 10))

    def _blank_placeholder(self, parent, text="未获取到该项信息（可能需要管理员权限）"):
        tk.Label(
            parent, text=text, bg=self.TH.CARD_BG, fg=self.TH.TEXT_LIGHT,
            font=("Microsoft YaHei", 9), justify=tk.LEFT, anchor=tk.W, wraplength=440,
        ).pack(anchor=tk.W, pady=(2, 4))

    def _scrollable(self, parent):
        """创建可滚动容器，返回 (outer_frame, inner_frame)"""
        bg = self.TH.BG
        outer = tk.Frame(parent, bg=bg)
        outer.pack(fill=tk.BOTH, expand=True)
        canvas = tk.Canvas(outer, bg=bg, highlightthickness=0)
        vsb = ttk.Scrollbar(outer, orient="vertical", command=canvas.yview)
        inner = tk.Frame(canvas, bg=bg)
        win = canvas.create_window((0, 0), window=inner, anchor="nw")
        canvas.configure(yscrollcommand=vsb.set)
        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)

        def _on_inner(_e):
            try:
                canvas.configure(scrollregion=canvas.bbox("all"))
            except Exception:
                pass

        def _on_canvas(event):
            try:
                canvas.itemconfigure(win, width=event.width)
            except Exception:
                pass

        def _on_wheel(event):
            try:
                canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")
            except Exception:
                pass

        inner.bind("<Configure>", _on_inner)
        canvas.bind("<Configure>", _on_canvas)
        canvas.bind("<Enter>", lambda e: canvas.bind_all("<MouseWheel>", _on_wheel))
        canvas.bind("<Leave>", lambda e: canvas.unbind_all("<MouseWheel>"))
        return outer, inner

    # ---------- 状态 / 异步 / 弹窗封装 ----------

    def _set_status(self, text):
        try:
            self.app.set_status(text)
        except Exception:
            pass

    @staticmethod
    def _set_progress(bar, label, percent):
        try:
            if bar is not None:
                bar["value"] = percent
            if label is not None:
                label.config(text=f"{percent}%")
        except Exception:
            pass

    def _run(self, work, done, on_progress=None):
        """
        统一封装 app.run_async。

        后台任务完成时页面可能已经被切走（控件已销毁），
        此时再碰控件会抛 TclError: bad window path name。
        所以回调统一包一层存活检查，页面没了就静默丢弃结果。
        """
        def guard(fn):
            if fn is None:
                return None

            def wrapped(*args, **kwargs):
                if not self._ui_alive():
                    return None
                return fn(*args, **kwargs)

            return wrapped

        try:
            self.app.run_async(work, on_done=guard(done),
                               on_progress=guard(on_progress))
        except Exception as e:
            if self._ui_alive():
                self._error("操作失败", f"无法启动后台任务：{e}")

    def _ui_alive(self):
        """页面控件是否还活着（用于拦截迟到的后台回调）"""
        if getattr(self, "_page_dead", False):
            return False
        try:
            return bool(self.parent is not None and self.parent.winfo_exists())
        except Exception:
            return False

    def _on_page_destroy(self, event):
        if event.widget is self.parent:
            self._page_dead = True

    def _error(self, title, msg):
        try:
            self.app.show_error(title, msg)
        except Exception:
            pass

    def _info(self, title, msg):
        try:
            self.app.show_info(title, msg)
        except Exception:
            pass

    def _warn(self, title, msg):
        try:
            self.app.show_warning(title, msg)
        except Exception:
            pass

    def _confirm(self, title, msg):
        try:
            return bool(self.app.ask_yes_no(title, msg))
        except Exception:
            return False

    def _fmt_size(self, value):
        """容量格式化：优先用 app.format_size，回退到模块内 format_size"""
        try:
            return self.app.format_size(value)
        except Exception:
            try:
                return format_size(value)
            except Exception:
                return str(value)

    # ---------- 值格式化 ----------

    @staticmethod
    def _fmt_value(v):
        """把任意值转成可读中文；None / 空一律显示"未获取到"，bool 显示是/否"""
        if v is None:
            return "未获取到"
        if isinstance(v, bool):
            return "是" if v else "否"
        if isinstance(v, (list, tuple)):
            parts = [str(x) for x in v if x is not None and str(x) != ""]
            return "、".join(parts) if parts else "未获取到"
        s = str(v).strip()
        return s if s != "" else "未获取到"

    @staticmethod
    def _has_data(data):
        """判断分组数据是否含有真实内容（空 dict / 空 list / 全 None 视为无数据）"""
        if data is None:
            return False
        if isinstance(data, dict):
            if not data:
                return False
            for v in data.values():
                if isinstance(v, bool):
                    return True
                if v not in (None, "", [], {}):
                    return True
            return False
        if isinstance(data, (list, tuple)):
            return len(data) > 0
        return True

    def _kv_row(self, parent, key, value, indent=0):
        """一行「键：值」：左列灰色键名固定宽度，右列深色值"""
        row = tk.Frame(parent, bg=self.TH.CARD_BG)
        row.pack(fill=tk.X, pady=1, padx=(indent * 16, 0))
        tk.Label(
            row, text=f"{key}：", width=9, anchor=tk.NW, justify=tk.LEFT,
            bg=self.TH.CARD_BG, fg=self.TH.TEXT_LIGHT, font=("Microsoft YaHei", 9),
        ).pack(side=tk.LEFT)
        tk.Label(
            row, text=self._fmt_value(value), anchor=tk.W, justify=tk.LEFT,
            bg=self.TH.CARD_BG, fg=self.TH.TEXT, font=("Microsoft YaHei", 9),
            wraplength=380,
        ).pack(side=tk.LEFT, fill=tk.X, expand=True)

    def _fill_group(self, body, title, data):
        """把某分组的数据填充到 body 中；无数据时显示占位说明（绝不造假）"""
        # 电池简要：{"存在": False} 表示没有电池
        if isinstance(data, dict) and "存在" in data and data.get("存在") is False:
            self._blank_placeholder(body, "未检测到电池（台式机或电池驱动未上报）。")
            return

        if not self._has_data(data):
            self._blank_placeholder(body)
            return

        if isinstance(data, dict):
            for key, value in data.items():
                if isinstance(value, list):
                    if not value:
                        self._kv_row(body, key, "未获取到")
                    else:
                        for idx, item in enumerate(value, 1):
                            tk.Label(
                                body, text=f"{key} {idx}", bg=self.TH.CARD_BG,
                                fg=self.TH.TEXT, font=("Microsoft YaHei", 9, "bold"),
                                anchor=tk.W,
                            ).pack(anchor=tk.W, pady=(4, 1))
                            if isinstance(item, dict):
                                for ik, iv in item.items():
                                    self._kv_row(body, ik, iv, indent=1)
                            else:
                                self._kv_row(body, "值", item, indent=1)
                else:
                    self._kv_row(body, key, value)
        elif isinstance(data, list):
            for idx, item in enumerate(data, 1):
                tk.Label(
                    body, text=f"{title} {idx}", bg=self.TH.CARD_BG,
                    fg=self.TH.TEXT, font=("Microsoft YaHei", 9, "bold"), anchor=tk.W,
                ).pack(anchor=tk.W, pady=(4, 1))
                if isinstance(item, dict):
                    for ik, iv in item.items():
                        self._kv_row(body, ik, iv, indent=1)
                else:
                    self._kv_row(body, "值", item, indent=1)

    def _group_card(self, parent, title, data):
        """构建单个分组卡片（未 pack，交由调用方 grid/pack）"""
        card = tk.Frame(
            parent, bg=self.TH.CARD_BG,
            highlightbackground=self.TH.BORDER, highlightthickness=1,
        )
        tk.Label(
            card, text=title, bg=self.TH.CARD_BG, fg=self.TH.TEXT,
            font=("Microsoft YaHei", 11, "bold"), anchor=tk.W,
        ).pack(anchor=tk.W, padx=14, pady=(10, 4))
        body = tk.Frame(card, bg=self.TH.CARD_BG)
        body.pack(fill=tk.BOTH, expand=True, padx=14, pady=(0, 10))
        self._fill_group(body, title, data)
        return card

    # ==================== 标签页 1：硬件总览 ====================

    def _build_hardware_tab(self, tab):
        toolbar = self._card(tab, pady=(10, 8))
        bar = tk.Frame(toolbar, bg=self.TH.CARD_BG)
        bar.pack(fill=tk.X, padx=15, pady=(12, 4))
        self._hw_refresh_btn = self.app.button(bar, "刷新硬件信息", self._load_hardware, kind="primary")
        self._hw_refresh_btn.pack(side=tk.LEFT)
        tk.Label(
            bar, text="数据来自 Windows 系统接口，读不到的项目会如实显示“未获取到”，不做任何估算。",
            bg=self.TH.CARD_BG, fg=self.TH.TEXT_LIGHT, font=("Microsoft YaHei", 9),
        ).pack(side=tk.LEFT, padx=(12, 0))

        self._hw_progress, self._hw_progress_label = self.app.progress_row(toolbar)
        try:
            frame = self._hw_progress.master
            frame.configure(bg=self.TH.CARD_BG)
            for child in frame.winfo_children():
                if isinstance(child, tk.Label):
                    child.configure(bg=self.TH.CARD_BG)
        except Exception:
            pass

        _outer, self._hw_inner = self._scrollable(tab)

    def _load_hardware(self):
        if self._hw_busy:
            return
        self._hw_busy = True
        self._last_error = ""
        try:
            self._hw_refresh_btn.config(state=tk.DISABLED)
        except Exception:
            pass
        self._set_progress(self._hw_progress, self._hw_progress_label, 0)
        self._set_status("正在读取硬件信息...")

        def work(progress_cb):
            try:
                return HardwareEngine(progress_callback=progress_cb).get_hardware_info()
            except Exception as e:
                self._last_error = f"读取硬件信息失败：{e}"
                return None

        def on_progress(percent, _msg):
            self._set_progress(self._hw_progress, self._hw_progress_label, percent)

        def done(result):
            self._hw_busy = False
            try:
                self._hw_refresh_btn.config(state=tk.NORMAL)
            except Exception:
                pass
            self._set_progress(self._hw_progress, self._hw_progress_label, 100)
            if result is None:
                self._error("读取失败", self._last_error or "读取硬件信息失败。")
                self._set_status("硬件信息读取失败")
                return
            try:
                self._render_hardware(result)
            except Exception as e:
                self._error("显示失败", f"渲染硬件信息时出错：{e}")
            self._set_status("硬件信息读取完成")

        self._run(work, done, on_progress)

    def _render_hardware(self, info):
        inner = self._hw_inner
        for w in inner.winfo_children():
            w.destroy()
        if not isinstance(info, dict) or not info:
            tk.Label(
                inner, text="未获取到任何硬件信息（可能需要管理员权限）。",
                bg=self.TH.BG, fg=self.TH.TEXT_LIGHT, font=("Microsoft YaHei", 10),
            ).pack(anchor=tk.W, padx=20, pady=16)
            return

        grid = tk.Frame(inner, bg=self.TH.BG)
        grid.pack(fill=tk.BOTH, expand=True, padx=24, pady=(0, 12))
        grid.columnconfigure(0, weight=1)
        grid.columnconfigure(1, weight=1)

        grid_map = (
            ("cpu", "CPU", 0, 0),
            ("memory", "内存", 0, 1),
            ("os", "操作系统", 1, 0),
            ("motherboard", "主板", 1, 1),
        )
        for key, title, row, col in grid_map:
            card = self._group_card(grid, title, info.get(key))
            card.grid(row=row, column=col, sticky="nsew", padx=6, pady=6)

        for row, (key, title) in enumerate((("disk", "硬盘"), ("gpu", "显卡"), ("battery", "电池")), start=2):
            card = self._group_card(grid, title, info.get(key))
            card.grid(row=row, column=0, columnspan=2, sticky="nsew", padx=6, pady=6)

    # ==================== 标签页 2：实时占用与温度 ====================

    def _build_live_tab(self, tab):
        self._alive = True
        self._refreshing = False
        self._live_frame = tab
        # 被销毁时停止定时器：仅当本标签页自身被销毁时置位
        tab.bind("<Destroy>", self._on_tab_destroy)

        _outer, inner = self._scrollable(tab)

        # 4 个指标卡
        cards = tk.Frame(inner, bg=self.TH.BG)
        cards.pack(fill=tk.X, padx=24, pady=(14, 8))
        for i in range(4):
            cards.columnconfigure(i, weight=1)

        defs = (
            ("cpu", "CPU 占用", self.TH.PRIMARY),
            ("mem", "内存占用", self.TH.ACCENT),
            ("dread", "磁盘读取", self.TH.WARNING),
            ("dwrite", "磁盘写入", getattr(self.TH, "PURPLE", "#722ed1")),
        )
        for i, (key, title, color) in enumerate(defs):
            card = tk.Frame(
                cards, bg=self.TH.CARD_BG,
                highlightbackground=self.TH.BORDER, highlightthickness=1,
            )
            card.grid(row=0, column=i, sticky="nsew", padx=6)
            value_lbl = tk.Label(
                card, text="--", bg=self.TH.CARD_BG, fg=color,
                font=("Microsoft YaHei", 22, "bold"),
            )
            value_lbl.pack(pady=(14, 0))
            tk.Label(
                card, text=title, bg=self.TH.CARD_BG, fg=self.TH.TEXT_LIGHT,
                font=("Microsoft YaHei", 9),
            ).pack()
            detail_lbl = tk.Label(
                card, text="", bg=self.TH.CARD_BG, fg=self.TH.TEXT_LIGHT,
                font=("Microsoft YaHei", 8),
            )
            detail_lbl.pack(pady=(2, 12))
            self._live_labels[key] = value_lbl
            if key == "mem":
                self._live_detail = detail_lbl

        # 温度区块
        temp_card = self._card(inner, fill=tk.X)
        head = self._card_body(temp_card)
        self._temp_btn = self.app.button(head, "读取温度", self._load_temperatures, kind="ghost")
        self._temp_btn.pack(side=tk.LEFT)
        tk.Label(
            head, text="温度来自 ACPI 热区接口，多数机型需管理员权限。",
            bg=self.TH.CARD_BG, fg=self.TH.TEXT_LIGHT, font=("Microsoft YaHei", 9),
        ).pack(side=tk.LEFT, padx=(12, 0))

        self._temp_tree, container = self.app.make_table(
            temp_card, ("sensor", "value", "status"),
            (300, 130, 130),
            headings=("传感器", "读数", "状态"),
            height=5,
        )
        _pack_if_needed(container, fill=tk.BOTH, expand=False, padx=15, pady=(0, 6))
        try:
            container.pack_configure(fill=tk.BOTH, expand=False)
        except Exception:
            pass

        self._hint(
            temp_card,
            "温度与占用来自系统接口。部分机型或非管理员权限下无法读取，此时显示“不可用”而非估算值。",
        )

        # 启动定时刷新
        self._refresh_live()

    def _on_tab_destroy(self, event):
        """标签页被销毁时停止定时器"""
        try:
            if event.widget is self._live_frame:
                self._alive = False
        except Exception:
            pass

    def _refresh_live(self):
        if not self._alive:
            return
        self._schedule_usage()
        try:
            self.app.root.after(2000, self._refresh_live)
        except Exception:
            pass

    def _schedule_usage(self):
        if self._refreshing:
            return
        self._refreshing = True
        self._last_error = ""

        def work(_progress_cb):
            try:
                return HardwareEngine().get_live_usage()
            except Exception as e:
                self._last_error = str(e)
                return None

        def done(result):
            self._refreshing = False
            if not self._alive or result is None:
                return
            try:
                self._render_usage(result)
            except Exception:
                pass

        try:
            self.app.run_async(work, on_done=done)
        except Exception:
            self._refreshing = False

    def _render_usage(self, data):
        if not isinstance(data, dict):
            return
        cpu = data.get("cpu_percent")
        mem = data.get("mem_percent")
        read = data.get("disk_read_mbps")
        write = data.get("disk_write_mbps")

        def _num(v, unit):
            try:
                return f"{float(v):.0f}{unit}" if unit == "%" else f"{float(v):.2f} {unit}"
            except (TypeError, ValueError):
                return "--"

        self._live_labels["cpu"].config(text=_num(cpu, "%"))
        self._live_labels["mem"].config(text=_num(mem, "%"))
        self._live_labels["dread"].config(text=_num(read, "MB/s"))
        self._live_labels["dwrite"].config(text=_num(write, "MB/s"))

        if self._live_detail is not None:
            used = data.get("mem_used")
            total = data.get("mem_total")
            if used and total:
                self._live_detail.config(
                    text=f"已用 {self._fmt_size(used)} / 共 {self._fmt_size(total)}")
            else:
                self._live_detail.config(text="")

    def _load_temperatures(self):
        self._last_error = ""
        try:
            self._temp_btn.config(state=tk.DISABLED)
        except Exception:
            pass
        self._set_status("正在读取温度传感器...")

        def work(progress_cb):
            try:
                return HardwareEngine(progress_callback=progress_cb).get_temperatures()
            except Exception as e:
                self._last_error = f"读取温度失败：{e}"
                return None

        def done(result):
            try:
                self._temp_btn.config(state=tk.NORMAL)
            except Exception:
                pass
            if result is None:
                self._error("读取失败", self._last_error or "读取温度失败。")
                return
            try:
                self._render_temperatures(result)
            except Exception:
                pass
            self._set_status("温度读取完成")

        self._run(work, done)

    def _render_temperatures(self, items):
        tree = self._temp_tree
        if tree is None:
            return
        for iid in tree.get_children():
            tree.delete(iid)
        if not isinstance(items, list) or not items:
            tree.insert("", tk.END, values=("未获取到温度传感器", "不可用", "不可用"))
            return
        for it in items:
            if not isinstance(it, dict):
                continue
            value = it.get("value")
            unit = it.get("unit") or "°C"
            if value is None:
                reading = "不可用"
            else:
                try:
                    reading = f"{float(value):.1f}{unit}"
                except (TypeError, ValueError):
                    reading = str(value)
            tree.insert("", tk.END, values=(
                it.get("name") or "未知传感器", reading, it.get("status") or "未知",
            ))

    # ==================== 标签页 3：硬盘健康 ====================

    def _build_disk_tab(self, tab):
        toolbar = self._card(tab, pady=(10, 8))
        bar = self._card_body(toolbar)
        self.app.button(bar, "刷新硬盘状态", self._load_disk_health, kind="primary").pack(side=tk.LEFT)
        tk.Label(
            bar, text="SMART 与温度/磨损通常需要管理员权限，读不到时显示“未知”/“不可用”。",
            bg=self.TH.CARD_BG, fg=self.TH.TEXT_LIGHT, font=("Microsoft YaHei", 9),
        ).pack(side=tk.LEFT, padx=(12, 0))

        _outer, inner = self._scrollable(tab)

        # 表格 1：硬盘健康
        health_card = self._card(inner, fill=tk.X)
        self._card_title(health_card, "硬盘健康")
        self._disk_tree, container = self.app.make_table(
            health_card,
            ("model", "type", "size", "health", "smart", "temp", "wear", "detail"),
            (200, 80, 90, 90, 110, 70, 110, 240),
            headings=("型号", "类型", "容量", "健康状态", "SMART预测", "温度", "通电/磨损", "详情"),
            height=4,
        )
        _pack_if_needed(container, fill=tk.BOTH, expand=False, padx=15, pady=(0, 8))
        try:
            container.pack_configure(fill=tk.BOTH, expand=False)
        except Exception:
            pass
        try:
            self._disk_tree.tag_configure("bad", foreground=self.TH.DANGER)
        except Exception:
            pass

        # 表格 2：SSD 与 TRIM
        ssd_card = self._card(inner, fill=tk.X)
        self._card_title(ssd_card, "SSD 与 TRIM")
        self._ssd_tree, container = self.app.make_table(
            ssd_card,
            ("drive", "type", "trim", "sys"),
            (90, 130, 160, 90),
            headings=("盘符", "类型", "TRIM 状态", "系统盘"),
            height=4,
        )
        _pack_if_needed(container, fill=tk.BOTH, expand=False, padx=15, pady=(0, 8))
        try:
            container.pack_configure(fill=tk.BOTH, expand=False)
        except Exception:
            pass
        try:
            self._ssd_tree.tag_configure("bad", foreground=self.TH.DANGER)
        except Exception:
            pass

        # 磁盘维护操作
        maint_card = self._card(inner, fill=tk.X)
        self._card_title(maint_card, "磁盘维护")
        mrow = self._card_body(maint_card)
        self.app.button(mrow, "检查磁盘错误", self._do_scan_disk, kind="ghost").pack(side=tk.LEFT)
        self.app.button(mrow, "启用 SSD TRIM 优化", self._do_optimize_ssd, kind="ghost").pack(
            side=tk.LEFT, padx=(8, 0))
        self.app.button(mrow, "安排下次开机检查", self._do_schedule_check, kind="danger").pack(
            side=tk.LEFT, padx=(8, 0))
        self._disk_scan_area = tk.Frame(maint_card, bg=self.TH.CARD_BG)
        self._disk_scan_area.pack(fill=tk.X, padx=15, pady=(0, 12))

        # 电池
        batt_card = self._card(inner, fill=tk.X)
        self._card_title(batt_card, "电池")
        brow = self._card_body(batt_card)
        self.app.button(brow, "读取电池信息", self._load_battery, kind="ghost").pack(side=tk.LEFT)
        self.app.button(brow, "设置充电阈值", self._set_charge_limit, kind="ghost").pack(
            side=tk.LEFT, padx=(8, 0))
        self._battery_area = tk.Frame(batt_card, bg=self.TH.CARD_BG)
        self._battery_area.pack(fill=tk.X, padx=15, pady=(0, 12))

        # 初始自动读取一次硬盘健康
        self._load_disk_health()

    def _load_disk_health(self):
        self._last_error = ""
        self._set_status("正在读取硬盘健康信息...")

        def work(progress_cb):
            try:
                engine = HardwareEngine(progress_callback=progress_cb)
                return engine.get_disk_health(), engine.check_disk_for_ssd()
            except Exception as e:
                self._last_error = f"读取硬盘状态失败：{e}"
                return None

        def done(result):
            if result is None:
                self._error("读取失败", self._last_error or "读取硬盘状态失败。")
                self._set_status("硬盘状态读取失败")
                return
            health, ssd = result
            try:
                self._render_disk_health(health)
                self._render_ssd(ssd)
            except Exception:
                pass
            self._set_status("硬盘状态读取完成")

        self._run(work, done)

    def _render_disk_health(self, items):
        tree = self._disk_tree
        if tree is None:
            return
        for iid in tree.get_children():
            tree.delete(iid)
        if not isinstance(items, list) or not items:
            tree.insert("", tk.END, values=("未获取到硬盘健康信息（可能需要管理员权限）",
                                            "", "", "", "", "", "", ""))
            return
        for it in items:
            if not isinstance(it, dict):
                continue
            health = it.get("health") or "未知"
            temp = it.get("temperature")
            if temp is None:
                temp_txt = "不可用"
            else:
                try:
                    temp_txt = f"{float(temp):.0f}°C"
                except (TypeError, ValueError):
                    temp_txt = str(temp)
            tags = () if health == "健康" else ("bad",)
            tree.insert("", tk.END, values=(
                it.get("model") or "未知",
                it.get("type") or "未知",
                it.get("size") or "未知",
                health,
                it.get("smart_predict") or "无",
                temp_txt,
                it.get("wear_or_hours") or "不可用",
                it.get("status_detail") or "",
            ), tags=tags)

    def _render_ssd(self, items):
        tree = self._ssd_tree
        if tree is None:
            return
        for iid in tree.get_children():
            tree.delete(iid)
        if not isinstance(items, list) or not items:
            tree.insert("", tk.END, values=("未获取到盘符信息", "", "未知", ""))
            return
        for it in items:
            if not isinstance(it, dict):
                continue
            trim = it.get("trim_enabled")
            if trim is True:
                trim_txt = "已启用"
            elif trim is False:
                trim_txt = "已禁用"
            else:
                trim_txt = "未知"
            tags = ("bad",) if trim is False else ()
            tree.insert("", tk.END, values=(
                it.get("drive") or "未知",
                it.get("type") or "未知",
                trim_txt,
                "是" if it.get("is_system") else "否",
            ), tags=tags)

    def _clear_area(self, area):
        if area is None:
            return
        for w in area.winfo_children():
            w.destroy()

    def _render_disk_scan(self, res):
        area = self._disk_scan_area
        self._clear_area(area)
        if area is None:
            return
        if not isinstance(res, dict):
            self._blank_placeholder(area, "未获取到磁盘错误检查结果。")
            return
        total = res.get("total_space")
        free = res.get("free_space")
        dirty = res.get("dirty_bit")
        if dirty is True:
            dirty_txt = "已标记为脏（建议执行磁盘检查）"
        elif dirty is False:
            dirty_txt = "正常（未标记为脏）"
        else:
            dirty_txt = "未知"
        rows = (
            ("盘符", res.get("drive")),
            ("读取错误", res.get("read_errors")),
            ("文件系统", res.get("file_system")),
            ("总容量", self._fmt_size(total) if total else None),
            ("可用空间", self._fmt_size(free) if free else None),
            ("脏位标记", dirty_txt),
            ("结果说明", res.get("message")),
        )
        for key, value in rows:
            self._kv_row(area, key, value)

    def _do_scan_disk(self):
        self._last_error = ""
        self._clear_area(self._disk_scan_area)
        self._set_status("正在检查 C 盘磁盘状态...")

        def work(progress_cb):
            try:
                return HardwareEngine(progress_callback=progress_cb).scan_disk_errors("C:")
            except Exception as e:
                self._last_error = f"检查磁盘错误失败：{e}"
                return None

        def done(result):
            if result is None:
                self._error("检查失败", self._last_error or "检查磁盘错误失败。")
                return
            try:
                self._render_disk_scan(result)
            except Exception:
                pass
            self._set_status("磁盘检查完成")

        self._run(work, done)

    def _do_optimize_ssd(self):
        self._set_status("正在执行 SSD TRIM 优化...")

        def work(progress_cb):
            try:
                return HardwareEngine(progress_callback=progress_cb).optimize_disk_for_ssd()
            except Exception as e:
                return (False, f"SSD 优化失败：{e}")

        def done(result):
            ok = False
            msg = ""
            if isinstance(result, tuple) and len(result) == 2:
                ok, msg = result
            msg = str(msg)
            if ok:
                self._info("SSD 优化完成", msg)
            else:
                self._warn("SSD 优化未执行", msg)
            self._set_status(msg[:60] if msg else "SSD 优化结束")

        self._run(work, done)

    def _do_schedule_check(self):
        if not self._confirm(
            "确认安排磁盘检查",
            "将在下次开机时对 C: 盘执行 chkdsk /F /R 磁盘检查与修复。\n\n"
            "· 该操作耗时可能较长（数十分钟到数小时）\n"
            "· 需要管理员权限，且需要重启电脑才会执行\n"
            "· 执行期间请保持供电，勿强制关机\n\n是否继续？",
        ):
            return
        self._set_status("正在安排下次开机磁盘检查...")

        def work(progress_cb):
            try:
                return HardwareEngine(progress_callback=progress_cb).schedule_disk_check("C:")
            except Exception as e:
                return (False, f"安排磁盘检查失败：{e}")

        def done(result):
            ok, msg = (result[0], result[1]) if isinstance(result, tuple) and len(result) == 2 else (False, "")
            msg = str(msg)
            if ok:
                self._info("已安排磁盘检查", msg)
            else:
                self._warn("未能安排磁盘检查", msg)
            self._set_status(msg[:60] if msg else "安排磁盘检查结束")

        self._run(work, done)

    def _load_battery(self):
        self._last_error = ""
        self._clear_area(self._battery_area)
        self._set_status("正在读取电池信息...")

        def work(progress_cb):
            try:
                return HardwareEngine(progress_callback=progress_cb).get_battery_info()
            except Exception as e:
                self._last_error = f"读取电池信息失败：{e}"
                return None

        def done(result):
            if result is None:
                self._error("读取失败", self._last_error or "读取电池信息失败。")
                return
            try:
                self._render_battery(result)
            except Exception:
                pass
            self._set_status("电池信息读取完成")

        self._run(work, done)

    def _render_battery(self, res):
        area = self._battery_area
        self._clear_area(area)
        if area is None:
            return
        if not isinstance(res, dict):
            self._blank_placeholder(area, "未获取到电池信息。")
            return
        if not res.get("exists"):
            self._blank_placeholder(area, res.get("message") or "未检测到电池（台式机或电池驱动未上报）。")
            return

        def _mwh(v):
            return f"{v} mWh" if v not in (None, "") else None

        hp = res.get("health_percent")
        if hp is None:
            hp_txt = None
        else:
            try:
                hp_txt = f"{float(hp):.1f}%"
            except (TypeError, ValueError):
                hp_txt = str(hp)

        rows = (
            ("设计容量", _mwh(res.get("design_capacity"))),
            ("满充容量", _mwh(res.get("full_capacity"))),
            ("健康度", hp_txt),
            ("循环次数", res.get("cycle_count")),
            ("当前电量", f"{res.get('current_charge')}%" if res.get("current_charge") not in (None, "") else None),
            ("状态", res.get("status")),
            ("说明", res.get("message")),
        )
        for key, value in rows:
            self._kv_row(area, key, value)

        if hp is not None:
            try:
                if float(hp) < 80:
                    tk.Label(
                        area, text="电池健康度低于 80%，建议考虑更换电池。",
                        bg=self.TH.CARD_BG, fg=self.TH.WARNING,
                        font=("Microsoft YaHei", 9, "bold"), anchor=tk.W,
                    ).pack(anchor=tk.W, pady=(4, 0))
            except (TypeError, ValueError):
                pass

    def _set_charge_limit(self):
        self._set_status("正在设置充电阈值...")

        def work(progress_cb):
            try:
                return HardwareEngine(progress_callback=progress_cb).set_battery_charge_limit(80)
            except Exception as e:
                return (False, f"设置充电阈值失败：{e}")

        def done(result):
            # 引擎会返回"不支持"的说明，这是预期行为：如实原样展示，不改写成成功
            ok, msg = (result[0], result[1]) if isinstance(result, tuple) and len(result) == 2 else (False, "")
            msg = str(msg)
            if ok:
                self._info("充电阈值", msg)
            else:
                self._warn("充电阈值", msg)
            self._set_status("充电阈值操作完成")

        self._run(work, done)

    # ==================== 标签页 4：网络工具 ====================

    def _build_network_tab(self, tab):
        _outer, inner = self._scrollable(tab)

        if NetworkEngine is None:
            warn_card = self._card(inner, fill=tk.X, pady=(10, 10))
            tk.Label(
                warn_card, text="网络引擎（network_tools.py）未能加载，网络工具暂不可用。",
                bg=self.TH.CARD_BG, fg=self.TH.DANGER,
                font=("Microsoft YaHei", 10, "bold"), anchor=tk.W,
            ).pack(anchor=tk.W, padx=15, pady=12)

        # ---------- 小卡片 1：网络信息 ----------
        info_card = self._card(inner, fill=tk.X)
        self._card_title(info_card, "网络信息")
        row = self._card_body(info_card)
        self.app.button(row, "读取", self._load_net_info, kind="ghost").pack(side=tk.LEFT)
        self._net_info_area = tk.Frame(info_card, bg=self.TH.CARD_BG)
        self._net_info_area.pack(fill=tk.X, padx=15, pady=(0, 12))

        # ---------- 小卡片 2：网络测速 ----------
        speed_card = self._card(inner, fill=tk.X)
        self._card_title(speed_card, "网络测速")
        srow = self._card_body(speed_card)
        ping_btn = self.app.button(srow, "Ping 测试", self._do_ping, kind="primary")
        ping_btn.pack(side=tk.LEFT)
        dl_btn = self.app.button(srow, "下载测速", self._do_download, kind="primary")
        dl_btn.pack(side=tk.LEFT, padx=(8, 0))
        ul_btn = self.app.button(srow, "上传测速", self._do_upload, kind="primary")
        ul_btn.pack(side=tk.LEFT, padx=(8, 0))
        self._speed_buttons = [ping_btn, dl_btn, ul_btn]

        self._speed_value_label = tk.Label(
            speed_card, text="未测试", bg=self.TH.CARD_BG, fg=self.TH.PRIMARY,
            font=("Microsoft YaHei", 22, "bold"),
        )
        self._speed_value_label.pack(pady=(4, 2))
        self._speed_detail_label = tk.Label(
            speed_card, text="", bg=self.TH.CARD_BG, fg=self.TH.TEXT_LIGHT,
            font=("Microsoft YaHei", 9), wraplength=900, justify=tk.CENTER,
        )
        self._speed_detail_label.pack(pady=(0, 12))

        # ---------- 小卡片 3：DNS 优选 ----------
        dns_card = self._card(inner, fill=tk.X)
        self._card_title(dns_card, "DNS 优选")
        drow = self._card_body(dns_card)
        self.app.button(drow, "开始测速", self._do_measure_dns, kind="primary").pack(side=tk.LEFT)
        tk.Label(
            drow, text="逐个真实测量公共 DNS 的解析延迟，耗时较长，请耐心等待。",
            bg=self.TH.CARD_BG, fg=self.TH.TEXT_LIGHT, font=("Microsoft YaHei", 9),
        ).pack(side=tk.LEFT, padx=(12, 0))

        self._dns_tree, container = self.app.make_table(
            dns_card,
            ("name", "ip", "avg", "status"),
            (220, 160, 140, 120),
            headings=("DNS名称", "IP", "平均延迟", "状态"),
            height=5,
        )
        _pack_if_needed(container, fill=tk.BOTH, expand=False, padx=15, pady=(0, 6))
        try:
            container.pack_configure(fill=tk.BOTH, expand=False)
        except Exception:
            pass

        dbtns = self._card_body(dns_card, pady=(0, 12))
        self.app.button(dbtns, "设为系统 DNS", self._do_set_dns, kind="ghost").pack(side=tk.LEFT)
        self.app.button(dbtns, "恢复自动获取", self._do_reset_dns, kind="ghost").pack(side=tk.LEFT, padx=(8, 0))
        self.app.button(dbtns, "清空 DNS 缓存", self._do_flush_dns, kind="ghost").pack(side=tk.LEFT, padx=(8, 0))
        tk.Label(
            dbtns, text="「设为系统 DNS」请先在表格中选择一行。",
            bg=self.TH.CARD_BG, fg=self.TH.TEXT_LIGHT, font=("Microsoft YaHei", 9),
        ).pack(side=tk.LEFT, padx=(12, 0))

        # ---------- 小卡片 4：网络修复与 hosts ----------
        fix_card = self._card(inner, fill=tk.X)
        self._card_title(fix_card, "网络修复与 hosts")

        frow1 = self._card_body(fix_card, pady=(8, 4))
        self.app.button(frow1, "检查 LSP", self._do_check_lsp, kind="ghost").pack(side=tk.LEFT)
        self.app.button(frow1, "重置 Winsock", self._do_reset_winsock, kind="danger").pack(side=tk.LEFT, padx=(8, 0))
        self.app.button(frow1, "重置 TCP/IP", self._do_reset_tcpip, kind="danger").pack(side=tk.LEFT, padx=(8, 0))
        tk.Label(
            frow1, text="重置 Winsock / TCP/IP 需重启电脑才生效。",
            bg=self.TH.CARD_BG, fg=self.TH.WARNING, font=("Microsoft YaHei", 9),
        ).pack(side=tk.LEFT, padx=(12, 0))

        self._lsp_area = tk.Frame(fix_card, bg=self.TH.CARD_BG)
        self._lsp_area.pack(fill=tk.X, padx=15, pady=(0, 6))

        frow2 = self._card_body(fix_card, pady=(8, 4))
        self.app.button(frow2, "查看 hosts", self._do_read_hosts, kind="ghost").pack(side=tk.LEFT)
        self.app.button(frow2, "备份 hosts", self._do_backup_hosts, kind="ghost").pack(side=tk.LEFT, padx=(8, 0))
        self.app.button(frow2, "重置 hosts", self._do_reset_hosts, kind="danger").pack(side=tk.LEFT, padx=(8, 0))

        self._hosts_tree, container = self.app.make_table(
            fix_card,
            ("ip", "domain", "comment", "enabled"),
            (150, 260, 260, 90),
            headings=("IP", "域名", "备注", "是否启用"),
            height=5,
        )
        _pack_if_needed(container, fill=tk.BOTH, expand=False, padx=15, pady=(0, 6))
        try:
            container.pack_configure(fill=tk.BOTH, expand=False)
        except Exception:
            pass

        frow3 = self._card_body(fix_card, pady=(8, 4))
        self.app.button(frow3, "连通性诊断", self._do_diagnose, kind="primary").pack(side=tk.LEFT)
        tk.Label(
            frow3, text="依次检查本机 IP、网关、DNS 解析、外网与目标网站连通性。",
            bg=self.TH.CARD_BG, fg=self.TH.TEXT_LIGHT, font=("Microsoft YaHei", 9),
        ).pack(side=tk.LEFT, padx=(12, 0))

        self._diag_tree, container = self.app.make_table(
            fix_card,
            ("step", "result", "detail"),
            (150, 90, 560),
            headings=("检查项", "结果", "详情"),
            height=6,
        )
        _pack_if_needed(container, fill=tk.BOTH, expand=False, padx=15, pady=(0, 12))
        try:
            container.pack_configure(fill=tk.BOTH, expand=False)
        except Exception:
            pass
        try:
            self._diag_tree.tag_configure("bad", foreground=self.TH.DANGER)
            self._diag_tree.tag_configure("ok", foreground=self.TH.ACCENT)
        except Exception:
            pass

    # ---------- 网络：引擎与异步封装 ----------

    def _net_engine(self, progress_cb=None):
        if NetworkEngine is None:
            raise RuntimeError("网络引擎（network_tools.py）未加载")
        return NetworkEngine(progress_callback=progress_cb)

    def _net_work(self, func_name, *args, **kwargs):
        """返回一个可在后台线程执行的工作函数，调用 NetworkEngine 的指定方法"""
        def work(progress_cb):
            try:
                engine = self._net_engine(progress_cb)
                return getattr(engine, func_name)(*args, **kwargs)
            except Exception as e:
                self._last_error = f"{func_name} 执行失败：{e}"
                return None
        return work

    # ---------- 网络信息 ----------

    def _load_net_info(self):
        self._last_error = ""
        self._clear_area(self._net_info_area)
        self._set_status("正在读取网络信息...")

        def done(result):
            if result is None:
                self._error("读取失败", self._last_error or "读取网络信息失败。")
                return
            try:
                self._render_net_info(result)
            except Exception:
                pass
            self._set_status("网络信息读取完成")

        self._run(self._net_work("get_network_info"), done)

    def _render_net_info(self, info):
        area = self._net_info_area
        self._clear_area(area)
        if area is None:
            return
        if not isinstance(info, dict) or not info:
            self._blank_placeholder(area, "未获取到网络信息。")
            return
        rows = (
            ("网卡", info.get("adapter")),
            ("IP 地址", info.get("ip")),
            ("默认网关", info.get("gateway")),
            ("DNS 服务器", info.get("dns_servers")),
            ("MAC 地址", info.get("mac")),
            ("连接状态", "已连接" if info.get("is_connected") else "未连接"),
            ("链路速率", info.get("link_speed")),
        )
        for key, value in rows:
            self._kv_row(area, key, value)

    # ---------- 网络测速 ----------

    def _set_speed_state(self, enabled, text=None):
        for btn in self._speed_buttons:
            try:
                btn.config(state=tk.NORMAL if enabled else tk.DISABLED)
            except Exception:
                pass
        if text is not None:
            try:
                self._speed_value_label.config(text=text, fg=self.TH.PRIMARY)
            except Exception:
                pass

    def _do_ping(self):
        self._set_speed_state(False, "测试中...")
        try:
            self._speed_detail_label.config(text="正在 Ping baidu.com（4 次）...")
        except Exception:
            pass
        self._set_status("正在进行 Ping 测试...")

        def done(result):
            self._set_speed_state(True)
            if result is None:
                self._speed_value_label.config(text="失败")
                self._speed_detail_label.config(text=self._last_error or "Ping 测试失败。")
                return
            try:
                if isinstance(result, dict) and result.get("success"):
                    avg = result.get("avg_ms")
                    self._speed_value_label.config(
                        text=f"平均 {float(avg):.0f} ms" if avg is not None else "成功")
                    self._speed_detail_label.config(text=result.get("message") or "")
                else:
                    self._speed_value_label.config(text="失败")
                    self._speed_detail_label.config(
                        text=(result or {}).get("message") or "Ping 测试失败，未获得有效结果。")
            except Exception:
                pass
            self._set_status("Ping 测试完成")

        self._run(self._net_work("test_ping", host="baidu.com", count=4), done)

    def _do_download(self):
        self._set_speed_state(False, "测速中...")
        try:
            self._speed_detail_label.config(text="正在下载测速（约 8 秒），请稍候...")
        except Exception:
            pass
        self._set_status("正在进行下载测速...")

        def done(result):
            self._set_speed_state(True)
            if result is None:
                self._speed_value_label.config(text="失败")
                self._speed_detail_label.config(text=self._last_error or "下载测速失败。")
                return
            try:
                if isinstance(result, dict) and result.get("success"):
                    mbps = result.get("mbps")
                    self._speed_value_label.config(
                        text=f"{float(mbps):.1f} Mbps" if mbps is not None else "成功")
                    self._speed_detail_label.config(text=result.get("message") or "")
                else:
                    self._speed_value_label.config(text="失败")
                    self._speed_detail_label.config(
                        text=(result or {}).get("message") or "下载测速失败。")
            except Exception:
                pass
            self._set_status("下载测速完成")

        self._run(self._net_work("test_download_speed", duration=8), done)

    def _do_upload(self):
        self._set_speed_state(False, "测速中...")
        try:
            self._speed_detail_label.config(text="正在上传测速，请稍候...")
        except Exception:
            pass
        self._set_status("正在进行上传测速...")

        def done(result):
            self._set_speed_state(True)
            if result is None:
                self._speed_value_label.config(text="失败")
                self._speed_detail_label.config(text=self._last_error or "上传测速失败。")
                return
            try:
                if isinstance(result, dict) and result.get("success"):
                    mbps = result.get("mbps")
                    self._speed_value_label.config(
                        text=f"{float(mbps):.1f} Mbps" if mbps is not None else "成功")
                    self._speed_detail_label.config(text=result.get("message") or "")
                else:
                    self._speed_value_label.config(text="失败")
                    self._speed_detail_label.config(
                        text=(result or {}).get("message") or "上传测速失败。")
            except Exception:
                pass
            self._set_status("上传测速完成")

        self._run(self._net_work("test_upload_speed", duration=8), done)

    # ---------- DNS 优选 ----------

    def _do_measure_dns(self):
        self._last_error = ""
        self._dns_ip_map = {}
        tree = self._dns_tree
        if tree is not None:
            for iid in tree.get_children():
                tree.delete(iid)
        self._set_status("正在测量各 DNS 服务器延迟，耗时较长...")

        def done(result):
            if result is None:
                self._error("测速失败", self._last_error or "DNS 测速失败。")
                return
            try:
                self._render_dns(result)
            except Exception:
                pass
            self._set_status("DNS 测速完成")

        self._run(self._net_work("measure_dns_servers"), done)

    def _render_dns(self, items):
        tree = self._dns_tree
        if tree is None:
            return
        for iid in tree.get_children():
            tree.delete(iid)
        self._dns_ip_map = {}
        if not isinstance(items, list) or not items:
            tree.insert("", tk.END, values=("未获取到 DNS 测速结果", "", "不可用", "失败"))
            return
        for it in items:
            if not isinstance(it, dict):
                continue
            avg = it.get("avg_ms")
            if avg is None:
                avg_txt = "不可用"
            else:
                try:
                    avg_txt = f"{float(avg):.1f} ms"
                except (TypeError, ValueError):
                    avg_txt = str(avg)
            status = "成功" if it.get("success") else "失败"
            iid = tree.insert("", tk.END, values=(
                it.get("name") or "未知", it.get("ip") or "", avg_txt, status,
            ))
            ip = it.get("ip") or it.get("dns")
            if ip:
                self._dns_ip_map[iid] = str(ip)

    def _do_set_dns(self):
        tree = self._dns_tree
        sel = ()
        try:
            sel = tree.selection() if tree is not None else ()
        except Exception:
            sel = ()
        if not sel:
            self._warn("提示", "请先在 DNS 表格中选择一行，再点击「设为系统 DNS」。")
            return
        ip = self._dns_ip_map.get(sel[0])
        if not ip:
            self._warn("提示", "未能读取所选行的 IP 地址。")
            return
        if not self._confirm("确认", f"将把当前活动网卡的 DNS 设置为 {ip}。\n\n"
                                     "此操作需要管理员权限，是否继续？"):
            return
        self._set_status(f"正在设置系统 DNS 为 {ip}...")

        def done(result):
            ok, msg = (result[0], result[1]) if isinstance(result, tuple) and len(result) == 2 else (False, "")
            msg = str(msg)
            if ok:
                self._info("DNS 设置成功", msg)
            else:
                self._warn("DNS 设置失败", msg)
            self._set_status("DNS 设置操作完成")

        self._run(self._net_work("set_dns_servers", [ip]), done)

    def _do_reset_dns(self):
        if not self._confirm("确认", "将把当前活动网卡的 DNS 恢复为自动获取（DHCP）。\n"
                                     "此操作需要管理员权限，是否继续？"):
            return
        self._set_status("正在恢复 DNS 自动获取...")

        def done(result):
            ok, msg = (result[0], result[1]) if isinstance(result, tuple) and len(result) == 2 else (False, "")
            msg = str(msg)
            if ok:
                self._info("恢复成功", msg)
            else:
                self._warn("恢复失败", msg)
            self._set_status("DNS 恢复操作完成")

        self._run(self._net_work("reset_dns_servers"), done)

    def _do_flush_dns(self):
        self._set_status("正在刷新 DNS 缓存...")

        def done(result):
            if result:
                self._info("完成", "DNS 解析缓存已刷新。")
            else:
                self._warn("未完成", "刷新 DNS 缓存未成功，请查看状态栏或稍后重试。")
            self._set_status("DNS 缓存刷新完成")

        self._run(self._net_work("flush_dns_cache"), done)

    # ---------- 网络修复与 hosts ----------

    def _render_lsp(self, result):
        area = self._lsp_area
        self._clear_area(area)
        if area is None:
            return
        if not isinstance(result, dict) or not result:
            self._blank_placeholder(area, "未获取到 LSP 检查结果。")
            return
        if not result.get("success"):
            self._kv_row(area, "检查结果", result.get("message") or "无法读取 Winsock 目录")
            return
        rows = (
            ("协议条目数", result.get("entry_count")),
            ("是否可疑", "是（疑似被注入）" if result.get("suspicious") else "否"),
            ("详情", result.get("detail")),
            ("结论", result.get("message")),
        )
        for key, value in rows:
            self._kv_row(area, key, value)

    def _do_check_lsp(self):
        self._last_error = ""
        self._clear_area(self._lsp_area)
        self._set_status("正在检查 Winsock LSP...")

        def done(result):
            if result is None:
                self._error("检查失败", self._last_error or "检查 LSP 失败。")
                return
            try:
                self._render_lsp(result)
            except Exception:
                pass
            self._set_status("LSP 检查完成")

        self._run(self._net_work("check_lsp"), done)

    def _do_reset_winsock(self):
        if not self._confirm(
            "确认重置 Winsock",
            "即将重置 Winsock 目录（netsh winsock reset catalog）。\n\n"
            "· 必须重启电脑才能生效\n"
            "· 可能导致部分安全软件、网络加速/代理软件的 Winsock 挂载失效，需重新安装或初始化\n"
            "· 需要管理员权限\n\n是否继续？",
        ):
            return
        self._set_status("正在重置 Winsock 目录...")

        def done(result):
            ok, msg = (result[0], result[1]) if isinstance(result, tuple) and len(result) == 2 else (False, "")
            msg = str(msg)
            if ok:
                self._info("Winsock 已重置", msg + "\n\n请重启电脑使其生效。")
            else:
                self._warn("重置失败", msg)
            self._set_status("Winsock 重置操作完成")

        self._run(self._net_work("reset_lsp"), done)

    def _do_reset_tcpip(self):
        if not self._confirm(
            "确认重置 TCP/IP",
            "即将重置 TCP/IP 协议栈（netsh int ip reset）。\n\n"
            "· 必须重启电脑才能生效\n"
            "· 可能清除自定义路由、静态 IP 等网络配置\n"
            "· 需要管理员权限\n\n是否继续？",
        ):
            return
        self._set_status("正在重置 TCP/IP 协议栈...")

        def done(result):
            ok, msg = (result[0], result[1]) if isinstance(result, tuple) and len(result) == 2 else (False, "")
            msg = str(msg)
            if ok:
                self._info("TCP/IP 已重置", msg + "\n\n请重启电脑使其生效。")
            else:
                self._warn("重置失败", msg)
            self._set_status("TCP/IP 重置操作完成")

        self._run(self._net_work("reset_tcpip"), done)

    def _do_read_hosts(self):
        self._last_error = ""
        tree = self._hosts_tree
        if tree is not None:
            for iid in tree.get_children():
                tree.delete(iid)
        self._set_status("正在读取 hosts 文件...")

        def done(result):
            if result is None:
                self._error("读取失败", self._last_error or "读取 hosts 失败。")
                return
            try:
                self._render_hosts(result)
            except Exception:
                pass
            self._set_status("hosts 读取完成")

        self._run(self._net_work("read_hosts"), done)

    def _render_hosts(self, result):
        tree = self._hosts_tree
        if tree is None:
            return
        for iid in tree.get_children():
            tree.delete(iid)
        if not isinstance(result, dict) or not result.get("success"):
            msg = (result or {}).get("message") if isinstance(result, dict) else "读取 hosts 失败"
            tree.insert("", tk.END, values=("", msg or "读取失败", "", ""))
            return
        entries = result.get("entries") or []
        if not entries:
            tree.insert("", tk.END, values=("", "无映射记录", "", ""))
            return
        for it in entries:
            if not isinstance(it, dict):
                continue
            tree.insert("", tk.END, values=(
                it.get("ip") or "",
                it.get("domain") or "",
                it.get("comment") or "",
                "是" if it.get("enabled") else "否",
            ))

    def _do_backup_hosts(self):
        self._set_status("正在备份 hosts...")

        def done(result):
            ok, info = (result[0], result[1]) if isinstance(result, tuple) and len(result) == 2 else (False, "")
            info = str(info)
            if ok:
                self._info("备份完成", f"hosts 已备份到：\n{info}")
            else:
                self._warn("备份失败", info)
            self._set_status("hosts 备份操作完成")

        self._run(self._net_work("backup_hosts"), done)

    def _do_reset_hosts(self):
        if not self._confirm(
            "确认重置 hosts",
            "即将把 hosts 文件重置为 Windows 默认内容（不包含任何映射）。\n\n"
            "· 操作前会自动备份当前 hosts\n"
            "· 需要管理员权限\n\n是否继续？",
        ):
            return
        self._set_status("正在重置 hosts...")

        def done(result):
            ok, msg = (result[0], result[1]) if isinstance(result, tuple) and len(result) == 2 else (False, "")
            msg = str(msg)
            if ok:
                self._info("重置成功", msg)
                self._do_read_hosts()  # 刷新表格
            else:
                self._warn("重置失败", msg)
            self._set_status("hosts 重置操作完成")

        self._run(self._net_work("reset_hosts"), done)

    def _do_diagnose(self):
        self._last_error = ""
        tree = self._diag_tree
        if tree is not None:
            for iid in tree.get_children():
                tree.delete(iid)
        self._set_status("正在进行连通性诊断...")

        def done(result):
            if result is None:
                self._error("诊断失败", self._last_error or "连通性诊断失败。")
                return
            try:
                self._render_diagnose(result)
            except Exception:
                pass
            self._set_status("连通性诊断完成")

        self._run(self._net_work("diagnose"), done)

    def _render_diagnose(self, items):
        tree = self._diag_tree
        if tree is None:
            return
        for iid in tree.get_children():
            tree.delete(iid)
        if not isinstance(items, list) or not items:
            tree.insert("", tk.END, values=("未获取到诊断结果", "", ""))
            return
        for it in items:
            if not isinstance(it, dict):
                continue
            result_txt = it.get("result") or ""
            tags = ("bad",) if "异常" in str(result_txt) else ("ok",)
            tree.insert("", tk.END, values=(
                it.get("step") or "", result_txt, it.get("detail") or "",
            ), tags=tags)


def build(app, parent):
    """
    页面入口：由主程序以 build(app, parent) 的方式调用。
    返回页面实例（app 提供主题 / 交互 / 异步能力，parent 为 tk.Frame 容器）。
    """
    return SystemInfoPage(app, parent)
