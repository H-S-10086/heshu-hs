# -*- coding: utf-8 -*-
"""
软件管理引擎
提供四类能力：
  1. 已安装软件清单（读取注册表三处 Uninstall 键：64位 / 32位 / 当前用户）
  2. 卸载（优先使用静默卸载命令）
  3. 卸载残留与幽灵软件检测（目录残留、孤立 AppData、失效软件记录）
  4. 软件搬家（移动安装目录 + 建立目录联接，软件无需重装）

说明：所有判定都基于真实注册表与文件系统数据；无法确认的一律标注"疑似/请确认"。
"""

import os
import re
import ctypes
import winreg
import subprocess
import shutil
from datetime import datetime

from scanner import ScanResult, get_size, format_size


def is_admin():
    """是否拥有管理员权限"""
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _run(cmd, timeout=60, shell=False):
    """执行命令，返回 (成功, 输出, 错误)"""
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=timeout, shell=shell)
    except (subprocess.TimeoutExpired, OSError, ValueError) as e:
        return False, "", str(e)
    out = (proc.stdout or b"")
    err = (proc.stderr or b"")
    text = text_err = ""
    for enc in ("utf-8", "gbk", "latin-1"):
        try:
            text = out.decode(enc, errors="ignore")
            text_err = err.decode(enc, errors="ignore")
            break
        except Exception:
            continue
    return proc.returncode == 0, text, text_err


# 三处卸载信息注册表位置：(根键, 子路径, 视图标记)
UNINSTALL_LOCATIONS = [
    (winreg.HKEY_LOCAL_MACHINE,
     r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall",
     winreg.KEY_WOW64_64KEY, "全机(64位)"),
    (winreg.HKEY_LOCAL_MACHINE,
     r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall",
     winreg.KEY_WOW64_32KEY, "全机(32位)"),
    (winreg.HKEY_CURRENT_USER,
     r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall",
     0, "当前用户"),
]


def _read_value(key, name, default=""):
    """安全读取注册表值"""
    try:
        val, _ = winreg.QueryValueEx(key, name)
        return val
    except (FileNotFoundError, OSError):
        return default


# 从命令行字符串里提取 Windows 绝对路径。
# 分两种情况：带引号的路径（允许含空格），以及不带引号的路径（不能含空格）。
# 注意：不能写成非贪婪匹配 —— 否则 "C:\Program Files\X.exe" 会被截断成 "C:\Program"。
_QUOTED_PATH_RE = re.compile(r'"([A-Za-z]:\\[^"]+)"')
_BARE_PATH_RE = re.compile(r'(?:^|\s)([A-Za-z]:\\[^\s"]+)')


def _extract_paths(text):
    """
    从一段命令字符串中提取出所有绝对路径。
    例如：'"C:\\Program Files\\WinRAR\\WinRAR.exe" "%1"'
          -> ['C:\\Program Files\\WinRAR\\WinRAR.exe']
    """
    if not text:
        return []
    s = str(text)
    out = []
    try:
        out.extend(m.group(1) for m in _QUOTED_PATH_RE.finditer(s))
        # 去掉已提取的部分，再扫不带引号的路径，避免重复
        stripped = _QUOTED_PATH_RE.sub(" ", s)
        out.extend(m.group(1) for m in _BARE_PATH_RE.finditer(stripped))
    except Exception:
        pass
    return out


def _normalize_name(name):
    """
    归一化名称用于模糊匹配：去掉括号补充说明、去掉空格/连字符/点。
    这样 "Windows Media Player (64-bit)" 与目录名 "Windows Media Player" 可以匹配上。
    """
    if not name:
        return ""
    low = str(name).lower()
    # 先去掉括号内容（版本/位数等补充说明），例如 "(64-bit)"、"[32位]"
    low = re.sub(r"[\[\(（【][^\]\)）】]*[\]\)）】]", "", low)
    low = re.sub(r"[\s\-_\.]+", "", low)
    return low


class SoftwareManager:
    """软件管理与搬家"""

    # 判定为"系统组件、不应卸载"的名称特征
    SYSTEM_COMPONENT_HINTS = ("microsoft visual c++", "windows sdk", "microsoft .net",
                              "windows driver", "update for", "hotfix", "kb",
                              "microsoft edge", "microsoft visual studio installer")

    def __init__(self, progress_callback=None):
        self.progress_callback = progress_callback
        self._stop = False

    def _emit(self, percent, message):
        if self.progress_callback:
            self.progress_callback(percent, message)

    def stop(self):
        self._stop = True

    def reset_stop(self):
        self._stop = False

    # ==================== 已安装软件清单 ====================

    def list_installed_software(self):
        """
        读取所有已安装软件。
        返回 [{"name","version","publisher","install_location","uninstall_string",
               "quiet_uninstall","estimated_size"(字节),"install_date","key_path",
               "hive","scope","is_system_component","is_update"}]
        """
        self._emit(5, "正在读取已安装软件清单...")
        software = []
        seen = set()

        for hive, sub, view, scope in UNINSTALL_LOCATIONS:
            try:
                key = winreg.OpenKey(hive, sub, 0, winreg.KEY_READ | view)
            except (FileNotFoundError, PermissionError, OSError):
                continue
            try:
                idx = 0
                while True:
                    try:
                        name = winreg.EnumKey(key, idx)
                    except OSError:
                        break
                    idx += 1
                    if idx > 3000:
                        break
                    try:
                        sk = winreg.OpenKey(hive, sub + "\\" + name, 0,
                                            winreg.KEY_READ | view)
                    except (FileNotFoundError, PermissionError, OSError):
                        continue
                    try:
                        display = _read_value(sk, "DisplayName", "")
                        if not display or not str(display).strip():
                            continue
                        # 跳过纯更新补丁
                        uninstall_str = str(_read_value(sk, "UninstallString", "") or "")
                        is_update = str(name).upper().startswith(("KB", "UPDATE"))

                        est_kb = _read_value(sk, "EstimatedSize", 0)
                        try:
                            est_bytes = int(est_kb) * 1024
                        except Exception:
                            est_bytes = 0

                        entry = {
                            "name": str(display).strip(),
                            "version": str(_read_value(sk, "DisplayVersion", "") or "").strip(),
                            "publisher": str(_read_value(sk, "Publisher", "") or "").strip(),
                            "install_location": str(_read_value(sk, "InstallLocation", "") or "").strip(),
                            "uninstall_string": uninstall_str.strip(),
                            "quiet_uninstall": str(_read_value(sk, "QuietUninstallString", "") or "").strip(),
                            "estimated_size": est_bytes,
                            "install_date": str(_read_value(sk, "InstallDate", "") or "").strip(),
                            "key_path": ("HKEY_LOCAL_MACHINE" if hive == winreg.HKEY_LOCAL_MACHINE
                                         else "HKEY_CURRENT_USER") + "\\" + sub + "\\" + name,
                            "hive": "HKLM" if hive == winreg.HKEY_LOCAL_MACHINE else "HKCU",
                            "scope": scope,
                            "is_system_component": False,
                            "is_update": is_update,
                        }

                        low = entry["name"].lower()
                        if any(h in low for h in self.SYSTEM_COMPONENT_HINTS):
                            entry["is_system_component"] = True

                        dedup_key = entry["name"].lower() + "|" + entry["version"]
                        if dedup_key in seen:
                            continue
                        seen.add(dedup_key)
                        software.append(entry)
                    finally:
                        try:
                            winreg.CloseKey(sk)
                        except Exception:
                            pass
            finally:
                try:
                    winreg.CloseKey(key)
                except Exception:
                    pass

        software.sort(key=lambda x: (-x["estimated_size"],
                                     x["name"].lower()))
        self._emit(25, f"共发现 {len(software)} 款已安装软件")
        return software

    # ==================== 卸载 ====================

    def uninstall(self, software, quiet=True):
        """
        执行卸载。
        quiet=True 时优先使用 QuietUninstallString（静默卸载，无界面）。
        注意：多数卸载程序会自行弹出界面，本方法只负责启动。
        返回 (是否成功启动, 信息)
        """
        cmd = software.get("quiet_uninstall") if quiet else ""
        if not cmd:
            cmd = software.get("uninstall_string", "")
        if not cmd:
            return False, "该软件未提供卸载命令，请到「控制面板 > 程序和功能」中卸载"

        # MsiExec 静默参数补全
        if cmd.lower().startswith("msiexec") and quiet and "/q" not in cmd.lower():
            cmd = cmd + " /qn /norestart"

        try:
            subprocess.Popen(cmd, shell=True)
            return True, ("已启动卸载程序，请在弹出的窗口中完成卸载。"
                          "卸载完成后建议使用「残留扫描」清理遗留文件。")
        except Exception as e:
            return False, f"启动卸载程序失败：{e}"

    def force_uninstall(self, software):
        """
        强制移除：删除注册表卸载项 + 删除安装目录。
        仅用于卸载程序损坏、无法正常卸载的软件。操作不可逆，请先确认。
        """
        if software.get("hive") == "HKLM" and not is_admin():
            return False, "该软件属于全机安装，强制移除需要管理员权限"

        removed = []
        errors = []

        # 1) 删除注册表项
        try:
            hive = (winreg.HKEY_LOCAL_MACHINE if software["hive"] == "HKLM"
                    else winreg.HKEY_CURRENT_USER)
            sub = software["key_path"].split("\\", 1)[1]
            winreg.DeleteKeyEx(hive, sub, winreg.KEY_WOW64_64KEY)
            removed.append("注册表记录")
        except Exception as e:
            errors.append(f"注册表删除失败：{e}")

        # 2) 删除安装目录
        loc = software.get("install_location", "")
        if loc and os.path.isdir(loc):
            try:
                # 先去掉只读属性
                _run(["attrib", "-r", "-h", "-s", "/s", "/d", loc], timeout=120)
                shutil.rmtree(loc, ignore_errors=False)
                removed.append(f"安装目录({loc})")
            except Exception as e:
                errors.append(f"目录删除失败：{e}")

        if removed:
            msg = "已强制移除：" + "、".join(removed)
            if errors:
                msg += "；部分步骤失败：" + errors[0]
            return True, msg
        return False, "强制移除未完成：" + ("；".join(errors) if errors else "无可移除内容")

    # ==================== 残留扫描 ====================

    def _collect_registry_anchors(self, software_list):
        """
        收集"注册表锚点路径"：凡是注册表里被引用过的文件/目录路径，都视为"有主"。

        来源（依次合并）：
          1. 卸载信息里的 InstallLocation / UninstallString / QuietUninstallString
          2. App Paths（HKLM 两个视图）
          3. Classes\\Applications 下各程序的 shell\\open\\command

        之所以要这么多来源，是因为实测发现：本机 145 条已安装记录里只有 43 条带
        InstallLocation，许多软件（如绿色版安装的 WinRAR）根本不写该字段。
        只靠 InstallLocation 判断会把大量正常程序误判为残留。
        """
        anchors = set()

        def _add(path):
            if not path:
                return
            p = str(path).strip().strip('"').strip()
            if not p:
                return
            try:
                p = os.path.expandvars(p)
            except Exception:
                pass
            if not os.path.isabs(p):
                return
            try:
                anchors.add(os.path.normcase(os.path.normpath(p)))
            except Exception:
                pass

        # 1) 卸载信息
        for s in software_list:
            _add(s.get("install_location"))
            for key in ("uninstall_string", "quiet_uninstall"):
                for p in _extract_paths(s.get(key, "")):
                    _add(p)

        # 2) App Paths
        app_paths_key = (r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths")
        for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
            try:
                key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, app_paths_key, 0,
                                     winreg.KEY_READ | view)
            except (FileNotFoundError, PermissionError, OSError):
                continue
            try:
                idx = 0
                while True:
                    try:
                        sub = winreg.EnumKey(key, idx)
                    except OSError:
                        break
                    idx += 1
                    if idx > 800:
                        break
                    try:
                        sk = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                                            app_paths_key + "\\" + sub, 0,
                                            winreg.KEY_READ | view)
                    except (FileNotFoundError, PermissionError, OSError):
                        continue
                    try:
                        for value_name in ("", "Path"):
                            try:
                                val, _ = winreg.QueryValueEx(sk, value_name)
                                for p in _extract_paths(str(val)):
                                    _add(p)
                            except (FileNotFoundError, OSError):
                                pass
                    finally:
                        try:
                            winreg.CloseKey(sk)
                        except Exception:
                            pass
            finally:
                try:
                    winreg.CloseKey(key)
                except Exception:
                    pass

        # 3) Classes\Applications（程序注册的打开命令）
        apps_key = r"SOFTWARE\Classes\Applications"
        try:
            key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, apps_key, 0,
                                 winreg.KEY_READ | winreg.KEY_WOW64_64KEY)
        except (FileNotFoundError, PermissionError, OSError):
            key = None
        if key is not None:
            try:
                idx = 0
                while True:
                    try:
                        sub = winreg.EnumKey(key, idx)
                    except OSError:
                        break
                    idx += 1
                    if idx > 800:
                        break
                    try:
                        sk = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                                            apps_key + "\\" + sub + r"\shell\open\command",
                                            0, winreg.KEY_READ | winreg.KEY_WOW64_64KEY)
                    except (FileNotFoundError, PermissionError, OSError):
                        continue
                    try:
                        val, _ = winreg.QueryValueEx(sk, "")
                        for p in _extract_paths(str(val)):
                            _add(p)
                    except (FileNotFoundError, OSError):
                        pass
                    finally:
                        try:
                            winreg.CloseKey(sk)
                        except Exception:
                            pass
            finally:
                try:
                    winreg.CloseKey(key)
                except Exception:
                    pass

        return anchors

    def scan_orphan_program_folders(self, software_list=None, max_items=200):
        """
        检测 Program Files 下"空壳残留目录"。

        【判定原则：精度优先，宁可漏报也不误报】
        删除 Program Files 下的目录是不可逆的高风险操作，因此只有同时满足下列
        全部条件时才会被列为候选：

          1. 不是系统组件或运行库目录（对照系统目录名单与前缀）
          2. 目录内**不含任何 .exe 可执行文件**（有可执行文件说明程序还有用）
          3. 目录内没有卸载器（unins*/uninstall*）
          4. 目录路径没有被注册表任何位置引用（卸载信息 / App Paths / Classes\\Applications）
          5. 目录名与任何已安装软件的名称、发布者都不匹配

        也就是说，本功能只报告"确实已经只剩空壳"的目录。这是刻意保守的取舍：
        早期版本仅凭"注册表里找不到 InstallLocation"就判定残留，实测在 21 个
        候选中误报 21 个（把 Adobe、Python312、WinRAR、Realtek、Windows 组件
        全部误判为残留），因此改为现在的多证据判定。
        """
        self._emit(35, "正在比对程序目录与注册表记录...")
        if software_list is None:
            software_list = self.list_installed_software()

        anchors = self._collect_registry_anchors(software_list)
        # 发布者做归一化后用于辅助判断（名称匹配已改为逐条比对，见下方条件 5）
        publishers = [_normalize_name(s.get("publisher", "")) for s in software_list]
        publishers = [p for p in publishers if p]

        results = []
        roots = [r"C:\Program Files", r"C:\Program Files (x86)"]
        for root in roots:
            if not os.path.isdir(root):
                continue
            try:
                with os.scandir(root) as it:
                    for e in it:
                        if self._stop or len(results) >= max_items:
                            break
                        if not e.is_dir(follow_symlinks=False):
                            continue
                        full = e.path

                        # 条件 1：系统组件与运行库
                        if self._is_system_dir(e.name):
                            continue

                        # 条件 4：被注册表引用的路径（含被引用目录的父目录）
                        try:
                            norm = os.path.normcase(os.path.normpath(full))
                        except Exception:
                            norm = full.lower()
                        if any(norm == a or a.startswith(norm + os.sep)
                               or norm.startswith(a + os.sep) for a in anchors):
                            continue

                        # 条件 5：名称/发布者匹配
                        fname = _normalize_name(e.name)
                        name_matched = None
                        for s in software_list:
                            n = _normalize_name(s.get("name", ""))
                            if n and (fname in n or n in fname):
                                name_matched = s
                                break
                        matched_by_name = name_matched is not None

                        # 条件 2、3：目录内是否有可执行文件或卸载器
                        has_exe, has_uninstaller = self._inspect_folder_exes(full)
                        if has_uninstaller or has_exe:
                            continue

                        # 条件 5 的细化处理：
                        # 目录名虽然匹配到某个已装软件，但如果该软件的实际安装位置在别处，
                        # 而本目录又是个不含 exe 的空壳，那它仍是残留。
                        # 实例：WO Mic 装在 E:\womic，却在 Program Files (x86) 留下
                        # 一个只剩空 driver 子目录的 WOMic 文件夹。
                        relocated_note = ""
                        if matched_by_name:
                            # 该软件的真实位置：优先 InstallLocation，
                            # 很多软件（如 WO Mic）不写该字段，则从卸载命令反推目录
                            actual = (name_matched.get("install_location") or "").strip().strip('"')
                            if not actual:
                                uninst = (name_matched.get("uninstall_string") or "").strip()
                                paths = _extract_paths(uninst)
                                if paths:
                                    actual = os.path.dirname(paths[0])
                            if not actual:
                                # 两条线索都拿不到位置，无法判断，保守跳过
                                continue
                            try:
                                same = (os.path.normcase(os.path.normpath(actual))
                                        == os.path.normcase(os.path.normpath(full)))
                            except Exception:
                                same = False
                            if same:
                                continue
                            relocated_note = (f"该软件的注册安装位置是 {actual}，"
                                              f"并非此处，因此本目录属于残留。")

                        if any(fname in p or p in fname for p in publishers) and not relocated_note:
                            continue

                        size = get_size(full)
                        results.append(ScanResult(
                            category="软件残留",
                            source="空壳残留目录",
                            path=full,
                            size=size,
                            description=(
                                "该目录内已不含任何可执行文件（也没有卸载程序），且注册表中找不到"
                                "引用它的记录，判断为软件卸载后遗留的空壳目录"
                                f"（占用 {format_size(size)}）。{relocated_note}"
                                "本工具已按多重条件核对，仍建议您确认目录内文件确实无用后再删除。"),
                        ))
            except (OSError, PermissionError):
                continue

        self._emit(50, f"发现 {len(results)} 个空壳残留目录")
        return results

    # 系统组件与运行库目录名单（这些即使没有安装记录也属正常）
    SYSTEM_DIR_NAMES = {
        "common files", "internet explorer", "modifiablewindowsapps", "windowsapps",
        "windows defender", "windows defender advanced threat protection",
        "windows mail", "windows media player", "windows nt", "windows photo viewer",
        "windows portable devices", "windows sidebar", "windows sidebar gadgets",
        "windowspowershell", "windows kits", "windows security", "windows journal",
        "windows journal viewer", "windows update", "windows mixed reality",
        "reference assemblies", "msbuild", "uninstall information",
        "microsoft.net", "dotnet", "dotnetcore", "temp", "vulkanrt",
        "easyanticheat", "easyanticheat_eos", "realtek", "teams installer",
        "microsoft office", "microsoft office 15", "microsoft office root",
    }
    SYSTEM_DIR_PREFIXES = ("windows", "microsoft", "dotnet", "easyanticheat",
                           "vulkanrt", "python")

    def _is_system_dir(self, folder_name):
        """判断目录名是否属于系统组件/运行库"""
        low = folder_name.strip().lower()
        if low in self.SYSTEM_DIR_NAMES:
            return True
        return low.startswith(self.SYSTEM_DIR_PREFIXES)

    def _inspect_folder_exes(self, folder, max_entries=4000, max_depth=3):
        """
        有界地检查目录内是否存在可执行文件与卸载器。

        返回 (是否存在exe, 是否存在卸载器)。
        使用有界遍历（深度与条目数都设上限），发现 exe 立即提前返回，
        避免在大目录上耗时过长。
        """
        has_uninstaller = False
        scanned = 0
        base_depth = folder.rstrip("\\/").count(os.sep)

        for dirpath, dirnames, filenames in os.walk(folder, onerror=lambda e: None):
            if self._stop:
                break
            if dirpath.count(os.sep) - base_depth >= max_depth:
                dirnames[:] = []
            for fn in filenames:
                scanned += 1
                if scanned > max_entries:
                    return True, has_uninstaller
                low = fn.lower()
                if not low.endswith(".exe"):
                    continue
                if "unins" in low or "uninstall" in low:
                    has_uninstaller = True
                return True, has_uninstaller

        return False, has_uninstaller

    def remove_empty_leftover_folders(self, paths):
        """
        删除"空壳残留目录"。

        安全约束：只删除**确实为空**的目录。只要目录内还有任何文件或子目录就跳过并记录原因，
        避免因判定偏差而删除有用的内容。返回 (已删除数量, 失败列表)。

        注意：不能用 CleanEngine 的通用目录清理来做这件事 —— 那个逻辑是"清空目录内容"
        并把目录本身保留下来，而空壳目录本身才是要删除的目标。
        """
        deleted = 0
        failed = []
        for p in paths:
            try:
                if not os.path.isdir(p):
                    failed.append((p, "该路径不是目录或已不存在"))
                    continue
                remaining = os.listdir(p)
                if remaining:
                    failed.append((p, f"目录内仍有 {len(remaining)} 项内容，出于安全考虑未删除"))
                    continue
                os.rmdir(p)
                deleted += 1
            except (OSError, PermissionError) as e:
                failed.append((p, str(e)))
        return deleted, failed

    def scan_orphan_appdata(self, software_list=None, max_items=200):
        """检测 AppData 下孤立的应用数据目录（已卸载软件的配置残留）"""
        self._emit(55, "正在比对应用数据目录...")
        if software_list is None:
            software_list = self.list_installed_software()

        names = [s["name"].lower() for s in software_list]
        publishers = [s["publisher"].lower() for s in software_list if s.get("publisher")]

        results = []
        roots = [
            (os.path.join(os.environ.get("LOCALAPPDATA", ""), ""), "本地应用数据"),
            (os.path.join(os.environ.get("APPDATA", ""), ""), "漫游应用数据"),
        ]
        # 这些是系统或常用组件的目录名，不参与判定
        whitelist = {"microsoft", "packages", "temp", "programs", "connecteddevicesplatform",
                     "google", "mozilla", "nvidia", "intel", "amd", "d3dscache", "crashreport",
                     "publishers", "history", "comms", "virtualstore", "iconscache", "字体",
                     "desktop", "windows", "tencent", "alibaba", "adobe", "apple", "jetbrains"}

        for root, label in roots:
            if not os.path.isdir(root):
                continue
            try:
                with os.scandir(root) as it:
                    for e in it:
                        if self._stop or len(results) >= max_items:
                            break
                        if not e.is_dir(follow_symlinks=False):
                            continue
                        fname = e.name.lower()
                        if fname in whitelist:
                            continue
                        if fname.startswith("."):
                            continue
                        # 名称或发布者能匹配上任何已装软件则跳过
                        if any(fname in n or n in fname for n in names):
                            continue
                        if any(fname in p or p in fname for p in publishers if p):
                            continue

                        size = get_size(e.path)
                        if size < 1024 * 1024:      # 小于 1MB 的忽略，避免噪声
                            continue

                        results.append(ScanResult(
                            category="软件残留",
                            source=f"{label}中的孤立目录",
                            path=e.path,
                            size=size,
                            description=(f"该目录位于{label}目录下，未匹配到任何已安装软件，"
                                         f"可能是已卸载软件的配置/缓存残留（占用 {format_size(size)}）。"
                                         "删除后对应软件的设置会丢失，请确认该软件已不再使用。"),
                        ))
            except (OSError, PermissionError):
                continue

        self._emit(65, f"发现 {len(results)} 个孤立应用数据目录")
        return results

    def scan_ghost_software(self, software_list=None):
        """
        检测"幽灵软件"：注册表里有记录，但安装目录已不存在。
        这些记录只会在控制面板里留下一堆无法卸载的条目。
        """
        self._emit(70, "正在检测失效的软件记录...")
        if software_list is None:
            software_list = self.list_installed_software()

        results = []
        for s in software_list:
            if self._stop:
                break
            loc = s.get("install_location", "").strip().strip('"')
            if not loc or not os.path.isabs(loc):
                continue
            if os.path.exists(loc):
                continue
            # 再确认一次卸载程序是否还在
            uninst = s.get("uninstall_string", "")
            exe = re.match(r'^"([^"]+)"', uninst)
            prog = exe.group(1) if exe else uninst.split(" ")[0]
            if prog and os.path.exists(prog):
                continue

            results.append(ScanResult(
                category="软件残留",
                source="失效的软件注册记录(幽灵软件)",
                path=s["key_path"],
                size=0,
                description=(f"「{s['name']}」在注册表中仍有卸载记录，但安装目录 {loc} 已不存在，"
                             "属于卸载不完整留下的无效条目。清理后该条目将从软件列表消失。"),
            ))

        self._emit(78, f"发现 {len(results)} 条失效软件记录")
        return results

    def scan_software_leftovers(self):
        """聚合全部软件残留扫描"""
        self.reset_stop()
        self._emit(30, "开始扫描软件残留...")
        software = self.list_installed_software()
        if self._stop:
            return []
        results = []
        results += self.scan_ghost_software(software)
        if self._stop:
            return results
        results += self.scan_orphan_program_folders(software)
        if self._stop:
            return results
        results += self.scan_orphan_appdata(software)
        self._emit(100, f"软件残留扫描完成，共 {len(results)} 项")
        return results

    def clean_ghost_registry_entries(self, items):
        """
        清理失效的软件注册记录（只删除注册表项，不动文件）。
        items 为 scan_ghost_software 返回的子集。
        """
        deleted = 0
        failed = []
        for it in items:
            try:
                hive_name, sub = it.path.split("\\", 1)
                hive = (winreg.HKEY_LOCAL_MACHINE if hive_name.upper() == "HKEY_LOCAL_MACHINE"
                        else winreg.HKEY_CURRENT_USER)
                # 先尝试 64 位视图，失败再试 32 位视图
                for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY, 0):
                    try:
                        winreg.DeleteKeyEx(hive, sub, view)
                        deleted += 1
                        break
                    except (FileNotFoundError, OSError):
                        continue
                else:
                    failed.append((it.path, "删除失败，可能需要管理员权限"))
            except Exception as e:
                failed.append((it.path, str(e)))
        return deleted, failed

    # ==================== 软件搬家 ====================

    def check_software_running(self, install_location):
        """
        粗略检查安装目录下是否有程序正在运行（用 tasklist 比对可执行文件名）。
        返回 (是否可能正在运行, 说明)
        """
        loc = install_location.strip().strip('"')
        if not loc or not os.path.isdir(loc):
            return False, "安装目录不存在，无需检查"
        names = set()
        try:
            for dirpath, _, filenames in os.walk(loc):
                for f in filenames:
                    if f.lower().endswith(".exe"):
                        names.add(f.lower())
                if len(names) > 200:
                    break
        except (OSError, PermissionError):
            pass
        if not names:
            return False, "未找到可执行文件"

        ok, out, _ = _run(["tasklist", "/fo", "csv", "/nh"], timeout=30)
        if not ok:
            return False, "无法获取进程列表，请自行确认软件已关闭"
        running = []
        for line in out.splitlines():
            low = line.lower()
            for n in names:
                if f'"{n}"' in low or f',{n},' in low:
                    running.append(n)
        if running:
            return True, "检测到可能正在运行的程序：" + "、".join(sorted(set(running))[:5])
        return False, "未检测到该软件正在运行"

    def move_software(self, software, target_root, create_junction=True):
        """
        软件搬家：把安装目录移动到 target_root，并在原位置创建目录联接（Junction），
        这样软件无需重装即可继续使用。

        create_junction=True 时必须创建成功，否则会回滚（把目录移回原位）。

        返回 (成功, 信息)
        """
        src = software.get("install_location", "").strip().strip('"')
        if not src or not os.path.isdir(src):
            return False, "该软件没有可用的安装目录信息，无法搬家（请使用大文件搬家功能）"
        if not target_root or not os.path.isdir(target_root):
            return False, "目标目录不存在"

        # 不允许搬到自己所在的盘
        if os.path.normcase(os.path.splitdrive(src)[0]) == os.path.normcase(os.path.splitdrive(target_root)[0]):
            return False, "目标位置与源位置在同一磁盘，搬家不会释放该磁盘空间"

        running, info = self.check_software_running(src)
        if running:
            return False, f"搬家前请先完全退出该软件。{info}"

        name = software.get("name") or os.path.basename(src.rstrip("\\/"))
        safe_name = re.sub(r'[\\/:*?"<>|]', "_", name).strip() or "moved_software"
        dest = os.path.join(target_root, safe_name)
        if os.path.exists(dest):
            dest = os.path.join(target_root, safe_name + "_" + datetime.now().strftime("%H%M%S"))

        need_admin = src.lower().startswith(("c:\\program files", "c:\\windows"))

        self._emit(20, f"正在移动 {src} -> {dest} ...")
        try:
            shutil.move(src, dest)
        except Exception as e:
            return False, f"移动失败：{e}（可能需要管理员权限或软件仍被占用）"

        if not create_junction:
            return True, (f"已将目录移动到 {dest}。注意：未创建目录联接，"
                          "原软件可能因路径变化而无法启动，需要重新安装。")

        self._emit(70, "正在创建目录联接...")
        ok, out, err = _run(["cmd", "/c", "mklink", "/J", src, dest], timeout=60)
        created = ok and os.path.exists(src)

        if not created:
            # 回滚
            self._emit(85, "创建联接失败，正在回滚...")
            try:
                shutil.move(dest, src)
                rollback = "已回滚到原位置"
            except Exception as e2:
                rollback = f"回滚也失败，请手动把 {dest} 移回 {src}（{e2}）"
            return False, ("无法创建目录联接（" + (err or out)[:200] +
                           f"）。{rollback}。" + ("提示：创建联接需要管理员权限。" if need_admin else ""))

        return True, (f"搬家完成：目录已移动到 {dest}，并已在原位置创建目录联接，"
                      f"软件可正常使用。已释放 C 盘约 {format_size(get_size(dest))}。")

    def undo_software_move(self, original_path):
        """
        撤销搬家：删除原位置的目录联接，再把数据移回去。
        注意：删除联接必须用 rmdir（不能递归删除，否则会误删目标数据）。
        """
        if not os.path.isdir(original_path):
            return False, "原路径不存在"
        # 判断是否为联接：用 dir 命令的 <JUNCTION> 标记
        ok, out, _ = _run(["cmd", "/c", "dir", "/AL", os.path.dirname(original_path)],
                          timeout=60)
        is_junction = "JUNCTION" in out.upper() or "<联接" in out
        if not is_junction:
            return False, "该路径不是目录联接，无法自动撤销。请手动确认后再操作。"

        ok2, out2, err2 = _run(["cmd", "/c", "rmdir", original_path], timeout=60)
        if not ok2:
            return False, f"删除联接失败：{(err2 or out2)[:200]}"
        return True, "目录联接已删除，数据仍在目标盘。如需搬回，请手动移动原目录。"

    # ==================== 汇总 ====================

    def get_software_stats(self, software_list=None):
        """软件清单统计，供 UI 显示"""
        if software_list is None:
            software_list = self.list_installed_software()
        total = len(software_list)
        ghost = sum(1 for s in software_list if s.get("is_system_component"))
        return {
            "total": total,
            "updates": sum(1 for s in software_list if s.get("is_update")),
            "system_components": ghost,
            "estimated_total": sum(s.get("estimated_size", 0) for s in software_list),
        }
