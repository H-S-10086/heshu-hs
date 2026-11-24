# -*- coding: utf-8 -*-
"""
注册表清理页面（page_registry）

对标 CCleaner / Wise Registry Cleaner 的注册表清理能力：
  1) 操作工具条：扫描、清理选中项、备份 / 还原注册表、创建系统还原点、打开备份目录、进度条
  2) 扫描结果表格：勾选无效注册表项后安全清理（清理前自动备份）
  3) 隐私开关：一键收紧 Windows 遥测 / 广告 ID / 活动历史 / 开始菜单推荐 / 错误报告 / Copilot 等
  4) 注册表体积信息：展示各 hive 文件体积

设计约束：
  - 仅使用标准库 + tkinter，以及 scanner.format_size、registry_cleaner.RegistryEngine，
    不引入任何第三方依赖。
  - 本模块 import 时不产生任何副作用（不创建窗口、不执行扫描）。
  - 所有耗时操作统一走 app.run_async 放到后台线程执行。
  - 所有引擎调用均以 try/except 包裹，异常通过 app.show_error 提示，绝不向界面冒泡。
"""

import os
import tkinter as tk
from tkinter import filedialog

from scanner import format_size
from registry_cleaner import RegistryEngine


def _backup_dir():
    """注册表备份目录：用户目录\\CleanMasterBackup\\registry"""
    return os.path.join(os.path.expanduser("~"), "CleanMasterBackup", "registry")


def _pack_if_needed(widget, **kw):
    """
    若控件尚未被几何管理器接管，则按给定参数 pack。
    兼容 app.card / app.make_table 可能已经代为 pack 的情况，避免重复布局。
    """
    try:
        if not widget.winfo_manager():
            widget.pack(**kw)
    except Exception:
        pass


class RegistryPage:
    """注册表清理页面（由 build(app, parent) 实例化）"""

    # 结果表格列定义
    TABLE_COLUMNS = ("sel", "category", "source", "path", "size", "desc")
    TABLE_WIDTHS = {"sel": 50, "category": 90, "source": 240, "path": 400, "size": 90, "desc": 380}
    TABLE_HEADINGS = {
        "sel": "选择",
        "category": "分类",
        "source": "垃圾来源备注",
        "path": "路径",
        "size": "估算大小",
        "desc": "说明",
    }

    def __init__(self, app, parent):
        self.app = app
        self.parent = parent
        self.TH = app.THEME

        # 结果表格
        self.tree = None
        self._rows = {}          # tree item id -> ScanResult
        self.summary_label = None
        self.select_all_var = None
        self.clean_btn = None

        # 进度条
        self.progress = None
        self.progress_label = None

        # 隐私开关
        self._privacy_frame = None
        self._privacy_vars = []  # [(tk.BooleanVar, switch_dict), ...]

        # 注册表体积信息
        self._info_label = None

        # 运行态
        self._busy = False
        self._last_error = ""
        self._pending_clean = []
        self.scan_results = []

        self._build()

    # ==================== 界面构建 ====================

    def _build(self):
        # 页面标题区
        self.app.section_title(
            self.parent, "注册表清理",
            "扫描无效注册表项并安全清理，清理前自动备份。",
        )
        self._build_toolbar()
        self._build_results_card()
        self._build_privacy_card()
        self._build_info_card()
        # 异步加载隐私开关状态与注册表体积信息
        self._load_privacy()
        self._load_registry_info()

    def _build_toolbar(self):
        """区块 1：操作工具条（卡片）"""
        card = self.app.card(self.parent)
        _pack_if_needed(card, fill=tk.X, padx=30, pady=(0, 10))

        bar = tk.Frame(card, bg=self.TH.CARD_BG)
        bar.pack(fill=tk.X, padx=15, pady=(12, 6))

        self.app.button(bar, "开始扫描", self._start_scan, kind="primary").pack(side=tk.LEFT)

        self.clean_btn = self.app.button(bar, "清理选中项", self._clean_selected, kind="danger")
        self.clean_btn.pack(side=tk.LEFT, padx=(8, 0))
        self.clean_btn.config(state=tk.DISABLED)  # 初始禁用，扫描出结果后启用

        self.app.button(bar, "备份注册表", self._do_backup, kind="ghost").pack(side=tk.LEFT, padx=(8, 0))
        self.app.button(bar, "还原注册表", self._do_restore, kind="ghost").pack(side=tk.LEFT, padx=(8, 0))
        self.app.button(bar, "创建系统还原点", self._do_restore_point, kind="ghost").pack(side=tk.LEFT, padx=(8, 0))
        self.app.button(bar, "打开备份目录", self._open_backup_dir, kind="ghost").pack(side=tk.LEFT, padx=(8, 0))

        # 进度条与百分比标签（app 已 pack 好）
        self.progress, self.progress_label = self.app.progress_row(card)

    def _build_results_card(self):
        """区块 2：扫描结果表格"""
        card = self.app.card(self.parent)
        _pack_if_needed(card, fill=tk.BOTH, expand=True, padx=30, pady=(0, 10))

        head = tk.Frame(card, bg=self.TH.CARD_BG)
        head.pack(fill=tk.X, padx=15, pady=(12, 4))

        self.summary_label = tk.Label(
            head, text="尚未扫描，请点击「开始扫描」。",
            bg=self.TH.CARD_BG, fg=self.TH.TEXT,
            font=("Microsoft YaHei", 10, "bold"), anchor=tk.W,
        )
        self.summary_label.pack(side=tk.LEFT)

        self.select_all_var = tk.BooleanVar(value=False)
        tk.Checkbutton(
            head, text="全选", variable=self.select_all_var, command=self._toggle_all,
            bg=self.TH.CARD_BG, fg=self.TH.TEXT, font=("Microsoft YaHei", 10),
            activebackground=self.TH.CARD_BG, selectcolor=self.TH.CARD_BG,
        ).pack(side=tk.RIGHT)

        # 结果表格（container 由 app 负责 pack 到 card）
        self.tree, container = self.app.make_table(
            card, self.TABLE_COLUMNS,
            [self.TABLE_WIDTHS[c] for c in self.TABLE_COLUMNS],
        )
        _pack_if_needed(container, fill=tk.BOTH, expand=True, padx=15, pady=4)

        # 强制表头模式，保证「选择」列是第 1 列（#1）
        try:
            self.tree.configure(show="headings")
        except Exception:
            pass
        for cid in self.TABLE_COLUMNS:
            try:
                self.tree.heading(cid, text=self.TABLE_HEADINGS[cid])
                self.tree.column(
                    cid, width=self.TABLE_WIDTHS[cid],
                    anchor=tk.CENTER if cid in ("sel", "size") else tk.W,
                )
            except Exception:
                pass
        self.tree.bind("<Button-1>", self._on_tree_click)

        tk.Label(
            card,
            text=("清理注册表前建议先创建还原点。本工具只清理已确认安全的类别"
                  "（如 SharedDLLs、App Paths、失效启动项引用等），其他键会跳过。"),
            bg=self.TH.CARD_BG, fg=self.TH.TEXT_LIGHT,
            font=("Microsoft YaHei", 8), justify=tk.LEFT, wraplength=900,
        ).pack(anchor=tk.W, padx=15, pady=(0, 12))

    def _build_privacy_card(self):
        """区块 3：隐私开关"""
        card = self.app.card(self.parent)
        _pack_if_needed(card, fill=tk.X, padx=30, pady=(0, 10))

        tk.Label(
            card, text="隐私开关", bg=self.TH.CARD_BG, fg=self.TH.TEXT,
            font=("Microsoft YaHei", 11, "bold"), anchor=tk.W,
        ).pack(anchor=tk.W, padx=15, pady=(12, 4))

        tk.Label(
            card,
            text=("一键收紧 Windows 隐私相关设置（遥测、广告 ID、活动历史、开始菜单推荐、"
                  "错误报告、Copilot 等）。勾选表示应用该项（写入目标值）。"),
            bg=self.TH.CARD_BG, fg=self.TH.TEXT_LIGHT,
            font=("Microsoft YaHei", 9), justify=tk.LEFT, wraplength=900,
        ).pack(anchor=tk.W, padx=15, pady=(0, 6))

        self._privacy_frame = tk.Frame(card, bg=self.TH.CARD_BG)
        self._privacy_frame.pack(fill=tk.X, padx=15, pady=(0, 4))

        tk.Label(
            self._privacy_frame, text="正在读取隐私开关状态...",
            bg=self.TH.CARD_BG, fg=self.TH.TEXT_LIGHT, font=("Microsoft YaHei", 9),
        ).pack(anchor=tk.W)

        self.app.button(card, "应用选中项", self._apply_privacy, kind="primary").pack(
            anchor=tk.W, padx=15, pady=(8, 12))

    def _build_info_card(self):
        """区块 4：注册表体积信息"""
        card = self.app.card(self.parent)
        _pack_if_needed(card, fill=tk.X, padx=30, pady=(0, 15))

        tk.Label(
            card, text="注册表体积信息", bg=self.TH.CARD_BG, fg=self.TH.TEXT,
            font=("Microsoft YaHei", 11, "bold"), anchor=tk.W,
        ).pack(anchor=tk.W, padx=15, pady=(12, 4))

        self._info_label = tk.Label(
            card, text="正在获取注册表文件信息...",
            bg=self.TH.CARD_BG, fg=self.TH.TEXT_LIGHT,
            font=("Microsoft YaHei", 9), justify=tk.LEFT, anchor=tk.W,
        )
        self._info_label.pack(anchor=tk.W, fill=tk.X, padx=15, pady=(0, 12))

    # ==================== 通用工具 ====================

    def _set_status(self, text):
        """安全更新底部状态栏"""
        try:
            self.app.set_status(text)
        except Exception:
            pass

    def _reset_progress(self):
        try:
            if self.progress is not None:
                self.progress['value'] = 0
            if self.progress_label is not None:
                self.progress_label.config(text="0%")
        except Exception:
            pass

    def _update_progress(self, percent, msg):
        """进度回调（由 app.run_async 保证在主线程执行）"""
        try:
            if self.progress is not None:
                self.progress['value'] = percent
            if self.progress_label is not None:
                self.progress_label.config(text=f"{percent}%")
        except Exception:
            pass
        self._set_status(msg)

    def _run(self, work, on_done, on_progress=None):
        """统一封装 app.run_async，避免调度异常导致界面崩溃"""
        try:
            self.app.run_async(
                work, on_done=on_done,
                on_progress=on_progress if on_progress is not None else self._update_progress,
            )
        except Exception as e:
            self._busy = False
            self._error("操作失败", f"无法启动后台任务：{e}")

    def _error(self, title, msg):
        """安全弹窗（错误）"""
        try:
            self.app.show_error(title, msg)
        except Exception:
            pass

    def _info(self, title, msg):
        """安全弹窗（信息）"""
        try:
            self.app.show_info(title, msg)
        except Exception:
            pass

    def _warn(self, title, msg):
        """安全弹窗（警告）"""
        try:
            self.app.show_warning(title, msg)
        except Exception:
            pass

    def _confirm(self, title, msg):
        """安全确认框，异常时按“否”处理"""
        try:
            return bool(self.app.ask_yes_no(title, msg))
        except Exception:
            return False

    def _set_clean_enabled(self, enabled):
        try:
            if self.clean_btn is not None:
                self.clean_btn.config(state=tk.NORMAL if enabled else tk.DISABLED)
        except Exception:
            pass

    @staticmethod
    def _as_pair(result):
        """把可能为 None / 非元组的结果规整为 (bool_or_int, str_or_list)"""
        if isinstance(result, tuple) and len(result) == 2:
            return result[0], result[1]
        return None, None

    # ==================== 表格交互 ====================

    def _clear_rows(self):
        try:
            for iid in self.tree.get_children():
                self.tree.delete(iid)
        except Exception:
            pass
        self._rows = {}

    def _on_tree_click(self, event):
        """点击「选择」列（第 1 列）切换勾选状态"""
        try:
            region = self.tree.identify_region(event.x, event.y)
            col = self.tree.identify_column(event.x)
            if region == "cell" and col == "#1":
                iid = self.tree.identify_row(event.y)
                if iid:
                    cur = self.tree.set(iid, "sel")
                    self.tree.set(iid, "sel", "" if cur == "✓" else "✓")
        except Exception:
            pass

    def _toggle_all(self):
        """「全选」复选框：批量勾选 / 取消勾选"""
        try:
            val = "✓" if self.select_all_var.get() else ""
            for iid in self.tree.get_children():
                self.tree.set(iid, "sel", val)
        except Exception:
            pass

    def _selected_results(self):
        """返回当前勾选的 ScanResult 列表"""
        items = []
        try:
            for iid in self.tree.get_children():
                if self.tree.set(iid, "sel") == "✓":
                    r = self._rows.get(iid)
                    if r is not None:
                        items.append(r)
        except Exception:
            pass
        return items

    # ==================== 扫描 ====================

    def _start_scan(self):
        if self._busy:
            return
        self._busy = True
        self._set_clean_enabled(False)
        self._reset_progress()
        self._clear_rows()
        self.summary_label.config(text="正在扫描无效注册表项...")
        self._set_status("正在扫描注册表...")

        def work(progress_cb):
            try:
                engine = RegistryEngine(progress_callback=progress_cb)
                return engine.scan_registry_junk()
            except Exception as e:
                self._last_error = f"扫描注册表失败：{e}"
                return None

        self._run(work, self._on_scan_done)

    def _on_scan_done(self, result):
        self._busy = False
        if result is None:
            self.summary_label.config(text="扫描失败。")
            self._set_status("注册表扫描失败")
            self._error("扫描失败", self._last_error or "扫描注册表时发生错误。")
            return
        try:
            self.scan_results = list(result)
        except Exception:
            self.scan_results = []

        self._clear_rows()
        total = 0
        for r in self.scan_results:
            try:
                size = getattr(r, "size", 0) or 0
                total += size
                iid = self.tree.insert("", tk.END, values=(
                    "✓",                                   # 默认全选
                    getattr(r, "category", ""),
                    getattr(r, "source", ""),
                    getattr(r, "path", ""),
                    format_size(size),
                    getattr(r, "description", ""),
                ))
                self._rows[iid] = r
            except Exception:
                continue

        self.summary_label.config(
            text=f"共发现 {len(self.scan_results)} 项注册表垃圾，估算可释放 {format_size(total)}。")
        try:
            self.select_all_var.set(True)
        except Exception:
            pass
        self._set_clean_enabled(bool(self.scan_results))
        self._set_status(f"扫描完成，发现 {len(self.scan_results)} 项注册表垃圾")

        if not self.scan_results:
            self._info("扫描完成", "未发现无效注册表项，系统注册表很干净。")

    # ==================== 清理选中项 ====================

    def _clean_selected(self):
        if self._busy:
            return
        items = self._selected_results()
        if not items:
            self._warn("提示", "请先勾选要清理的注册表项。")
            return

        self._pending_clean = items
        self._busy = True
        self._set_clean_enabled(False)
        self._reset_progress()
        self._set_status("正在备份注册表...")

        # 第一步：自动备份注册表
        def work(progress_cb):
            try:
                return RegistryEngine().backup_registry()
            except Exception as e:
                return (False, f"备份注册表失败：{e}")

        self._run(work, self._after_backup_before_clean)

    def _after_backup_before_clean(self, result):
        ok, info = self._as_pair(result)
        if ok:
            self._info("备份完成", f"注册表已备份到：\n{info}\n\n即将开始清理。")
        else:
            proceed = self._confirm(
                "备份失败",
                f"注册表备份失败：\n{info or '未知错误'}\n\n"
                "继续清理有一定风险（可能无法还原）。是否仍要继续清理？",
            )
            if not proceed:
                self._busy = False
                self._pending_clean = []
                self._set_clean_enabled(bool(self.scan_results))
                self._set_status("已取消清理")
                return
        self._start_clean_items()

    def _start_clean_items(self):
        items = list(self._pending_clean)
        self._reset_progress()
        self._set_status("正在清理注册表...")

        def work(progress_cb):
            try:
                engine = RegistryEngine(progress_callback=progress_cb)
                return engine.clean_registry_items(items)
            except Exception as e:
                return (0, [f"清理注册表失败：{e}"])

        self._run(work, self._on_clean_done)

    def _on_clean_done(self, result):
        self._busy = False
        self._pending_clean = []
        count, logs = self._as_pair(result)
        count = count or 0
        logs = logs if isinstance(logs, list) else []

        brief = "\n".join(str(x) for x in logs[:20])
        if len(logs) > 20:
            brief += f"\n...（共 {len(logs)} 条日志，此处仅显示前 20 条）"
        if not brief:
            brief = "（无日志）"

        self._info("清理完成", f"成功清理 {count} 项注册表垃圾。\n\n日志摘要：\n{brief}")
        self._set_status(f"注册表清理完成，成功 {count} 项")
        # 自动重新扫描
        self._start_scan()

    # ==================== 备份 / 还原 / 还原点 / 打开目录 ====================

    def _do_backup(self):
        if self._busy:
            return
        self._busy = True
        self._reset_progress()
        self._set_status("正在备份注册表...")

        def work(progress_cb):
            try:
                return RegistryEngine().backup_registry()
            except Exception as e:
                return (False, f"备份注册表失败：{e}")

        def done(result):
            self._busy = False
            ok, info = self._as_pair(result)
            if ok:
                self._info("备份完成", f"注册表已备份到：\n{info}")
                self._set_status("注册表备份完成")
            else:
                self._error("备份失败", info or "备份注册表失败。")
                self._set_status("注册表备份失败")

        self._run(work, done)

    def _do_restore(self):
        if self._busy:
            return
        try:
            path = filedialog.askopenfilename(
                title="选择注册表备份文件(.reg)",
                filetypes=[("注册表备份文件", "*.reg"), ("所有文件", "*.*")],
            )
        except Exception as e:
            self._error("选择文件失败", str(e))
            return
        if not path:
            return
        if not self._confirm(
            "确认还原",
            f"确定要从以下备份文件还原注册表吗？\n{path}\n\n"
            "还原会覆盖当前相关注册表设置，可能需要管理员权限。",
        ):
            return

        self._busy = True
        self._reset_progress()
        self._set_status("正在还原注册表...")

        def work(progress_cb):
            try:
                return RegistryEngine().restore_registry(path)
            except Exception as e:
                return (False, f"还原注册表失败：{e}")

        def done(result):
            self._busy = False
            ok, info = self._as_pair(result)
            if ok:
                self._info("还原完成", info or "注册表已还原。")
                self._set_status("注册表还原完成")
            else:
                self._error("还原失败", info or "还原注册表失败。")
                self._set_status("注册表还原失败")

        self._run(work, done)

    def _do_restore_point(self):
        if self._busy:
            return
        if not self._confirm(
            "创建系统还原点",
            "将创建一个系统还原点（可能耗时较久，建议在管理员身份下运行）。是否继续？",
        ):
            return

        self._busy = True
        self._reset_progress()
        self._set_status("正在创建系统还原点...")

        def work(progress_cb):
            try:
                return RegistryEngine().create_restore_point()
            except Exception as e:
                return (False, f"创建系统还原点失败：{e}")

        def done(result):
            self._busy = False
            ok, info = self._as_pair(result)
            if ok:
                self._info("完成", info or "系统还原点创建成功。")
                self._set_status("系统还原点创建完成")
            else:
                self._warn("未能创建还原点", info or "创建系统还原点失败。")
                self._set_status("创建系统还原点失败")

        self._run(work, done)

    def _open_backup_dir(self):
        """打开用户目录\\CleanMasterBackup\\registry"""
        d = _backup_dir()
        try:
            os.makedirs(d, exist_ok=True)
        except Exception:
            pass
        try:
            os.startfile(d)
        except Exception as e:
            self._error("打开失败", f"无法打开备份目录：\n{d}\n{e}")

    # ==================== 隐私开关 ====================

    def _load_privacy(self):
        def work(progress_cb):
            try:
                return RegistryEngine(progress_callback=progress_cb).get_privacy_switches()
            except Exception as e:
                self._last_error = f"读取隐私开关失败：{e}"
                return None

        self._run(work, self._render_privacy)

    def _render_privacy(self, switches):
        if self._privacy_frame is None:
            return
        # 清空并重建
        try:
            for w in self._privacy_frame.winfo_children():
                w.destroy()
        except Exception:
            pass
        self._privacy_vars = []

        if switches is None:
            tk.Label(
                self._privacy_frame, text=self._last_error or "读取隐私开关失败。",
                bg=self.TH.CARD_BG, fg=self.TH.DANGER, font=("Microsoft YaHei", 9),
            ).pack(anchor=tk.W)
            return
        if not switches:
            tk.Label(
                self._privacy_frame, text="未获取到隐私开关定义。",
                bg=self.TH.CARD_BG, fg=self.TH.TEXT_LIGHT, font=("Microsoft YaHei", 9),
            ).pack(anchor=tk.W)
            return

        for sw in switches:
            try:
                name = sw.get("name") or sw.get("id") or "未命名开关"
                if sw.get("need_admin"):
                    name += "（需要管理员权限）"
                var = tk.BooleanVar(value=bool(sw.get("enabled")))
                row = tk.Frame(self._privacy_frame, bg=self.TH.CARD_BG)
                row.pack(fill=tk.X, pady=(4, 0))
                tk.Checkbutton(
                    row, text=name, variable=var, anchor=tk.W, justify=tk.LEFT,
                    bg=self.TH.CARD_BG, fg=self.TH.TEXT, font=("Microsoft YaHei", 10),
                    activebackground=self.TH.CARD_BG, selectcolor=self.TH.CARD_BG,
                ).pack(anchor=tk.W)
                tk.Label(
                    row, text=sw.get("desc", ""), bg=self.TH.CARD_BG, fg=self.TH.TEXT_LIGHT,
                    font=("Microsoft YaHei", 9), justify=tk.LEFT, anchor=tk.W, wraplength=820,
                ).pack(anchor=tk.W, padx=(24, 0))
                self._privacy_vars.append((var, sw))
            except Exception:
                continue

    def _apply_privacy(self):
        if self._busy:
            return
        selected = [sw for (var, sw) in self._privacy_vars if _safe_get(var)]
        if not selected:
            self._warn("提示", "请至少勾选一个要应用的隐私开关。")
            return

        self._busy = True
        self._set_status("正在应用隐私开关...")

        def work(progress_cb):
            try:
                return RegistryEngine(progress_callback=progress_cb).apply_privacy_switches(selected)
            except Exception as e:
                return (0, [f"应用隐私开关失败：{e}"])

        def done(result):
            self._busy = False
            count, logs = self._as_pair(result)
            count = count or 0
            logs = logs if isinstance(logs, list) else []
            brief = "\n".join(str(x) for x in logs[:20])
            if len(logs) > 20:
                brief += f"\n...（共 {len(logs)} 条日志，此处仅显示前 20 条）"
            if not brief:
                brief = "（无日志）"
            self._info("应用完成", f"成功应用 {count} 项隐私开关。\n\n日志摘要：\n{brief}")
            self._set_status(f"隐私开关应用完成，成功 {count} 项")
            self._load_privacy()  # 刷新该区块

        self._run(work, done)

    # ==================== 注册表体积信息 ====================

    def _load_registry_info(self):
        def work(progress_cb):
            try:
                return RegistryEngine().get_registry_info()
            except Exception as e:
                self._last_error = f"获取注册表体积信息失败：{e}"
                return None

        def done(result):
            if self._info_label is None:
                return
            if not isinstance(result, dict):
                self._info_label.config(text="未获取到注册表文件信息")
                return
            hives = result.get("hive_sizes") or []
            if not hives:
                self._info_label.config(text="未获取到注册表文件信息")
                return
            lines = []
            for h in hives:
                try:
                    name = h.get("name", "")
                    readable = h.get("readable_size") or format_size(h.get("size", 0) or 0)
                    lines.append(f"  {name}：{readable}")
                except Exception:
                    continue
            lines.append(f"  合计：{format_size(result.get('total_size', 0) or 0)}")
            self._info_label.config(text="\n".join(lines))

        self._run(work, done)


def _safe_get(var):
    """安全读取 BooleanVar，异常时按 False 处理"""
    try:
        return bool(var.get())
    except Exception:
        return False


def build(app, parent):
    """
    页面入口：由主程序以 build(app, parent) 的方式调用。
    app 为提供主题 / 交互 / 异步能力的应用对象，parent 为 tk.Frame 容器。
    返回页面实例。
    """
    return RegistryPage(app, parent)
