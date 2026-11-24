# -*- coding: utf-8 -*-
"""
磁盘占用趋势页面
把 DiskTrendEngine 记录的历史快照画成趋势图，回答：
  - 我的空间是被谁一点点吃掉的？
  - 这次清理到底释放了多少？
  - 按现在的速度，还能撑多久？

本页面只读历史数据与当前容量，不做任何删除操作。
"""

import os
import tkinter as tk
from tkinter import ttk
from datetime import datetime

from scanner import format_size
from disktrend import DiskTrendEngine


def build(app, parent):
    return DiskTrendPage(app, parent).build()


class DiskTrendPage:
    def __init__(self, app, parent):
        self.app = app
        self.parent = parent
        self.engine = DiskTrendEngine()
        self.drive_var = tk.StringVar(value="C")
        self._chart_points = []   # (x, y, snapshot) 用于悬停/调试

    # ==================== 界面 ====================

    def build(self):
        T = self.app.THEME

        self.app.section_title(self.parent, "磁盘占用趋势",
                               "记录每次启动与每次清理后的磁盘占用，看清空间是怎么变化的。"
                               "数据只保存在本机，不会上传。")

        # 顶部工具条
        card = self.app.card(self.parent, pady=(0, 10))
        bar = tk.Frame(card, bg=T.CARD_BG)
        bar.pack(fill=tk.X, padx=16, pady=12)

        tk.Label(bar, text="盘符：", bg=T.CARD_BG, fg=T.TEXT,
                 font=("Microsoft YaHei", 9)).pack(side=tk.LEFT)
        drives = [d for d in ("C", "D", "E", "F") if os.path.exists(d + ":\\")]
        cb = ttk.Combobox(bar, textvariable=self.drive_var, width=5, state="readonly",
                          values=drives, font=("Microsoft YaHei", 9))
        cb.pack(side=tk.LEFT, padx=(0, 12))
        cb.bind("<<ComboboxSelected>>", lambda e: self.refresh())

        self.app.button(bar, "🔁 刷新", self.refresh).pack(side=tk.LEFT)
        self.app.button(bar, "📸 立即记录快照", self.record_now, kind="ghost").pack(
            side=tk.LEFT, padx=6)
        self.app.button(bar, "🗑 清空历史", self.clear_history, kind="danger").pack(
            side=tk.LEFT)

        # 结论区：大号文字
        self.summary = tk.Label(card, text="正在读取历史...", bg=T.CARD_BG, fg=T.TEXT,
                                font=("Microsoft YaHei", 11), anchor=tk.W,
                                justify=tk.LEFT, wraplength=940)
        self.summary.pack(fill=tk.X, padx=16, pady=(0, 12))

        # 趋势图
        chart_card = self.app.card(self.parent, pady=(0, 10))
        self.canvas = tk.Canvas(chart_card, bg="#ffffff", height=320,
                                highlightthickness=0)
        self.canvas.pack(fill=tk.BOTH, expand=True, padx=16, pady=(8, 8))
        self.canvas.bind("<Configure>", lambda e: self._draw_chart())

        # 快照明细表
        table_holder = tk.Frame(self.parent, bg=T.BG)
        table_holder.pack(fill=tk.BOTH, expand=True, padx=20, pady=(0, 10))
        self.tree, _ = self.app.make_table(
            table_holder,
            ("time", "free", "used", "change", "reason"),
            (170, 120, 120, 130, 150),
            headings=("时间", "剩余空间", "已用空间", "较上次变化", "记录原因"),
            height=6)

        self.refresh()
        return self

    # ==================== 交互 ====================

    def refresh(self):
        """刷新数据并重绘"""
        self._draw_chart()
        self._render_summary()
        self._render_table()

    def record_now(self):
        """手动记录一次快照"""
        result = self.engine.record_snapshot(self.drive_var.get(), force=True,
                                             reason="手动记录")
        self.app.set_status(result.get("message", ""))
        self.refresh()

    def clear_history(self):
        if not self.app.ask_yes_no("确认", "将清空该盘的全部历史记录，是否继续？"):
            return
        ok, msg = self.engine.clear_history(self.drive_var.get())
        self.app.show_info("提示", msg)
        self.refresh()

    # ==================== 渲染 ====================

    def _render_summary(self):
        info = self.engine.analyze(self.drive_var.get())
        if info["points"] == 0:
            self.summary.config(text=info["message"])
            return

        last = info["last"]
        capacity = info["capacity"]
        free = last["free"]
        lines = []
        lines.append(f"{self.drive_var.get()}: 盘总容量 {format_size(capacity)}，"
                     f"当前剩余 {format_size(free)}（{free / capacity * 100:.1f}%）。")

        if info["points"] >= 2:
            change = info["free_change_bytes"]
            span = info["span_days"]
            direction = info["direction"]
            change_txt = format_size(abs(change))
            if change >= 0:
                lines.append(f"过去 {span:.1f} 天{direction}，累计多出了 {change_txt} 空间。")
            else:
                lines.append(f"过去 {span:.1f} 天{direction}，累计减少了 {change_txt} 空间。")

            rate = info["rate_bytes_per_day"]
            if info["days_until_full"]:
                lines.append(f"按当前速度（每天约消耗 {format_size(abs(rate))}），"
                             f"预计还能用约 {info['days_until_full']:.0f} 天。")
            biggest = info.get("biggest_clean")
            if biggest:
                lines.append(f"单次释放最多的一次发生在 {biggest['time']}（{biggest.get('reason', '')}），"
                             f"释放了 {format_size(biggest['gain'])}。")
        else:
            lines.append("目前只有 1 条记录，等积累几天后就能看到趋势。")

        self.summary.config(text="\n".join(lines))

    def _render_table(self):
        """快照明细表（最新在前，并计算相对上一条的变化）"""
        history_desc = list(reversed(self.engine.get_history(self.drive_var.get())))
        for row in self.tree.get_children():
            self.tree.delete(row)
        for i, p in enumerate(history_desc):
            change = "—"
            if i + 1 < len(history_desc):
                diff = p["free"] - history_desc[i + 1]["free"]
                sign = "+" if diff >= 0 else "-"
                change = f"{sign}{format_size(abs(diff))}"
            self.tree.insert("", tk.END, values=(
                p["dt"].strftime("%Y-%m-%d %H:%M"),
                format_size(p["free"]),
                format_size(p["used"]),
                change,
                p.get("reason", ""),
            ))

    # ==================== 图表 ====================

    def _draw_chart(self):
        canvas = self.canvas
        if not canvas.winfo_exists():
            return
        w = canvas.winfo_width()
        h = canvas.winfo_height()
        if w < 20 or h < 20:
            return
        canvas.delete("all")

        history = self.engine.get_history(self.drive_var.get())
        if not history:
            canvas.create_text(w // 2, h // 2,
                               text="暂无历史数据\n本工具会在每次启动和每次清理后自动记录快照",
                               fill="#999999", font=("Microsoft YaHei", 11),
                               justify=tk.CENTER)
            return

        capacity = max(p["total"] for p in history) or 1

        # 边距
        pad_l, pad_r, pad_t, pad_b = 74, 24, 20, 44
        plot_w = w - pad_l - pad_r
        plot_h = h - pad_t - pad_b
        if plot_w <= 10 or plot_h <= 10:
            return

        t0 = history[0]["dt"].timestamp()
        t1 = history[-1]["dt"].timestamp()
        if t1 <= t0:
            t1 = t0 + 1

        # 计算坐标：时间戳映射到 x，剩余空间映射到 y
        def x_of(ts):
            return pad_l + (ts - t0) / (t1 - t0) * plot_w

        def y_of(free_bytes):
            return pad_t + (1 - free_bytes / capacity) * plot_h

        # 网格 + Y 轴刻度
        for i in range(6):
            frac = i / 5.0
            y = pad_t + frac * plot_h
            value = capacity * (1 - frac)
            canvas.create_line(pad_l, y, w - pad_r, y, fill="#f0f2f5", width=1)
            canvas.create_text(pad_l - 8, y, text=format_size(value), anchor=tk.E,
                               fill="#8c8c8c", font=("Microsoft YaHei", 8))
        # 边框
        canvas.create_rectangle(pad_l, pad_t, w - pad_r, pad_t + plot_h,
                                outline="#e8e8e8")

        # 数据点坐标
        pts = [(x_of(p["dt"].timestamp()), y_of(p["free"]), p) for p in history]
        self._chart_points = pts

        # 面积填充（柔和蓝）
        if len(pts) >= 2:
            area = [(pts[0][0], pad_t + plot_h)]
            for x, y, _ in pts:
                area.append((x, y))
            area.append((pts[-1][0], pad_t + plot_h))
            flat = []
            for x, y in area:
                flat.extend([x, y])
            canvas.create_polygon(flat, fill="#e6f4ff", outline="")
            # 折线
            line_coords = []
            for x, y, _ in pts:
                line_coords.extend([x, y])
            canvas.create_line(line_coords, fill="#1890ff", width=2, smooth=True)

        # 数据点
        for x, y, p in pts:
            r = 3
            canvas.create_oval(x - r, y - r, x + r, y + r, fill="#1890ff",
                               outline="#ffffff", width=1)

        # X 轴时间标签（最多 6 个，避免拥挤）
        n = len(pts)
        label_count = min(6, n)
        for i in range(label_count):
            idx = int(i * (n - 1) / max(1, label_count - 1))
            x, _y, p = pts[idx]
            span_days = (history[-1]["dt"] - history[0]["dt"]).total_seconds() / 86400
            fmt = "%m-%d" if span_days > 2 else "%m-%d %H:%M"
            canvas.create_text(x, pad_t + plot_h + 16, text=p["dt"].strftime(fmt),
                               fill="#8c8c8c", font=("Microsoft YaHei", 8))

        # 标注当前剩余
        last = history[-1]
        canvas.create_text(w - pad_r, pad_t - 2,
                           text=f"当前剩余 {format_size(last['free'])}",
                           anchor=tk.SE, fill="#1890ff",
                           font=("Microsoft YaHei", 10, "bold"))
