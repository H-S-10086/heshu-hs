# -*- coding: utf-8 -*-
"""
弹窗拦截页面（page_popup）

三个标签页：
  1) 监控与规则    —— 选择监控模式、启停监控、维护拦截规则
  2) 扫描当前窗口  —— 枚举当前所有可见窗口，标记命中规则的窗口
  3) 拦截日志      —— 查看 / 清空命中记录

设计约束：
  - 仅使用标准库 + tkinter（含 simpledialog）+ popup_blocker.PopupBlocker，
    不引入任何第三方依赖。
  - 本模块被 import 时不产生任何副作用（仅定义类与函数）。
  - 所有耗时操作（枚举窗口、手动检查一次、启停监控）统一通过 app.run_async
    放到后台线程执行。
  - 所有引擎调用均以 try/except 包裹，异常通过 app.show_error 提示。
  - 整个页面只创建一个 PopupBlocker 实例（self.blocker）。
  - 引擎的 log_callback / state_callback 在后台线程被调用，
    因此更新界面一律通过 app.root.after(0, ...) 回到主线程。
"""

import tkinter as tk
from tkinter import ttk, simpledialog

from popup_blocker import PopupBlocker


_FONT = ("Microsoft YaHei", 9)

# 匹配方式的显示映射
MATCH_TEXT = {"title": "窗口标题", "class": "窗口类名", "both": "两者皆可"}


class PopupPage:
    """弹窗拦截页面控制器"""

    def __init__(self, app, parent):
        self.app = app
        self.parent = parent
        self.TH = app.THEME

        # 表格行与数据 dict 一一对应
        self.rule_rows = []      # 规则 dict 列表
        self.win_rows = []       # 窗口 dict 列表
        self.log_rows = []       # 日志 dict 列表

        # 引擎（回调在后台线程触发，内部用 after 回主线程）
        self.blocker = PopupBlocker(log_callback=self._on_log,
                                    state_callback=self._on_state)

        self._busy = False

        self._build()

    # ==================== 界面搭建 ====================

    def _build(self):
        self.app.section_title(self.parent, "弹窗拦截",
                               "拦截软件广告弹窗，让电脑安静下来。")

        nb = ttk.Notebook(self.parent)
        nb.pack(fill=tk.BOTH, expand=True, padx=8, pady=(0, 8))
        self.notebook = nb

        tab_monitor = tk.Frame(nb, bg=self.TH.BG)
        tab_scan = tk.Frame(nb, bg=self.TH.BG)
        tab_logs = tk.Frame(nb, bg=self.TH.BG)
        nb.add(tab_monitor, text="  监控与规则  ")
        nb.add(tab_scan, text="  扫描当前窗口  ")
        nb.add(tab_logs, text="  拦截日志  ")

        self._build_tab_monitor(tab_monitor)
        self._build_tab_scan(tab_scan)
        self._build_tab_logs(tab_logs)

        # 初始化数据展示
        self._refresh_rules()
        self._refresh_logs()

    # ---------- 标签页 1：监控与规则 ----------

    def _build_tab_monitor(self, parent):
        T = self.TH

        card = self.app.card(parent)
        card.pack(fill=tk.X, padx=10, pady=(10, 6))

        tk.Label(card, text="监控模式：", bg=T.CARD_BG, fg=T.TEXT,
                 font=("Microsoft YaHei", 10, "bold")).pack(anchor=tk.W, padx=12,
                                                             pady=(10, 2))
        self.mode_var = tk.StringVar(value="record")
        tk.Radiobutton(card, text="仅记录（安全，不关闭任何窗口）", variable=self.mode_var,
                       value="record", bg=T.CARD_BG, fg=T.TEXT, font=_FONT,
                       activebackground=T.CARD_BG, selectcolor=T.CARD_BG).pack(
            anchor=tk.W, padx=24)
        tk.Radiobutton(card, text="自动关闭（命中规则就关闭窗口）", variable=self.mode_var,
                       value="close", bg=T.CARD_BG, fg=T.TEXT, font=_FONT,
                       activebackground=T.CARD_BG, selectcolor=T.CARD_BG).pack(
            anchor=tk.W, padx=24)

        row = tk.Frame(card, bg=T.CARD_BG)
        row.pack(fill=tk.X, padx=12, pady=(8, 8))
        self.app.button(row, "启动监控", self._start_monitor, kind="primary").pack(side=tk.LEFT)
        self.app.button(row, "停止监控", self._stop_monitor, kind="ghost").pack(
            side=tk.LEFT, padx=(6, 0))
        tk.Label(row, text="状态：", bg=T.CARD_BG, fg=T.TEXT,
                 font=_FONT).pack(side=tk.LEFT, padx=(16, 2))
        self.status_label = tk.Label(row, text="已停止", bg=T.CARD_BG,
                                     fg=T.TEXT_LIGHT, font=("Microsoft YaHei", 9, "bold"))
        self.status_label.pack(side=tk.LEFT)

        # ---- 规则表格 ----
        rule_card = self.app.card(parent)
        rule_card.pack(fill=tk.BOTH, expand=True, padx=10, pady=(0, 6))
        tk.Label(rule_card, text="匹配规则", bg=T.CARD_BG, fg=T.TEXT,
                 font=("Microsoft YaHei", 10, "bold")).pack(anchor=tk.W, padx=12,
                                                            pady=(8, 2))
        self.rule_tree, _ = self.app.make_table(
            rule_card,
            ("kw", "match", "state", "note"),
            (220, 120, 80, 320),
            headings=("关键词", "匹配方式", "状态", "备注"),
            height=8)

        rule_btn = tk.Frame(rule_card, bg=T.CARD_BG)
        rule_btn.pack(fill=tk.X, padx=12, pady=(4, 4))
        self.app.button(rule_btn, "添加规则", self._add_rule, kind="primary").pack(side=tk.LEFT)
        self.app.button(rule_btn, "删除规则", self._remove_rule, kind="danger").pack(
            side=tk.LEFT, padx=(6, 0))
        self.app.button(rule_btn, "启用/禁用规则", self._toggle_rule, kind="ghost").pack(
            side=tk.LEFT, padx=(6, 0))
        self.app.button(rule_btn, "保存规则", self._save_rules, kind="ghost").pack(
            side=tk.LEFT, padx=(6, 0))

        tk.Label(parent,
                 text="默认只提供示例规则且默认关闭。请先在「扫描当前窗口」里确认哪些窗口是广告，"
                      "再添加为规则，避免误关正常程序。",
                 bg=T.BG, fg=T.WARNING, font=_FONT, wraplength=900,
                 justify=tk.LEFT).pack(anchor=tk.W, padx=10, pady=(0, 6))

    # ---------- 标签页 2：扫描当前窗口 ----------

    def _build_tab_scan(self, parent):
        T = self.TH

        card = self.app.card(parent)
        card.pack(fill=tk.X, padx=10, pady=(10, 6))

        tk.Label(card, text="枚举当前所有可见的顶层窗口，用于确认哪些是广告弹窗。",
                 bg=T.CARD_BG, fg=T.TEXT_LIGHT, font=_FONT, wraplength=900,
                 justify=tk.LEFT).pack(anchor=tk.W, padx=12, pady=(10, 6))

        row = tk.Frame(card, bg=T.CARD_BG)
        row.pack(fill=tk.X, padx=12, pady=(0, 8))
        self.app.button(row, "扫描当前所有窗口", self._scan_windows, kind="primary").pack(
            side=tk.LEFT)
        self.app.button(row, "把选中窗口加入规则", self._add_selected_window_rule,
                        kind="ghost").pack(side=tk.LEFT, padx=(6, 0))
        self.app.button(row, "关闭选中窗口", self._close_selected_window,
                        kind="danger").pack(side=tk.LEFT, padx=(6, 0))

        self.win_tree, _ = self.app.make_table(
            parent,
            ("title", "cls", "proc", "pid", "hit"),
            (320, 200, 160, 80, 200),
            headings=("窗口标题", "窗口类名", "进程", "PID", "是否命中规则"),
            height=14)
        try:
            self.win_tree.tag_configure("hit", foreground=self.TH.DANGER)
        except Exception:
            pass

        tk.Label(parent,
                 text="提示：关闭窗口相当于点击该窗口的关闭按钮，不会强制结束进程。",
                 bg=T.BG, fg=T.WARNING, font=_FONT, wraplength=900,
                 justify=tk.LEFT).pack(anchor=tk.W, padx=10, pady=(0, 6))

    # ---------- 标签页 3：拦截日志 ----------

    def _build_tab_logs(self, parent):
        T = self.TH

        card = self.app.card(parent)
        card.pack(fill=tk.X, padx=10, pady=(10, 6))

        row = tk.Frame(card, bg=T.CARD_BG)
        row.pack(fill=tk.X, padx=12, pady=(10, 6))
        self.app.button(row, "手动检查一次", self._manual_check, kind="primary").pack(
            side=tk.LEFT)
        self.app.button(row, "刷新", self._refresh_logs, kind="ghost").pack(
            side=tk.LEFT, padx=(6, 0))
        self.app.button(row, "清空日志", self._clear_logs, kind="ghost").pack(
            side=tk.LEFT, padx=(6, 0))

        self.log_summary = tk.Label(card, text="尚未检查", bg=T.CARD_BG, fg=T.PRIMARY,
                                    font=("Microsoft YaHei", 10, "bold"), anchor=tk.W)
        self.log_summary.pack(anchor=tk.W, fill=tk.X, padx=12, pady=(0, 8))

        self.log_tree, _ = self.app.make_table(
            parent,
            ("time", "title", "cls", "proc", "rule", "action"),
            (80, 300, 180, 150, 140, 100),
            headings=("时间", "窗口标题", "窗口类名", "进程", "命中规则", "动作"),
            height=14)

    # ==================== 通用小工具 ====================

    def _error(self, title, msg):
        try:
            self.app.show_error(title, msg)
        except Exception:
            pass

    def _warn(self, title, msg):
        try:
            self.app.show_warning(title, msg)
        except Exception:
            pass

    def _info(self, title, msg):
        try:
            self.app.show_info(title, msg)
        except Exception:
            pass

    def _confirm(self, title, msg):
        try:
            return bool(self.app.ask_yes_no(title, msg))
        except Exception:
            return False

    def _set_status(self, text):
        try:
            self.app.set_status(text)
        except Exception:
            pass

    @staticmethod
    def _as_pair(result):
        if isinstance(result, tuple) and len(result) == 2:
            return result[0], result[1]
        return None, None

    # ==================== 引擎回调（后台线程 → after 回主线程） ====================

    def _on_state(self, msg):
        """状态变化回调：由引擎在后台线程调用"""
        try:
            self.app.root.after(0, lambda m=msg: self._apply_state(m))
        except Exception:
            pass

    def _apply_state(self, msg):
        try:
            running = self.blocker.is_running()
        except Exception:
            running = False
        try:
            self.status_label.config(text="运行中" if running else "已停止",
                                     fg=self.TH.ACCENT if running else self.TH.TEXT_LIGHT)
        except Exception:
            pass
        self._set_status(msg or ("弹窗监控运行中" if running else "弹窗监控已停止"))

    def _on_log(self, entry):
        """新拦截记录回调：由引擎在后台线程调用"""
        try:
            self.app.root.after(0, lambda e=entry: self._append_log_row(e))
        except Exception:
            pass

    def _append_log_row(self, entry):
        try:
            self.log_rows.append(entry)
            self.log_tree.insert("", tk.END, values=self._log_values(entry))
            # 控制表格行数，避免长期运行后过大
            children = self.log_tree.get_children()
            if len(children) > 500:
                for iid in children[:len(children) - 500]:
                    self.log_tree.delete(iid)
        except Exception:
            pass

    @staticmethod
    def _log_values(entry):
        return (entry.get("time", ""), entry.get("title", ""),
                entry.get("class", ""), entry.get("process", ""),
                entry.get("rule", ""), entry.get("action", ""))

    # ==================== 规则维护 ====================

    def _refresh_rules(self):
        try:
            rules = self.blocker.get_rules()
        except Exception as e:
            rules = []
            self._error("读取规则失败", str(e))

        self.rule_rows = []
        try:
            for iid in self.rule_tree.get_children():
                self.rule_tree.delete(iid)
            for r in (rules or []):
                iid = self.rule_tree.insert("", tk.END, values=(
                    r.get("keyword", ""),
                    MATCH_TEXT.get(r.get("match", "title"), r.get("match", "title")),
                    "启用" if r.get("enabled") else "禁用",
                    r.get("note", ""),
                ))
                self.rule_rows.append(r)
        except Exception:
            pass

    def _selected_rule(self):
        """返回当前选中行对应的规则 dict，无选中返回 None"""
        try:
            sel = self.rule_tree.selection()
            if not sel:
                return None
            idx = self.rule_tree.index(sel[0])
            if 0 <= idx < len(self.rule_rows):
                return self.rule_rows[idx]
        except Exception:
            pass
        return None

    def _add_rule(self):
        try:
            keyword = simpledialog.askstring(
                "添加规则",
                "请输入要拦截的关键词（窗口标题或类名包含该关键词时命中）：",
                parent=self.app.root)
        except Exception as e:
            self._error("添加规则失败", str(e))
            return
        if keyword is None:
            return
        keyword = keyword.strip()
        if not keyword:
            self._warn("提示", "关键词不能为空。")
            return

        try:
            match = simpledialog.askstring(
                "匹配方式",
                "请输入匹配方式：title（窗口标题）/ class（窗口类名）/ both（两者皆可）",
                initialvalue="title", parent=self.app.root)
        except Exception:
            match = "title"
        if match is None:
            match = "title"
        match = (match or "").strip().lower()
        if match not in ("title", "class", "both"):
            match = "title"

        try:
            ok, msg = self.blocker.add_rule(keyword, match, "用户添加")
        except Exception as e:
            self._error("添加规则失败", str(e))
            return
        if ok:
            self._refresh_rules()
            self._set_status(msg)
        else:
            self._warn("未能添加规则", msg or "添加规则失败。")

    def _remove_rule(self):
        rule = self._selected_rule()
        if not rule:
            self._warn("提示", "请先在规则表格中选择一行。")
            return
        keyword = rule.get("keyword", "")
        if not self._confirm("确认删除", f"确定要删除规则「{keyword}」吗？"):
            return
        try:
            ok, msg = self.blocker.remove_rule(keyword)
        except Exception as e:
            self._error("删除规则失败", str(e))
            return
        if ok:
            self._refresh_rules()
            self._set_status(msg)
        else:
            self._warn("未能删除规则", msg or "删除规则失败。")

    def _toggle_rule(self):
        rule = self._selected_rule()
        if not rule:
            self._warn("提示", "请先在规则表格中选择一行。")
            return
        keyword = rule.get("keyword", "")
        try:
            ok, msg = self.blocker.set_rule_enabled(keyword, not rule.get("enabled"))
        except Exception as e:
            self._error("更新规则失败", str(e))
            return
        if ok:
            self._refresh_rules()
            self._set_status(msg)
        else:
            self._warn("未能更新规则", msg or "更新规则状态失败。")

    def _save_rules(self):
        try:
            ok, msg = self.blocker.save_rules()
        except Exception as e:
            self._error("保存规则失败", str(e))
            return
        if ok:
            self._set_status(msg)
            self._info("保存成功", msg or "规则已保存。")
        else:
            self._error("保存失败", msg or "规则保存失败。")

    # ==================== 监控启停 ====================

    def _start_monitor(self):
        if self._busy:
            return
        mode = self.mode_var.get()

        def work(_progress_cb):
            try:
                return self.blocker.start(mode=mode)
            except Exception as e:
                return (False, str(e))

        def on_done(result):
            ok, msg = self._as_pair(result)
            if ok:
                self._set_status(msg)
            else:
                self._warn("未能启动监控", msg or "启动监控失败。")
            self._apply_state(msg)

        self.app.run_async(work, on_done=on_done)

    def _stop_monitor(self):
        def work(_progress_cb):
            try:
                return self.blocker.stop()
            except Exception as e:
                return (False, str(e))

        def on_done(result):
            ok, msg = self._as_pair(result)
            if not ok:
                self._warn("提示", msg or "监控未在运行。")
            self._apply_state(msg)

        self.app.run_async(work, on_done=on_done)

    # ==================== 扫描当前窗口 ====================

    def _scan_windows(self):
        self._set_status("正在扫描当前窗口...")

        def work(_progress_cb):
            try:
                return self.blocker.enum_windows()
            except Exception as e:
                return {"_error": str(e)}

        def on_done(result):
            if isinstance(result, dict) and result.get("_error"):
                self._error("扫描失败", result["_error"])
                return
            self.win_rows = list(result or [])
            self._render_window_rows()
            self._set_status(f"扫描完成，发现 {len(self.win_rows)} 个窗口")

        self.app.run_async(work, on_done=on_done)

    def _render_window_rows(self):
        """渲染窗口表格，命中规则的行用红色标记"""
        try:
            for iid in self.win_tree.get_children():
                self.win_tree.delete(iid)
            for w in self.win_rows:
                try:
                    hit = self.blocker.match_window(w)
                except Exception:
                    hit = None
                hit_text = hit.get("keyword") if hit else "—"
                tags = ("hit",) if hit else ()
                self.win_tree.insert("", tk.END, values=(
                    w.get("title", ""),
                    w.get("class", ""),
                    w.get("process", ""),
                    w.get("pid", ""),
                    hit_text,
                ), tags=tags)
        except Exception:
            pass

    def _selected_window(self):
        try:
            sel = self.win_tree.selection()
            if not sel:
                return None
            idx = self.win_tree.index(sel[0])
            if 0 <= idx < len(self.win_rows):
                return self.win_rows[idx]
        except Exception:
            pass
        return None

    def _add_selected_window_rule(self):
        win = self._selected_window()
        if not win:
            self._warn("提示", "请先在窗口表格中选择一行。")
            return
        title = (win.get("title") or "").strip()
        if not title:
            self._warn("提示", "该窗口没有可用作关键词的标题。")
            return
        try:
            ok, msg = self.blocker.add_rule(title, "title", "从窗口列表添加")
        except Exception as e:
            self._error("添加规则失败", str(e))
            return
        if ok:
            self._refresh_rules()
            self._render_window_rows()
            self._set_status(msg)
        else:
            self._warn("未能添加规则", msg or "添加规则失败。")

    def _close_selected_window(self):
        win = self._selected_window()
        if not win:
            self._warn("提示", "请先在窗口表格中选择一行。")
            return
        hwnd = win.get("hwnd")
        if hwnd is None:
            self._warn("提示", "该行没有可用的窗口句柄。")
            return
        title = win.get("title", "")
        if not self._confirm(
                "确认关闭",
                f"将向窗口「{title}」发送关闭指令（相当于点击其关闭按钮）。是否继续？"):
            return
        try:
            ok = self.blocker.close_window(hwnd)
        except Exception as e:
            self._error("关闭失败", str(e))
            return
        if ok:
            self._set_status(f"已发送关闭指令：{title}")
            self._scan_windows()   # 重新扫描刷新列表
        else:
            self._warn("关闭失败", f"未能关闭窗口：{title}")

    # ==================== 拦截日志 ====================

    def _manual_check(self):
        self._set_status("正在检查当前窗口...")

        def work(_progress_cb):
            try:
                return self.blocker.scan_once()
            except Exception as e:
                return {"_error": str(e)}

        def on_done(result):
            if not isinstance(result, dict):
                self._error("检查失败", "引擎未返回有效结果。")
                return
            if result.get("_error"):
                self._error("检查失败", result["_error"])
                return
            scanned = result.get("scanned", 0)
            matched = result.get("matched", []) or []
            msg = f"检查了 {scanned} 个窗口，命中 {len(matched)} 个"
            self.log_summary.config(text=msg)
            self._set_status(msg)
            self._refresh_logs()

        self.app.run_async(work, on_done=on_done)

    def _refresh_logs(self):
        try:
            logs = self.blocker.get_logs(limit=100)
        except Exception:
            logs = []
        self.log_rows = list(logs or [])
        try:
            for iid in self.log_tree.get_children():
                self.log_tree.delete(iid)
            for entry in self.log_rows:
                self.log_tree.insert("", tk.END, values=self._log_values(entry))
        except Exception:
            pass

    def _clear_logs(self):
        try:
            self.blocker.clear_logs()
        except Exception as e:
            self._error("清空日志失败", str(e))
            return
        self.log_rows = []
        try:
            for iid in self.log_tree.get_children():
                self.log_tree.delete(iid)
        except Exception:
            pass
        self.log_summary.config(text="日志已清空")
        self._set_status("已清空拦截日志")


def build(app, parent):
    """页面入口：主程序以 build(app, parent) 调用"""
    return PopupPage(app, parent)
