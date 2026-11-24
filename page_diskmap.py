# -*- coding: utf-8 -*-
"""
磁盘空间分析页面
包含三个标签页：
  1. 磁盘空间分析 —— Treemap 矩形树图可视化 + 目录逐层下钻 + 文件类型占用统计
  2. 重复文件     —— 哈希比对找出完全重复的文件并按组清理
  3. 大文件排行   —— 列出体积最大的文件，支持定位与搬家

本页面只做展示与交互，实际计算与删除交给 diskmap.DiskMapEngine。
"""

import os
import tkinter as tk
from tkinter import ttk

from scanner import format_size
from diskmap import DiskMapEngine, layout_treemap

# 树图配色：按文件类型分类映射到一组柔和色
TYPE_COLORS = {
    "视频": "#ff9c6e",
    "图片": "#69c0ff",
    "音频": "#b37feb",
    "文档": "#5cdbd3",
    "安装包": "#ffc53d",
    "压缩包": "#ffa940",
    "程序与库": "#85a5ff",
    "数据与缓存": "#95de64",
    "网页与脚本": "#40a9ff",
    "字体": "#d3adf7",
    "无扩展名": "#bfbfbf",
    "其他": "#d9d9d9",
}
DIR_COLOR = "#1890ff"


def _color_for(item):
    """根据条目类型挑选颜色"""
    if item.get("is_dir"):
        return DIR_COLOR
    name = item.get("name", "")
    ext = os.path.splitext(name)[1].lower()
    if not ext:
        return TYPE_COLORS["无扩展名"]
    for cat, exts in _EXT_GROUPS.items():
        if ext in exts:
            return TYPE_COLORS.get(cat, TYPE_COLORS["其他"])
    return TYPE_COLORS["其他"]


# 从 diskmap 复用扩展名分组
from diskmap import FILE_TYPE_MAP as _TYPE_GROUPS  # noqa: E402
_EXT_GROUPS = {}
for _c, _exts in _TYPE_GROUPS.items():
    for _e in _exts:
        _EXT_GROUPS[_e] = _c


class DiskMapPage:
    """磁盘空间分析页面控制器"""

    def __init__(self, app, parent):
        self.app = app
        self.parent = parent
        self.engine = DiskMapEngine()
        self.analysis = None          # 最近一次 analyze 的结果
        self.node_stack = []          # 下钻路径栈
        self.rects = []               # 当前画布上的矩形（用于点击命中）
        self.current_node = None
        self.dup_result = None
        self.dup_rows = []
        self.type_filter = None       # 大文件排行的类型筛选（None 表示全部）

    # ==================== 界面搭建 ====================

    def build(self):
        nb = ttk.Notebook(self.parent)
        nb.pack(fill=tk.BOTH, expand=True, padx=8, pady=8)
        self.notebook = nb

        tab1 = tk.Frame(nb, bg=self.app.THEME.BG)
        tab2 = tk.Frame(nb, bg=self.app.THEME.BG)
        tab3 = tk.Frame(nb, bg=self.app.THEME.BG)
        nb.add(tab1, text="  磁盘空间分析  ")
        nb.add(tab2, text="  重复文件  ")
        nb.add(tab3, text="  大文件排行  ")

        self._build_tab_analysis(tab1)
        self._build_tab_duplicates(tab2)
        self._build_tab_largest(tab3)

    # ---------- 标签页 1：空间分析 ----------

    def _build_tab_analysis(self, parent):
        T = self.app.THEME

        # 工具条
        card = tk.Frame(parent, bg=T.CARD_BG, bd=0,
                        highlightbackground=T.BORDER, highlightthickness=1)
        card.pack(fill=tk.X, padx=10, pady=(10, 6))
        bar = tk.Frame(card, bg=T.CARD_BG)
        bar.pack(fill=tk.X, padx=12, pady=10)

        tk.Label(bar, text="扫描范围：", bg=T.CARD_BG, fg=T.TEXT,
                 font=("Microsoft YaHei", 9)).pack(side=tk.LEFT)

        user_profile = os.environ.get("USERPROFILE", "C:\\Users")
        self.root_var = tk.StringVar(value=os.path.join(user_profile, "Downloads"))
        self.root_combo = ttk.Combobox(bar, textvariable=self.root_var, width=42,
                                       font=("Microsoft YaHei", 9))
        self.root_combo["values"] = [
            os.path.join(user_profile, "Downloads"),
            user_profile,
            r"C:\Program Files",
            r"C:\Program Files (x86)",
            r"C:\Windows",
            "C:\\",
        ]
        self.root_combo.pack(side=tk.LEFT, padx=(0, 10))

        tk.Label(bar, text="保留层数：", bg=T.CARD_BG, fg=T.TEXT,
                 font=("Microsoft YaHei", 9)).pack(side=tk.LEFT)
        self.depth_var = tk.StringVar(value="3")
        ttk.Combobox(bar, textvariable=self.depth_var, width=5, state="readonly",
                     values=("1", "2", "3", "4", "5"),
                     font=("Microsoft YaHei", 9)).pack(side=tk.LEFT, padx=(0, 10))

        self.app.button(bar, "🔍 开始分析", self.start_analysis).pack(side=tk.LEFT)
        self.app.button(bar, "⬆ 返回上级", self.go_up, kind="ghost").pack(side=tk.LEFT, padx=6)
        self.app.button(bar, "⏹ 停止", self.stop_analysis, kind="ghost").pack(side=tk.LEFT)

        # 进度
        prog_frame = tk.Frame(card, bg=T.CARD_BG)
        prog_frame.pack(fill=tk.X, padx=12, pady=(0, 10))
        self.progress = ttk.Progressbar(prog_frame, mode="determinate")
        self.progress.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.progress_label = tk.Label(prog_frame, text="就绪", bg=T.CARD_BG,
                                        fg=T.TEXT_LIGHT, font=("Microsoft YaHei", 9),
                                        width=30, anchor=tk.W)
        self.progress_label.pack(side=tk.LEFT, padx=8)

        self.summary_label = tk.Label(card, text="尚未分析", bg=T.CARD_BG, fg=T.PRIMARY,
                                       font=("Microsoft YaHei", 10, "bold"))
        self.summary_label.pack(anchor=tk.W, padx=12, pady=(0, 10))

        # 主体：左树图 + 右明细
        body = tk.PanedWindow(parent, orient=tk.HORIZONTAL, sashwidth=6,
                              bg=T.BG, bd=0)
        body.pack(fill=tk.BOTH, expand=True, padx=10, pady=(0, 10))

        left = tk.Frame(body, bg=T.CARD_BG)
        self.canvas = tk.Canvas(left, bg="#fafafa", highlightthickness=0)
        self.canvas.pack(fill=tk.BOTH, expand=True)
        self.canvas.bind("<Configure>", lambda e: self.render_treemap())
        self.canvas.bind("<Button-1>", self.on_canvas_click)
        body.add(left, minsize=520)

        right = tk.Frame(body, bg=T.BG)
        body.add(right, minsize=380)

        tk.Label(right, text="本层子项（点击树图可下钻）", bg=T.BG, fg=T.TEXT,
                 font=("Microsoft YaHei", 9, "bold")).pack(anchor=tk.W, pady=(0, 2))
        self.children_tree, _ = self.app.make_table(
            right, ("name", "size", "pct"), (220, 90, 60),
            headings=("名称", "大小", "占比"), height=8)
        self.children_tree.bind("<Double-1>", self.on_child_double_click)

        tk.Label(right, text="文件类型占用统计（点击筛选大文件）", bg=T.BG, fg=T.TEXT,
                 font=("Microsoft YaHei", 9, "bold")).pack(anchor=tk.W, pady=(10, 2))
        self.type_tree, _ = self.app.make_table(
            right, ("type", "size", "pct", "count"), (110, 90, 60, 70),
            headings=("类型", "占用", "占比", "文件数"), height=7)
        self.type_tree.bind("<<TreeviewSelect>>", self.on_type_select)

        self.app.hint(parent,
                      "说明：树图面积与文件体积成正比。蓝色块为文件夹，彩色块为文件类型。"
                      "双击右侧条目或点击树图进入下一层；点击类型可筛选大文件排行。")

    # ---------- 标签页 2：重复文件 ----------

    def _build_tab_duplicates(self, parent):
        T = self.app.THEME

        card = tk.Frame(parent, bg=T.CARD_BG, bd=0,
                        highlightbackground=T.BORDER, highlightthickness=1)
        card.pack(fill=tk.X, padx=10, pady=(10, 6))
        bar = tk.Frame(card, bg=T.CARD_BG)
        bar.pack(fill=tk.X, padx=12, pady=10)

        tk.Label(bar, text="扫描目录：", bg=T.CARD_BG, fg=T.TEXT,
                 font=("Microsoft YaHei", 9)).pack(side=tk.LEFT)
        up = os.environ.get("USERPROFILE", "C:\\Users")
        self.dup_root_var = tk.StringVar(value=up)
        ttk.Combobox(bar, textvariable=self.dup_root_var, width=38,
                     font=("Microsoft YaHei", 9),
                     values=[up, up + "\\Downloads", up + "\\Documents",
                             up + "\\Desktop", up + "\\Pictures", "C:\\"]).pack(
            side=tk.LEFT, padx=(0, 10))

        tk.Label(bar, text="最小体积：", bg=T.CARD_BG, fg=T.TEXT,
                 font=("Microsoft YaHei", 9)).pack(side=tk.LEFT)
        self.dup_min_var = tk.StringVar(value="1 MB")
        ttk.Combobox(bar, textvariable=self.dup_min_var, width=8, state="readonly",
                     values=("100 KB", "1 MB", "10 MB", "100 MB"),
                     font=("Microsoft YaHei", 9)).pack(side=tk.LEFT, padx=(0, 10))

        self.app.button(bar, "🔍 查找重复文件", self.start_dup_scan).pack(side=tk.LEFT)
        self.app.button(bar, "🗑 删除除首个外的副本", self.delete_duplicates,
                        kind="danger").pack(side=tk.LEFT, padx=6)

        prog_frame = tk.Frame(card, bg=T.CARD_BG)
        prog_frame.pack(fill=tk.X, padx=12, pady=(0, 10))
        self.dup_progress = ttk.Progressbar(prog_frame, mode="determinate")
        self.dup_progress.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.dup_progress_label = tk.Label(prog_frame, text="就绪", bg=T.CARD_BG,
                                            fg=T.TEXT_LIGHT,
                                            font=("Microsoft YaHei", 9), width=34,
                                            anchor=tk.W)
        self.dup_progress_label.pack(side=tk.LEFT, padx=8)

        self.dup_summary = tk.Label(card, text="尚未查找", bg=T.CARD_BG, fg=T.PRIMARY,
                                     font=("Microsoft YaHei", 10, "bold"))
        self.dup_summary.pack(anchor=tk.W, padx=12, pady=(0, 10))

        holder = tk.Frame(parent, bg=T.BG)
        holder.pack(fill=tk.BOTH, expand=True, padx=10, pady=(0, 8))
        self.dup_tree, _ = self.app.make_table(
            holder,
            ("sel", "size", "count", "keep", "stats"),
            (50, 90, 70, 330, 150),
            headings=("选择", "单文件大小", "重复数", "保留的文件（其余将被删除）",
                      "可释放"),
            height=14)
        self.dup_tree.bind("<Button-1>", self.on_dup_click)

        self.app.hint(parent,
                      "规则：每组重复文件中保留路径排序最靠前的一个，其余可删除。"
                      "删除不可恢复，建议先自行确认这些文件确实不再需要。", color=T.WARNING)

    # ---------- 标签页 3：大文件排行 ----------

    def _build_tab_largest(self, parent):
        T = self.app.THEME

        card = tk.Frame(parent, bg=T.CARD_BG, bd=0,
                        highlightbackground=T.BORDER, highlightthickness=1)
        card.pack(fill=tk.X, padx=10, pady=(10, 6))
        bar = tk.Frame(card, bg=T.CARD_BG)
        bar.pack(fill=tk.X, padx=12, pady=10)
        tk.Label(bar,
                 text="大文件排行来自「磁盘空间分析」的结果。请先在上一个标签页执行一次分析。",
                 bg=T.CARD_BG, fg=T.TEXT_LIGHT,
                 font=("Microsoft YaHei", 9)).pack(side=tk.LEFT)
        self.app.button(bar, "📂 打开所在文件夹", self.open_largest_folder,
                        kind="ghost").pack(side=tk.LEFT, padx=8)
        self.app.button(bar, "📋 复制路径", self.copy_largest_path,
                        kind="ghost").pack(side=tk.LEFT)

        holder = tk.Frame(parent, bg=T.BG)
        holder.pack(fill=tk.BOTH, expand=True, padx=10, pady=(0, 8))
        self.largest_tree, _ = self.app.make_table(
            holder,
            ("size", "name", "folder"),
            (100, 300, 480),
            headings=("大小", "文件名", "所在目录"),
            height=16)

        self.app.hint(parent,
                      "需要释放 C 盘空间时，可先关闭相关软件，再用「深层空间回收」或手动把这些文件搬到其他盘。")

    # ==================== 空间分析逻辑 ====================

    def start_analysis(self):
        root = self.root_var.get().strip()
        if not root or not os.path.exists(root):
            self.app.show_warning("提示", "请选择有效的扫描目录")
            return
        try:
            depth = int(self.depth_var.get())
        except ValueError:
            depth = 3

        self.progress["value"] = 0
        self.progress_label.config(text="正在分析...")
        self.summary_label.config(text=f"正在分析 {root} ...（首次分析大目录可能较慢）")

        engine = DiskMapEngine()

        def work(progress_cb):
            engine.progress_callback = progress_cb
            return engine.analyze(root, max_depth=depth)

        def on_progress(percent, msg):
            self.progress["value"] = percent
            self.progress_label.config(text=msg)

        def on_done(result):
            self.analysis = result
            self.progress["value"] = 100
            self.progress_label.config(text="分析完成")
            self.node_stack = [result["tree"]]
            self.render_current()

        self._engine_ref = engine
        self.app.run_async(work, on_done=on_done, on_progress=on_progress)

    def stop_analysis(self):
        engine = getattr(self, "_engine_ref", None)
        if engine:
            engine.stop()
        self.progress_label.config(text="已请求停止")

    def go_up(self):
        if len(self.node_stack) > 1:
            self.node_stack.pop()
            self.render_current()

    def render_current(self):
        """渲染当前节点：树图 + 子项列表"""
        if not self.node_stack:
            return
        node = self.node_stack[-1]
        self.current_node = node

        total = self.analysis["size"] if self.analysis else 0
        path = node.get("path", "")
        size = node.get("size", 0)
        pct = (size / total * 100) if total else 0
        self.summary_label.config(
            text=f"当前位置：{path}　本层体积 {format_size(size)}（占分析范围 {pct:.1f}%）　"
                 f"分析范围总计 {format_size(total)}　"
                 f"共 {self.analysis.get('file_count', 0)} 个文件 / "
                 f"{self.analysis.get('dir_count', 0)} 个目录")

        # 子项列表
        for row in self.children_tree.get_children():
            self.children_tree.delete(row)
        children = node.get("children", [])
        for c in children:
            cps = (c.get("size", 0) / size * 100) if size else 0
            self.children_tree.insert("", tk.END, values=(
                ("📁 " if c.get("is_dir") else "📄 ") + c.get("name", ""),
                format_size(c.get("size", 0)),
                f"{cps:.1f}%",
            ))

        # 类型统计（只在根节点显示全量统计）
        for row in self.type_tree.get_children():
            self.type_tree.delete(row)
        for t in (self.analysis.get("types", []) if self.analysis else []):
            self.type_tree.insert("", tk.END, values=(
                t["name"], format_size(t["size"]), f"{t['percent']:.1f}%", t["count"]))

        # 大文件排行（可按类型筛选）
        for row in self.largest_tree.get_children():
            self.largest_tree.delete(row)
        largest = self.analysis.get("largest", []) if self.analysis else []
        if self.type_filter:
            largest = [f for f in largest
                       if self._category_of_name(f["name"]) == self.type_filter]
        for f in largest:
            self.largest_tree.insert("", tk.END, values=(
                format_size(f["size"]), f["name"], f["dir"]))

        self.render_treemap()

    def on_type_select(self, _event):
        """点击"文件类型统计"中的某类，筛选大文件排行只显示该类；再点同一行取消筛选"""
        sel = self.type_tree.selection()
        if not sel:
            return
        values = self.type_tree.item(sel[0], "values")
        if not values:
            return
        type_name = values[0]
        # 再点同一行 = 取消筛选
        if self.type_filter == type_name:
            self.type_filter = None
            self.app.set_status("已取消类型筛选")
            # 取消选中
            self.type_tree.selection_remove(sel[0])
        else:
            self.type_filter = type_name
            self.app.set_status(f"大文件排行已筛选为类型：{type_name}")
        self.render_current()

    def _category_of_name(self, filename):
        """按扩展名归类文件类型"""
        ext = os.path.splitext(filename)[1].lower()
        if not ext:
            return "无扩展名"
        return _EXT_GROUPS.get(ext, "其他")

    def render_treemap(self):
        """在画布上绘制 Treemap"""
        if not self.current_node:
            return
        canvas = self.canvas
        width = canvas.winfo_width()
        height = canvas.winfo_height()
        if width < 10 or height < 10:
            return

        canvas.delete("all")
        items = self.current_node.get("children", [])
        if not items:
            canvas.create_text(width // 2, height // 2,
                               text="该文件夹没有可显示的子项\n"
                                    "（可能已达保留层数上限，可在上方点击「返回上级」或重新分析）",
                               fill="#999999", font=("Microsoft YaHei", 10),
                               justify=tk.CENTER)
            self.rects = []
            return

        rects = layout_treemap(items, 2, 2, width - 4, height - 4)
        self.rects = []
        for r in rects:
            item = r["item"]
            color = _color_for(item)
            x, y, w, h = r["x"], r["y"], r["w"], r["h"]
            canvas.create_rectangle(x, y, x + w, y + h, fill=color, outline="#ffffff",
                                    width=1)
            # 只在空间足够时绘制文字
            if w > 56 and h > 22:
                label = item.get("name", "")
                max_chars = max(3, int(w / 8))
                if len(label) > max_chars:
                    label = label[:max_chars - 1] + "…"
                canvas.create_text(x + 5, y + 5, text=label, anchor=tk.NW,
                                   fill="#ffffff", font=("Microsoft YaHei", 8),
                                   width=max(20, w - 10))
                if h > 38:
                    canvas.create_text(x + 5, y + 20,
                                       text=format_size(item.get("size", 0)),
                                       anchor=tk.NW, fill="#f0f0f0",
                                       font=("Microsoft YaHei", 7))
            self.rects.append((r, item))

    def on_canvas_click(self, event):
        """点击树图命中条目"""
        for r, item in self.rects:
            if (r["x"] <= event.x <= r["x"] + r["w"] and
                    r["y"] <= event.y <= r["y"] + r["h"]):
                self.drill_into(item)
                return

    def drill_into(self, item):
        """进入某个条目（文件夹则下钻，文件则打开所在目录）"""
        if item.get("is_dir"):
            children = item.get("children")
            if children:
                self.node_stack.append(item)
                self.render_current()
                return
            # 已达层数上限，对这个目录单独重新分析
            path = item.get("path")
            if path and os.path.isdir(path):
                if not self.app.ask_yes_no(
                        "重新分析", f"该文件夹的子项未在首次分析中展开。\n"
                                     f"是否对 {path} 单独执行一次分析？"):
                    return
                self.root_var.set(path)
                self.start_analysis()
                return
        else:
            folder = os.path.dirname(item.get("path", ""))
            self._open_folder(folder)

    def on_child_double_click(self, _event):
        """双击子项列表进入"""
        sel = self.children_tree.selection()
        if not sel:
            return
        idx = self.children_tree.index(sel[0])
        children = self.current_node.get("children", []) if self.current_node else []
        if 0 <= idx < len(children):
            self.drill_into(children[idx])

    def _open_folder(self, folder):
        """在资源管理器中打开目录"""
        if not folder or not os.path.isdir(folder):
            self.app.show_warning("提示", "目录不存在")
            return
        try:
            os.startfile(folder)
        except Exception as e:
            self.app.show_error("打开失败", str(e))

    # ==================== 重复文件逻辑 ====================

    def _parse_min_size(self):
        text = self.dup_min_var.get().strip().upper().replace(" ", "")
        try:
            if text.endswith("KB"):
                return int(float(text[:-2]) * 1024)
            if text.endswith("MB"):
                return int(float(text[:-2]) * 1024 * 1024)
            if text.endswith("GB"):
                return int(float(text[:-2]) * 1024 ** 3)
            return int(float(text))
        except Exception:
            return 1024 * 1024

    def start_dup_scan(self):
        root = self.dup_root_var.get().strip()
        if not root or not os.path.exists(root):
            self.app.show_warning("提示", "请选择有效的扫描目录")
            return
        min_size = self._parse_min_size()

        self.dup_progress["value"] = 0
        self.dup_progress_label.config(text="正在收集候选文件...")
        self.dup_summary.config(text="正在查找重复文件...")

        engine = DiskMapEngine()

        def work(progress_cb):
            engine.progress_callback = progress_cb
            return engine.find_duplicates([root], min_size=min_size)

        def on_progress(percent, msg):
            self.dup_progress["value"] = percent
            self.dup_progress_label.config(text=msg)

        def on_done(result):
            self.dup_result = result
            self.dup_progress["value"] = 100
            self.dup_progress_label.config(text="查找完成")
            self.render_duplicates(result)

        self._dup_engine = engine
        self.app.run_async(work, on_done=on_done, on_progress=on_progress)

    def render_duplicates(self, result):
        for row in self.dup_tree.get_children():
            self.dup_tree.delete(row)
        self.dup_rows = result.get("groups", [])

        if not self.dup_rows:
            self.dup_summary.config(
                text=f"未发现重复文件（扫描 {result.get('scanned', 0)} 个文件）。"
                     f"可尝试调低最小体积阈值或更换扫描目录。")
            return

        for g in self.dup_rows:
            files = g.get("files", [])
            keep = files[0] if files else ""
            self.dup_tree.insert("", tk.END, values=(
                "✓",
                format_size(g.get("size", 0)),
                g.get("count", 0),
                keep,
                format_size(g.get("wasted", 0)),
            ))

        self.dup_summary.config(
            text=f"共扫描 {result.get('scanned', 0)} 个文件，发现 {len(self.dup_rows)} 组重复，"
                 f"可释放 {format_size(result.get('total_wasted', 0))}")

    def on_dup_click(self, event):
        try:
            if self.dup_tree.identify_region(event.x, event.y) != "cell":
                return
            if self.dup_tree.identify_column(event.x) != "#1":
                return
            item = self.dup_tree.identify_row(event.y)
            if item:
                cur = self.dup_tree.set(item, "sel")
                self.dup_tree.set(item, "sel", "" if cur == "✓" else "✓")
        except Exception:
            pass

    def delete_duplicates(self):
        if not self.dup_rows:
            self.app.show_warning("提示", "请先查找重复文件")
            return
        selected = []
        for i, row in enumerate(self.dup_tree.get_children()):
            if self.dup_tree.set(row, "sel") == "✓" and i < len(self.dup_rows):
                selected.append(self.dup_rows[i])
        if not selected:
            self.app.show_warning("提示", "请勾选要处理的重复文件组")
            return

        to_delete = []
        total = 0
        for g in selected:
            files = g.get("files", [])
            if len(files) > 1:
                to_delete.extend(files[1:])       # 保留第一个
                total += g.get("wasted", 0)

        if not self.app.ask_yes_no(
                "确认删除",
                f"将删除 {len(to_delete)} 个重复文件副本，预计释放 {format_size(total)}。\n\n"
                "删除不可恢复，是否继续？"):
            return

        def work(_progress_cb):
            return DiskMapEngine().delete_files(to_delete)

        def on_done(result):
            msg = (f"删除完成：成功 {result.get('deleted', 0)} 个，"
                   f"释放 {format_size(result.get('freed', 0))}。")
            failed = result.get("failed", [])
            if failed:
                msg += f"\n\n{len(failed)} 个文件删除失败（多因文件被占用）。"
                msg += "\n示例：" + failed[0][0]
            self.app.show_info("结果", msg)
            self.start_dup_scan()

        self.app.run_async(work, on_done=on_done)

    # ==================== 大文件排行 ====================

    def open_largest_folder(self):
        sel = self.largest_tree.selection()
        if not sel:
            self.app.show_warning("提示", "请先在列表中选择一个文件")
            return
        vals = self.largest_tree.item(sel[0], "values")
        if len(vals) >= 3:
            self._open_folder(vals[2])

    def copy_largest_path(self):
        sel = self.largest_tree.selection()
        if not sel:
            return
        vals = self.largest_tree.item(sel[0], "values")
        if len(vals) >= 3:
            path = os.path.join(vals[2], vals[1])
            try:
                self.app.root.clipboard_clear()
                self.app.root.clipboard_append(path)
                self.app.set_status(f"已复制路径：{path}")
            except Exception:
                pass


def build(app, parent):
    """页面入口：主程序通过此函数挂载页面"""
    page = DiskMapPage(app, parent)
    page.build()
    return page
