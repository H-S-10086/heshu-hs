# -*- coding: utf-8 -*-
"""
软件管理页面模块

三个标签页：
  1. 已安装软件     —— 读取注册表软件清单，支持搜索过滤、卸载、强制移除、打开安装目录
  2. 卸载残留扫描   —— 扫描卸载残留与失效注册记录，区分「注册表记录」与「磁盘目录」分别清理
  3. 软件搬家       —— 把软件安装目录移动到其他盘并建立目录联接（Junction）

挂载方式：主程序以 page_software.build(app, parent) 调用，parent 为 tk.Frame。
本模块只依赖标准库与 scanner / software_manager，导入时无任何副作用。
"""

import os
import shutil
import tkinter as tk
from tkinter import ttk, simpledialog

from scanner import format_size
from software_manager import SoftwareManager


def build(app, parent):
    """构建「软件管理」页面并挂载到 parent（返回页面对象）"""
    page = SoftwarePage(app, parent)
    return page


class SoftwarePage:
    """软件管理页面"""

    def __init__(self, app, parent):
        self.app = app
        self.parent = parent
        self.THEME = app.THEME

        # ---- 数据缓存 ----
        self.all_software = []       # 全部已安装软件（完整列表，与标签页1表格数据源一致）
        self.shown_software = []     # 标签页1当前显示列表（过滤后，与表格行顺序一一对应）
        self.move_list = []          # 标签页3当前显示列表（与表格行顺序一一对应）
        self.leftover_results = []   # 标签页2扫描结果（与表格行顺序一一对应）
        self.sw_stats = {}           # 软件清单统计
        self.move_loaded = False     # 搬家页是否已载入过数据

        # ---- 忙状态，防止重复触发 ----
        self._sw_busy = False
        self._lo_busy = False
        self._mv_busy = False

        # ---- 控件变量 ----
        self.search_var = tk.StringVar()
        self.lo_select_all_var = tk.BooleanVar(value=True)
        self.mv_target_var = tk.StringVar()

        self._build_ui()

    # ==================== 界面搭建 ====================

    def _build_ui(self):
        app = self.app
        T = self.THEME

        app.section_title(self.parent, "软件管理",
                          "卸载软件、清理卸载残留、把软件搬到其他盘释放 C 盘空间。")

        self.nb = ttk.Notebook(self.parent)
        self.nb.pack(fill=tk.BOTH, expand=True, padx=12, pady=(0, 10))

        self._build_tab_installed()
        self._build_tab_leftover()
        self._build_tab_move()

        # 切换标签页时懒加载搬家数据
        self.nb.bind("<<NotebookTabChanged>>", self._on_tab_changed)

    # ---------- 标签页 1：已安装软件 ----------

    def _build_tab_installed(self):
        app = self.app
        T = self.THEME

        tab = tk.Frame(self.nb, bg=T.BG)
        self.nb.add(tab, text="  已安装软件  ")

        # 工具条卡片
        card = app.card(tab, pady=(10, 6), padx=12)
        bar = tk.Frame(card, bg=T.CARD_BG)
        bar.pack(fill=tk.X, padx=12, pady=10)

        tk.Label(bar, text="搜索：", bg=T.CARD_BG, fg=T.TEXT,
                 font=("Microsoft YaHei", 9)).pack(side=tk.LEFT)
        entry = tk.Entry(bar, textvariable=self.search_var, font=("Microsoft YaHei", 9),
                         width=28, relief=tk.SOLID, bd=1)
        entry.pack(side=tk.LEFT, padx=(0, 10))

        self.btn_refresh = app.button(bar, "刷新列表", self._refresh_software, kind="primary")
        self.btn_refresh.pack(side=tk.LEFT, padx=4)
        self.btn_uninstall = app.button(bar, "卸载选中", self._uninstall_selected, kind="danger")
        self.btn_uninstall.pack(side=tk.LEFT, padx=4)
        self.btn_force = app.button(bar, "强制移除", self._force_uninstall_selected, kind="danger")
        self.btn_force.pack(side=tk.LEFT, padx=4)
        self.btn_open_dir = app.button(bar, "打开安装目录", self._open_install_dir, kind="ghost")
        self.btn_open_dir.pack(side=tk.LEFT, padx=4)

        # 进度条
        self.sw_progress, self.sw_progress_label = app.progress_row(tab)

        # 汇总信息
        self.sw_summary = tk.Label(tab, text="尚未读取软件列表，请点击「刷新列表」",
                                   bg=T.BG, fg=T.TEXT_LIGHT,
                                   font=("Microsoft YaHei", 9), anchor=tk.W)
        self.sw_summary.pack(fill=tk.X, padx=22, pady=(0, 4))

        # 表格
        self.sw_tree, _ = app.make_table(
            tab,
            ("name", "ver", "pub", "size", "loc", "date", "scope"),
            (240, 110, 170, 100, 320, 100, 100),
            headings=("软件名称", "版本", "发布者", "占用大小", "安装位置", "安装日期", "范围"),
            height=13)
        try:
            self.sw_tree.tag_configure("sys", foreground=T.TEXT_LIGHT)
        except Exception:
            pass

        # 搜索框内容变化 → 实时过滤
        self.search_var.trace_add("write", self._on_search_change)

    # ---------- 标签页 2：卸载残留扫描 ----------

    def _build_tab_leftover(self):
        app = self.app
        T = self.THEME

        tab = tk.Frame(self.nb, bg=T.BG)
        self.nb.add(tab, text="  卸载残留扫描  ")

        card = app.card(tab, pady=(10, 6), padx=12)
        bar = tk.Frame(card, bg=T.CARD_BG)
        bar.pack(fill=tk.X, padx=12, pady=10)

        self.btn_scan_lo = app.button(bar, "开始扫描", self._scan_leftovers, kind="primary")
        self.btn_scan_lo.pack(side=tk.LEFT, padx=4)
        self.btn_clean_lo = app.button(bar, "清理选中", self._clean_leftovers, kind="danger")
        self.btn_clean_lo.pack(side=tk.LEFT, padx=4)
        tk.Checkbutton(bar, text="全选", variable=self.lo_select_all_var,
                       bg=T.CARD_BG, fg=T.TEXT, font=("Microsoft YaHei", 9),
                       activebackground=T.CARD_BG, selectcolor=T.CARD_BG,
                       command=self._toggle_select_all_lo).pack(side=tk.LEFT, padx=10)

        self.lo_progress, self.lo_progress_label = app.progress_row(tab)

        self.lo_summary = tk.Label(tab, text="尚未扫描，请点击「开始扫描」",
                                   bg=T.BG, fg=T.TEXT_LIGHT,
                                   font=("Microsoft YaHei", 9), anchor=tk.W)
        self.lo_summary.pack(fill=tk.X, padx=22, pady=(0, 4))

        self.lo_tree, _ = app.make_table(
            tab,
            ("sel", "category", "source", "path", "size", "desc"),
            (50, 90, 230, 420, 90, 360),
            headings=("选择", "分类", "残留来源", "路径", "大小", "说明"),
            height=13)
        # 点击第一列切换勾选
        self.lo_tree.bind("<Button-1>", self._on_leftover_click)

        tk.Label(tab,
                 text="“疑似残留”是通过注册表比对推断的，绿色版/免安装软件可能没有注册表记录，请确认后再删除。",
                 bg=T.BG, fg=T.TEXT_LIGHT, font=("Microsoft YaHei", 9),
                 anchor=tk.W, justify=tk.LEFT, wraplength=940).pack(
            fill=tk.X, padx=22, pady=(2, 6))

    # ---------- 标签页 3：软件搬家 ----------

    def _build_tab_move(self):
        app = self.app
        T = self.THEME

        tab = tk.Frame(self.nb, bg=T.BG)
        self.nb.add(tab, text="  软件搬家  ")

        tk.Label(tab,
                 text="把软件安装目录移动到其他盘，并在原位置创建目录联接（Junction），"
                      "软件无需重装即可继续使用。",
                 bg=T.BG, fg=T.TEXT_LIGHT, font=("Microsoft YaHei", 9),
                 anchor=tk.W, justify=tk.LEFT, wraplength=940).pack(
            fill=tk.X, padx=22, pady=(10, 4))

        # 上半部分：软件表格（复用已安装软件列表）
        self.mv_tree, _ = app.make_table(
            tab,
            ("name", "pub", "loc", "size"),
            (220, 180, 420, 100),
            headings=("软件名称", "发布者", "安装位置", "大小"),
            height=10)

        # 下半部分：操作区
        card = app.card(tab, pady=(6, 4), padx=12)
        inner = tk.Frame(card, bg=T.CARD_BG)
        inner.pack(fill=tk.X, padx=12, pady=10)

        # 目标盘选择
        row1 = tk.Frame(inner, bg=T.CARD_BG)
        row1.pack(fill=tk.X, pady=(0, 8))
        tk.Label(row1, text="目标盘：", bg=T.CARD_BG, fg=T.TEXT,
                 font=("Microsoft YaHei", 9)).pack(side=tk.LEFT)
        self._target_drives = self._get_target_drives()
        self.mv_combo = ttk.Combobox(row1, textvariable=self.mv_target_var,
                                     values=self._target_drives, width=12,
                                     state="readonly", font=("Microsoft YaHei", 9))
        self.mv_combo.pack(side=tk.LEFT)
        if self._target_drives:
            self.mv_combo.current(0)
        else:
            tk.Label(row1, text="未检测到 C 盘以外的可用磁盘", bg=T.CARD_BG,
                     fg=T.DANGER, font=("Microsoft YaHei", 9)).pack(side=tk.LEFT, padx=8)

        # 操作按钮
        row2 = tk.Frame(inner, bg=T.CARD_BG)
        row2.pack(fill=tk.X)
        self.btn_check_run = app.button(row2, "检查是否正在运行", self._check_move_running,
                                        kind="ghost")
        self.btn_check_run.pack(side=tk.LEFT, padx=(0, 6))
        self.btn_move = app.button(row2, "开始搬家", self._start_move, kind="primary")
        self.btn_move.pack(side=tk.LEFT, padx=6)
        self.btn_undo_move = app.button(row2, "撤销搬家", self._undo_move, kind="ghost")
        self.btn_undo_move.pack(side=tk.LEFT, padx=6)

        # 结果提示
        self.mv_status = tk.Label(inner, text="请先在上方选中要搬家的软件",
                                  bg=T.CARD_BG, fg=T.TEXT_LIGHT,
                                  font=("Microsoft YaHei", 9), anchor=tk.W,
                                  justify=tk.LEFT, wraplength=900)
        self.mv_status.pack(fill=tk.X, pady=(8, 0))

        tk.Label(tab,
                 text="搬家需要目标盘有足够空间；对安装在 Program Files 的软件需要管理员权限；"
                      "搬家中途请勿关闭本程序。搬家的软件必须先完全退出。",
                 bg=T.BG, fg=T.WARNING, font=("Microsoft YaHei", 9),
                 anchor=tk.W, justify=tk.LEFT, wraplength=940).pack(
            fill=tk.X, padx=22, pady=(2, 8))

    # ==================== 标签页 1 逻辑 ====================

    def _on_search_change(self, *_args):
        """搜索框内容变化 → 重新过滤表格"""
        self._render_software()

    def _refresh_software(self):
        """刷新软件列表（后台线程）"""
        if self._sw_busy:
            return
        self._sw_busy = True
        self.btn_refresh.config(state=tk.DISABLED)
        self.app.set_status("正在读取已安装软件清单...")

        def work(progress_cb):
            mgr = SoftwareManager(progress_cb)
            software = mgr.list_installed_software()
            stats = mgr.get_software_stats(software)
            return software, stats

        def on_progress(percent, _msg):
            try:
                self.sw_progress["value"] = percent
                self.sw_progress_label.config(text=f"{percent}%")
            except Exception:
                pass

        def on_done(result):
            self._sw_busy = False
            self.btn_refresh.config(state=tk.NORMAL)
            try:
                software, stats = result
            except Exception:
                return
            self.all_software = list(software or [])
            self.sw_stats = stats or {}
            self.shown_software = []
            self._render_software()
            self._render_move()
            self.app.set_status(
                f"已读取 {len(self.all_software)} 款软件，"
                f"估算占用合计 {format_size(self.sw_stats.get('estimated_total', 0))}")

        self.app.run_async(work, on_done=on_done, on_progress=on_progress)

    def _render_software(self):
        """按关键词过滤并填充标签页1表格"""
        try:
            keyword = self.search_var.get().strip().lower()
        except Exception:
            keyword = ""

        if keyword:
            shown = [s for s in self.all_software
                     if keyword in str(s.get("name", "")).lower()
                     or keyword in str(s.get("publisher", "")).lower()]
        else:
            shown = list(self.all_software)

        self.shown_software = shown

        for row in self.sw_tree.get_children():
            self.sw_tree.delete(row)

        for s in shown:
            size = s.get("estimated_size", 0) or 0
            values = (
                s.get("name", "") or "—",
                s.get("version", "") or "—",
                s.get("publisher", "") or "—",
                format_size(size) if size else "—",
                s.get("install_location", "") or "—",
                s.get("install_date", "") or "—",
                s.get("scope", "") or "—",
            )
            tags = ("sys",) if s.get("is_system_component") else ()
            self.sw_tree.insert("", tk.END, values=values, tags=tags)

        total_size = self.sw_stats.get(
            "estimated_total", sum(s.get("estimated_size", 0) for s in self.all_software))
        text = f"共 {len(self.all_software)} 款软件，估算占用合计 {format_size(total_size)}"
        if keyword:
            text += f"　（当前过滤出 {len(shown)} 款）"
        self.sw_summary.config(text=text)

    def _selected_software(self):
        """返回标签页1当前选中的软件 dict（可能为 None）"""
        sel = self.sw_tree.selection()
        if not sel:
            return None
        try:
            idx = self.sw_tree.index(sel[0])
        except Exception:
            return None
        if 0 <= idx < len(self.shown_software):
            return self.shown_software[idx]
        return None

    def _uninstall_selected(self):
        sw = self._selected_software()
        if not sw:
            self.app.show_warning("提示", "请先在表格中选中要卸载的软件")
            return
        name = sw.get("name", "")
        if not self.app.ask_yes_no(
                "确认卸载",
                f"即将卸载「{name}」。\n\n"
                "将启动软件自带的卸载程序，请在弹出窗口中完成卸载。\n"
                "卸载完成后，可到「卸载残留扫描」清理遗留文件。\n\n是否继续？"):
            return

        def work(progress_cb):
            return SoftwareManager(progress_cb).uninstall(sw, quiet=True)

        def on_done(result):
            try:
                ok, msg = result
            except Exception:
                return
            if ok:
                self.app.show_info("卸载", msg)
            else:
                self.app.show_error("无法卸载", msg)

        self.app.set_status(f"正在卸载 {name} ...")
        self.app.run_async(work, on_done=on_done)

    def _force_uninstall_selected(self):
        sw = self._selected_software()
        if not sw:
            self.app.show_warning("提示", "请先在表格中选中要强制移除的软件")
            return
        name = sw.get("name", "")
        msg = ("强制移除会直接删除注册表记录和安装目录，不可恢复，"
               "仅用于无法正常卸载的软件。\n\n")
        if sw.get("is_system_component"):
            msg += ("⚠ 该软件疑似系统组件，强制移除可能导致系统或其它程序异常，"
                    "强烈建议不要移除。\n\n")
        msg += f"确定要强制移除「{name}」吗？"
        if not self.app.ask_yes_no("危险操作确认", msg):
            return

        def work(progress_cb):
            return SoftwareManager(progress_cb).force_uninstall(sw)

        def on_done(result):
            try:
                ok, msg2 = result
            except Exception:
                return
            if ok:
                self.app.show_info("强制移除", msg2)
                self._refresh_software()
            else:
                self.app.show_error("强制移除未完成", msg2)

        self.app.set_status(f"正在强制移除 {name} ...")
        self.app.run_async(work, on_done=on_done)

    def _open_install_dir(self):
        sw = self._selected_software()
        if not sw:
            self.app.show_warning("提示", "请先在表格中选中软件")
            return
        loc = str(sw.get("install_location", "") or "").strip().strip('"')
        if not loc:
            self.app.show_warning("提示", "该软件没有记录安装目录，无法打开")
            return
        if not os.path.isdir(loc):
            self.app.show_warning("提示", f"安装目录不存在：\n{loc}")
            return
        try:
            os.startfile(loc)
        except Exception as e:
            self.app.show_error("打开失败", f"无法打开安装目录：\n{e}")

    # ==================== 标签页 2 逻辑 ====================

    def _on_leftover_click(self, event):
        """点击第一列切换勾选"""
        try:
            if self.lo_tree.identify_region(event.x, event.y) != "cell":
                return
            if self.lo_tree.identify_column(event.x) != "#1":
                return
            item = self.lo_tree.identify_row(event.y)
            if not item:
                return
            cur = self.lo_tree.set(item, "sel")
            self.lo_tree.set(item, "sel", "" if cur == "✓" else "✓")
        except Exception:
            pass

    def _toggle_select_all_lo(self):
        """全选 / 全不选"""
        val = "✓" if self.lo_select_all_var.get() else ""
        for item in self.lo_tree.get_children():
            self.lo_tree.set(item, "sel", val)

    def _scan_leftovers(self):
        if self._lo_busy:
            return
        self._lo_busy = True
        self.btn_scan_lo.config(state=tk.DISABLED)
        self.app.set_status("正在扫描卸载残留...")

        def work(progress_cb):
            return SoftwareManager(progress_cb).scan_software_leftovers()

        def on_progress(percent, _msg):
            try:
                self.lo_progress["value"] = percent
                self.lo_progress_label.config(text=f"{percent}%")
            except Exception:
                pass

        def on_done(results):
            self._lo_busy = False
            self.btn_scan_lo.config(state=tk.NORMAL)
            self.leftover_results = list(results or [])
            self._render_leftovers()

        self.app.run_async(work, on_done=on_done, on_progress=on_progress)

    def _render_leftovers(self):
        for row in self.lo_tree.get_children():
            self.lo_tree.delete(row)
        total_size = 0
        for r in self.leftover_results:
            size = getattr(r, "size", 0) or 0
            total_size += size
            self.lo_tree.insert("", tk.END, values=(
                "✓",
                getattr(r, "category", ""),
                getattr(r, "source", ""),
                getattr(r, "path", ""),
                format_size(size) if size else "—",
                getattr(r, "description", ""),
            ))
        self.lo_summary.config(
            text=f"共发现 {len(self.leftover_results)} 项残留，合计约 {format_size(total_size)}")
        self.app.set_status(
            f"残留扫描完成，共 {len(self.leftover_results)} 项，合计 {format_size(total_size)}")

    def _selected_leftovers(self):
        """返回被勾选的 ScanResult 列表（按表格顺序）"""
        selected = []
        for item in self.lo_tree.get_children():
            if self.lo_tree.set(item, "sel") != "✓":
                continue
            try:
                idx = self.lo_tree.index(item)
            except Exception:
                continue
            if 0 <= idx < len(self.leftover_results):
                selected.append(self.leftover_results[idx])
        return selected

    def _clean_leftovers(self):
        selected = self._selected_leftovers()
        if not selected:
            self.app.show_warning("提示", "请先勾选要清理的残留项")
            return

        # 分类：注册表记录（幽灵软件）与 磁盘残留
        ghost = [r for r in selected if "幽灵软件" in str(getattr(r, "source", ""))]
        disk = [r for r in selected if "幽灵软件" not in str(getattr(r, "source", ""))]
        disk_size = sum(getattr(r, "size", 0) or 0 for r in disk)

        if not self.app.ask_yes_no(
                "确认清理",
                f"即将处理 {len(selected)} 项残留：\n"
                f"· 失效注册表记录 {len(ghost)} 条\n"
                f"· 磁盘残留目录/文件 {len(disk)} 项，预计释放约 {format_size(disk_size)}\n\n"
                "删除后不可恢复，是否继续？"):
            return

        if self._lo_busy:
            return
        self._lo_busy = True
        self.btn_clean_lo.config(state=tk.DISABLED)
        self.app.set_status("正在清理残留...")

        def work(progress_cb):
            deleted = 0
            failures = []
            freed = 0

            # 1) 注册表记录：交给引擎处理
            if ghost:
                try:
                    cnt, fail = SoftwareManager().clean_ghost_registry_entries(ghost)
                    deleted += cnt
                    failures += list(fail or [])
                except Exception as e:
                    failures.append(("<注册表记录>", str(e)))

            # 2) 磁盘残留：逐个删除
            total = len(disk) or 1
            for i, r in enumerate(disk):
                path = getattr(r, "path", "")
                try:
                    if os.path.isdir(path):
                        shutil.rmtree(path, ignore_errors=False)
                    elif os.path.exists(path):
                        os.remove(path)
                    else:
                        failures.append((path, "路径已不存在"))
                        continue
                    freed += getattr(r, "size", 0) or 0
                    deleted += 1
                except Exception as e:
                    failures.append((path, str(e)))
                try:
                    progress_cb(int((i + 1) / total * 100), f"正在清理：{path}")
                except Exception:
                    pass

            return deleted, failures, freed

        def on_done(result):
            self._lo_busy = False
            self.btn_clean_lo.config(state=tk.NORMAL)
            try:
                deleted, failures, freed = result
            except Exception:
                return
            summary = f"清理完成：成功 {deleted} 项，释放约 {format_size(freed)}。"
            if failures:
                summary += f"\n\n失败 {len(failures)} 项：\n"
                for p, reason in failures[:8]:
                    summary += f"· {p}：{reason}\n"
                if len(failures) > 8:
                    summary += f"… 其余 {len(failures) - 8} 项略"
            self.app.show_info("清理结果", summary)
            # 自动重新扫描
            self._scan_leftovers()

        self.app.run_async(work, on_done=on_done)

    # ==================== 标签页 3 逻辑 ====================

    def _on_tab_changed(self, _event=None):
        """切到「软件搬家」标签页时懒加载一次软件列表"""
        try:
            if self.nb.index(self.nb.select()) != 2:
                return
        except Exception:
            return
        if self.move_loaded:
            return
        if self.all_software:
            self._render_move()
        else:
            self._refresh_software()

    def _render_move(self):
        """填充搬家页表格（复用已安装软件列表）"""
        self.move_list = list(self.all_software)
        for row in self.mv_tree.get_children():
            self.mv_tree.delete(row)
        for s in self.move_list:
            size = s.get("estimated_size", 0) or 0
            self.mv_tree.insert("", tk.END, values=(
                s.get("name", "") or "—",
                s.get("publisher", "") or "—",
                s.get("install_location", "") or "—",
                format_size(size) if size else "—",
            ))
        self.move_loaded = True

    def _get_target_drives(self):
        """获取 C 盘以外的可用目标盘（如 ['D:\\', 'E:\\']）"""
        drives = []
        try:
            from diskmap import DiskMapEngine
            info = DiskMapEngine().list_drives()
            for d in info:
                letter = str(d.get("letter") or d.get("drive", "")).strip()
                if not letter:
                    continue
                letter = letter[0].upper()
                if letter == "C":
                    continue
                if d.get("type") == "光驱":
                    continue
                root = letter + ":\\"
                if root not in drives:
                    drives.append(root)
        except Exception:
            drives = []
        if not drives:
            # 退回自行探测
            for letter in "DEFGHIJKLMNOPQRSTUVWXYZ":
                root = letter + ":\\"
                try:
                    if os.path.exists(root):
                        drives.append(root)
                except Exception:
                    continue
        return drives

    def _selected_move_software(self):
        """返回搬家页当前选中的软件 dict（可能为 None）"""
        sel = self.mv_tree.selection()
        if not sel:
            return None
        try:
            idx = self.mv_tree.index(sel[0])
        except Exception:
            return None
        if 0 <= idx < len(self.move_list):
            return self.move_list[idx]
        return None

    def _check_move_running(self):
        sw = self._selected_move_software()
        if not sw:
            self.app.show_warning("提示", "请先在上方表格中选中要搬家的软件")
            return
        loc = str(sw.get("install_location", "") or "").strip().strip('"')
        if not loc:
            self.mv_status.config(text="该软件没有记录安装目录，无法检查/搬家",
                                  fg=self.THEME.WARNING)
            return

        self.mv_status.config(text="正在检查软件运行状态...", fg=self.THEME.TEXT_LIGHT)
        self.btn_check_run.config(state=tk.DISABLED)

        def work(progress_cb):
            return SoftwareManager(progress_cb).check_software_running(loc)

        def on_done(result):
            self.btn_check_run.config(state=tk.NORMAL)
            try:
                running, info = result
            except Exception:
                return
            self.mv_status.config(
                text=info,
                fg=self.THEME.DANGER if running else self.THEME.ACCENT)

        self.app.run_async(work, on_done=on_done)

    def _start_move(self):
        sw = self._selected_move_software()
        if not sw:
            self.app.show_warning("提示", "请先在上方表格中选中要搬家的软件")
            return
        src = str(sw.get("install_location", "") or "").strip().strip('"')
        if not src:
            self.app.show_warning("提示", "该软件没有可用的安装目录，无法搬家")
            return
        target = self.mv_target_var.get().strip()
        if not target:
            self.app.show_warning("提示", "请选择目标盘")
            return
        if self._mv_busy:
            return

        name = sw.get("name", "")
        if not self.app.ask_yes_no(
                "确认搬家",
                f"即将把「{name}」的安装目录：\n{src}\n\n"
                f"移动到：{target}\n并在原位置创建目录联接。\n\n"
                "请确认该软件已完全退出，且目标盘空间充足。是否继续？"):
            return

        self._mv_busy = True
        self.btn_move.config(state=tk.DISABLED)
        self.mv_status.config(text=f"正在搬家：{name} ...", fg=self.THEME.PRIMARY)

        def work(progress_cb):
            return SoftwareManager(progress_cb).move_software(sw, target, create_junction=True)

        def on_done(result):
            self._mv_busy = False
            self.btn_move.config(state=tk.NORMAL)
            try:
                ok, msg = result
            except Exception:
                return
            if ok:
                self.mv_status.config(text=msg, fg=self.THEME.ACCENT)
                self.app.show_info("搬家完成", msg)
                self.move_loaded = False
                self._refresh_software()
            else:
                self.mv_status.config(text=msg, fg=self.THEME.DANGER)
                self.app.show_error("搬家未完成", msg)

        self.app.run_async(work, on_done=on_done)

    def _undo_move(self):
        if self._mv_busy:
            return

        def ask_original():
            return simpledialog.askstring(
                "撤销搬家",
                "请输入搬家前软件的原安装路径（例如 C:\\Program Files\\XXX）：",
                parent=self.parent.winfo_toplevel())

        try:
            original = ask_original()
        except Exception as e:
            self.app.show_error("撤销搬家", f"无法弹出输入框：{e}")
            return
        if not original or not original.strip():
            return

        self._mv_busy = True
        self.btn_undo_move.config(state=tk.DISABLED)
        self.mv_status.config(text="正在撤销搬家...", fg=self.THEME.PRIMARY)

        def work(progress_cb):
            return SoftwareManager(progress_cb).undo_software_move(original.strip())

        def on_done(result):
            self._mv_busy = False
            self.btn_undo_move.config(state=tk.NORMAL)
            try:
                ok, msg = result
            except Exception:
                return
            if ok:
                self.mv_status.config(text=msg, fg=self.THEME.ACCENT)
                self.app.show_info("撤销搬家", msg)
            else:
                self.mv_status.config(text=msg, fg=self.THEME.DANGER)
                self.app.show_error("撤销未完成", msg)

        self.app.run_async(work, on_done=on_done)
