# -*- coding: utf-8 -*-
"""
启动项管理页面
包含五个标签页：
  1. 开机启动项   —— 自启项全景，支持禁用/启用/删除/恢复（对标 Autoruns）
  2. 系统服务     —— 自动启动服务列表与启动类型调整
  3. 计划任务     —— 计划任务列表与启用/禁用/删除
  4. 开机耗时     —— 读取系统开机耗时记录
  5. 自动清理计划 —— 创建/删除定时自动清理的计划任务

本页面只做展示与交互，实际查询与修改全部交给 startup_manager.StartupEngine。
所有耗时操作均通过 app.run_async 放到后台线程执行，避免界面卡死。
"""

import os
import sys
import tkinter as tk
from tkinter import ttk

from scanner import format_size
from startup_manager import StartupEngine


class StartupPage:
    """启动项与服务管理页面控制器"""

    def __init__(self, app, parent):
        self.app = app
        self.parent = parent

        # 各标签页的最新数据（用于按表格行号回查原始 dict）
        self.startup_rows = []     # 开机启动项
        self.service_rows = []     # 系统服务
        self.task_rows = []        # 计划任务
        self.boot_records = []     # 开机耗时记录

    # ==================== 界面搭建 ====================

    def build(self):
        """搭建整个页面：标题 + 五标签页 Notebook"""
        T = self.app.THEME
        self.app.section_title(
            self.parent, "启动项管理",
            "管理开机自启项、系统服务与计划任务，加快开机速度。")

        nb = ttk.Notebook(self.parent)
        nb.pack(fill=tk.BOTH, expand=True, padx=8, pady=8)
        self.notebook = nb

        tab1 = tk.Frame(nb, bg=T.BG)
        tab2 = tk.Frame(nb, bg=T.BG)
        tab3 = tk.Frame(nb, bg=T.BG)
        tab4 = tk.Frame(nb, bg=T.BG)
        tab5 = tk.Frame(nb, bg=T.BG)
        nb.add(tab1, text="  开机启动项  ")
        nb.add(tab2, text="  系统服务  ")
        nb.add(tab3, text="  计划任务  ")
        nb.add(tab4, text="  开机耗时  ")
        nb.add(tab5, text="  自动清理计划  ")

        self._build_tab_startup(tab1)
        self._build_tab_services(tab2)
        self._build_tab_tasks(tab3)
        self._build_tab_boot(tab4)
        self._build_tab_auto(tab5)

    # ---------- 通用小工具 ----------

    def _make_toolbar(self, parent):
        """创建一个白色工具条卡片，返回 (卡片, 按钮行)"""
        T = self.app.THEME
        card = tk.Frame(parent, bg=T.CARD_BG, bd=0,
                        highlightbackground=T.BORDER, highlightthickness=1)
        card.pack(fill=tk.X, padx=10, pady=(10, 6))
        bar = tk.Frame(card, bg=T.CARD_BG)
        bar.pack(fill=tk.X, padx=12, pady=(10, 6))
        return card, bar

    def _make_progress(self, parent, bg):
        """在指定容器内创建进度条 + 状态文字，返回 (进度条, 标签)"""
        T = self.app.THEME
        frame = tk.Frame(parent, bg=bg)
        frame.pack(fill=tk.X, padx=12, pady=(0, 8))
        bar = ttk.Progressbar(frame, mode="determinate")
        bar.pack(side=tk.LEFT, fill=tk.X, expand=True)
        label = tk.Label(frame, text="就绪", bg=bg, fg=T.TEXT_LIGHT,
                         font=("Microsoft YaHei", 9), width=34, anchor=tk.W)
        label.pack(side=tk.LEFT, padx=8)
        return bar, label

    def _make_summary(self, parent, bg, text="尚未扫描"):
        """创建一行加粗的汇总文字标签"""
        label = tk.Label(parent, text=text, bg=bg, fg=self.app.THEME.PRIMARY,
                         font=("Microsoft YaHei", 10, "bold"))
        label.pack(anchor=tk.W, padx=12, pady=(0, 10))
        return label

    def _row_by_selection(self, tree, rows):
        """根据表格选中行回查原始 dict（无选中返回 None）"""
        sel = tree.selection()
        if not sel:
            return None
        try:
            idx = tree.index(sel[0])
        except Exception:
            return None
        if 0 <= idx < len(rows):
            return rows[idx]
        return None

    # ---------- 标签页 1：开机启动项 ----------

    def _build_tab_startup(self, parent):
        T = self.app.THEME
        card, bar = self._make_toolbar(parent)

        self.app.button(bar, "刷新", self.refresh_startup).pack(side=tk.LEFT)
        self.app.button(bar, "禁用选中", lambda: self._apply_startup_enabled(False),
                        kind="ghost").pack(side=tk.LEFT, padx=6)
        self.app.button(bar, "启用选中", lambda: self._apply_startup_enabled(True),
                        kind="ghost").pack(side=tk.LEFT, padx=6)
        self.app.button(bar, "删除选中", self.delete_selected, kind="danger").pack(
            side=tk.LEFT, padx=6)
        self.app.button(bar, "恢复已删除项", self.recover_item, kind="ghost").pack(
            side=tk.LEFT, padx=6)

        self.startup_bar, self.startup_label = self._make_progress(card, T.CARD_BG)
        self.startup_summary = self._make_summary(card, T.CARD_BG)

        holder = tk.Frame(parent, bg=T.BG)
        holder.pack(fill=tk.BOTH, expand=True, padx=10, pady=(0, 8))
        self.startup_tree, _ = self.app.make_table(
            holder,
            ("loc", "name", "cmd", "pub", "state", "risk", "size"),
            (150, 190, 300, 140, 70, 70, 90),
            headings=("位置", "名称", "启动命令", "发布者", "状态", "风险", "目标大小"),
            height=14)
        # 失效项红色、可疑项橙色
        self.startup_tree.tag_configure("dead", foreground=T.DANGER)
        self.startup_tree.tag_configure("suspect", foreground=T.WARNING)

    def refresh_startup(self):
        """刷新开机启动项列表（后台线程执行）"""
        self.startup_bar["value"] = 0
        self.startup_label.config(text="正在扫描启动项...")
        self.startup_summary.config(text="正在扫描启动项...")

        def work(progress_cb):
            try:
                return StartupEngine(progress_cb).scan_startup_items()
            except Exception:
                return None

        def on_progress(percent, msg):
            self.startup_bar["value"] = percent
            self.startup_label.config(text=msg)

        def on_done(items):
            if items is None:
                self.startup_label.config(text="扫描失败")
                self.app.show_error("扫描失败", "无法获取开机启动项列表，请稍后重试或检查权限。")
                return
            self._render_startup(items)

        self.app.run_async(work, on_done=on_done, on_progress=on_progress)

    def _render_startup(self, items):
        tree = self.startup_tree
        for row in tree.get_children():
            tree.delete(row)

        self.startup_rows = list(items)
        enabled = dead = suspect = 0
        for it in self.startup_rows:
            risk = it.get("risk", "")
            size = it.get("size", 0) or 0
            tags = ()
            if risk == "失效":
                dead += 1
                tags = ("dead",)
            elif risk == "可疑":
                suspect += 1
                tags = ("suspect",)
            if it.get("enabled"):
                enabled += 1
            tree.insert("", tk.END, values=(
                it.get("location", ""),
                it.get("name", ""),
                it.get("command", ""),
                it.get("publisher", "") or "—",
                "已启用" if it.get("enabled") else "已禁用",
                risk,
                format_size(size) if size else "—",
            ), tags=tags)

        self.startup_bar["value"] = 100
        self.startup_label.config(text="扫描完成")
        total = len(self.startup_rows)
        self.startup_summary.config(
            text="共 %d 项，启用中 %d 项，失效 %d 项，可疑 %d 项" % (total, enabled, dead, suspect))
        self.app.set_status("启动项扫描完成，共 %d 项" % total)

    def _apply_startup_enabled(self, enabled):
        """禁用/启用选中的启动项"""
        item = self._row_by_selection(self.startup_tree, self.startup_rows)
        if not item:
            self.app.show_warning("提示", "请先在列表中选择一个启动项")
            return
        action = "启用" if enabled else "禁用"

        def work(_progress_cb):
            try:
                return StartupEngine().set_startup_item_enabled(item, enabled)
            except Exception as exc:
                return False, str(exc)

        def on_done(result):
            ok, msg = result if result else (False, "操作失败")
            if ok:
                self.app.show_info("完成", msg)
                self.refresh_startup()
            else:
                self.app.show_error("%s失败" % action, msg)

        self.app.run_async(work, on_done=on_done)

    def delete_selected(self):
        """删除选中的启动项（删除前二次确认，引擎会自动备份）"""
        item = self._row_by_selection(self.startup_tree, self.startup_rows)
        if not item:
            self.app.show_warning("提示", "请先在列表中选择一个启动项")
            return
        if not self.app.ask_yes_no(
                "确认删除",
                "即将删除启动项“%s”。\n\n"
                "删除前会自动备份到用户目录，可通过「恢复已删除项」还原。\n是否继续？"
                % item.get("name", "")):
            return

        def work(_progress_cb):
            try:
                return StartupEngine().delete_startup_item(item)
            except Exception as exc:
                return False, str(exc)

        def on_done(result):
            ok, msg = result if result else (False, "删除失败")
            if ok:
                self.app.show_info("删除完成", msg)
                self.refresh_startup()
            else:
                self.app.show_error("删除失败", msg)

        self.app.run_async(work, on_done=on_done)

    def recover_item(self):
        """列出备份并恢复此前删除的启动项"""
        def work(_progress_cb):
            try:
                return StartupEngine().list_startup_backups()
            except Exception:
                return None

        def on_done(backups):
            if backups is None:
                self.app.show_error("读取失败", "无法读取启动项备份列表。")
                return
            if not backups:
                self.app.show_info("提示", "暂无可恢复的启动项备份。")
                return
            self._show_backup_dialog(backups)

        self.app.run_async(work, on_done=on_done)

    def _show_backup_dialog(self, backups):
        """自定义弹窗：列出备份供用户选择恢复"""
        T = self.app.THEME
        win = tk.Toplevel(self.app.root)
        win.title("恢复已删除的启动项")
        win.configure(bg=T.CARD_BG)
        win.geometry("620x360")
        try:
            win.transient(self.app.root)
            win.grab_set()
        except Exception:
            pass

        tk.Label(win, text="请选择要恢复的备份：", bg=T.CARD_BG, fg=T.TEXT,
                 font=("Microsoft YaHei", 10, "bold")).pack(anchor=tk.W, padx=16, pady=(14, 6))

        listbox = tk.Listbox(win, font=("Microsoft YaHei", 9), activestyle="none")
        listbox.pack(fill=tk.BOTH, expand=True, padx=16)
        for b in backups:
            listbox.insert(tk.END, "%s　|　%s　|　%s"
                           % (b.get("name", ""), b.get("type", ""), b.get("time", "")))

        def do_recover():
            sel = listbox.curselection()
            if not sel:
                self.app.show_warning("提示", "请先选择一个备份记录")
                return
            backup = backups[sel[0]]
            win.destroy()
            self._do_recover(backup)

        row = tk.Frame(win, bg=T.CARD_BG)
        row.pack(fill=tk.X, padx=16, pady=12)
        self.app.button(row, "恢复选中项", do_recover).pack(side=tk.RIGHT)
        self.app.button(row, "取消", win.destroy, kind="ghost").pack(side=tk.RIGHT, padx=8)

    def _do_recover(self, backup):
        """按备份 ID 执行恢复"""
        bid = backup.get("id", "")
        if not bid:
            self.app.show_error("恢复失败", "备份记录缺少 ID，无法恢复。")
            return

        def work(_progress_cb):
            try:
                return StartupEngine().recover_startup_item(bid)
            except Exception as exc:
                return False, str(exc)

        def on_done(result):
            ok, msg = result if result else (False, "恢复失败")
            if ok:
                self.app.show_info("恢复完成", msg)
                self.refresh_startup()
            else:
                self.app.show_error("恢复失败", msg)

        self.app.run_async(work, on_done=on_done)

    # ---------- 标签页 2：系统服务 ----------

    def _build_tab_services(self, parent):
        T = self.app.THEME

        tk.Label(parent,
                 text="修改系统服务有风险。请只对建议列为“可改为手动/可禁用”的服务操作，系统关键服务请勿改动。",
                 bg=T.BG, fg=T.WARNING, font=("Microsoft YaHei", 9, "bold"),
                 wraplength=900, justify=tk.LEFT).pack(anchor=tk.W, padx=12, pady=(10, 0))

        card, bar = self._make_toolbar(parent)
        self.app.button(bar, "刷新", self.refresh_services).pack(side=tk.LEFT)
        self.app.button(bar, "改为手动", lambda: self._apply_service("manual"),
                        kind="ghost").pack(side=tk.LEFT, padx=6)
        self.app.button(bar, "设为自动", lambda: self._apply_service("auto"),
                        kind="ghost").pack(side=tk.LEFT, padx=6)
        self.app.button(bar, "禁用", lambda: self._apply_service("disabled"),
                        kind="danger").pack(side=tk.LEFT, padx=6)

        self.service_bar, self.service_label = self._make_progress(card, T.CARD_BG)
        self.service_summary = self._make_summary(card, T.CARD_BG)

        holder = tk.Frame(parent, bg=T.BG)
        holder.pack(fill=tk.BOTH, expand=True, padx=10, pady=(0, 8))
        self.service_tree, _ = self.app.make_table(
            holder,
            ("name", "display", "state", "start", "suggest", "reason"),
            (180, 200, 90, 90, 110, 260),
            headings=("服务名", "显示名", "当前状态", "启动类型", "建议", "判定理由"),
            height=14)
        self.service_tree.tag_configure("ok", foreground=T.TEXT_LIGHT)
        self.service_tree.tag_configure("change", foreground=T.WARNING)

    def refresh_services(self):
        """刷新自动启动服务列表（后台线程执行）"""
        self.service_bar["value"] = 0
        self.service_label.config(text="正在读取服务列表...")
        self.service_summary.config(text="正在读取服务列表...")

        def work(progress_cb):
            try:
                return StartupEngine(progress_cb).scan_services(only_auto=True)
            except Exception:
                return None

        def on_progress(percent, msg):
            self.service_bar["value"] = percent
            self.service_label.config(text=msg)

        def on_done(services):
            if services is None:
                self.service_label.config(text="读取失败")
                self.app.show_error("读取失败", "无法获取服务列表，可能需要管理员权限。")
                return
            self._render_services(services)

        self.app.run_async(work, on_done=on_done, on_progress=on_progress)

    def _render_services(self, services):
        tree = self.service_tree
        for row in tree.get_children():
            tree.delete(row)

        self.service_rows = list(services)
        keep = 0
        for svc in self.service_rows:
            suggest = svc.get("suggest", "")
            if suggest == "建议保留":
                keep += 1
                tag = ("ok",)
            else:
                tag = ("change",)
            tree.insert("", tk.END, values=(
                svc.get("name", ""),
                svc.get("display", ""),
                svc.get("state", ""),
                svc.get("start_type", ""),
                suggest,
                svc.get("reason", ""),
            ), tags=tag)

        self.service_bar["value"] = 100
        self.service_label.config(text="读取完成")
        total = len(self.service_rows)
        self.service_summary.config(
            text="共 %d 个自动启动服务，其中建议保留 %d 个，可考虑调整 %d 个"
                 % (total, keep, total - keep))
        self.app.set_status("服务列表读取完成，共 %d 个" % total)

    def _apply_service(self, start_type):
        """修改选中服务的启动类型（修改前二次确认）"""
        svc = self._row_by_selection(self.service_tree, self.service_rows)
        if not svc:
            self.app.show_warning("提示", "请先在列表中选择一个服务")
            return
        cn = {"manual": "手动", "auto": "自动", "disabled": "禁用"}[start_type]
        if not self.app.ask_yes_no(
                "确认修改服务",
                "即将把服务“%s”的启动类型改为“%s”。\n\n"
                "警告：对系统服务做错误的改动可能导致系统或硬件功能异常，"
                "严重时可能造成设备无法正常工作或系统不稳定。\n"
                "请确认该服务不在系统关键服务之列，且您清楚改动的影响。\n\n"
                "是否继续？" % (svc.get("name", ""), cn)):
            return

        def work(_progress_cb):
            try:
                return StartupEngine().set_service_start_type(svc.get("name", ""), start_type)
            except Exception as exc:
                return False, str(exc)

        def on_done(result):
            ok, msg = result if result else (False, "修改失败")
            if ok:
                self.app.show_info("完成", msg)
                self.refresh_services()
            else:
                self.app.show_error("修改失败", msg)

        self.app.run_async(work, on_done=on_done)

    # ---------- 标签页 3：计划任务 ----------

    def _build_tab_tasks(self, parent):
        T = self.app.THEME
        card, bar = self._make_toolbar(parent)

        self.app.button(bar, "刷新", self.refresh_tasks).pack(side=tk.LEFT)
        self.app.button(bar, "启用", lambda: self._apply_task_enabled(True),
                        kind="ghost").pack(side=tk.LEFT, padx=6)
        self.app.button(bar, "禁用", lambda: self._apply_task_enabled(False),
                        kind="ghost").pack(side=tk.LEFT, padx=6)
        self.app.button(bar, "删除", self.delete_task, kind="danger").pack(
            side=tk.LEFT, padx=6)

        self.task_bar, self.task_label = self._make_progress(card, T.CARD_BG)
        self.task_summary = self._make_summary(card, T.CARD_BG)

        holder = tk.Frame(parent, bg=T.BG)
        holder.pack(fill=tk.BOTH, expand=True, padx=10, pady=(0, 8))
        self.task_tree, _ = self.app.make_table(
            holder,
            ("name", "path", "status", "schedule", "command", "ms"),
            (180, 220, 80, 140, 280, 80),
            headings=("任务名", "路径", "状态", "触发计划", "执行命令", "来源"),
            height=14)
        # 系统自带任务用灰色前景
        self.task_tree.tag_configure("ms", foreground=T.TEXT_LIGHT)

    def refresh_tasks(self):
        """刷新计划任务列表（后台线程执行）"""
        self.task_bar["value"] = 0
        self.task_label.config(text="正在读取计划任务...")
        self.task_summary.config(text="正在读取计划任务...")

        def work(progress_cb):
            try:
                return StartupEngine(progress_cb).scan_scheduled_tasks()
            except Exception:
                return None

        def on_progress(percent, msg):
            self.task_bar["value"] = percent
            self.task_label.config(text=msg)

        def on_done(tasks):
            if tasks is None:
                self.task_label.config(text="读取失败")
                self.app.show_error("读取失败", "无法获取计划任务列表。")
                return
            self._render_tasks(tasks)

        self.app.run_async(work, on_done=on_done, on_progress=on_progress)

    def _render_tasks(self, tasks):
        tree = self.task_tree
        for row in tree.get_children():
            tree.delete(row)

        self.task_rows = list(tasks)
        ms_count = 0
        for task in self.task_rows:
            is_ms = bool(task.get("is_microsoft"))
            if is_ms:
                ms_count += 1
            tree.insert("", tk.END, values=(
                task.get("name", ""),
                task.get("path", ""),
                task.get("status", ""),
                task.get("schedule", ""),
                task.get("command", ""),
                "系统自带" if is_ms else "第三方",
            ), tags=("ms",) if is_ms else ())

        self.task_bar["value"] = 100
        self.task_label.config(text="读取完成")
        total = len(self.task_rows)
        self.task_summary.config(
            text="共 %d 个计划任务，其中系统自带 %d 个，第三方 %d 个"
                 % (total, ms_count, total - ms_count))
        self.app.set_status("计划任务读取完成，共 %d 个" % total)

    def _apply_task_enabled(self, enabled):
        """启用/禁用选中的计划任务"""
        task = self._row_by_selection(self.task_tree, self.task_rows)
        if not task:
            self.app.show_warning("提示", "请先在列表中选择一个计划任务")
            return
        action = "启用" if enabled else "禁用"

        def work(_progress_cb):
            try:
                return StartupEngine().set_scheduled_task_enabled(
                    task.get("name", ""), enabled)
            except Exception as exc:
                return False, str(exc)

        def on_done(result):
            ok, msg = result if result else (False, "操作失败")
            if ok:
                self.app.show_info("完成", msg)
                self.refresh_tasks()
            else:
                self.app.show_error("%s失败" % action, msg)

        self.app.run_async(work, on_done=on_done)

    def delete_task(self):
        """删除选中的计划任务（系统自带任务额外警告）"""
        task = self._row_by_selection(self.task_tree, self.task_rows)
        if not task:
            self.app.show_warning("提示", "请先在列表中选择一个计划任务")
            return

        if task.get("is_microsoft"):
            if not self.app.ask_yes_no(
                    "警告：系统自带任务",
                    "该任务由 Windows 系统自带（路径：%s）。\n\n"
                    "删除后可能导致相关系统功能（如维护、更新、安全扫描）异常，"
                    "并且系统更新时可能自动重建该任务。\n\n确定仍要删除吗？"
                    % task.get("path", "")):
                return
        else:
            if not self.app.ask_yes_no(
                    "确认删除",
                    "即将删除计划任务“%s”。删除后不可恢复，是否继续？"
                    % task.get("name", "")):
                return

        def work(_progress_cb):
            try:
                return StartupEngine().delete_scheduled_task(task.get("name", ""))
            except Exception as exc:
                return False, str(exc)

        def on_done(result):
            ok, msg = result if result else (False, "删除失败")
            if ok:
                self.app.show_info("删除完成", msg)
                self.refresh_tasks()
            else:
                self.app.show_error("删除失败", msg)

        self.app.run_async(work, on_done=on_done)

    # ---------- 标签页 4：开机耗时 ----------

    def _build_tab_boot(self, parent):
        T = self.app.THEME

        card = tk.Frame(parent, bg=T.CARD_BG, bd=0,
                        highlightbackground=T.BORDER, highlightthickness=1)
        card.pack(fill=tk.X, padx=10, pady=(10, 6))
        inner = tk.Frame(card, bg=T.CARD_BG)
        inner.pack(fill=tk.X, padx=16, pady=16)

        self.boot_avg = tk.Label(inner, text="尚未读取", bg=T.CARD_BG, fg=T.PRIMARY,
                                 font=("Microsoft YaHei", 26, "bold"),
                                 wraplength=860, justify=tk.LEFT)
        self.boot_avg.pack(anchor=tk.W)

        self.boot_detail = tk.Label(inner, text="点击「刷新」读取系统最近的开机耗时记录。",
                                    bg=T.CARD_BG, fg=T.TEXT_LIGHT,
                                    font=("Microsoft YaHei", 9),
                                    wraplength=860, justify=tk.LEFT)
        self.boot_detail.pack(anchor=tk.W, pady=(4, 10))

        self.boot_bar, self.boot_label = self._make_progress(card, T.CARD_BG)
        btn_row = tk.Frame(card, bg=T.CARD_BG)
        btn_row.pack(fill=tk.X, padx=12, pady=(0, 12))
        self.app.button(btn_row, "刷新", self.refresh_boot).pack(side=tk.LEFT)

        tk.Label(parent, text="最近开机耗时记录", bg=T.BG, fg=T.TEXT,
                 font=("Microsoft YaHei", 10, "bold")).pack(anchor=tk.W, padx=12, pady=(6, 4))
        holder = tk.Frame(parent, bg=T.BG)
        holder.pack(fill=tk.BOTH, expand=True, padx=10, pady=(0, 8))
        self.boot_tree, _ = self.app.make_table(
            holder,
            ("time", "sec"),
            (280, 180),
            headings=("记录时间", "开机耗时（秒）"),
            height=10)

    def refresh_boot(self):
        """读取系统开机耗时记录（后台线程执行）"""
        self.boot_bar["value"] = 0
        self.boot_label.config(text="正在读取开机耗时记录...")

        def work(progress_cb):
            try:
                return StartupEngine(progress_cb).analyze_boot_time()
            except Exception:
                return None

        def on_progress(percent, msg):
            self.boot_bar["value"] = percent
            self.boot_label.config(text=msg)

        def on_done(result):
            if result is None:
                self.boot_label.config(text="读取失败")
                self.app.show_error("读取失败", "无法读取开机耗时信息。")
                return
            self._render_boot(result)

        self.app.run_async(work, on_done=on_done, on_progress=on_progress)

    def _render_boot(self, result):
        T = self.app.THEME
        tree = self.boot_tree
        for row in tree.get_children():
            tree.delete(row)

        self.boot_records = list(result.get("records") or [])
        self.boot_bar["value"] = 100
        self.boot_label.config(text="读取完成")

        if not result.get("success"):
            # 取不到数据时如实展示引擎返回的说明，绝不编造数字
            self.boot_avg.config(text="暂无开机耗时数据", fg=T.TEXT_LIGHT)
            self.boot_detail.config(text=result.get("message") or "未获取到开机耗时记录。")
            self.app.set_status("未获取到开机耗时记录")
            return

        avg_ms = result.get("avg_ms", 0) or 0
        self.boot_avg.config(text="平均 %.1f 秒" % (avg_ms / 1000.0), fg=T.PRIMARY)
        self.boot_detail.config(text=result.get("message") or "")

        for rec in self.boot_records:
            boot_ms = rec.get("boot_ms", 0) or 0
            tree.insert("", tk.END, values=(
                rec.get("time", ""),
                "%.1f" % (boot_ms / 1000.0),
            ))
        self.app.set_status("已读取 %d 条开机耗时记录，平均 %.1f 秒"
                            % (len(self.boot_records), avg_ms / 1000.0))

    # ---------- 标签页 5：自动清理计划 ----------

    def _build_tab_auto(self, parent):
        T = self.app.THEME

        card = tk.Frame(parent, bg=T.CARD_BG, bd=0,
                        highlightbackground=T.BORDER, highlightthickness=1)
        card.pack(fill=tk.X, padx=10, pady=(10, 6))
        inner = tk.Frame(card, bg=T.CARD_BG)
        inner.pack(fill=tk.X, padx=16, pady=16)

        tk.Label(inner, text="定时自动清理系统垃圾", bg=T.CARD_BG, fg=T.TEXT,
                 font=("Microsoft YaHei", 12, "bold")).pack(anchor=tk.W)
        tk.Label(inner,
                 text="解决“清理完垃圾又长回来”的痛点：创建一个计划任务，"
                      "在指定的时间自动清理系统垃圾，无需手动操作。",
                 bg=T.CARD_BG, fg=T.TEXT_LIGHT, font=("Microsoft YaHei", 9),
                 wraplength=860, justify=tk.LEFT).pack(anchor=tk.W, pady=(2, 14))

        # 频率 + 时间
        row1 = tk.Frame(inner, bg=T.CARD_BG)
        row1.pack(fill=tk.X, pady=(0, 10))
        tk.Label(row1, text="执行频率：", bg=T.CARD_BG, fg=T.TEXT,
                 font=("Microsoft YaHei", 9)).pack(side=tk.LEFT)
        self.auto_freq_var = tk.StringVar(value="每周")
        ttk.Combobox(row1, textvariable=self.auto_freq_var, width=6, state="readonly",
                     values=("每天", "每周"), font=("Microsoft YaHei", 9)).pack(
            side=tk.LEFT, padx=(0, 18))
        tk.Label(row1, text="执行时间（HH:MM）：", bg=T.CARD_BG, fg=T.TEXT,
                 font=("Microsoft YaHei", 9)).pack(side=tk.LEFT)
        self.auto_time_var = tk.StringVar(value="12:00")
        tk.Entry(row1, textvariable=self.auto_time_var, width=8,
                 font=("Microsoft YaHei", 9)).pack(side=tk.LEFT)

        # 脚本路径
        row2 = tk.Frame(inner, bg=T.CARD_BG)
        row2.pack(fill=tk.X, pady=(0, 12))
        tk.Label(row2, text="清理脚本：", bg=T.CARD_BG, fg=T.TEXT,
                 font=("Microsoft YaHei", 9)).pack(side=tk.LEFT)
        # 打包为 exe 后没有独立的 .py 文件，改用主程序自带的 --auto-clean 参数
        if getattr(sys, "frozen", False):
            default_script = f'"{sys.executable}" --auto-clean'
        else:
            default_script = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                          "auto_clean_runner.py")
        self.auto_script_var = tk.StringVar(value=default_script)
        tk.Entry(row2, textvariable=self.auto_script_var, width=64,
                 font=("Microsoft YaHei", 9)).pack(side=tk.LEFT)

        # 按钮
        row3 = tk.Frame(inner, bg=T.CARD_BG)
        row3.pack(fill=tk.X, pady=(0, 10))
        self.app.button(row3, "创建计划任务", self.create_auto_task).pack(side=tk.LEFT)
        self.app.button(row3, "删除计划任务", self.delete_auto_task,
                        kind="danger").pack(side=tk.LEFT, padx=8)
        self.app.button(row3, "查询状态", self.query_auto_task,
                        kind="ghost").pack(side=tk.LEFT)

        self.auto_status = tk.Label(inner, text="尚未查询状态", bg=T.CARD_BG,
                                    fg=T.TEXT_LIGHT, font=("Microsoft YaHei", 9),
                                    wraplength=860, justify=tk.LEFT)
        self.auto_status.pack(anchor=tk.W)

        tk.Label(parent,
                 text="说明：计划任务名称固定为「C盘清理大师-自动清理」；"
                      "创建和删除计划任务都需要管理员权限，请以管理员身份运行本程序。\n"
                      "自动清理默认只清系统垃圾，不会删除微信/QQ 接收的文件；"
                      "如确需清理社交缓存，请在脚本参数后追加 --include-social（只清 30 天前的文件）。",
                 bg=T.BG, fg=T.TEXT_LIGHT, font=("Microsoft YaHei", 9),
                 wraplength=900, justify=tk.LEFT).pack(anchor=tk.W, padx=12, pady=(0, 8))

    def create_auto_task(self):
        """创建定时自动清理计划任务"""
        script = self.auto_script_var.get().strip()
        when = "daily" if self.auto_freq_var.get() == "每天" else "weekly"
        time_str = self.auto_time_var.get().strip()

        if not script:
            self.app.show_warning("提示", "请填写清理脚本的路径。")
            return
        if not os.path.isfile(script):
            self.app.show_warning("提示", "脚本文件不存在：%s" % script)
            return

        self.auto_status.config(text="正在创建计划任务...", fg=self.app.THEME.TEXT_LIGHT)

        def work(_progress_cb):
            try:
                return StartupEngine().create_auto_clean_task(
                    script, when=when, time_str=time_str)
            except Exception as exc:
                return False, str(exc)

        def on_done(result):
            ok, msg = result if result else (False, "创建失败")
            if ok:
                self.auto_status.config(text=msg, fg=self.app.THEME.ACCENT)
                self.app.show_info("创建完成", msg)
            else:
                self.auto_status.config(text=msg, fg=self.app.THEME.DANGER)
                self.app.show_error("创建失败", msg)

        self.app.run_async(work, on_done=on_done)

    def delete_auto_task(self):
        """删除定时自动清理计划任务"""
        if not self.app.ask_yes_no(
                "确认删除",
                "即将删除自动清理计划任务「C盘清理大师-自动清理」。\n"
                "删除后系统将不再自动清理，是否继续？"):
            return

        self.auto_status.config(text="正在删除计划任务...", fg=self.app.THEME.TEXT_LIGHT)

        def work(_progress_cb):
            try:
                return StartupEngine().delete_auto_clean_task()
            except Exception as exc:
                return False, str(exc)

        def on_done(result):
            ok, msg = result if result else (False, "删除失败")
            if ok:
                self.auto_status.config(text=msg, fg=self.app.THEME.ACCENT)
                self.app.show_info("删除完成", msg)
            else:
                self.auto_status.config(text=msg, fg=self.app.THEME.DANGER)
                self.app.show_error("删除失败", msg)

        self.app.run_async(work, on_done=on_done)

    def query_auto_task(self):
        """查询自动清理计划任务的状态"""
        self.auto_status.config(text="正在查询状态...", fg=self.app.THEME.TEXT_LIGHT)

        def work(_progress_cb):
            try:
                return StartupEngine().get_auto_clean_task_status()
            except Exception:
                return None

        def on_done(status):
            if status is None:
                self.auto_status.config(text="查询失败", fg=self.app.THEME.DANGER)
                self.app.show_error("查询失败", "无法获取自动清理计划任务的状态。")
                return
            if status.get("exists"):
                self.auto_status.config(
                    text="计划任务已创建：状态 %s，下次运行 %s"
                         % (status.get("status", "未知") or "未知",
                            status.get("next_run", "未计划") or "未计划"),
                    fg=self.app.THEME.ACCENT)
            else:
                self.auto_status.config(text="自动清理计划任务尚未创建",
                                        fg=self.app.THEME.TEXT_LIGHT)

        self.app.run_async(work, on_done=on_done)


def build(app, parent):
    """页面入口：主程序通过此函数挂载页面"""
    page = StartupPage(app, parent)
    page.build()
    return page
