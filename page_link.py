# -*- coding: utf-8 -*-
"""
设备协同页面（配合 Android App）

与「手机互传」页的区别：
  · 手机互传：手机用浏览器访问，零安装，适合临时传文件
  · 设备协同：手机装 App，能做事后剪贴板同步、通知转发这类浏览器做不到的事

本页负责：开启服务、显示配对码、查看已连接设备、剪贴板同步、通知列表
"""

import os
import time
import tkinter as tk
from tkinter import ttk

from scanner import format_size


def build(app, parent):
    return LinkPage(app, parent).build()


class LinkPage:
    def __init__(self, app, parent):
        self.app = app
        self.parent = parent
        self._refresh_job = None
        self._alive = True
        self._last_notif = -1
        self._last_clip = -1
        # 与「手机互传」页共用同一个传输引擎与协同服务
        try:
            import page_transfer
            self.transfer = page_transfer.get_shared_engine()
        except Exception:
            self.transfer = None
        self.link = None

    # ==================== 界面 ====================

    def build(self):
        T = self.app.THEME

        self.app.section_title(
            self.parent, "设备协同",
            "手机装 App 后可与电脑实时互联：同一 Wi-Fi 下会自动出现在「可连接的设备」里，"
            "选中后需对方点「允许」才建立连接。不同网络时用手动地址 + 配对码连接。")

        # 服务卡片
        card = self.app.card(self.parent, pady=(0, 8))
        inner = tk.Frame(card, bg=T.CARD_BG)
        inner.pack(fill=tk.X, padx=16, pady=14)

        left = tk.Frame(inner, bg=T.CARD_BG)
        left.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        self.status = tk.Label(left, text="协同服务未启动", bg=T.CARD_BG,
                               fg=T.TEXT_LIGHT, font=("Microsoft YaHei", 11, "bold"),
                               anchor=tk.W)
        self.status.pack(fill=tk.X, pady=(0, 8))

        btn_row = tk.Frame(left, bg=T.CARD_BG)
        btn_row.pack(fill=tk.X, pady=(0, 10))
        self.toggle_btn = self.app.button(btn_row, "▶ 开启协同服务", self.toggle)
        self.toggle_btn.pack(side=tk.LEFT)
        self.app.button(btn_row, "🔄 换配对码", self.refresh_code,
                        kind="ghost").pack(side=tk.LEFT, padx=8)
        self.app.button(btn_row, "📂 打开接收目录", self.open_folder,
                        kind="ghost").pack(side=tk.LEFT)

        # 配对码
        code_row = tk.Frame(left, bg=T.CARD_BG)
        code_row.pack(fill=tk.X, pady=(4, 0))
        tk.Label(code_row, text="配对码：", bg=T.CARD_BG, fg=T.TEXT,
                 font=("Microsoft YaHei", 10)).pack(side=tk.LEFT)
        self.code_label = tk.Label(code_row, text="------", bg=T.CARD_BG,
                                    fg=T.PRIMARY, font=("Consolas", 26, "bold"))
        self.code_label.pack(side=tk.LEFT, padx=(4, 12))
        tk.Label(code_row, text="不在同一网络时：手机 App 手动输入地址后，填这 6 位配对码",
                 bg=T.CARD_BG, fg=T.TEXT_LIGHT,
                 font=("Microsoft YaHei", 9)).pack(side=tk.LEFT)

        # 右侧：连接状态（只显示"已连接"，不显示已配对数量）
        right = tk.Frame(inner, bg=T.CARD_BG)
        right.pack(side=tk.RIGHT, padx=(20, 0))
        self.device_label = tk.Label(right, text="已连接设备：0", bg=T.CARD_BG,
                                      fg=T.ACCENT, font=("Microsoft YaHei", 10, "bold"),
                                      anchor=tk.E)
        self.device_label.pack(anchor=tk.E)
        tk.Label(right, text="右键设备可断开 / 调试", bg=T.CARD_BG,
                 fg=T.TEXT_LIGHT, font=("Microsoft YaHei", 8),
                 anchor=tk.E).pack(anchor=tk.E)

        # 已连接设备明细：多台手机可以同时连同一台电脑
        self._devices_shown = False
        self.device_tree, self.device_table = self.app.make_table(
            card, ("device", "addr", "dur"), (180, 150, 100),
            headings=("已连接设备", "地址", "已连接时长"), height=3)
        self.device_tree.bind("<Button-3>", self._on_device_menu)
        self.device_table.pack_forget()

        # ============ 可连接的设备（同一 Wi-Fi 自动发现，选中后需对方同意） ============
        self._disc_sig = None
        self._req_dialog_open = False
        disc_card = self.app.card(self.parent, pady=(0, 8))
        disc_head = tk.Frame(disc_card, bg=T.CARD_BG)
        disc_head.pack(fill=tk.X, padx=16, pady=(10, 4))

        tk.Label(disc_head, text="📶 可连接的设备", bg=T.CARD_BG, fg=T.TEXT,
                 font=("Microsoft YaHei", 10, "bold")).pack(side=tk.LEFT)
        self.disc_hint = tk.Label(
            disc_head, text="同一 Wi-Fi 下打开手机 App 即可出现在这里",
            bg=T.CARD_BG, fg=T.TEXT_LIGHT, font=("Microsoft YaHei", 9))
        self.disc_hint.pack(side=tk.LEFT, padx=10)

        self.app.button(disc_head, "请求连接", self._request_connect).pack(side=tk.RIGHT)
        self.app.button(disc_head, "刷新", self._refresh_discoverable,
                        kind="ghost").pack(side=tk.RIGHT, padx=8)

        self.disc_tree, self.disc_table = self.app.make_table(
            disc_card, ("name", "addr", "state"), (200, 150, 100),
            headings=("设备", "地址", "状态"), height=3)
        self.disc_table.pack(fill=tk.X, padx=16, pady=(0, 10))
        self.disc_tree.bind("<Double-1>", lambda _e: self._request_connect())

        # 剪贴板同步卡片
        clip_card = self.app.card(self.parent, pady=(0, 8))
        clip_inner = tk.Frame(clip_card, bg=T.CARD_BG)
        clip_inner.pack(fill=tk.X, padx=16, pady=12)

        row1 = tk.Frame(clip_inner, bg=T.CARD_BG)
        row1.pack(fill=tk.X, pady=(0, 8))
        tk.Label(row1, text="📋 剪贴板同步", bg=T.CARD_BG, fg=T.TEXT,
                 font=("Microsoft YaHei", 10, "bold")).pack(side=tk.LEFT)
        self.clip_var = tk.BooleanVar(value=True)
        tk.Checkbutton(row1, text="启用", variable=self.clip_var,
                       command=self.on_clip_toggle, bg=T.CARD_BG, fg=T.TEXT,
                       activebackground=T.CARD_BG,
                       font=("Microsoft YaHei", 9)).pack(side=tk.LEFT, padx=10)

        row2 = tk.Frame(clip_inner, bg=T.CARD_BG)
        row2.pack(fill=tk.X)
        self.clip_entry = tk.Entry(row2, font=("Microsoft YaHei", 9))
        self.clip_entry.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.app.button(row2, "发送到手机", self.push_clipboard).pack(
            side=tk.LEFT, padx=(8, 0))
        self.app.button(row2, "取当前剪贴板", self.grab_clipboard,
                        kind="ghost").pack(side=tk.LEFT, padx=6)

        self.app.hint(self.parent,
                      "说明：电脑上复制文字会自动同步到手机；手机上复制会写入电脑剪贴板。"
                      "手机端需已安装 App 并配对成功。浏览器访问的「手机互传」不做剪贴板同步。",
                      color=T.TEXT_LIGHT)

        # ============ 手机麦克风 ============
        mic_card = self.app.card(self.parent, pady=(0, 8))
        mic_inner = tk.Frame(mic_card, bg=T.CARD_BG)
        mic_inner.pack(fill=tk.X, padx=16, pady=12)

        mic_head = tk.Frame(mic_inner, bg=T.CARD_BG)
        mic_head.pack(fill=tk.X, pady=(0, 6))
        tk.Label(mic_head, text="🎤 手机麦克风（实时）", bg=T.CARD_BG, fg=T.TEXT,
                 font=("Microsoft YaHei", 10, "bold")).pack(side=tk.LEFT)
        self.mic_state = tk.Label(mic_head, text="待机（在手机端点「麦克风」）",
                                   bg=T.CARD_BG, fg=T.TEXT_LIGHT,
                                   font=("Microsoft YaHei", 9))
        self.mic_state.pack(side=tk.LEFT, padx=12)

        mic_row = tk.Frame(mic_inner, bg=T.CARD_BG)
        mic_row.pack(fill=tk.X)
        tk.Label(mic_row, text="声音接到：", bg=T.CARD_BG, fg=T.TEXT,
                 font=("Microsoft YaHei", 9)).pack(side=tk.LEFT)
        self.mic_dev_var = tk.StringVar(value="自动（推荐）")
        self.mic_combo = ttk.Combobox(mic_row, textvariable=self.mic_dev_var,
                                      width=34, state="readonly",
                                      font=("Microsoft YaHei", 9))
        self.mic_combo.pack(side=tk.LEFT, padx=(0, 8))
        self.mic_combo.bind("<<ComboboxSelected>>", self.on_mic_device_change)

        self.mic_route_hint = tk.Label(mic_inner, text="", bg=T.CARD_BG,
                                       fg=T.TEXT_LIGHT, wraplength=760,
                                       justify=tk.LEFT,
                                       font=("Microsoft YaHei", 9))
        self.mic_route_hint.pack(fill=tk.X, pady=(6, 0))

        self.app.hint(self.parent,
                      "手机当麦克风：手机采集的声音实时送到电脑，其它软件（微信/QQ/会议/游戏）"
                      "把麦克风选成提示里那个设备即可。声音不落盘，只做实时传输。",
                      color=T.TEXT_LIGHT)

        # 双栏：通知 / 剪贴板记录
        body = tk.Frame(self.parent, bg=T.BG)
        body.pack(fill=tk.BOTH, expand=True, padx=20, pady=(0, 10))

        ntf_card = tk.Frame(body, bg=T.CARD_BG, bd=0,
                            highlightbackground=T.BORDER, highlightthickness=1)
        ntf_card.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(0, 6))
        tk.Label(ntf_card, text="🔔 手机通知", bg=T.CARD_BG, fg=T.TEXT,
                 font=("Microsoft YaHei", 10, "bold")).pack(anchor=tk.W,
                                                            padx=12, pady=(10, 4))
        self.notif_tree, _ = self.app.make_table(
            ntf_card, ("time", "app", "title", "text"), (70, 90, 110, 200),
            headings=("时间", "应用", "标题", "内容"), height=9)
        self.app.button(ntf_card, "清空通知", self.clear_notif,
                        kind="ghost").pack(anchor=tk.W, padx=12, pady=(4, 10))

        log_card = tk.Frame(body, bg=T.CARD_BG, bd=0,
                            highlightbackground=T.BORDER, highlightthickness=1)
        log_card.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(6, 0))
        tk.Label(log_card, text="📋 剪贴板同步记录", bg=T.CARD_BG, fg=T.TEXT,
                 font=("Microsoft YaHei", 10, "bold")).pack(anchor=tk.W,
                                                            padx=12, pady=(10, 4))
        self.clip_tree, _ = self.app.make_table(
            log_card, ("time", "dir", "ok", "preview"), (70, 90, 60, 240),
            headings=("时间", "方向", "状态", "内容预览"), height=9)
        tk.Label(log_card, text="双击记录可写回剪贴板", bg=T.CARD_BG,
                 fg=T.TEXT_LIGHT, font=("Microsoft YaHei", 8)).pack(
            anchor=tk.W, padx=12, pady=(2, 10))

        self.parent.bind("<Destroy>", self._on_destroy, add="+")
        # 从别的页面切回来时，服务可能已经在运行：先挂上共享实例再刷状态，
        # 否则会误显示"未启动"（两个页面必须是一体的）
        self._ensure_link()
        self._refresh_mic_devices()
        self._schedule_refresh()
        self._sync_ui()
        self._refresh_discoverable()
        return self

    # ==================== 服务控制 ====================

    def _ensure_link(self):
        """确保传输引擎与协同服务都已创建并互相挂接"""
        try:
            import page_transfer
            if self.transfer is None:
                self.transfer = page_transfer.get_shared_engine()
            self.link = page_transfer.get_shared_link()
            self.transfer.link = self.link
            return self.link
        except Exception as e:
            self.app.show_error("初始化失败", str(e))
            return None

    def toggle(self):
        link = self._ensure_link()
        if link is None:
            return

        if link.is_running():
            link.stop()
            if self.transfer and self.transfer.is_running():
                self.transfer.stop()
            self._sync_ui()
            self.app.set_status("协同服务已停止")
            return

        # 先启动 HTTP（App 的文件传输与配对都走它），再启动协同
        if not self.transfer.is_running():
            ok, msg = self.transfer.start()
            if not ok:
                self.app.show_error("启动失败", msg)
                return
        ok, msg = link.start()
        if not ok:
            self.app.show_error("启动失败", msg)
            return
        self._sync_ui()
        self.app.set_status(msg)

    def refresh_code(self):
        link = self._ensure_link()
        if link is None:
            return
        code = link.refresh_pair_code()
        self.code_label.config(text=code)
        self.app.set_status("配对码已更新")

    def open_folder(self):
        target = self.transfer.save_dir if self.transfer else ""
        if not target:
            return
        try:
            os.makedirs(target, exist_ok=True)
            os.startfile(target)
        except Exception as e:
            self.app.show_error("打开失败", str(e))

    def _sync_ui(self):
        T = self.app.THEME
        link = self.link
        if link is None:
            self.status.config(text="协同服务未启动", fg=T.TEXT_LIGHT)
            self.toggle_btn.config(text="▶ 开启协同服务", bg=T.PRIMARY, fg="white")
            return

        st = link.get_status()
        if st["running"]:
            self.status.config(
                text="✓ 协同服务运行中（局域网内手机可发现本机）", fg=T.ACCENT)
            self.toggle_btn.config(text="⏹ 停止服务", bg=T.DANGER, fg="white")
        else:
            self.status.config(text="协同服务未启动", fg=T.TEXT_LIGHT)
            self.toggle_btn.config(text="▶ 开启协同服务", bg=T.PRIMARY, fg="white")

        self.code_label.config(text=st["pair_code"])
        devices = st.get("devices") or []
        self.device_label.config(text="已连接设备：%d" % len(devices))
        # 只显示"已连接"，不再展示已配对数量
        for row in self.device_tree.get_children():
            self.device_tree.delete(row)
        now = time.time()
        for d in devices:
            secs = int(max(0, now - float(d.get("ts") or now)))
            if secs >= 3600:
                dur = "%d:%02d:%02d" % (secs // 3600, secs % 3600 // 60, secs % 60)
            else:
                dur = "%02d:%02d" % (secs // 60, secs % 60)
            self.device_tree.insert("", tk.END, iid=str(d.get("sid")), values=(
                d.get("device", "未知设备"), d.get("addr", ""), dur))
        if devices and not self._devices_shown:
            self.device_table.pack(fill=tk.X, expand=False, padx=16, pady=(0, 10))
            self._devices_shown = True
        elif not devices and self._devices_shown:
            self.device_table.pack_forget()
            self._devices_shown = False

    # ==================== 可连接设备（发现 + 双向确认） ====================

    def _refresh_discoverable(self):
        """刷新「可连接的设备」列表（只在设备集合变化时重建，避免闪烁）"""
        link = self.link
        if link is None:
            return
        try:
            items = link.discoverable_devices()
        except Exception:
            items = []
        sig = tuple((d.get("id"), d.get("name"), d.get("addr")) for d in items)
        if sig == self._disc_sig:
            return
        self._disc_sig = sig
        for row in self.disc_tree.get_children():
            self.disc_tree.delete(row)
        for d in items:
            self.disc_tree.insert("", tk.END, iid=str(d.get("id")),
                                  values=(d.get("name", "手机"),
                                          d.get("addr", ""), "在线"))
        if items:
            self.disc_hint.config(
                text="选中设备后点「请求连接」，手机上点「允许」即可")
        elif link.is_running():
            self.disc_hint.config(
                text="暂未发现设备：请确认手机 App 已打开，且与本机在同一 Wi-Fi")
        else:
            self.disc_hint.config(text="同一 Wi-Fi 下打开手机 App 即可出现在这里")

    def _request_connect(self):
        """电脑主动请求连接选中的手机（手机确认后才会连上）"""
        link = self._ensure_link()
        if link is None:
            return
        sel = self.disc_tree.selection()
        if not sel:
            self.app.show_warning("提示", "请先在「可连接的设备」里选一台手机")
            return
        ok, msg = link.request_connect(sel[0])
        self.app.set_status(msg)
        if not ok:
            self.app.show_warning("无法连接", msg)
            return
        self.disc_hint.config(text=msg)
        self._sync_ui()

    def _poll_connect_requests(self):
        """处理手机的连接请求：弹确认框；同时展示请求结果"""
        link = self.link
        if link is None:
            return
        try:
            for ev in link.pop_connect_events():
                text = ev.get("text") or ""
                self.app.set_status(text)
                self.disc_hint.config(
                    text=text,
                    fg=self.app.THEME.ACCENT if ev.get("ok") else self.app.THEME.WARNING)
        except Exception:
            pass

        if self._req_dialog_open:
            return
        try:
            reqs = link.pending_connect_requests()
        except Exception:
            return
        if not reqs:
            return
        req = reqs[0]
        self._req_dialog_open = True
        try:
            allow = self.app.ask_yes_no(
                "设备请求连接",
                "「%s」（%s）请求连接这台电脑。\n\n"
                "允许后它会出现在「已连接设备」里，可以互发消息和文件。\n\n"
                "是否允许？" % (req.get("device", "手机"), req.get("addr", "")))
        finally:
            self._req_dialog_open = False
        ok, msg = link.respond_connect_request(req.get("reqId"), bool(allow))
        self.app.set_status(msg)
        self._refresh_discoverable()
        self._sync_ui()

    # ==================== 设备右键菜单 ====================

    def _on_device_menu(self, event):
        row = self.device_tree.identify_row(event.y)
        if not row:
            return
        self.device_tree.selection_set(row)
        values = self.device_tree.item(row, "values") or ()
        name = values[0] if values else "设备"
        addr = values[1] if len(values) > 1 else ""
        menu = tk.Menu(self.parent, tearoff=0, font=("Microsoft YaHei", 9))
        menu.add_command(label="查看详情",
                         command=lambda: self._device_info(row, name, addr))
        menu.add_command(label="发送测试消息",
                         command=lambda: self._device_test(row, name))
        menu.add_command(label="复制地址",
                         command=lambda: self._copy_device_addr(addr))
        menu.add_separator()
        menu.add_command(label="断开连接",
                         command=lambda: self._device_kick(row, name))
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            try:
                menu.grab_release()
            except Exception:
                pass

    def _device_snapshot(self, sid):
        link = self.link
        if link is None:
            return None
        for d in link.connected_clients:
            if str(d.get("sid")) == str(sid):
                return d
        return None

    def _device_info(self, sid, name, addr):
        d = self._device_snapshot(sid) or {}
        secs = int(max(0, time.time() - float(d.get("ts") or time.time())))
        self.app.show_info(
            "设备详情",
            "设备：%s\n地址：%s\n已连接：%d 分 %d 秒\n连接 ID：%s\n\n"
            "提示：右键菜单里可以发送调试消息或强制断开。"
            % (name, addr or d.get("addr", ""), secs // 60, secs % 60, sid))

    def _device_test(self, sid, name):
        link = self._ensure_link()
        if link is None:
            return
        try:
            ok = link.send_to_session(
                int(sid),
                {"type": "text", "text": "[调试] 来自电脑的测试消息",
                 "from": "pc", "ts": int(time.time() * 1000)})
        except Exception:
            ok = False
        self.app.set_status("测试消息已发往 %s" % name if ok
                            else "发送失败：%s 可能已断开" % name)

    def _copy_device_addr(self, addr):
        if not addr:
            return
        try:
            self.app.root.clipboard_clear()
            self.app.root.clipboard_append(addr)
            self.app.set_status("已复制地址：%s" % addr)
        except Exception:
            pass

    def _device_kick(self, sid, name):
        link = self._ensure_link()
        if link is None:
            return
        if not self.app.ask_yes_no(
                "断开设备", "确定要断开「%s」吗？\n断开后它需要重新连接。" % name):
            return
        try:
            ok = link.kick_session(int(sid))
        except Exception:
            ok = False
        self.app.set_status("已断开 %s" % name if ok else "该设备已不在线")
        self._sync_ui()

    # ==================== 剪贴板 ====================

    def on_clip_toggle(self):
        link = self._ensure_link()
        if link is None:
            return
        link.clipboard_sync_enabled = bool(self.clip_var.get())
        self.app.set_status("剪贴板同步已" + ("开启" if self.clip_var.get() else "关闭"))

    def push_clipboard(self):
        link = self._ensure_link()
        if link is None:
            return
        text = self.clip_entry.get().strip()
        if not text:
            self.app.show_warning("提示", "请先输入内容")
            return
        ok, msg = link.push_clipboard_to_phones(text)
        self.app.set_status(msg)

    def grab_clipboard(self):
        try:
            import link_service
            text = link_service.get_clipboard_text() or ""
            self.clip_entry.delete(0, tk.END)
            self.clip_entry.insert(0, text)
            self.app.set_status("已读取当前剪贴板")
        except Exception as e:
            self.app.show_error("读取失败", str(e))

    def clear_notif(self):
        if self.link:
            self.link.notifications = []
            self._last_notif = -1
            self._refresh_lists()

    # ==================== 手机麦克风 ====================

    def _refresh_mic_devices(self):
        """填充输出设备；第一项是自动选择最佳虚拟麦克风通路"""
        if not hasattr(self, "mic_combo"):
            return
        auto_label = "自动（推荐）"
        items = [auto_label, "不输出（只听不到）"]
        self._mic_dev_map = {auto_label: "auto", items[1]: None}
        try:
            import mic_receiver
            for d in mic_receiver.list_output_devices():
                label = d["name"]
                items.append(label)
                self._mic_dev_map[label] = d["index"]
        except Exception:
            pass
        try:
            self.mic_combo["values"] = items
            if self.mic_dev_var.get() not in items:
                self.mic_dev_var.set(auto_label)
        except Exception:
            pass
        self._update_mic_route_hint()

    def _update_mic_route_hint(self):
        if not hasattr(self, "mic_route_hint"):
            return
        try:
            import mic_receiver
            route = mic_receiver.detect_mic_route()
        except Exception as e:
            route = {"kind": "none", "note": "音频模块不可用：%s" % e}
        self._mic_route = route
        if self.mic_dev_var.get() == "不输出（只听不到）":
            text = "当前选择不输出：手机声音不会送到电脑声卡。"
            color = self.app.THEME.TEXT_LIGHT
        elif route.get("kind") == "cable":
            text = "✅ " + route.get("note", "")
            color = self.app.THEME.ACCENT
        elif route.get("kind") == "stereo_mix":
            text = "⚠ " + route.get("note", "")
            color = "#d46b08"
        else:
            text = "✗ " + route.get("note", "")
            color = self.app.THEME.DANGER
        try:
            self.mic_route_hint.config(text=text, fg=color)
        except Exception:
            pass

    def on_mic_device_change(self, _event=None):
        label = self.mic_dev_var.get()
        link = self._ensure_link()
        if link is None or link.mic is None:
            return
        device = getattr(self, "_mic_dev_map", {}).get(label)
        ok, msg = link.mic.set_output_device(device)
        self._update_mic_route_hint()
        self.app.set_status(msg)

    def _refresh_mic_status(self):
        if not hasattr(self, "mic_state"):
            return
        link = self.link
        if link is None or getattr(link, "mic", None) is None:
            self.mic_state.config(text="音频模块不可用", fg=self.app.THEME.TEXT_LIGHT)
            return
        try:
            st = link.mic.status()
        except Exception:
            return
        if st.get("active"):
            secs = st.get("seconds", 0)
            kb = st.get("bytes", 0) / 1024.0
            if st.get("playing"):
                target = (st.get("route") or {}).get("mic", "")
                txt = "● 已接入 · %.1f 秒 · %.0f KB" % (secs, kb)
                if target:
                    txt += " · 麦克风选「%s」" % target
                else:
                    txt += " · 实时输出中"
                self.mic_state.config(text=txt, fg=self.app.THEME.ACCENT)
            else:
                txt = "● 已接入 · %.1f 秒 · %.0f KB（未输出声音）" % (secs, kb)
                self.mic_state.config(text=txt, fg=self.app.THEME.WARNING)
        else:
            self.mic_state.config(text="待机（在手机端点「麦克风」）",
                                  fg=self.app.THEME.TEXT_LIGHT)

    # ==================== 刷新 ====================

    def _schedule_refresh(self):
        if not self._alive:
            return
        try:
            self._refresh_lists()
            self._sync_ui()
            self._refresh_discoverable()
            self._poll_connect_requests()
            self._refresh_mic_status()
        except Exception:
            pass
        self._refresh_job = self.app.root.after(1500, self._schedule_refresh)

    def _refresh_lists(self):
        link = self.link
        if link is None:
            return

        notifs = link.recent_notifications(60)
        if len(notifs) != self._last_notif:
            self._last_notif = len(notifs)
            for row in self.notif_tree.get_children():
                self.notif_tree.delete(row)
            for n in notifs:
                self.notif_tree.insert("", tk.END, values=(
                    n["time"], n["app"], n["title"][:20], n["text"][:60]))

        clips = link.recent_clipboard(60)
        if len(clips) != self._last_clip:
            self._last_clip = len(clips)
            for row in self.clip_tree.get_children():
                self.clip_tree.delete(row)
            for c in clips:
                self.clip_tree.insert("", tk.END, values=(
                    c["time"], c["dir"], "成功" if c["ok"] else "失败",
                    c["preview"][:60]))

    def _on_destroy(self, event):
        if event.widget is not self.parent:
            return
        self._alive = False
        if self._refresh_job:
            try:
                self.app.root.after_cancel(self._refresh_job)
            except Exception:
                pass
            self._refresh_job = None
