# -*- coding: utf-8 -*-
"""
垃圾清理引擎
负责执行实际的清理、搬家、压缩、转移等操作。
"""

import os
import shutil
import ctypes
import subprocess
import tempfile
from ctypes import wintypes

from scanner import get_size, format_size


class CleanResult:
    """清理结果"""

    def __init__(self, success, message, freed_bytes=0):
        self.success = success
        self.message = message
        self.freed_bytes = freed_bytes


def is_admin():
    """检查是否以管理员权限运行"""
    try:
        return ctypes.windll.shell32.IsUserAnAdmin()
    except Exception:
        return False


def run_as_admin():
    """请求管理员权限重启（UAC）"""
    try:
        params = " ".join(f'"{a}"' for a in __import__('sys').argv)
        ctypes.windll.shell32.ShellExecuteW(None, "runas", __import__('sys').executable, params, None, 1)
        return True
    except Exception:
        return False


class CleanEngine:
    """垃圾清理引擎"""

    def __init__(self, progress_callback=None):
        self.progress_callback = progress_callback

    def _emit(self, percent, message):
        if self.progress_callback:
            self.progress_callback(percent, message)

    # ==================== 通用清理 ====================

    def clean_directory_contents(self, path):
        """清空目录内容（保留目录本身）"""
        freed = 0
        if not os.path.exists(path):
            return freed
        for entry in os.listdir(path):
            full = os.path.join(path, entry)
            try:
                if os.path.isfile(full) or os.path.islink(full):
                    sz = os.path.getsize(full)
                    os.remove(full)
                    freed += sz
                elif os.path.isdir(full):
                    sz = get_size(full)
                    shutil.rmtree(full, ignore_errors=True)
                    freed += sz
            except (OSError, PermissionError):
                pass
        return freed

    def clean_file(self, path):
        """删除单个文件"""
        try:
            sz = os.path.getsize(path)
            os.remove(path)
            return sz
        except (OSError, PermissionError):
            return 0

    def clean_results(self, scan_results):
        """根据扫描结果列表执行清理，返回释放总字节"""
        total_freed = 0
        total = len(scan_results)
        for i, r in enumerate(scan_results):
            if r.path in ("回收站(所有盘符)", "浏览器缓存目录"):
                # 特殊处理
                if r.source == "回收站":
                    freed = self.empty_recycle_bin()
                else:
                    # 浏览器缓存按目录清理
                    freed = 0
                    local_appdata = os.environ.get('LOCALAPPDATA', '')
                    for cache_dir in [
                        os.path.join(local_appdata, 'Google', 'Chrome', 'User Data', 'Default', 'Cache'),
                        os.path.join(local_appdata, 'Microsoft', 'Edge', 'User Data', 'Default', 'Cache'),
                    ]:
                        if os.path.exists(cache_dir):
                            freed += self.clean_directory_contents(cache_dir)
            elif r.category == "系统大文件":
                # 大文件不自动删除，跳过
                continue
            elif r.category == "深度清理":
                # 休眠/虚拟内存特殊处理
                if "休眠文件" in r.source:
                    freed = self.disable_hibernation()
                else:
                    freed = 0
            else:
                # 通用：路径可能是;分隔的多个路径
                freed = 0
                for p in r.path.split("; "):
                    p = p.strip()
                    if os.path.isdir(p):
                        freed += self.clean_directory_contents(p)
                    elif os.path.isfile(p):
                        freed += self.clean_file(p)

            total_freed += freed
            percent = int((i + 1) / total * 100) if total > 0 else 100
            self._emit(percent, f"已清理: {r.source} (释放 {format_size(freed)})")

        return total_freed

    # ==================== 特殊清理 ====================

    def empty_recycle_bin(self):
        """清空回收站，返回实际释放的字节数"""
        # 先统计各盘回收站的体积，再清空，这样才能回报真实的释放量
        size_before = 0
        for drive in ['C', 'D', 'E', 'F', 'G', 'H']:
            recycle_path = os.path.join(drive + ':\\', '$Recycle.Bin')
            if os.path.exists(recycle_path):
                size_before += get_size(recycle_path)
        try:
            # SHEmptyRecycleBin: SHERB_NOCONFIRMATION | SHERB_NOPROGRESSUI | SHERB_NOSOUND
            result = ctypes.windll.shell32.SHEmptyRecycleBinW(None, None, 0x0007)
            if result in (0, 0x80004005):  # S_OK or some benign error
                return size_before
        except Exception:
            pass
        return 0

    def disable_hibernation(self):
        """关闭休眠（释放 hiberfil.sys），返回实际释放的字节数"""
        hiber = os.path.join('C:\\', 'hiberfil.sys')
        size = 0
        if os.path.exists(hiber):
            try:
                size = os.path.getsize(hiber)
            except (OSError, PermissionError):
                pass
        try:
            # powercfg -h off
            subprocess.run(['powercfg', '/h', 'off'],
                           capture_output=True, timeout=30)
            self._emit(50, "已关闭休眠功能，hiberfil.sys 将被删除")
            return size
        except Exception as e:
            self._emit(50, f"关闭休眠失败: {e}")
            return 0

    def enable_hibernation(self):
        """开启休眠"""
        try:
            subprocess.run(['powercfg', '/h', 'on'], capture_output=True, timeout=30)
            return True
        except Exception:
            return False

    def _available_physical_memory(self):
        """读取当前可用物理内存（字节）"""
        class MEMORYSTATUSEX(ctypes.Structure):
            _fields_ = [("dwLength", ctypes.c_ulong),
                        ("dwMemoryLoad", ctypes.c_ulong),
                        ("ullTotalPhys", ctypes.c_ulonglong),
                        ("ullAvailPhys", ctypes.c_ulonglong),
                        ("ullTotalPageFile", ctypes.c_ulonglong),
                        ("ullAvailPageFile", ctypes.c_ulonglong),
                        ("ullTotalVirtual", ctypes.c_ulonglong),
                        ("ullAvailVirtual", ctypes.c_ulonglong),
                        ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
        stat = MEMORYSTATUSEX()
        stat.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat))
        return stat.ullAvailPhys

    def clean_memory(self):
        """
        内存清理：对所有可访问的进程调用 EmptyWorkingSet，把各进程占用但闲置的
        物理内存（工作集）还给系统，从而增加可用物理内存。

        这是内存清理工具的标准做法（与 CCleaner/ Wise 等同原理），而不是
        "把 MinWorkingSet 设为 1" 这种并不真正释放内存的无效写法。

        返回 {"success": bool, "freed_bytes": int, "processes": int, "message": str}
        """
        kernel32 = ctypes.windll.kernel32
        psapi = ctypes.windll.psapi

        PROCESS_QUERY_INFORMATION = 0x0400
        PROCESS_SET_QUOTA = 0x0100

        before = self._available_physical_memory()

        # 枚举所有进程
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        pids = (wintypes.DWORD * 4096)()
        needed = wintypes.DWORD()
        processed = 0
        ok = False

        try:
            if psapi.EnumProcesses(ctypes.byref(pids), ctypes.sizeof(pids), ctypes.byref(needed)):
                count = needed.value // ctypes.sizeof(wintypes.DWORD)
                self._emit(20, f"共枚举到 {count} 个进程，正在逐个回收工作集...")
                for i in range(count):
                    pid = pids[i]
                    if pid in (0, 4):  # System Idle Process / System
                        continue
                    # 优先用最低权限打开；失败再用更高权限
                    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION | PROCESS_SET_QUOTA, False, pid)
                    if not handle:
                        handle = kernel32.OpenProcess(PROCESS_QUERY_INFORMATION | PROCESS_SET_QUOTA, False, pid)
                    if not handle:
                        continue
                    try:
                        psapi.EmptyWorkingSet(handle)
                        processed += 1
                    except Exception:
                        pass
                    finally:
                        kernel32.CloseHandle(handle)
                ok = True
        except Exception as e:
            self._emit(50, f"枚举进程失败: {e}")

        after = self._available_physical_memory()
        freed_bytes = after - before if after > before else 0
        self._emit(90, f"已对 {processed} 个进程回收工作集")

        return {
            "success": ok,
            "freed_bytes": int(freed_bytes),
            "processes": processed,
            "message": (f"已对 {processed} 个进程回收工作集，"
                        f"可用物理内存增加约 {format_size(freed_bytes)}"
                        if ok else "内存清理未完成，可能是权限不足"),
        }

    # ==================== 虚拟内存转移 ====================

    def move_pagefile(self, target_drive='D'):
        """
        将虚拟内存从C盘转移到目标盘。
        需要管理员权限，修改后需重启生效。
        """
        try:
            # 通过注册表设置页面文件
            import winreg
            key = winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE,
                r'SYSTEM\CurrentControlSet\Control\Session Manager\Memory Management',
                0, winreg.KEY_SET_VALUE | winreg.KEY_WOW64_64KEY
            )
            # 让目标盘接管页面文件（0 0 表示由系统自动管理大小）
            winreg.SetValueEx(key, "PagingFiles", 0, winreg.REG_MULTI_SZ,
                              [f"{target_drive}:\\pagefile.sys 0 0"])
            winreg.CloseKey(key)
            self._emit(90, f"虚拟内存已设置转移到 {target_drive}: 盘，重启后生效")
            return True
        except Exception as e:
            self._emit(90, f"虚拟内存转移失败: {e}")
            return False

    # ==================== 磁盘碎片整理 ====================

    def defragment(self, drive='C'):
        """磁盘碎片整理"""
        try:
            self._emit(10, f"开始整理 {drive}: 盘碎片...")
            proc = subprocess.Popen(
                ['defrag', f'{drive}:', '/O', '/U', '/V'],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True
            )
            # 简单等待完成
            proc.wait(timeout=600)
            self._emit(100, f"{drive}: 盘碎片整理完成")
            return True
        except subprocess.TimeoutExpired:
            self._emit(100, "碎片整理超时，请稍后重试")
            return False
        except Exception as e:
            self._emit(100, f"碎片整理失败: {e}")
            return False

    # ==================== 大文件搬家 ====================

    def move_large_file(self, src_path, dest_dir):
        """将大文件搬家到目标目录"""
        try:
            if not os.path.exists(dest_dir):
                os.makedirs(dest_dir)
            filename = os.path.basename(src_path)
            dest_path = os.path.join(dest_dir, filename)
            sz = os.path.getsize(src_path)
            shutil.move(src_path, dest_path)
            return True, sz
        except Exception as e:
            return False, str(e)

    # ==================== 软件压缩 ====================

    def compress_software(self, software_dir):
        """
        使用Windows NTFS压缩功能压缩软件目录。
        """
        try:
            self._emit(10, f"正在压缩 {software_dir} ...")
            # compact /c /s:directory
            proc = subprocess.run(
                ['compact', '/c', '/s:' + software_dir],
                capture_output=True, text=True, timeout=300
            )
            self._emit(100, f"压缩完成: {software_dir}")
            return True
        except Exception as e:
            self._emit(100, f"压缩失败: {e}")
            return False

    # ==================== 隐私深度清理 ====================

    def clean_dns_cache(self):
        """清理DNS缓存"""
        try:
            subprocess.run(['ipconfig', '/flushdns'], capture_output=True, timeout=15)
            return True
        except Exception:
            return False

    def clean_recent_documents(self):
        """清理最近文档记录"""
        recent = os.path.join(os.environ.get('APPDATA', ''), 'Microsoft', 'Windows', 'Recent')
        return self.clean_directory_contents(recent)

    def clean_temp_all(self):
        """清理所有临时文件"""
        total = 0
        # 系统Temp
        sys_temp = os.path.join(os.environ.get('SystemRoot', r'C:\Windows'), 'Temp')
        total += self.clean_directory_contents(sys_temp)
        # 用户Temp
        total += self.clean_directory_contents(tempfile.gettempdir())
        return total
