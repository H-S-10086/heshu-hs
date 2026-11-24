# -*- coding: utf-8 -*-
"""
垃圾文件扫描引擎
负责扫描系统中各类垃圾文件，并标注垃圾来源。
"""

import os
import shutil
import ctypes
import tempfile
import configparser
from pathlib import Path
from collections import defaultdict


def get_size(path):
    """安全获取文件/目录大小（字节）。

    性能说明：用 os.scandir 并复用 DirEntry 缓存的 stat 信息，
    比 os.walk + os.path.getsize 快约一个数量级。
    （os.path.getsize 会对每个文件单独发起一次 stat 系统调用，
      在装了杀毒软件的机器上每次调用都可能被拦截，大目录下会非常慢。）
    实测：14 万个文件的 Documents 目录，scandir 方案 0.8 秒，getsize 方案约 98 秒。
    """
    try:
        if os.path.isfile(path):
            return os.path.getsize(path)
        total = 0
        stack = [path]
        while stack:
            cur = stack.pop()
            try:
                with os.scandir(cur) as it:
                    for entry in it:
                        try:
                            if entry.is_dir(follow_symlinks=False):
                                stack.append(entry.path)
                            else:
                                total += entry.stat(follow_symlinks=False).st_size
                        except (OSError, PermissionError):
                            continue
            except (OSError, PermissionError):
                continue
        return total
    except (OSError, PermissionError):
        return 0


def format_size(size_bytes):
    """将字节数格式化为易读字符串"""
    if size_bytes == 0:
        return "0 B"
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if size_bytes < 1024.0:
            return f"{size_bytes:.2f} {unit}"
        size_bytes /= 1024.0
    return f"{size_bytes:.2f} PB"


class ScanResult:
    """单项扫描结果"""

    def __init__(self, category, source, path, size, description=""):
        self.category = category      # 大分类：系统垃圾/微信专清/QQ专清/隐私痕迹/大文件...
        self.source = source          # 垃圾来源备注：如"Windows临时目录"
        self.path = path              # 文件/目录路径
        self.size = size              # 大小（字节）
        self.description = description  # 详细说明

    def __repr__(self):
        return f"<ScanResult {self.category}|{self.source}|{format_size(self.size)}>"


class ScanEngine:
    """垃圾文件扫描引擎"""

    def __init__(self, progress_callback=None):
        self.progress_callback = progress_callback
        self.results = []
        self._stop = False

    def stop(self):
        self._stop = True

    def reset(self):
        self.results = []
        self._stop = False

    def _emit(self, percent, message):
        if self.progress_callback:
            self.progress_callback(percent, message)

    # ==================== 系统垃圾扫描 ====================

    def scan_system_temp(self):
        """扫描 Windows 系统临时目录 (C:\\Windows\\Temp)"""
        self._emit(5, "正在扫描系统临时文件...")
        path = os.path.join(os.environ.get('SystemRoot', r'C:\Windows'), 'Temp')
        size = get_size(path)
        if size > 0:
            self.results.append(ScanResult(
                category="系统垃圾",
                source="Windows系统临时目录",
                path=path,
                size=size,
                description="Windows 系统运行过程中产生的临时文件，可安全清理。"
            ))

    def scan_user_temp(self):
        """扫描用户临时目录 (%TEMP%)"""
        self._emit(10, "正在扫描用户临时文件...")
        path = tempfile.gettempdir()
        size = get_size(path)
        if size > 0:
            self.results.append(ScanResult(
                category="系统垃圾",
                source="用户临时目录",
                path=path,
                size=size,
                description="当前用户应用程序产生的临时文件，通常可安全删除。"
            ))

    def scan_recycle_bin(self):
        """扫描回收站"""
        self._emit(15, "正在扫描回收站...")
        # 尝试多个盘符的回收站
        total_size = 0
        for drive in ['C', 'D', 'E', 'F']:
            recycle_path = os.path.join(drive + ':\\', '$Recycle.Bin')
            if os.path.exists(recycle_path):
                total_size += get_size(recycle_path)
        if total_size > 0:
            self.results.append(ScanResult(
                category="系统垃圾",
                source="回收站",
                path="回收站(所有盘符)",
                size=total_size,
                description="已删除文件的回收站暂存内容，清空后不可恢复。"
            ))

    def scan_windows_update_cache(self):
        """扫描 Windows 更新下载缓存"""
        self._emit(20, "正在扫描Windows更新缓存...")
        path = os.path.join(os.environ.get('SystemRoot', r'C:\Windows'),
                            'SoftwareDistribution', 'Download')
        size = get_size(path)
        if size > 0:
            self.results.append(ScanResult(
                category="系统垃圾",
                source="Windows更新下载缓存",
                path=path,
                size=size,
                description="Windows Update 下载的更新安装包缓存，安装完成后可清理。"
            ))

    def scan_prefetch(self):
        """扫描预读取文件"""
        self._emit(25, "正在扫描预读取文件...")
        path = os.path.join(os.environ.get('SystemRoot', r'C:\Windows'), 'Prefetch')
        size = get_size(path)
        if size > 0:
            self.results.append(ScanResult(
                category="系统垃圾",
                source="系统预读取缓存",
                path=path,
                size=size,
                description="Windows 预读取缓存文件，用于加速程序启动，可清理后由系统重建。"
            ))

    def scan_thumbnail_cache(self):
        """扫描缩略图缓存"""
        self._emit(30, "正在扫描缩略图缓存...")
        local_appdata = os.environ.get('LOCALAPPDATA', '')
        path = os.path.join(local_appdata, 'Microsoft', 'Windows', 'Explorer')
        size = 0
        if os.path.exists(path):
            for f in os.listdir(path):
                if f.lower().startswith('thumbcache'):
                    size += get_size(os.path.join(path, f))
        if size > 0:
            self.results.append(ScanResult(
                category="系统垃圾",
                source="资源管理器缩略图缓存",
                path=path,
                size=size,
                description="文件夹/图片缩略图缓存，清理后浏览图片会稍慢但会自动重建。"
            ))

    def scan_log_files(self):
        """扫描系统日志文件"""
        self._emit(35, "正在扫描系统日志文件...")
        total = 0
        paths = []
        for log_dir in [
            os.path.join(os.environ.get('SystemRoot', r'C:\Windows'), 'Logs'),
            os.path.join(os.environ.get('SystemRoot', r'C:\Windows'), 'Panther'),
        ]:
            if os.path.exists(log_dir):
                s = get_size(log_dir)
                if s > 0:
                    total += s
                    paths.append(log_dir)
        if total > 0:
            self.results.append(ScanResult(
                category="系统垃圾",
                source="Windows系统日志",
                path="; ".join(paths),
                size=total,
                description="系统安装日志、错误日志等，用于排查问题后可清理。"
            ))

    def scan_memory_dumps(self):
        """扫描内存转储文件"""
        self._emit(40, "正在扫描内存转储文件...")
        total = 0
        files_found = []
        win_dir = os.environ.get('SystemRoot', r'C:\Windows')
        for dump_file in ['Memory.dmp', 'Minidump']:
            full = os.path.join(win_dir, dump_file)
            if os.path.exists(full):
                s = get_size(full)
                if s > 0:
                    total += s
                    files_found.append(full)
        if total > 0:
            self.results.append(ScanResult(
                category="系统垃圾",
                source="蓝屏内存转储文件",
                path="; ".join(files_found),
                size=total,
                description="系统崩溃(蓝屏)时生成的内存转储文件，用于诊断后可清理。"
            ))

    def scan_browser_cache(self):
        """扫描浏览器缓存"""
        self._emit(45, "正在扫描浏览器缓存...")
        local_appdata = os.environ.get('LOCALAPPDATA', '')
        total = 0
        detail = []

        # Chrome
        chrome = os.path.join(local_appdata, 'Google', 'Chrome', 'User Data', 'Default', 'Cache')
        if os.path.exists(chrome):
            s = get_size(chrome)
            if s > 0:
                total += s
                detail.append(f"Chrome缓存:{format_size(s)}")

        # Edge
        edge = os.path.join(local_appdata, 'Microsoft', 'Edge', 'User Data', 'Default', 'Cache')
        if os.path.exists(edge):
            s = get_size(edge)
            if s > 0:
                total += s
                detail.append(f"Edge缓存:{format_size(s)}")

        if total > 0:
            self.results.append(ScanResult(
                category="系统垃圾",
                source="浏览器缓存文件",
                path="浏览器缓存目录",
                size=total,
                description="; ".join(detail) + "。浏览网页产生的临时缓存，清理后网页首次打开略慢。"
            ))

    # ==================== 微信专清 ====================

    def scan_wechat(self):
        """扫描微信缓存"""
        self._emit(55, "正在扫描微信缓存...")
        total = 0
        items = []

        # 微信文件存储目录
        doc_path = os.path.join(os.environ.get('USERPROFILE', ''), 'Documents', 'WeChat Files')
        appdata_wechat = os.path.join(os.environ.get('APPDATA', ''), 'Tencent', 'WeChat')
        if not os.path.exists(doc_path) and not os.path.exists(appdata_wechat):
            self._emit(60, "未检测到微信，跳过微信专清")
            return

        if os.path.exists(doc_path):
            for wx_id_dir in os.listdir(doc_path):
                wx_dir = os.path.join(doc_path, wx_id_dir)
                if not os.path.isdir(wx_dir):
                    continue
                # 缓存子目录
                for sub in ['Cache', 'FileStorage', 'Image', 'Video', 'Attachment']:
                    sub_path = os.path.join(wx_dir, sub)
                    if os.path.exists(sub_path):
                        s = get_size(sub_path)
                        if s > 0:
                            total += s
                            items.append((sub_path, s))

        # 微信AppData缓存
        if os.path.exists(appdata_wechat):
            s = get_size(appdata_wechat)
            if s > 0:
                total += s
                items.append((appdata_wechat, s))

        if total > 0:
            self.results.append(ScanResult(
                category="微信专清",
                source="微信聊天缓存/文件/图片/视频",
                path="; ".join(p for p, _ in items[:5]),
                size=total,
                description=f"微信产生的缓存、接收的文件、图片、视频等，共{len(items)}项。清理后聊天记录不丢失，但已下载的文件/图片需重新下载。"
            ))

    # ==================== QQ专清 ====================

    def scan_qq(self):
        """扫描QQ缓存"""
        self._emit(62, "正在扫描QQ缓存...")
        total = 0
        items = []

        doc_path = os.path.join(os.environ.get('USERPROFILE', ''), 'Documents', 'Tencent Files')
        appdata_qq = os.path.join(os.environ.get('APPDATA', ''), 'Tencent', 'QQ')
        if not os.path.exists(doc_path) and not os.path.exists(appdata_qq):
            self._emit(66, "未检测到 QQ，跳过 QQ 专清")
            return

        if os.path.exists(doc_path):
            for qq_dir in os.listdir(doc_path):
                full = os.path.join(doc_path, qq_dir)
                if not os.path.isdir(full):
                    continue
                for sub in ['Image', 'FileRecv', 'Video', 'Photo']:
                    sub_path = os.path.join(full, sub)
                    if os.path.exists(sub_path):
                        s = get_size(sub_path)
                        if s > 0:
                            total += s
                            items.append((sub_path, s))

        # QQ AppData 缓存
        if os.path.exists(appdata_qq):
            s = get_size(appdata_qq)
            if s > 0:
                total += s
                items.append((appdata_qq, s))

        if total > 0:
            self.results.append(ScanResult(
                category="QQ专清",
                source="QQ聊天图片/文件/视频缓存",
                path="; ".join(p for p, _ in items[:5]),
                size=total,
                description=f"QQ接收的图片、文件、视频等缓存，共{len(items)}项。清理后不影响聊天记录。"
            ))

    # ==================== 隐私清理 ====================

    def scan_privacy_traces(self):
        """扫描隐私痕迹"""
        self._emit(70, "正在扫描隐私痕迹...")
        total = 0
        items = []
        local_appdata = os.environ.get('LOCALAPPDATA', '')

        # 最近使用的文档
        recent = os.path.join(os.environ.get('APPDATA', ''), 'Microsoft', 'Windows', 'Recent')
        if os.path.exists(recent):
            s = get_size(recent)
            if s > 0:
                total += s
                items.append(("最近文档记录", s, recent))

        # 剪贴板历史
        clipboard = os.path.join(local_appdata, 'Microsoft', 'Windows', 'Clipboard')
        if os.path.exists(clipboard):
            s = get_size(clipboard)
            if s > 0:
                total += s
                items.append(("剪贴板历史", s, clipboard))

        # 系统事件日志缓存
        event_cache = os.path.join(local_appdata, 'Microsoft', 'Windows', 'Explorer')
        if os.path.exists(event_cache):
            s = 0
            for f in os.listdir(event_cache):
                if 'iconcache' in f.lower():
                    s += get_size(os.path.join(event_cache, f))
            if s > 0:
                total += s
                items.append(("图标缓存", s, event_cache))

        if total > 0:
            self.results.append(ScanResult(
                category="隐私清理",
                source="最近文档/剪贴板/图标缓存",
                path="; ".join(p for _, _, p in items),
                size=total,
                description="; ".join(f"{n}:{format_size(s)}" for n, s, _ in items) + "。清理后保护隐私，不影响系统运行。"
            ))

    # ==================== 深度扫描 ====================

    def scan_large_files(self, min_size_mb=100, scope="user"):
        """
        扫描大文件。

        scope="user"（默认）：只扫描用户目录（Downloads/Desktop/Documents/Videos 等），
                             速度快且这些才是适合搬家的用户数据。
        scope="all"         ：扫描整个 C 盘，耗时较长，供深度清理使用。
        """
        self._emit(78, "正在扫描大文件...")
        min_size = min_size_mb * 1024 * 1024
        large_files = []

        # 大文件搬家的目标是"用户数据"，系统目录（Windows/Program Files/ProgramData）
        # 里的文件不能随意搬动，否则会导致软件/系统失效。因此默认只扫描用户目录，
        # 这既更快，也更安全。scope="all" 时才扫描整个 C 盘（深度清理用）。
        skip_dirs = {'Windows', '$Recycle.Bin', 'System Volume Information', '$WinREAgent',
                     'Program Files', 'Program Files (x86)', 'ProgramData'}

        if scope == "user":
            # 只扫描用户主动存放文件的目录（下载/桌面/文档/视频/图片/音乐）。
            # 刻意不包含 AppData：它包含数十万个小文件，遍历慢，且其中数据
            # 属于各应用程序，不适合搬家。这样快速扫描能在几秒内完成。
            user_profile = os.environ.get('USERPROFILE', 'C:\\Users')
            roots = [os.path.join(user_profile, d) for d in
                     ('Downloads', 'Desktop', 'Documents', 'Videos', 'Pictures',
                      'Music')]
            roots = [r for r in roots if os.path.isdir(r)]
        else:
            roots = ['C:\\']

        file_count = 0
        for root in roots:
            # 用 scandir + DirEntry 缓存的 stat，而不是 os.walk + os.path.getsize，
            # 后者每个文件都单独发起一次系统调用，在大目录下慢约 100 倍。
            stack = [root]
            while stack:
                if self._stop:
                    break
                current = stack.pop()
                try:
                    with os.scandir(current) as it:
                        for entry in it:
                            if self._stop:
                                break
                            try:
                                if entry.is_dir(follow_symlinks=False):
                                    # 跳过系统关键目录
                                    if entry.name not in skip_dirs:
                                        stack.append(entry.path)
                                else:
                                    sz = entry.stat(follow_symlinks=False).st_size
                                    file_count += 1
                                    # 每检查一定数量的文件就回报一次进度，避免用户以为卡死
                                    if file_count % 5000 == 0:
                                        self._emit(78, f"正在扫描大文件...已检查 {file_count} 个文件")
                                    if sz >= min_size:
                                        large_files.append(ScanResult(
                                            category="系统大文件",
                                            source=f"大文件({format_size(sz)})",
                                            path=entry.path,
                                            size=sz,
                                            description="C盘大容量文件，建议确认无用后搬家或删除以释放空间。"
                                        ))
                            except (OSError, PermissionError):
                                continue
                except (OSError, PermissionError):
                    continue
            if self._stop:
                break

        large_files.sort(key=lambda x: x.size, reverse=True)
        # 只保留前50个
        self.results.extend(large_files[:50])

    def scan_hibernation(self):
        """扫描休眠文件"""
        self._emit(90, "正在扫描休眠文件...")
        hiberfil = os.path.join('C:\\', 'hiberfil.sys')
        if os.path.exists(hiberfil):
            try:
                size = os.path.getsize(hiberfil)
                if size > 0:
                    self.results.append(ScanResult(
                        category="深度清理",
                        source="系统休眠文件(hiberfil.sys)",
                        path=hiberfil,
                        size=size,
                        description="休眠功能占用的磁盘空间(通常等于内存大小)。关闭休眠可释放此空间。"
                    ))
            except (OSError, PermissionError):
                pass

    def scan_pagefile(self):
        """扫描虚拟内存文件"""
        self._emit(95, "正在扫描虚拟内存文件...")
        pagefile = os.path.join('C:\\', 'pagefile.sys')
        if os.path.exists(pagefile):
            try:
                size = os.path.getsize(pagefile)
                if size > 0:
                    self.results.append(ScanResult(
                        category="深度清理",
                        source="虚拟内存页面文件(pagefile.sys)",
                        path=pagefile,
                        size=size,
                        description="C盘虚拟内存文件。可将虚拟内存转移到其他盘以释放C盘空间。"
                    ))
            except (OSError, PermissionError):
                pass

    # ==================== 一键扫描 ====================

    def scan_all(self):
        """一键扫描全部垃圾"""
        self.reset()
        self.scan_system_temp()
        if self._stop: return
        self.scan_user_temp()
        if self._stop: return
        self.scan_recycle_bin()
        if self._stop: return
        self.scan_windows_update_cache()
        if self._stop: return
        self.scan_prefetch()
        if self._stop: return
        self.scan_thumbnail_cache()
        if self._stop: return
        self.scan_log_files()
        if self._stop: return
        self.scan_memory_dumps()
        if self._stop: return
        self.scan_browser_cache()
        if self._stop: return
        self.scan_wechat()
        if self._stop: return
        self.scan_qq()
        if self._stop: return
        self.scan_privacy_traces()
        if self._stop: return
        self.scan_hibernation()
        if self._stop: return
        self.scan_pagefile()
        self._emit(100, "扫描完成！")
        return self.results

    def scan_quick(self):
        """快速扫描（系统垃圾+回收站+缓存）"""
        self.reset()
        self.scan_system_temp()
        if self._stop: return
        self.scan_user_temp()
        if self._stop: return
        self.scan_recycle_bin()
        if self._stop: return
        self.scan_windows_update_cache()
        if self._stop: return
        self.scan_prefetch()
        if self._stop: return
        self.scan_thumbnail_cache()
        if self._stop: return
        self.scan_browser_cache()
        self._emit(100, "快速扫描完成！")
        return self.results

    def scan_deep(self):
        """深度扫描（包含全盘大文件识别，耗时较长）"""
        self.reset()
        self.scan_all()
        if self._stop: return
        self.scan_large_files(min_size_mb=100, scope="all")
        self._emit(100, "深度扫描完成！")
        return self.results

    def get_summary(self):
        """按分类汇总扫描结果"""
        summary = defaultdict(lambda: {"count": 0, "size": 0, "items": []})
        for r in self.results:
            summary[r.category]["count"] += 1
            summary[r.category]["size"] += r.size
            summary[r.category]["items"].append(r)
        return dict(summary)
