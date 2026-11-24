# -*- coding: utf-8 -*-
"""
文件粉碎页面（page_shredder）

三个标签页：
  1) 粉碎文件        —— 选文件 / 文件夹，按覆写算法彻底销毁后删除
  2) 空闲空间擦除    —— 覆写磁盘空闲块，抹掉已删除文件残留数据
  3) 隐私文件粉碎    —— 扫描「建议粉碎」的隐私痕迹并批量粉碎

设计约束：
  - 仅使用标准库 + tkinter（含 filedialog）+ scanner.format_size + shredder.ShredEngine，
    不引入任何第三方依赖。
  - 本模块被 import 时不产生任何副作用（仅定义类与函数）。
  - 所有耗时操作统一通过 app.run_async 放到后台线程执行。
  - 所有引擎调用均以 try/except 包裹，异常通过 app.show_error 提示。
  - 整个页面只创建一个 ShredEngine 实例（self.engine），所有后台任务复用它，
    这样「停止」按钮（engine.stop()）才能真正生效。
"""

import os
import tkinter as tk
from tkinter import ttk, filedialog

from scanner import format_size
from shredder import ShredEngine


_FONT = ("Microsoft YaHei", 9)


class ShredderPage:
    """文件粉碎页面控制器"""

    def __init__(self, app, parent):
        self.app = app
        self.parent = parent
        self.TH = app.THEME

        # 全局唯一的粉碎引擎：三个标签页共用
        self.engine = ShredEngine()

        # 待粉碎清单（与 listbox 行一一对应）
        self.targets = []

        # 算法下拉数据
        self.alg_infos = []      # [{"id","name","passes","desc"}]
        self.alg_display = []    # 下拉框显示文本
        self.alg_ids = []        # 与显示文本一一对应的算法 id

        # 隐私文件粉碎扫描结果（与表格行一一对应）
        self.privacy_rows = []

        self._busy = False

        self._build()

    # ==================== 界面搭建 ====================

    def _build(self):
        self.app.section_title(
            self.parent, "文件粉碎",
            "不可恢复地彻底删除敏感文件，防止被恢复软件还原。")

        nb = ttk.Notebook(self.parent)
        nb.pack(fill=tk.BOTH, expand=True, padx=8, pady=(0, 8))
        self.notebook = nb

        tab_files = tk.Frame(nb, bg=self.TH.BG)
        tab_free = tk.Frame(nb, bg=self.TH.BG)
        tab_privacy = tk.Frame(nb, bg=self.TH.BG)
        nb.add(tab_files, text="  粉碎文件  ")
        nb.add(tab_free, text="  空闲空间擦除  ")
        nb.add(tab_privacy, text="  隐私文件粉碎  ")

        # 先取算法列表，再建控件（下拉框需要值）
        self._load_algorithms()

        self._build_tab_files(tab_files)
        self._build_tab_free_space(tab_free)
        self._build_tab_privacy(tab_privacy)

    # ---------- 标签页 1：粉碎文件 ----------

    def _build_tab_files(self, parent):
        T = self.TH

        # ---- 顶部卡片：清单 + 算法 + 操作 ----
        card = self.app.card(parent)
        card.pack(fill=tk.X, padx=10, pady=(10, 6))

        tk.Label(card, text="待粉碎清单（文件或文件夹；文件夹内的文件会逐个粉碎，文件夹本身保留）",
                 bg=T.CARD_BG, fg=T.TEXT,
                 font=("Microsoft YaHei", 10, "bold")).pack(anchor=tk.W, padx=12, pady=(10, 4))

        list_holder = tk.Frame(card, bg=T.CARD_BG)
        list_holder.pack(fill=tk.BOTH, expand=True, padx=12)
        self.listbox = tk.Listbox(list_holder, height=8, font=_FONT, bg="white",
                                  fg=T.TEXT, selectmode=tk.EXTENDED,
                                  exportselection=False, highlightthickness=1,
                                  highlightbackground=T.BORDER, activestyle="none")
        list_sb = ttk.Scrollbar(list_holder, orient="vertical", command=self.listbox.yview)
        self.listbox.configure(yscrollcommand=list_sb.set)
        self.listbox.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        list_sb.pack(side=tk.RIGHT, fill=tk.Y)

        btn_row = tk.Frame(card, bg=T.CARD_BG)
        btn_row.pack(fill=tk.X, padx=12, pady=(8, 4))
        self.app.button(btn_row, "添加文件", self._add_files, kind="ghost").pack(side=tk.LEFT)
        self.app.button(btn_row, "添加文件夹", self._add_folder, kind="ghost").pack(
            side=tk.LEFT, padx=(6, 0))
        self.app.button(btn_row, "移除选中", self._remove_selected, kind="ghost").pack(
            side=tk.LEFT, padx=(6, 0))
        self.app.button(btn_row, "清空清单", self._clear_targets, kind="ghost").pack(
            side=tk.LEFT, padx=(6, 0))

        tk.Label(card, text="覆写算法：", bg=T.CARD_BG, fg=T.TEXT,
                 font=_FONT).pack(anchor=tk.W, padx=12, pady=(8, 0))
        alg_row = tk.Frame(card, bg=T.CARD_BG)
        alg_row.pack(fill=tk.X, padx=12, pady=(4, 2))
        self.files_alg_combo = ttk.Combobox(alg_row, values=self.alg_display,
                                            state="readonly", width=30, font=_FONT)
        try:
            self.files_alg_combo.current(self._alg_index("dod"))
        except Exception:
            pass
        self.files_alg_combo.pack(side=tk.LEFT)
        self.files_alg_combo.bind(
            "<<ComboboxSelected>>",
            lambda e: self._update_alg_desc(self.files_alg_combo, self.files_alg_desc))

        self.files_alg_desc = tk.Label(card, text=self._alg_desc_text("dod"),
                                       bg=T.CARD_BG, fg=T.TEXT_LIGHT, font=_FONT,
                                       wraplength=900, justify=tk.LEFT, anchor=tk.W)
        self.files_alg_desc.pack(anchor=tk.W, fill=tk.X, padx=12, pady=(0, 6))

        act_row = tk.Frame(card, bg=T.CARD_BG)
        act_row.pack(fill=tk.X, padx=12, pady=(0, 6))
        self.app.button(act_row, "预览(dry_run)", self._preview_files, kind="ghost").pack(
            side=tk.LEFT)
        self.file_shred_btn = self.app.button(act_row, "开始粉碎", self._start_shred_files,
                                              kind="danger")
        self.file_shred_btn.pack(side=tk.LEFT, padx=(6, 0))
        self.app.button(act_row, "停止", self._stop_engine, kind="ghost").pack(
            side=tk.LEFT, padx=(6, 0))

        self.file_prog, self.file_prog_label = self._make_progress(card)
        self.file_result = tk.Label(card, text="尚未开始", bg=T.CARD_BG, fg=T.PRIMARY,
                                    font=("Microsoft YaHei", 10, "bold"), anchor=tk.W,
                                    justify=tk.LEFT, wraplength=900)
        self.file_result.pack(anchor=tk.W, fill=tk.X, padx=12, pady=(0, 8))

        # ---- 底部卡片：只读日志 ----
        log_card = self.app.card(parent)
        log_card.pack(fill=tk.BOTH, expand=True, padx=10, pady=(0, 6))
        tk.Label(log_card, text="粉碎日志", bg=T.CARD_BG, fg=T.TEXT,
                 font=("Microsoft YaHei", 10, "bold")).pack(anchor=tk.W, padx=12,
                                                            pady=(8, 2))
        log_holder = tk.Frame(log_card, bg=T.CARD_BG)
        log_holder.pack(fill=tk.BOTH, expand=True, padx=12, pady=(0, 8))
        self.log_text = tk.Text(log_holder, height=8, state=tk.DISABLED, bg="#fafafa",
                                fg=T.TEXT, font=("Microsoft YaHei", 8), wrap=tk.WORD,
                                relief=tk.FLAT, highlightthickness=1,
                                highlightbackground=T.BORDER)
        log_sb = ttk.Scrollbar(log_holder, orient="vertical", command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=log_sb.set)
        self.log_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        log_sb.pack(side=tk.RIGHT, fill=tk.Y)

        tk.Label(parent,
                 text="⚠ Gutmann(35遍) 等算法对大文件耗时极长（可能数十分钟），"
                      "建议先对单个文件试跑。",
                 bg=T.BG, fg=T.WARNING, font=_FONT, wraplength=900,
                 justify=tk.LEFT).pack(anchor=tk.W, padx=10, pady=(0, 6))

    # ---------- 标签页 2：空闲空间擦除 ----------

    def _build_tab_free_space(self, parent):
        T = self.TH

        card = self.app.card(parent)
        card.pack(fill=tk.X, padx=10, pady=(10, 6))

        tk.Label(card, text="空闲空间擦除", bg=T.CARD_BG, fg=T.TEXT,
                 font=("Microsoft YaHei", 12, "bold")).pack(anchor=tk.W, padx=12,
                                                             pady=(10, 2))
        tk.Label(card,
                 text="把磁盘上已删除文件残留的数据物理覆盖掉，防止被恢复软件还原。"
                      "这是高耗时操作。",
                 bg=T.CARD_BG, fg=T.TEXT_LIGHT, font=_FONT, wraplength=900,
                 justify=tk.LEFT).pack(anchor=tk.W, padx=12, pady=(0, 8))

        row = tk.Frame(card, bg=T.CARD_BG)
        row.pack(fill=tk.X, padx=12, pady=(0, 4))
        tk.Label(row, text="目标盘：", bg=T.CARD_BG, fg=T.TEXT,
                 font=_FONT).pack(side=tk.LEFT)
        drives = [d + ":\\" for d in ("C", "D", "E", "F") if os.path.exists(d + ":\\")]
        if not drives:
            drives = ["C:\\"]
        self.fs_drive_combo = ttk.Combobox(row, values=drives, state="readonly",
                                           width=12, font=_FONT)
        self.fs_drive_combo.current(0)
        self.fs_drive_combo.pack(side=tk.LEFT, padx=(0, 16))

        tk.Label(row, text="覆写算法：", bg=T.CARD_BG, fg=T.TEXT,
                 font=_FONT).pack(side=tk.LEFT)
        self.fs_alg_combo = ttk.Combobox(row, values=self.alg_display, state="readonly",
                                         width=30, font=_FONT)
        try:
            self.fs_alg_combo.current(self._alg_index("zero"))
        except Exception:
            pass
        self.fs_alg_combo.pack(side=tk.LEFT)
        self.fs_alg_combo.bind(
            "<<ComboboxSelected>>",
            lambda e: self._update_alg_desc(self.fs_alg_combo, self.fs_alg_desc))

        self.fs_alg_desc = tk.Label(card, text=self._alg_desc_text("zero"),
                                    bg=T.CARD_BG, fg=T.TEXT_LIGHT, font=_FONT,
                                    wraplength=900, justify=tk.LEFT, anchor=tk.W)
        self.fs_alg_desc.pack(anchor=tk.W, fill=tk.X, padx=12, pady=(0, 6))

        act_row = tk.Frame(card, bg=T.CARD_BG)
        act_row.pack(fill=tk.X, padx=12, pady=(0, 6))
        self.app.button(act_row, "预览", self._preview_free_space, kind="ghost").pack(
            side=tk.LEFT)
        self.fs_start_btn = self.app.button(act_row, "开始擦除", self._start_free_space,
                                            kind="danger")
        self.fs_start_btn.pack(side=tk.LEFT, padx=(6, 0))
        self.app.button(act_row, "停止", self._stop_engine, kind="ghost").pack(
            side=tk.LEFT, padx=(6, 0))

        self.fs_prog, self.fs_prog_label = self._make_progress(card)
        tk.Label(card,
                 text="⚠ 强烈警告：SSD（固态硬盘）不建议频繁使用此功能，"
                      "大量写入会消耗其写入寿命。",
                 bg=T.CARD_BG, fg=T.DANGER, font=("Microsoft YaHei", 9, "bold"),
                 wraplength=900, justify=tk.LEFT).pack(anchor=tk.W, padx=12, pady=(0, 10))

        log_card = self.app.card(parent)
        log_card.pack(fill=tk.BOTH, expand=True, padx=10, pady=(0, 6))
        tk.Label(log_card, text="擦除日志", bg=T.CARD_BG, fg=T.TEXT,
                 font=("Microsoft YaHei", 10, "bold")).pack(anchor=tk.W, padx=12,
                                                            pady=(8, 2))
        log_holder = tk.Frame(log_card, bg=T.CARD_BG)
        log_holder.pack(fill=tk.BOTH, expand=True, padx=12, pady=(0, 8))
        self.fs_log_text = tk.Text(log_holder, height=10, state=tk.DISABLED, bg="#fafafa",
                                   fg=T.TEXT, font=("Microsoft YaHei", 8), wrap=tk.WORD,
                                   relief=tk.FLAT, highlightthickness=1,
                                   highlightbackground=T.BORDER)
        fs_sb = ttk.Scrollbar(log_holder, orient="vertical", command=self.fs_log_text.yview)
        self.fs_log_text.configure(yscrollcommand=fs_sb.set)
        self.fs_log_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        fs_sb.pack(side=tk.RIGHT, fill=tk.Y)

    # ---------- 标签页 3：隐私文件粉碎 ----------

    def _build_tab_privacy(self, parent):
        T = self.TH

        card = self.app.card(parent)
        card.pack(fill=tk.X, padx=10, pady=(10, 6))

        row = tk.Frame(card, bg=T.CARD_BG)
        row.pack(fill=tk.X, padx=12, pady=(10, 4))
        tk.Label(row, text="覆写算法：", bg=T.CARD_BG, fg=T.TEXT,
                 font=_FONT).pack(side=tk.LEFT)
        self.privacy_alg_combo = ttk.Combobox(row, values=self.alg_display,
                                              state="readonly", width=30, font=_FONT)
        try:
            self.privacy_alg_combo.current(self._alg_index("dod"))
        except Exception:
            pass
        self.privacy_alg_combo.pack(side=tk.LEFT, padx=(0, 12))
        self.privacy_alg_combo.bind(
            "<<ComboboxSelected>>",
            lambda e: self._update_alg_desc(self.privacy_alg_combo, self.privacy_alg_desc))

        self.app.button(row, "扫描建议粉碎的文件", self._scan_privacy, kind="primary").pack(
            side=tk.LEFT, padx=(0, 12))

        self.privacy_select_all = tk.BooleanVar(value=True)
        tk.Checkbutton(row, text="全选", variable=self.privacy_select_all,
                       bg=T.CARD_BG, fg=T.TEXT, font=_FONT,
                       activebackground=T.CARD_BG, selectcolor=T.CARD_BG,
                       command=self._toggle_privacy_select_all).pack(side=tk.LEFT)

        self.privacy_alg_desc = tk.Label(card, text=self._alg_desc_text("dod"),
                                         bg=T.CARD_BG, fg=T.TEXT_LIGHT, font=_FONT,
                                         wraplength=900, justify=tk.LEFT, anchor=tk.W)
        self.privacy_alg_desc.pack(anchor=tk.W, fill=tk.X, padx=12, pady=(0, 6))

        self.privacy_summary = tk.Label(card, text="尚未扫描", bg=T.CARD_BG,
                                        fg=T.PRIMARY, font=("Microsoft YaHei", 10, "bold"),
                                        anchor=tk.W)
        self.privacy_summary.pack(anchor=tk.W, fill=tk.X, padx=12, pady=(0, 4))

        act_row = tk.Frame(card, bg=T.CARD_BG)
        act_row.pack(fill=tk.X, padx=12, pady=(0, 8))
        self.app.button(act_row, "粉碎选中", self._shred_privacy_selected,
                        kind="danger").pack(side=tk.LEFT)

        self.privacy_prog, self.privacy_prog_label = self._make_progress(card)

        # 结果表格
        self.privacy_tree, _ = self.app.make_table(
            parent,
            ("sel", "source", "path", "size", "desc"),
            (50, 220, 420, 90, 340),
            headings=("选择", "来源", "路径", "大小", "说明"),
            height=12)
        self.privacy_tree.bind("<Button-1>", self._on_privacy_click)

        tk.Label(parent,
                 text="这些文件通常包含使用痕迹。普通删除后仍可能被恢复，因此建议使用粉碎。",
                 bg=T.BG, fg=T.WARNING, font=_FONT, wraplength=900,
                 justify=tk.LEFT).pack(anchor=tk.W, padx=10, pady=(0, 6))

    # ==================== 通用小工具 ====================

    def _make_progress(self, parent):
        """在卡片内创建进度条 + 百分比标签（使用卡片底色，视觉更统一）"""
        holder = tk.Frame(parent, bg=self.TH.CARD_BG)
        holder.pack(fill=tk.X, padx=12, pady=(2, 4))
        bar = ttk.Progressbar(holder, mode="determinate")
        bar.pack(side=tk.LEFT, fill=tk.X, expand=True)
        label = tk.Label(holder, text="0%", bg=self.TH.CARD_BG, fg=self.TH.PRIMARY,
                         font=("Microsoft YaHei", 9, "bold"), width=5)
        label.pack(side=tk.LEFT, padx=8)
        return bar, label

    def _set_log(self, text_widget, lines):
        """把日志行写入只读文本区（覆盖）"""
        try:
            text_widget.config(state=tk.NORMAL)
            text_widget.delete("1.0", tk.END)
            for line in (lines or []):
                text_widget.insert(tk.END, str(line) + "\n")
            text_widget.see(tk.END)
            text_widget.config(state=tk.DISABLED)
        except Exception:
            pass

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

    def _reset_progress(self, bar, label):
        try:
            bar["value"] = 0
            label.config(text="0%")
        except Exception:
            pass

    # ---------- 算法相关 ----------

    def _load_algorithms(self):
        """从引擎读取算法列表，填充下拉框数据"""
        try:
            algs = self.engine.get_algorithms()
        except Exception as e:
            algs = []
            self._error("算法加载失败", f"无法读取覆写算法列表：{e}")

        self.alg_infos = []
        for a in (algs or []):
            try:
                self.alg_infos.append({
                    "id": a.get("id"),
                    "name": a.get("name") or a.get("id"),
                    "passes": a.get("passes"),
                    "desc": a.get("desc", ""),
                })
            except Exception:
                continue
        self.alg_display = [a["name"] for a in self.alg_infos]
        self.alg_ids = [a["id"] for a in self.alg_infos]

    def _alg_index(self, alg_id):
        """返回指定算法 id 在下拉框中的索引，找不到时返回 0"""
        try:
            return self.alg_ids.index(alg_id)
        except (ValueError, AttributeError):
            return 0

    def _alg_desc_text(self, alg_id):
        """返回指定算法的原始说明文本"""
        for a in self.alg_infos:
            if a["id"] == alg_id:
                return a["desc"]
        return ""

    def _current_alg(self, combo, default="dod"):
        """读取下拉框当前选中的算法 id"""
        try:
            idx = combo.current()
        except Exception:
            idx = -1
        if 0 <= idx < len(self.alg_ids):
            return self.alg_ids[idx]
        return default

    def _update_alg_desc(self, combo, label):
        """下拉框选中变化时刷新算法说明"""
        try:
            idx = combo.current()
            if 0 <= idx < len(self.alg_infos):
                label.config(text=self.alg_infos[idx].get("desc", ""))
        except Exception:
            pass

    # ==================== 标签页 1：粉碎文件 ====================

    def _add_files(self):
        try:
            paths = filedialog.askopenfilenames(title="选择要粉碎的文件")
        except Exception as e:
            self._error("选择文件失败", str(e))
            return
        for p in (paths or []):
            self._add_target(p)

    def _add_folder(self):
        try:
            path = filedialog.askdirectory(title="选择要粉碎的文件夹")
        except Exception as e:
            self._error("选择文件夹失败", str(e))
            return
        if path:
            self._add_target(path)

    def _add_target(self, path):
        """加入清单（自动去重）"""
        if not path:
            return
        if path in self.targets:
            return
        self.targets.append(path)
        try:
            self.listbox.insert(tk.END, path)
        except Exception:
            pass

    def _remove_selected(self):
        try:
            indices = list(self.listbox.curselection())
        except Exception:
            indices = []
        if not indices:
            self._warn("提示", "请先在清单中选中要移除的项。")
            return
        for i in reversed(indices):
            try:
                self.listbox.delete(i)
                if 0 <= i < len(self.targets):
                    self.targets.pop(i)
            except Exception:
                continue

    def _clear_targets(self):
        self.targets = []
        try:
            self.listbox.delete(0, tk.END)
        except Exception:
            pass
        self._set_status("已清空待粉碎清单")

    def _preview_files(self):
        if not self.targets:
            self._warn("提示", "请先添加要粉碎的文件或文件夹。")
            return
        alg = self._current_alg(self.files_alg_combo, "dod")
        paths = list(self.targets)
        self._reset_progress(self.file_prog, self.file_prog_label)
        self._set_status("正在预览粉碎计划...")

        def work(progress_cb):
            try:
                self.engine.progress_callback = progress_cb
                self.engine.reset_stop()
                return self.engine.shred_files(paths, algorithm=alg, dry_run=True)
            except Exception as e:
                return {"_error": str(e)}

        def on_done(result):
            if not isinstance(result, dict):
                self._error("预览失败", "引擎未返回有效结果。")
                return
            if result.get("_error"):
                self._error("预览失败", result["_error"])
                return
            count = result.get("success_count", 0)
            size = result.get("total_bytes", 0)
            msg = (f"将粉碎 {count} 个文件，总体积 {format_size(size)}"
                   f"（预览模式不会写入任何数据）")
            self.file_result.config(text=msg)
            self._set_log(self.log_text, result.get("logs"))
            self._set_status("预览完成")
            self._info("预览结果", msg)

        self.app.run_async(work, on_done=on_done)

    def _start_shred_files(self):
        if self._busy:
            return
        if not self.targets:
            self._warn("提示", "请先添加要粉碎的文件或文件夹。")
            return
        if not self._confirm(
                "确认粉碎",
                "粉碎将永久销毁这些文件，无法通过任何恢复软件找回，是否继续？"):
            return

        alg = self._current_alg(self.files_alg_combo, "dod")
        paths = list(self.targets)
        self._busy = True
        self.file_shred_btn.config(state=tk.DISABLED)
        self._reset_progress(self.file_prog, self.file_prog_label)
        self.file_result.config(text="正在粉碎...")
        self._set_status("正在粉碎文件...")

        def work(progress_cb):
            try:
                self.engine.progress_callback = progress_cb
                self.engine.reset_stop()
                return self.engine.shred_files(paths, algorithm=alg, dry_run=False)
            except Exception as e:
                return {"_error": str(e)}

        def on_progress(percent, msg):
            try:
                self.file_prog["value"] = percent
                self.file_prog_label.config(text=f"{percent}%")
            except Exception:
                pass

        def on_done(result):
            self._busy = False
            try:
                self.file_shred_btn.config(state=tk.NORMAL)
            except Exception:
                pass
            if not isinstance(result, dict):
                self._error("粉碎失败", "引擎未返回有效结果。")
                return
            if result.get("_error"):
                self._error("粉碎失败", result["_error"])
                return

            sc = result.get("success_count", 0)
            fc = result.get("fail_count", 0)
            total = result.get("total_bytes", 0)
            failures = result.get("failures", []) or []
            stopped = result.get("stopped", False)

            head = ("已停止。" if stopped else "粉碎完成。")
            msg = f"{head}成功 {sc} 个，失败 {fc} 个，共 {format_size(total)}。"
            self.file_result.config(text=msg)
            self._set_log(self.log_text, result.get("logs"))

            detail = msg
            if failures:
                lines = [f"· {f.get('path', '')}：{f.get('reason', '')}"
                         for f in failures[:10]]
                detail += "\n\n失败明细：\n" + "\n".join(lines)
                if len(failures) > 10:
                    detail += f"\n… 其余 {len(failures) - 10} 条略"
            self._set_status(msg)
            self._info("粉碎结果", detail)

        self.app.run_async(work, on_done=on_done, on_progress=on_progress)

    # ==================== 标签页 2：空闲空间擦除 ====================

    def _preview_free_space(self):
        drive = self._fs_drive()
        alg = self._current_alg(self.fs_alg_combo, "zero")
        self._reset_progress(self.fs_prog, self.fs_prog_label)
        self._set_status("正在预览空闲空间擦除...")

        def work(progress_cb):
            try:
                self.engine.progress_callback = progress_cb
                self.engine.reset_stop()
                return self.engine.write_free_space(drive, alg, dry_run=True)
            except Exception as e:
                return {"_error": str(e)}

        def on_done(result):
            if not isinstance(result, dict):
                self._error("预览失败", "引擎未返回有效结果。")
                return
            if result.get("_error"):
                self._error("预览失败", result["_error"])
                return
            written = result.get("written_bytes", 0)
            free_before = result.get("free_before", 0)
            msg = f"将写入约 {format_size(written)}（当前剩余 {format_size(free_before)}）"
            self._set_log(self.fs_log_text, result.get("logs"))
            self._set_status("预览完成")
            self._info("预览结果", result.get("message") or msg)

        self.app.run_async(work, on_done=on_done)

    def _start_free_space(self):
        drive = self._fs_drive()
        alg = self._current_alg(self.fs_alg_combo, "zero")
        if not self._confirm(
                "确认擦除",
                f"即将对 {drive} 的空闲空间进行覆写擦除。\n\n"
                "· 这是高耗时操作，机械硬盘可能需要几十分钟\n"
                "· 若是 SSD，会消耗写入寿命\n"
                "· 过程中磁盘可用空间会被临时占满\n\n是否继续？"):
            return

        self._busy = True
        try:
            self.fs_start_btn.config(state=tk.DISABLED)
        except Exception:
            pass
        self._reset_progress(self.fs_prog, self.fs_prog_label)
        self._set_status("正在擦除空闲空间...")

        def work(progress_cb):
            try:
                self.engine.progress_callback = progress_cb
                self.engine.reset_stop()
                return self.engine.write_free_space(drive, alg, dry_run=False)
            except Exception as e:
                return {"_error": str(e)}

        def on_progress(percent, msg):
            try:
                self.fs_prog["value"] = percent
                self.fs_prog_label.config(text=f"{percent}%")
            except Exception:
                pass

        def on_done(result):
            self._busy = False
            try:
                self.fs_start_btn.config(state=tk.NORMAL)
            except Exception:
                pass
            if not isinstance(result, dict):
                self._error("擦除失败", "引擎未返回有效结果。")
                return
            if result.get("_error"):
                self._error("擦除失败", result["_error"])
                return
            msg = result.get("message") or (
                f"完成：写入 {format_size(result.get('written_bytes', 0))}，"
                f"耗时 {result.get('seconds', 0)} 秒。")
            self._set_log(self.fs_log_text, result.get("logs"))
            self._set_status(msg)
            if result.get("success"):
                self._info("擦除完成", msg)
            else:
                self._warn("擦除未完成", msg)

        self.app.run_async(work, on_done=on_done, on_progress=on_progress)

    def _fs_drive(self):
        try:
            return self.fs_drive_combo.get() or "C:\\"
        except Exception:
            return "C:\\"

    # ==================== 标签页 3：隐私文件粉碎 ====================

    def _scan_privacy(self):
        self._reset_progress(self.privacy_prog, self.privacy_prog_label)
        self.privacy_summary.config(text="正在扫描建议粉碎的文件...")
        self._set_status("正在扫描隐私文件...")

        def work(progress_cb):
            try:
                self.engine.progress_callback = progress_cb
                return self.engine.scan_privacy_shred_targets()
            except Exception as e:
                return {"_error": str(e)}

        def on_done(result):
            if not isinstance(result, list):
                self.privacy_summary.config(text="扫描失败。")
                self._error("扫描失败", "引擎未返回有效的扫描结果。")
                return
            total = 0
            try:
                for iid in self.privacy_tree.get_children():
                    self.privacy_tree.delete(iid)
            except Exception:
                pass
            self.privacy_rows = list(result)
            for r in self.privacy_rows:
                size = getattr(r, "size", 0) or 0
                total += size
                try:
                    self.privacy_tree.insert("", tk.END, values=(
                        "✓",
                        getattr(r, "source", ""),
                        getattr(r, "path", ""),
                        format_size(size),
                        getattr(r, "description", ""),
                    ))
                except Exception:
                    continue
            self.privacy_select_all.set(True)
            if self.privacy_rows:
                self.privacy_summary.config(
                    text=f"共发现 {len(self.privacy_rows)} 项，合计 {format_size(total)}")
                self._set_status(f"扫描完成，共 {len(self.privacy_rows)} 项")
            else:
                self.privacy_summary.config(text="未发现建议粉碎的文件")
                self._set_status("扫描完成，未发现建议粉碎的文件")

        self.app.run_async(work, on_done=on_done)

    def _on_privacy_click(self, event):
        """点击第 1 列切换 ✓ 勾选"""
        try:
            if self.privacy_tree.identify_region(event.x, event.y) != "cell":
                return
            if self.privacy_tree.identify_column(event.x) != "#1":
                return
            item = self.privacy_tree.identify_row(event.y)
            if item:
                cur = self.privacy_tree.set(item, "sel")
                self.privacy_tree.set(item, "sel", "" if cur == "✓" else "✓")
        except Exception:
            pass

    def _toggle_privacy_select_all(self):
        try:
            val = "✓" if self.privacy_select_all.get() else ""
            for iid in self.privacy_tree.get_children():
                self.privacy_tree.set(iid, "sel", val)
        except Exception:
            pass

    def _shred_privacy_selected(self):
        if self._busy:
            return
        if not self.privacy_rows:
            self._warn("提示", "请先扫描建议粉碎的文件。")
            return
        paths = []
        try:
            for iid in self.privacy_tree.get_children():
                if self.privacy_tree.set(iid, "sel") == "✓":
                    idx = self.privacy_tree.index(iid)
                    if 0 <= idx < len(self.privacy_rows):
                        paths.append(self.privacy_rows[idx].path)
        except Exception:
            paths = []
        if not paths:
            self._warn("提示", "请先勾选要粉碎的文件。")
            return
        if not self._confirm(
                "确认粉碎",
                f"将粉碎 {len(paths)} 个文件/目录，粉碎后将无法恢复，是否继续？"):
            return

        alg = self._current_alg(self.privacy_alg_combo, "dod")
        self._busy = True
        self._reset_progress(self.privacy_prog, self.privacy_prog_label)
        self.privacy_summary.config(text="正在粉碎选中项...")
        self._set_status("正在粉碎隐私文件...")

        def work(progress_cb):
            try:
                self.engine.progress_callback = progress_cb
                self.engine.reset_stop()
                return self.engine.shred_files(paths, algorithm=alg, dry_run=False)
            except Exception as e:
                return {"_error": str(e)}

        def on_progress(percent, msg):
            try:
                self.privacy_prog["value"] = percent
                self.privacy_prog_label.config(text=f"{percent}%")
            except Exception:
                pass

        def on_done(result):
            self._busy = False
            if not isinstance(result, dict):
                self._error("粉碎失败", "引擎未返回有效结果。")
                return
            if result.get("_error"):
                self._error("粉碎失败", result["_error"])
                return
            sc = result.get("success_count", 0)
            fc = result.get("fail_count", 0)
            total = result.get("total_bytes", 0)
            msg = f"粉碎完成：成功 {sc} 个，失败 {fc} 个，共 {format_size(total)}。"
            self.privacy_summary.config(text=msg)
            self._set_status(msg)
            self._info("粉碎结果", msg)
            # 重新扫描，刷新清单
            self._scan_privacy()

        self.app.run_async(work, on_done=on_done, on_progress=on_progress)

    # ==================== 停止 ====================

    def _stop_engine(self):
        try:
            self.engine.stop()
            self._set_status("已请求停止，当前操作会在完成当前文件/数据块后中断")
        except Exception as e:
            self._error("停止失败", str(e))


def build(app, parent):
    """页面入口：主程序以 build(app, parent) 调用"""
    return ShredderPage(app, parent)
