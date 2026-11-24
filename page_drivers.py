# -*- coding: utf-8 -*-
"""
内核驱动服务页面

为什么需要这个页面
------------------
Windows 的服务分两类，但任务管理器和绝大多数"优化软件"只显示用户态服务。
内核驱动服务（Type=1/2）完全不可见——实测本机 583 个驱动、其中 144 个是
已经失效的残留记录，常规工具一个都发现不了。

本页面**只做诊断展示**，不提供任何禁用/删除按钮。
禁用正在使用的内核驱动会导致设备失灵、软件异常，严重时蓝屏。
真要清理必须走"卸载软件 → 停服务 → pnputil 删驱动包"的完整链路。
"""

import os
import tkinter as tk
from tkinter import ttk

from scanner import format_size
from drivers import DriverEngine


FILTERS = [
    ("all", "全部驱动"),
    ("third", "仅第三方"),
    ("running", "仅运行中"),
    ("orphan", "仅残留（文件丢失）"),
    ("virtual", "仅虚拟设备类"),
    ("unsigned", "仅未签名"),
]


def build(app, parent):
    return DriverPage(app, parent).build()


class DriverPage:
    def __init__(self, app, parent):
        self.app = app
        self.parent = parent
        self.engine = DriverEngine()
        self.all_items = []
        self.shown_items = []
        self.filter_key = "all"

    # ==================== 界面 ====================

    def build(self):
        T = self.app.THEME

        self.app.section_title(
            self.parent, "内核驱动服务",
            "检测任务管理器看不到的内核驱动服务。这类驱动由虚拟声卡、虚拟摄像头、"
            "反作弊、VPN、硬件监控等软件安装，卸载不干净会长期滞留。"
            "本页只做诊断，不提供禁用/删除操作。")

        # 安全提示
        warn_card = self.app.card(self.parent, pady=(0, 8))
        tk.Label(warn_card,
                 text="⚠ 内核驱动一旦禁用可能导致设备失灵、软件异常甚至蓝屏，"
                      "因此本工具刻意不提供一键禁用。如需清理残留驱动，"
                      "请先卸载对应软件，再用管理员命令处理。",
                 bg=T.CARD_BG, fg=T.DANGER, font=("Microsoft YaHei", 9),
                 wraplength=920, justify=tk.LEFT, anchor=tk.W).pack(
            fill=tk.X, padx=16, pady=10)

        # 工具条
        bar_card = self.app.card(self.parent, pady=(0, 8))
        bar = tk.Frame(bar_card, bg=T.CARD_BG)
        bar.pack(fill=tk.X, padx=16, pady=12)

        self.scan_btn = self.app.button(bar, "🔍 扫描驱动服务", self.start_scan)
        self.scan_btn.pack(side=tk.LEFT)

        tk.Label(bar, text="筛选：", bg=T.CARD_BG, fg=T.TEXT,
                 font=("Microsoft YaHei", 9)).pack(side=tk.LEFT, padx=(14, 2))
        self.filter_var = tk.StringVar(value=FILTERS[0][1])
        self.filter_combo = ttk.Combobox(
            bar, textvariable=self.filter_var, width=18, state="readonly",
            values=[f[1] for f in FILTERS], font=("Microsoft YaHei", 9))
        self.filter_combo.pack(side=tk.LEFT)
        self.filter_combo.bind("<<ComboboxSelected>>", self.on_filter_change)

        self.app.button(bar, "📋 复制服务名", self.copy_name, kind="ghost").pack(
            side=tk.LEFT, padx=8)

        # 进度
        prog = tk.Frame(bar_card, bg=T.CARD_BG)
        prog.pack(fill=tk.X, padx=16, pady=(0, 12))
        self.progress = ttk.Progressbar(prog, mode="determinate")
        self.progress.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.progress_label = tk.Label(prog, text="就绪", bg=T.CARD_BG,
                                       fg=T.TEXT_LIGHT, font=("Microsoft YaHei", 9),
                                       width=34, anchor=tk.W)
        self.progress_label.pack(side=tk.LEFT, padx=8)

        # 统计卡片
        self.stats_frame = tk.Frame(self.parent, bg=T.BG)
        self.stats_frame.pack(fill=tk.X, padx=20, pady=(0, 8))
        self._build_stat_cards()

        # 表格
        table_holder = tk.Frame(self.parent, bg=T.BG)
        table_holder.pack(fill=tk.BOTH, expand=True, padx=20, pady=(0, 6))
        self.tree, _ = self.app.make_table(
            table_holder,
            ("name", "display", "type", "start", "state", "signer", "file", "hint"),
            (170, 220, 90, 100, 70, 200, 150, 100),
            headings=("服务名", "显示名", "类型", "启动类型", "状态",
                      "签名者 / 发布者", "驱动文件", "类别提示"),
            height=13)
        self.tree.bind("<<TreeviewSelect>>", self.on_select)
        try:
            self.tree.tag_configure("orphan", foreground=T.DANGER)
            self.tree.tag_configure("third", foreground=T.WARNING)
            self.tree.tag_configure("running_third", foreground="#722ed1")
        except Exception:
            pass

        # 详情
        detail_card = self.app.card(self.parent, pady=(0, 10))
        self.detail = tk.Label(detail_card, text="选中一行查看详细说明与处置建议",
                               bg=T.CARD_BG, fg=T.TEXT_LIGHT,
                               font=("Microsoft YaHei", 9), wraplength=920,
                               justify=tk.LEFT, anchor=tk.W)
        self.detail.pack(fill=tk.X, padx=16, pady=12)

        return self

    def _build_stat_cards(self):
        """五个统计指标卡"""
        T = self.app.THEME
        specs = [
            ("total", "驱动服务总数", T.PRIMARY),
            ("third_party", "第三方驱动", T.WARNING),
            ("running", "运行中", T.ACCENT),
            ("orphan", "残留(文件丢失)", T.DANGER),
            ("virtual", "虚拟设备类", "#722ed1"),
        ]
        self.stat_labels = {}
        for i, (key, label, color) in enumerate(specs):
            card = tk.Frame(self.stats_frame, bg=T.CARD_BG, bd=0,
                            highlightbackground=T.BORDER, highlightthickness=1)
            card.grid(row=0, column=i, padx=4, pady=2, sticky="nsew")
            self.stats_frame.columnconfigure(i, weight=1)
            value = tk.Label(card, text="—", bg=T.CARD_BG, fg=color,
                             font=("Microsoft YaHei", 18, "bold"))
            value.pack(pady=(8, 0))
            tk.Label(card, text=label, bg=T.CARD_BG, fg=T.TEXT_LIGHT,
                     font=("Microsoft YaHei", 8)).pack(pady=(0, 8))
            self.stat_labels[key] = value

    # ==================== 交互 ====================

    def start_scan(self):
        self.scan_btn.config(state=tk.DISABLED, text="扫描中...")
        self.progress["value"] = 0
        self.progress_label.config(text="正在枚举驱动服务...")

        engine = DriverEngine()

        def work(progress_cb):
            engine.progress_callback = progress_cb
            return engine.scan_kernel_drivers()

        def on_progress(percent, msg):
            self.progress["value"] = percent
            self.progress_label.config(text=msg)

        def on_done(items):
            self.scan_btn.config(state=tk.NORMAL, text="🔍 重新扫描")
            self.all_items = items or []
            self.progress["value"] = 100
            self.progress_label.config(text=f"扫描完成，共 {len(self.all_items)} 个驱动服务")
            self._update_stats()
            self.render()

        self._engine_ref = engine
        self.app.run_async(work, on_done=on_done, on_progress=on_progress)

    def _update_stats(self):
        items = self.all_items
        third = [i for i in items if i["is_third_party"]]
        orphan = [i for i in third if not i["image_exists"]]
        virtual = [i for i in items if i.get("category_hint")]
        self.stat_labels["total"].config(text=str(len(items)))
        self.stat_labels["third_party"].config(text=str(len(third)))
        self.stat_labels["running"].config(text=str(sum(1 for i in items if i["running"])))
        self.stat_labels["orphan"].config(text=str(len(orphan)))
        self.stat_labels["virtual"].config(text=str(len(virtual)))

    def on_filter_change(self, _event=None):
        label = self.filter_var.get()
        for key, text in FILTERS:
            if text == label:
                self.filter_key = key
                break
        self.render()

    def _apply_filter(self):
        items = self.all_items
        key = self.filter_key
        if key == "third":
            return [i for i in items if i["is_third_party"]]
        if key == "running":
            return [i for i in items if i["running"]]
        if key == "orphan":
            return [i for i in items if i["is_third_party"] and not i["image_exists"]]
        if key == "virtual":
            return [i for i in items if i.get("category_hint")]
        if key == "unsigned":
            return [i for i in items if i.get("signed") is False and i["image_exists"]]
        return items

    def render(self):
        for row in self.tree.get_children():
            self.tree.delete(row)
        self.shown_items = self._apply_filter()

        for it in self.shown_items:
            if it["is_third_party"] and not it["image_exists"]:
                tag = "orphan"
                file_cell = "已丢失"
            elif it["is_third_party"]:
                tag = "running_third" if it["running"] else "third"
                file_cell = format_size(it["image_size"]) if it["image_size"] else "—"
            else:
                tag = ""
                file_cell = "系统内置" if it.get("is_builtin") else (
                    format_size(it["image_size"]) if it["image_size"] else "—")

            signer = it.get("signer") or it.get("publisher") or ""
            if it.get("is_ddk_default_publisher") and not it.get("signer"):
                signer = "DDK 默认发布者"

            self.tree.insert("", tk.END, values=(
                it["name"],
                it["display"][:40],
                it["type_cn"],
                it["start_type"],
                it["state"],
                signer[:38] or "—",
                file_cell,
                it.get("category_hint") or "—",
            ), tags=(tag,) if tag else ())

    def on_select(self, _event=None):
        sel = self.tree.selection()
        if not sel:
            return
        idx = self.tree.index(sel[0])
        if idx < 0 or idx >= len(self.shown_items):
            return
        it = self.shown_items[idx]
        lines = [
            f"服务名：{it['name']}　显示名：{it['display']}",
            f"类型：{it['type_cn']}　启动类型：{it['start_type']}　状态：{it['state']}",
            f"驱动文件：{it['image_path'] or '(内核内置，无独立文件)'}"
            f"{'（不存在）' if it['image_path'] and not it['image_exists'] else ''}",
        ]
        if it.get("publisher"):
            lines.append(f"文件发布者：{it['publisher']}")
        if it.get("signer"):
            lines.append(f"数字签名者：{it['signer']}")
        elif it.get("signed") is False:
            lines.append("数字签名：未签名或验证未通过")
        if it.get("category_hint"):
            lines.append(f"类别：{it['category_hint']}")
        lines.append("")
        lines.append(f"建议：{it['advice']}")
        self.detail.config(text="\n".join(lines), fg=self.app.THEME.TEXT)

    def copy_name(self):
        sel = self.tree.selection()
        if not sel:
            self.app.show_warning("提示", "请先选中一行")
            return
        idx = self.tree.index(sel[0])
        if 0 <= idx < len(self.shown_items):
            name = self.shown_items[idx]["name"]
            try:
                self.app.root.clipboard_clear()
                self.app.root.clipboard_append(name)
                self.app.set_status(f"已复制服务名：{name}")
            except Exception:
                pass
