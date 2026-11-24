# -*- coding: utf-8 -*-
"""
虚拟设备专项管理页面

与「内核驱动服务」页的区别
--------------------------
  · 内核驱动服务页：以**驱动服务**为视角，覆盖全部 583 个驱动
  · 本页面：以**设备**为视角，只关注没有真实硬件的虚拟设备
    （虚拟麦克风/声卡、虚拟网卡、虚拟摄像头、虚拟光驱等）

用户痛点："我从来没见过这个麦克风，它是哪来的？"
本页面回答：这是什么设备 → 哪个驱动 → 哪家厂商 → 属于哪个软件 → 软件还在不在 → 是否残留

安全边界：只诊断，不提供一键卸载设备。移除虚拟设备需走设备管理器或 pnputil，
属于高风险操作，本页只给指引。
"""

import os
import tkinter as tk
from tkinter import ttk

from scanner import format_size
from virtual_devices import VirtualDeviceEngine


CATEGORY_FILTERS = ["全部", "虚拟音频", "虚拟网卡", "虚拟摄像头",
                    "虚拟光驱", "虚拟磁盘", "其他虚拟设备", "仅残留"]


def build(app, parent):
    return VirtualDevicePage(app, parent).build()


class VirtualDevicePage:
    def __init__(self, app, parent):
        self.app = app
        self.parent = parent
        self.engine = VirtualDeviceEngine()
        self.all_devices = []
        self.shown_devices = []
        self.filter_text = "全部"

    # ==================== 界面 ====================

    def build(self):
        T = self.app.THEME

        self.app.section_title(
            self.parent, "虚拟设备管理",
            "检测没有真实硬件的软件模拟设备：虚拟麦克风/声卡、虚拟网卡、虚拟摄像头、"
            "虚拟光驱等。这类设备用户平时感觉不到，卸载软件后却常常残留。"
            "本页只做诊断，不提供一键卸载。")

        # 说明卡片
        info_card = self.app.card(self.parent, pady=(0, 8))
        tk.Label(info_card,
                 text="识别原理：真实硬件设备的 ID 形如 HDAUDIO\\... / USB\\VID_... / PCI\\...，"
                      "而软件模拟的设备一律挂在 ROOT\\ 下（如 ROOT\\MEDIA\\0000）。"
                      "因此本页只列出 ROOT 下的设备，不会误报你的声卡、摄像头等真实硬件。",
                 bg=T.CARD_BG, fg=T.TEXT_LIGHT, font=("Microsoft YaHei", 9),
                 wraplength=920, justify=tk.LEFT, anchor=tk.W).pack(
            fill=tk.X, padx=16, pady=10)

        # 工具条
        bar_card = self.app.card(self.parent, pady=(0, 8))
        bar = tk.Frame(bar_card, bg=T.CARD_BG)
        bar.pack(fill=tk.X, padx=16, pady=12)

        self.scan_btn = self.app.button(bar, "🔍 扫描虚拟设备", self.start_scan)
        self.scan_btn.pack(side=tk.LEFT)

        tk.Label(bar, text="分类：", bg=T.CARD_BG, fg=T.TEXT,
                 font=("Microsoft YaHei", 9)).pack(side=tk.LEFT, padx=(14, 2))
        self.filter_var = tk.StringVar(value="全部")
        self.filter_combo = ttk.Combobox(
            bar, textvariable=self.filter_var, width=14, state="readonly",
            values=CATEGORY_FILTERS, font=("Microsoft YaHei", 9))
        self.filter_combo.pack(side=tk.LEFT)
        self.filter_combo.bind("<<ComboboxSelected>>", self.on_filter_change)

        self.app.button(bar, "📋 复制设备名", self.copy_name, kind="ghost").pack(
            side=tk.LEFT, padx=8)

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
            ("name", "category", "service", "provider", "state", "owner", "ep", "flag"),
            (230, 100, 170, 180, 80, 200, 180, 70),
            headings=("设备名称", "分类", "驱动服务", "厂商", "状态",
                      "所属软件", "关联端点", "判定"),
            height=11)
        self.tree.bind("<<TreeviewSelect>>", self.on_select)
        try:
            self.tree.tag_configure("orphan", foreground=T.DANGER)
            self.tree.tag_configure("normal", foreground=T.TEXT)
            self.tree.tag_configure("disabled", foreground=T.TEXT_LIGHT)
        except Exception:
            pass

        # 详情
        detail_card = self.app.card(self.parent, pady=(0, 10))
        self.detail = tk.Label(detail_card, text="选中一行查看详情与处置建议",
                               bg=T.CARD_BG, fg=T.TEXT_LIGHT,
                               font=("Microsoft YaHei", 9), wraplength=920,
                               justify=tk.LEFT, anchor=tk.W)
        self.detail.pack(fill=tk.X, padx=16, pady=12)

        return self

    def _build_stat_cards(self):
        T = self.app.THEME
        specs = [
            ("total", "虚拟设备总数", T.PRIMARY),
            ("audio", "虚拟音频", "#722ed1"),
            ("net", "虚拟网卡", "#13c2c2"),
            ("running", "驱动运行中", T.ACCENT),
            ("orphan", "残留（软件已卸载）", T.DANGER),
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
        self.progress_label.config(text="正在扫描...")

        engine = VirtualDeviceEngine()

        def work(progress_cb):
            engine.progress_callback = progress_cb
            return engine.scan_devices()

        def on_progress(percent, msg):
            self.progress["value"] = percent
            self.progress_label.config(text=msg)

        def on_done(devices):
            self.scan_btn.config(state=tk.NORMAL, text="🔍 重新扫描")
            self.all_devices = devices or []
            self.progress["value"] = 100
            cats = {}
            for d in self.all_devices:
                cats[d["category"]] = cats.get(d["category"], 0) + 1
            detail = "、".join(f"{k} {v}" for k, v in cats.items()) or "无"
            self.progress_label.config(
                text=f"完成：{len(self.all_devices)} 个（{detail}）")
            self._update_stats()
            self.render()

        self._engine_ref = engine
        self.app.run_async(work, on_done=on_done, on_progress=on_progress)

    def _update_stats(self):
        items = self.all_devices
        cats = {}
        for d in items:
            cats[d["category"]] = cats.get(d["category"], 0) + 1
        self.stat_labels["total"].config(text=str(len(items)))
        self.stat_labels["audio"].config(text=str(cats.get("虚拟音频", 0)))
        self.stat_labels["net"].config(text=str(cats.get("虚拟网卡", 0)))
        self.stat_labels["running"].config(text=str(sum(1 for d in items if d["driver_running"])))
        self.stat_labels["orphan"].config(text=str(sum(1 for d in items if d["is_orphan"])))

    def on_filter_change(self, _event=None):
        self.filter_text = self.filter_var.get()
        self.render()

    def render(self):
        for row in self.tree.get_children():
            self.tree.delete(row)

        f = self.filter_text
        if f == "全部":
            self.shown_devices = list(self.all_devices)
        elif f == "仅残留":
            self.shown_devices = [d for d in self.all_devices if d["is_orphan"]]
        else:
            self.shown_devices = [d for d in self.all_devices if d["category"] == f]

        for d in self.shown_devices:
            eps = "、".join(e["name"] for e in d["endpoints"][:2]) or "—"
            owner = d["owner"] or "—"
            if d["owner"] and not d["owner_installed"]:
                owner = f"{d['owner']}（未安装）"
            flag = "残留" if d["is_orphan"] else ("已禁用" if d["disabled"] else "正常")

            if d["is_orphan"]:
                tag = "orphan"
            elif d["disabled"]:
                tag = "disabled"
            else:
                tag = "normal"

            self.tree.insert("", tk.END, values=(
                d["name"][:34],
                d["category"],
                d["service"] or "—",
                d["provider"][:28] or "—",
                d["status_text"],
                owner[:28],
                eps[:30],
                flag,
            ), tags=(tag,))

    def on_select(self, _event=None):
        sel = self.tree.selection()
        if not sel:
            return
        idx = self.tree.index(sel[0])
        if idx < 0 or idx >= len(self.shown_devices):
            return
        d = self.shown_devices[idx]

        lines = [
            f"设备名称：{d['name']}",
            f"分类：{d['category']}　状态：{d['status_text']}",
            f"设备 ID：{d['instance_id']}",
        ]
        if d["service"]:
            lines.append(f"驱动服务：{d['service']}")
        if d["driver_version"]:
            lines.append(f"驱动版本：{d['driver_version']}")
        if d["provider"]:
            lines.append(f"厂商：{d['provider']}")
        if d["inf"]:
            lines.append(f"驱动包：{d['inf']}")
        lines.append(f"驱动文件：{d['driver_file'] or '(无)'}"
                     f"{'（不存在）' if d['driver_file'] and not d['driver_exists'] else ''}")
        if d["endpoints"]:
            lines.append("关联端点：" + "、".join(
                f"{e['name']}［{e['direction']}］" for e in d["endpoints"]))
        if d["owner"]:
            state = "已安装" if d["owner_installed"] else "未在已安装列表中"
            lines.append(f"所属软件：{d['owner']}（{state}）")
        if d["match_reason"]:
            lines.append(f"归属依据：{d['match_reason']}")
        lines.append("")
        lines.append(f"建议：{d['advice']}")
        self.detail.config(text="\n".join(lines), fg=self.app.THEME.TEXT)

    def copy_name(self):
        sel = self.tree.selection()
        if not sel:
            self.app.show_warning("提示", "请先选中一行")
            return
        idx = self.tree.index(sel[0])
        if 0 <= idx < len(self.shown_devices):
            name = self.shown_devices[idx]["name"]
            try:
                self.app.root.clipboard_clear()
                self.app.root.clipboard_append(name)
                self.app.set_status(f"已复制：{name}")
            except Exception:
                pass
