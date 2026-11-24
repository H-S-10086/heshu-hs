# -*- coding: utf-8 -*-
"""
注册表清理引擎
负责扫描、备份与清理无效注册表项，以及开关系统隐私相关注册表设置。

仅使用 Python 标准库（winreg / ctypes / subprocess / os / datetime），
不引入任何第三方依赖，便于 PyInstaller 单文件打包。
本模块只包含业务逻辑，不含任何 UI 代码。
"""

import os
import ctypes
import subprocess
import winreg
from datetime import datetime

from scanner import ScanResult, get_size, format_size


# ==================== 模块级常量与工具函数 ====================

# 根键名称 -> winreg 句柄
_ROOT_MAP = {
    "HKEY_LOCAL_MACHINE": winreg.HKEY_LOCAL_MACHINE,
    "HKLM": winreg.HKEY_LOCAL_MACHINE,
    "HKEY_CURRENT_USER": winreg.HKEY_CURRENT_USER,
    "HKCU": winreg.HKEY_CURRENT_USER,
    "HKEY_CLASSES_ROOT": winreg.HKEY_CLASSES_ROOT,
    "HKCR": winreg.HKEY_CLASSES_ROOT,
    "HKEY_USERS": winreg.HKEY_USERS,
    "HKU": winreg.HKEY_USERS,
}

# winreg 句柄 -> reg.exe 使用的根键名称
_HIVE_NAMES = {
    winreg.HKEY_LOCAL_MACHINE: "HKLM",
    winreg.HKEY_CURRENT_USER: "HKCU",
    winreg.HKEY_CLASSES_ROOT: "HKCR",
    winreg.HKEY_USERS: "HKU",
}

# 遍历上限，避免在超大注册表上卡死
_MAX_SUBKEYS = 600          # 单个键最多枚举的子键数
_MAX_VALUES = 600           # 单个键最多枚举的值数
_MAX_SOFTWARE_KEYS = 40     # HKLM\SOFTWARE 下最多深入检查的厂商键数
_MAX_SOFTWARE_APPS = 25     # 每个厂商键下最多检查的应用键数
_MAX_RESULTS_PER_CAT = 5    # 每个类别最多产出的 ScanResult 条数

# 缩放后的 MUI 缓存值名后缀
_MUI_SUFFIXES = (".friendlyappname", ".applicationcompany", ".applicationname")

# HKLM\SOFTWARE 下这些顶层键不做软件路径遍历（内容多为系统组件，误判风险高）
_SKIP_SOFTWARE_TOP = {
    "microsoft", "windows", "classes", "policies", "wow6432node",
    "registeredapplications", "clients", "licensing", "odbc",
}

# 允许清理的键（子路径前缀 -> 清理策略）。不在其中的键一律跳过，保证安全。
_ALLOWED_RULES = [
    # 无效的卸载残留
    ("software\\microsoft\\windows\\currentversion\\uninstall", "uninstall_subkeys"),
    ("software\\wow6432node\\microsoft\\windows\\currentversion\\uninstall", "uninstall_subkeys"),
    # 无效的共享 DLL 计数
    ("software\\microsoft\\windows\\currentversion\\shareddlls", "value_name_path"),
    ("software\\wow6432node\\microsoft\\windows\\currentversion\\shareddlls", "value_name_path"),
    # 失效的 MUI 缓存
    ("software\\microsoft\\windows\\shellnoroam\\muicache", "value_name_path"),
    ("software\\classes\\local settings\\software\\microsoft\\windows\\shell\\muicache", "value_name_path"),
    # 无效的 App Paths
    ("software\\microsoft\\windows\\currentversion\\app paths", "apppaths_subkeys"),
    ("software\\wow6432node\\microsoft\\windows\\currentversion\\app paths", "apppaths_subkeys"),
    # 无效的启动项引用
    ("software\\microsoft\\windows\\currentversion\\run", "run_command"),
    ("software\\microsoft\\windows\\currentversion\\runonce", "run_command"),
    ("software\\wow6432node\\microsoft\\windows\\currentversion\\run", "run_command"),
    ("software\\wow6432node\\microsoft\\windows\\currentversion\\runonce", "run_command"),
    # 无效的字体记录
    ("software\\microsoft\\windows nt\\currentversion\\fonts", "fonts_values"),
    # 无效的最近使用记录 / 安装位置记录
    ("software\\microsoft\\windows\\currentversion\\explorer\\runmru", "value_data_path"),
    ("software\\microsoft\\windows\\currentversion\\explorer\\typedpaths", "value_data_path"),
    ("software\\microsoft\\windows\\currentversion\\installlocationsmru", "value_data_path"),
]


def _is_admin():
    """判断当前进程是否具备管理员权限（返回非 0 即管理员）"""
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _split_reg_path(path):
    """
    将 "HKEY_LOCAL_MACHINE\\SOFTWARE\\..." 形式的注册表路径
    拆分为 (根键句柄, 子路径)。解析失败返回 (None, None)。
    """
    try:
        if not path or not isinstance(path, str):
            return None, None
        p = path.strip().strip("\\")
        if "\\" in p:
            head, tail = p.split("\\", 1)
        else:
            head, tail = p, ""
        hive = _ROOT_MAP.get(head.upper())
        if hive is None:
            return None, None
        return hive, tail
    except Exception:
        return None, None


def _looks_like_path(text):
    """粗略判断一个字符串是否像“文件/目录路径”"""
    if not isinstance(text, str):
        return False
    t = text.strip().strip('"')
    if len(t) < 4:
        return False
    if "\\" in t:
        return True
    if len(t) > 1 and t[1] == ":":
        return True
    return False


def _path_missing(path):
    """
    判断给定路径是否“看起来是路径且当前不存在”。
    非路径字符串一律返回 False（避免误删普通字符串值）。
    """
    try:
        if not isinstance(path, str):
            return False
        # 去掉引号与 DisplayIcon 这类 "路径,索引" 形式的索引部分
        t = path.strip().strip('"').split(",")[0].strip()
        if not t:
            return False
        t = os.path.expandvars(t)
        if not _looks_like_path(t):
            return False
        return not os.path.exists(t)
    except (OSError, ValueError):
        return False


def _strip_mui_suffix(name):
    """去掉 MUI 缓存值名结尾的 .FriendlyAppName / .ApplicationCompany 等后缀"""
    try:
        low = name.lower()
        for suf in _MUI_SUFFIXES:
            if low.endswith(suf):
                return name[:-len(suf)]
    except Exception:
        pass
    return name


def _extract_cmd_exe(cmd):
    """从命令行字符串中提取可执行文件路径（粗粒度解析，含引号与参数场景）"""
    try:
        if not isinstance(cmd, str):
            return ""
        c = cmd.strip()
        if not c:
            return ""
        if c.startswith('"'):
            end = c.find('"', 1)
            exe = c[1:end] if end > 0 else c[1:].strip()
        else:
            idx = c.find(" ")
            exe = c[:idx] if idx > 0 else c
        return os.path.expandvars(exe.strip())
    except Exception:
        return ""


def _fonts_dir():
    """字体文件所在目录"""
    return os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "Fonts")


def _read_dword(hive, subkey, value_name):
    """
    读取 DWORD 类型注册表值。
    返回 (值, 值是否存在, 键是否存在)。任何异常均按“不存在”处理。
    """
    try:
        key = winreg.OpenKey(hive, subkey, 0, winreg.KEY_READ | winreg.KEY_WOW64_64KEY)
    except FileNotFoundError:
        return None, False, False
    except OSError:
        return None, False, False
    try:
        try:
            data, _vtype = winreg.QueryValueEx(key, value_name)
        except FileNotFoundError:
            return None, False, True
        except OSError:
            return None, False, True
        return data, True, True
    finally:
        try:
            winreg.CloseKey(key)
        except OSError:
            pass


def _write_dword(hive, subkey, value_name, data):
    """写入 DWORD 值，必要时创建键。成功返回 True。"""
    key = None
    try:
        try:
            key = winreg.CreateKeyEx(hive, subkey, 0, winreg.KEY_WRITE | winreg.KEY_WOW64_64KEY)
        except OSError:
            # 回退到 32 位视图
            try:
                key = winreg.CreateKeyEx(hive, subkey, 0, winreg.KEY_WRITE | winreg.KEY_WOW64_32KEY)
            except OSError:
                return False
        winreg.SetValueEx(key, value_name, 0, winreg.REG_DWORD, int(data))
        return True
    except (OSError, ValueError, TypeError):
        return False
    finally:
        if key is not None:
            try:
                winreg.CloseKey(key)
            except OSError:
                pass


# ==================== 隐私开关定义 ====================

# 每项说明：id / name / desc / root / key / value / data(目标值) / need_admin
_PRIVACY_DEFS = [
    {
        "id": "telemetry",
        "name": "Windows 遥测数据收集级别",
        "desc": "将遥测级别设为“安全(0)”以最大程度减少向微软发送的诊断数据。",
        "root": "HKEY_LOCAL_MACHINE",
        "key": r"SOFTWARE\Policies\Microsoft\Windows\DataCollection",
        "value": "AllowTelemetry",
        "data": 0,
        "need_admin": True,
    },
    {
        "id": "advertising_id",
        "name": "广告 ID（个性化广告追踪）",
        "desc": "关闭广告 ID，阻止应用跨应用追踪并投放个性化广告。",
        "root": "HKEY_CURRENT_USER",
        "key": r"Software\Microsoft\Windows\CurrentVersion\AdvertisingInfo",
        "value": "Enabled",
        "data": 0,
        "need_admin": False,
    },
    {
        "id": "activity_history",
        "name": "活动历史记录（时间线）",
        "desc": "关闭设备本地的活动历史记录采集，减少使用痕迹留存。",
        "root": "HKEY_CURRENT_USER",
        "key": r"Software\Microsoft\Windows\CurrentVersion\SearchSettings",
        "value": "IsDeviceSearchHistoryEnabled",
        "data": 0,
        "need_admin": False,
    },
    {
        "id": "background_apps",
        "name": "应用后台运行权限收紧",
        "desc": "禁止应用在后台运行（GlobalUserDisabled=1），减少后台活动与流量。",
        "root": "HKEY_CURRENT_USER",
        "key": r"Software\Microsoft\Windows\CurrentVersion\BackgroundAccessApplications",
        "value": "GlobalUserDisabled",
        "data": 1,
        "need_admin": False,
    },
    {
        "id": "content_delivery_tips",
        "name": "开始菜单推荐内容（Windows 提示）",
        "desc": "关闭“获取 Windows 使用提示与建议”等推广内容推送。",
        "root": "HKEY_CURRENT_USER",
        "key": r"Software\Microsoft\Windows\CurrentVersion\ContentDeliveryManager",
        "value": "SubscribedContent-338389Enabled",
        "data": 0,
        "need_admin": False,
    },
    {
        "id": "content_delivery_suggest",
        "name": "开始菜单建议（应用/设置推荐）",
        "desc": "关闭开始菜单中的应用与设置建议推送（SubscribedContent 系列）。",
        "root": "HKEY_CURRENT_USER",
        "key": r"Software\Microsoft\Windows\CurrentVersion\ContentDeliveryManager",
        "value": "SubscribedContent-338388Enabled",
        "data": 0,
        "need_admin": False,
    },
    {
        "id": "wer_disabled",
        "name": "Windows 错误报告（WER）",
        "desc": "关闭 Windows 错误报告，避免崩溃数据被上传。",
        "root": "HKEY_LOCAL_MACHINE",
        "key": r"SOFTWARE\Microsoft\Windows\Windows Error Reporting",
        "value": "Disabled",
        "data": 1,
        "need_admin": True,
    },
    {
        "id": "recall",
        "name": "Windows Recall（回顾）",
        "desc": "通过策略关闭 Windows Recall 的历史快照记录功能，防止屏幕内容被持续记录。",
        "root": "HKEY_LOCAL_MACHINE",
        "key": r"SOFTWARE\Policies\Microsoft\Windows\WindowsAI",
        "value": "DisableAIDataAnalysis",
        "data": 1,
        "need_admin": True,
    },
    {
        "id": "copilot_button",
        "name": "Copilot 按钮与入口",
        "desc": "隐藏任务栏 Copilot 按钮，减少系统内置 AI 助手的入口暴露。",
        "root": "HKEY_CURRENT_USER",
        "key": r"Software\Microsoft\Windows\CurrentVersion\Explorer\Advanced",
        "value": "ShowCopilotButton",
        "data": 0,
        "need_admin": False,
    },
]


class RegistryEngine:
    """注册表清理引擎（扫描 / 备份 / 清理 / 隐私开关）"""

    def __init__(self, progress_callback=None):
        self.progress_callback = progress_callback
        # 记录扫描阶段确认过、允许后续清理的键（子路径小写 -> 策略）
        self._scanned_keys = {}

    def _emit(self, percent, message):
        """上报进度（percent 取 0-100），回调异常不影响主流程"""
        if self.progress_callback:
            try:
                self.progress_callback(percent, message)
            except Exception:
                pass

    # ==================== 通用读注册表辅助 ====================

    def _open_key(self, hive, subkey, access=None):
        """安全打开注册表键，失败返回 None"""
        if access is None:
            access = winreg.KEY_READ
        try:
            return winreg.OpenKey(hive, subkey, 0, access | winreg.KEY_WOW64_64KEY)
        except OSError:
            pass
        try:
            return winreg.OpenKey(hive, subkey, 0, access | winreg.KEY_WOW64_32KEY)
        except OSError:
            return None

    def _enum_subkeys(self, hive, subkey, limit=_MAX_SUBKEYS):
        """枚举子键名列表（有数量上限），失败返回空列表"""
        names = []
        key = self._open_key(hive, subkey)
        if key is None:
            return names
        try:
            for i in range(limit):
                try:
                    names.append(winreg.EnumKey(key, i))
                except OSError:
                    break
        finally:
            try:
                winreg.CloseKey(key)
            except OSError:
                pass
        return names

    def _enum_values(self, hive, subkey, limit=_MAX_VALUES):
        """枚举 (值名, 数据, 类型) 列表（有数量上限），失败返回空列表"""
        values = []
        key = self._open_key(hive, subkey)
        if key is None:
            return values
        try:
            for i in range(limit):
                try:
                    name, data, vtype = winreg.EnumValue(key, i)
                except OSError:
                    break
                values.append((name, data, vtype))
        finally:
            try:
                winreg.CloseKey(key)
            except OSError:
                pass
        return values

    def _query_value(self, hive, subkey, value_name):
        """读取单个值，返回数据或 None"""
        key = self._open_key(hive, subkey)
        if key is None:
            return None
        try:
            try:
                data, _vtype = winreg.QueryValueEx(key, value_name)
                return data
            except OSError:
                return None
        finally:
            try:
                winreg.CloseKey(key)
            except OSError:
                pass

    # ==================== 失效判定（扫描与清理共用，保证一致） ====================

    def _is_value_junk(self, strategy, name, data, vtype):
        """判断一个值是否属于“无效注册表项”"""
        try:
            if strategy == "value_name_path":
                # 值名本身就是文件路径（SharedDLLs / MuiCache）
                base = _strip_mui_suffix(name)
                return _path_missing(base)

            if strategy == "value_data_path":
                # 值数据是文件/目录路径（安装位置 MRU、TypedPaths、RunMRU 等）
                if isinstance(data, str):
                    return _path_missing(data)
                return False

            if strategy == "value_data_exe_path":
                # 值数据是指向可执行文件的路径
                if isinstance(data, str) and data.strip().strip('"').lower().endswith(".exe"):
                    return _path_missing(data)
                return False

            if strategy == "run_command":
                # 启动项：命令行的可执行文件不存在
                if isinstance(data, str):
                    exe = _extract_cmd_exe(data)
                    return bool(exe) and _path_missing(exe)
                return False

            if strategy == "fonts_values":
                # 字体键：值数据是字体文件名，检查 Fonts 目录下是否存在
                if isinstance(data, str):
                    fn = data.strip()
                    if not fn:
                        return False
                    return not os.path.exists(os.path.join(_fonts_dir(), fn))
                return False
        except Exception:
            return False
        return False

    def _is_subkey_junk(self, hive, full_subkey, strategy):
        """判断一个子键是否属于“无效注册表项”"""
        try:
            if strategy == "apppaths_subkeys":
                # App Paths：子键默认值指向的可执行文件不存在
                default = self._query_value(hive, full_subkey, "")
                name = full_subkey.rsplit("\\", 1)[-1]
                if isinstance(default, str) and default.strip():
                    return _path_missing(default)
                if not default and name.lower().endswith(".exe"):
                    return True
                return False

            if strategy == "uninstall_subkeys":
                # 卸载项：UninstallString / InstallLocation / DisplayIcon 指向的位置不存在
                candidates = []
                for val_name in ("UninstallString", "QuietUninstallString",
                                 "InstallLocation", "DisplayIcon"):
                    v = self._query_value(hive, full_subkey, val_name)
                    if isinstance(v, str) and v.strip():
                        candidates.append(v.strip())
                if not candidates:
                    return False
                checked = False
                for c in candidates:
                    # MSI 卸载由系统接管，不做判断
                    if "msiexec" in c.lower():
                        return False
                    target = _extract_cmd_exe(c) or c
                    if not _looks_like_path(target):
                        # 无法判断的引用，按“有效”处理，避免误删
                        return False
                    if not _path_missing(target):
                        return False
                    checked = True
                return checked
        except Exception:
            return False
        return False

    # ==================== 扫描：注册表垃圾 ====================

    def scan_registry_junk(self):
        """
        扫描无效注册表项。真实读取注册表（winreg 遍历），无问题项时返回空列表。
        返回 list[ScanResult]，绝不会抛出异常。
        """
        results = []
        self._scanned_keys = {}
        self._emit(1, "正在扫描注册表：准备中...")

        steps = [
            (10, "正在扫描无效的卸载残留...", self._scan_uninstall_residue),
            (20, "正在扫描无效的共享 DLL 计数...", self._scan_shareddlls),
            (30, "正在扫描失效的 MUI 缓存...", self._scan_muicache),
            (40, "正在扫描无效的 App Paths...", self._scan_app_paths),
            (50, "正在扫描无效的启动项引用...", self._scan_run_entries),
            (65, "正在扫描无效的软件路径引用...", self._scan_software_paths),
            (78, "正在扫描无效的字体记录...", self._scan_fonts),
            (90, "正在扫描无效的最近使用记录...", self._scan_mru),
        ]

        for percent, message, func in steps:
            self._emit(percent, message)
            try:
                found = func()
                if found:
                    results.extend(found)
            except Exception:
                # 单个类别失败不影响整体扫描
                continue

        self._emit(100, f"注册表扫描完成，发现 {len(results)} 项")
        return results

    def _scan_uninstall_residue(self):
        """扫描无效的卸载残留（Uninstall 子键）与安装位置 MRU"""
        out = []
        containers = [
            ("HKEY_LOCAL_MACHINE", r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
            ("HKEY_LOCAL_MACHINE", r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
            ("HKEY_CURRENT_USER", r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
        ]
        for hive_name, subkey in containers:
            hive = _ROOT_MAP[hive_name]
            junk = self._collect_junk_subkeys(hive, subkey, "uninstall_subkeys")
            if junk:
                self._scanned_keys[subkey.lower()] = "uninstall_subkeys"
                out.append(ScanResult(
                    category="注册表垃圾",
                    source="无效的卸载残留(Uninstall)",
                    path=hive_name + "\\" + subkey,
                    size=len(junk) * 512,
                    description=(f"发现 {len(junk)} 个卸载项指向已不存在的位置（软件已卸载或移动）。"
                                 "清理这些残留项可让控制面板的软件列表更干净，安全可清理。"),
                ))
                if len(out) >= _MAX_RESULTS_PER_CAT:
                    break

        # 安装位置记录（InstallLocationsMRU）
        mru_key = r"SOFTWARE\Microsoft\Windows\CurrentVersion\InstallLocationsMRU"
        hive = winreg.HKEY_LOCAL_MACHINE
        junk = self._collect_junk_values(hive, mru_key, "value_data_path")
        if junk:
            self._scanned_keys[mru_key.lower()] = "value_data_path"
            out.append(ScanResult(
                category="注册表垃圾",
                source="无效的安装位置记录(InstallLocationsMRU)",
                path="HKEY_LOCAL_MACHINE\\" + mru_key,
                size=len(junk) * 512,
                description=(f"发现 {len(junk)} 条安装位置记录指向不存在的目录。"
                             "清理后不影响软件运行，安全可清理。"),
            ))
        return out[:_MAX_RESULTS_PER_CAT]

    def _scan_shareddlls(self):
        """扫描无效的 SharedDLLs 计数（值名指向不存在的文件）"""
        out = []
        containers = [
            ("HKEY_LOCAL_MACHINE", r"SOFTWARE\Microsoft\Windows\CurrentVersion\SharedDLLs"),
            ("HKEY_LOCAL_MACHINE", r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\SharedDLLs"),
        ]
        for hive_name, subkey in containers:
            hive = _ROOT_MAP[hive_name]
            junk = self._collect_junk_values(hive, subkey, "value_name_path")
            if junk:
                self._scanned_keys[subkey.lower()] = "value_name_path"
                out.append(ScanResult(
                    category="注册表垃圾",
                    source="无效的SharedDLLs计数",
                    path=hive_name + "\\" + subkey,
                    size=len(junk) * 512,
                    description=(f"发现 {len(junk)} 条共享 DLL 引用计数指向不存在的文件。"
                                 "清理无效计数可避免卸载软件时弹出“共享文件被占用”提示，安全可清理。"),
                ))
        return out[:_MAX_RESULTS_PER_CAT]

    def _scan_muicache(self):
        """扫描失效的 MUI Cache（缓存的应用路径已不存在）"""
        out = []
        containers = [
            ("HKEY_LOCAL_MACHINE", r"SOFTWARE\Microsoft\Windows\ShellNoRoam\MUICache"),
            ("HKEY_CURRENT_USER",
             r"Software\Classes\Local Settings\Software\Microsoft\Windows\Shell\MuiCache"),
        ]
        for hive_name, subkey in containers:
            hive = _ROOT_MAP[hive_name]
            junk = self._collect_junk_values(hive, subkey, "value_name_path")
            if junk:
                self._scanned_keys[subkey.lower()] = "value_name_path"
                out.append(ScanResult(
                    category="注册表垃圾",
                    source="失效的MUI缓存",
                    path=hive_name + "\\" + subkey,
                    size=len(junk) * 512,
                    description=(f"发现 {len(junk)} 条 MUI 缓存记录了已不存在的程序路径。"
                                 "清理后系统会按需重建，安全可清理。"),
                ))
        return out[:_MAX_RESULTS_PER_CAT]

    def _scan_app_paths(self):
        """扫描无效的 App Paths（子键默认值指向不存在的 exe）"""
        out = []
        containers = [
            ("HKEY_LOCAL_MACHINE", r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths"),
            ("HKEY_LOCAL_MACHINE", r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\App Paths"),
            ("HKEY_CURRENT_USER", r"Software\Microsoft\Windows\CurrentVersion\App Paths"),
        ]
        for hive_name, subkey in containers:
            hive = _ROOT_MAP[hive_name]
            junk = self._collect_junk_subkeys(hive, subkey, "apppaths_subkeys")
            if junk:
                self._scanned_keys[subkey.lower()] = "apppaths_subkeys"
                out.append(ScanResult(
                    category="注册表垃圾",
                    source="无效的App Paths",
                    path=hive_name + "\\" + subkey,
                    size=len(junk) * 512,
                    description=(f"发现 {len(junk)} 个 App Paths 注册项指向不存在的程序。"
                                 "清理后可避免“找不到程序”类报错，安全可清理。"),
                ))
        return out[:_MAX_RESULTS_PER_CAT]

    def _scan_run_entries(self):
        """扫描无效的启动项引用（Run / RunOnce 指向不存在的文件）"""
        out = []
        containers = [
            ("HKEY_CURRENT_USER", r"Software\Microsoft\Windows\CurrentVersion\Run"),
            ("HKEY_CURRENT_USER", r"Software\Microsoft\Windows\CurrentVersion\RunOnce"),
            ("HKEY_LOCAL_MACHINE", r"SOFTWARE\Microsoft\Windows\CurrentVersion\Run"),
            ("HKEY_LOCAL_MACHINE", r"SOFTWARE\Microsoft\Windows\CurrentVersion\RunOnce"),
            ("HKEY_LOCAL_MACHINE", r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Run"),
            ("HKEY_LOCAL_MACHINE", r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\RunOnce"),
        ]
        for hive_name, subkey in containers:
            hive = _ROOT_MAP[hive_name]
            junk = self._collect_junk_values(hive, subkey, "run_command")
            if junk:
                self._scanned_keys[subkey.lower()] = "run_command"
                out.append(ScanResult(
                    category="注册表垃圾",
                    source="无效的启动项引用",
                    path=hive_name + "\\" + subkey,
                    size=len(junk) * 512,
                    description=(f"发现 {len(junk)} 个开机自启动项指向已不存在的程序（通常是残留）。"
                                 "清理可加快开机速度，安全可清理。"),
                ))
        return out[:_MAX_RESULTS_PER_CAT]

    def _scan_software_paths(self):
        """
        扫描无效的软件路径引用。
        HKLM\\SOFTWARE 下键非常多，这里只做有上限的浅层遍历，挑明显失效的 exe 路径值。
        """
        out = []
        hive = winreg.HKEY_LOCAL_MACHINE
        top_keys = self._enum_subkeys(hive, "SOFTWARE", _MAX_SOFTWARE_KEYS * 2)
        scanned = 0
        for vendor in top_keys:
            if vendor.lower() in _SKIP_SOFTWARE_TOP:
                continue
            if scanned >= _MAX_SOFTWARE_KEYS:
                break
            scanned += 1
            vendor_key = "SOFTWARE\\" + vendor
            candidates = [(vendor, vendor_key)]
            for app in self._enum_subkeys(hive, vendor_key, _MAX_SOFTWARE_APPS):
                candidates.append((vendor + "\\" + app, vendor_key + "\\" + app))
            for label, key_path in candidates:
                junk = self._collect_junk_values(hive, key_path, "value_data_exe_path")
                if junk:
                    self._scanned_keys[key_path.lower()] = "value_data_exe_path"
                    out.append(ScanResult(
                        category="注册表垃圾",
                        source="无效的软件路径引用",
                        path="HKEY_LOCAL_MACHINE\\" + key_path,
                        size=len(junk) * 512,
                        description=(f"「{label}」下发现 {len(junk)} 个指向不存在程序的路径值。"
                                     "若该软件仍在使用，请勿清理；确认已卸载则可安全清理。"),
                    ))
                    if len(out) >= _MAX_RESULTS_PER_CAT:
                        return out
        return out

    def _scan_fonts(self):
        """扫描无效的字体记录（Fonts 键值指向不存在的 .ttf 文件）"""
        out = []
        containers = [
            ("HKEY_LOCAL_MACHINE", r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Fonts"),
            ("HKEY_CURRENT_USER", r"Software\Microsoft\Windows NT\CurrentVersion\Fonts"),
        ]
        for hive_name, subkey in containers:
            hive = _ROOT_MAP[hive_name]
            junk = self._collect_junk_values(hive, subkey, "fonts_values")
            if junk:
                self._scanned_keys[subkey.lower()] = "fonts_values"
                out.append(ScanResult(
                    category="注册表垃圾",
                    source="无效的字体记录",
                    path=hive_name + "\\" + subkey,
                    size=len(junk) * 512,
                    description=(f"发现 {len(junk)} 条字体注册记录指向不存在的字体文件。"
                                 "清理可让字体列表更整洁，安全可清理。"),
                ))
        return out[:_MAX_RESULTS_PER_CAT]

    def _scan_mru(self):
        """扫描无效的最近使用记录 MRU（记录路径已不存在）"""
        out = []
        containers = [
            ("HKEY_CURRENT_USER", r"Software\Microsoft\Windows\CurrentVersion\Explorer\RunMRU"),
            ("HKEY_CURRENT_USER", r"Software\Microsoft\Windows\CurrentVersion\Explorer\TypedPaths"),
        ]
        for hive_name, subkey in containers:
            hive = _ROOT_MAP[hive_name]
            junk = self._collect_junk_values(hive, subkey, "value_data_path")
            if junk:
                self._scanned_keys[subkey.lower()] = "value_data_path"
                out.append(ScanResult(
                    category="注册表垃圾",
                    source="无效的最近使用记录(MRU)",
                    path=hive_name + "\\" + subkey,
                    size=len(junk) * 512,
                    description=(f"发现 {len(junk)} 条最近使用记录指向已不存在的路径。"
                                 "清理有助于保护隐私，安全可清理。"),
                ))
        return out[:_MAX_RESULTS_PER_CAT]

    # ==================== 收集失效条目（扫描计数用） ====================

    def _collect_junk_values(self, hive, subkey, strategy):
        """返回该键中判定为无效的值名列表"""
        names = []
        for name, data, vtype in self._enum_values(hive, subkey):
            if self._is_value_junk(strategy, name, data, vtype):
                names.append(name)
        return names

    def _collect_junk_subkeys(self, hive, subkey, strategy):
        """返回该键中判定为无效的子键名列表"""
        names = []
        for name in self._enum_subkeys(hive, subkey):
            full = subkey + "\\" + name
            if self._is_subkey_junk(hive, full, strategy):
                names.append(name)
        return names

    # ==================== 注册表信息 ====================

    def get_registry_info(self):
        """
        获取各注册表 hive 文件大小。
        只读取文件大小，不尝试读取正在被占用的文件内容。
        """
        result = {"hive_sizes": [], "total_size": 0}
        try:
            config_dir = os.path.join(
                os.environ.get("SystemRoot", r"C:\Windows"), "System32", "config"
            )
            hive_names = ["SOFTWARE", "SYSTEM", "SAM", "SECURITY", "DEFAULT",
                          "DRIVERS", "ELAM", "userdiff", "BBI", "BCD00000000"]
            total = 0
            for name in hive_names:
                path = os.path.join(config_dir, name)
                try:
                    if os.path.exists(path):
                        size = get_size(path)
                        result["hive_sizes"].append({
                            "name": name,
                            "path": path,
                            "size": size,
                            "readable_size": format_size(size),
                        })
                        total += size
                except (OSError, PermissionError):
                    continue
            result["total_size"] = total
        except Exception:
            pass
        return result

    # ==================== 备份与还原 ====================

    def backup_registry(self, backup_dir=None):
        """
        用 reg.exe export 导出关键根键到备份文件。

        说明：导出操作本身只需读取权限，一般无需管理员；
        但导出 HKLM\\SOFTWARE 时若部分子键不可访问，导出文件可能不完整。
        返回 (成功, 备份文件路径字符串或错误信息)。
        """
        try:
            if backup_dir is None:
                backup_dir = os.path.join(
                    os.path.expanduser("~"), "CleanMasterBackup", "registry"
                )
            os.makedirs(backup_dir, exist_ok=True)
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")

            exports = [
                ("HKEY_CURRENT_USER", os.path.join(backup_dir, f"HKCU_{ts}.reg")),
                (r"HKEY_LOCAL_MACHINE\SOFTWARE", os.path.join(backup_dir, f"HKLM_SOFTWARE_{ts}.reg")),
            ]
            files = []
            errors = []
            for root, out in exports:
                try:
                    proc = subprocess.run(
                        ["reg", "export", root, out, "/y"],
                        capture_output=True, timeout=300
                    )
                    if os.path.exists(out) and os.path.getsize(out) > 0:
                        files.append(out)
                    else:
                        errors.append(f"{root} 导出失败(返回码 {proc.returncode})")
                except Exception as e:
                    errors.append(f"{root} 导出异常: {e}")

            if files:
                return True, "; ".join(files)
            return False, "备份失败：" + ("；".join(errors) if errors else "未生成任何备份文件")
        except Exception as e:
            return False, f"备份注册表失败: {e}"

    def restore_registry(self, backup_file):
        """
        用 reg.exe import 还原备份文件。
        需要管理员权限（导入 HKLM\\SOFTWARE 时）。
        返回 (成功, 说明或错误信息)。
        """
        try:
            if not backup_file or not isinstance(backup_file, str):
                return False, "未指定备份文件"
            if not os.path.isfile(backup_file):
                return False, f"备份文件不存在: {backup_file}"
            proc = subprocess.run(
                ["reg", "import", backup_file], capture_output=True, timeout=300
            )
            if proc.returncode == 0:
                return True, f"已从备份文件还原: {backup_file}"
            err = ""
            try:
                err = (proc.stderr or b"").decode("gbk", errors="ignore").strip()
            except Exception:
                err = ""
            return False, f"reg import 失败(返回码 {proc.returncode})" + (f": {err}" if err else "")
        except Exception as e:
            return False, f"还原注册表失败: {e}"

    # ==================== 系统还原点 ====================

    def create_restore_point(self, description="合枢 HS-清理前备份"):
        """
        创建系统还原点（需要管理员权限）。

        优先使用 PowerShell 的 Checkpoint-Computer；Windows 默认限制每天
        只能创建一次，失败概率较高。失败时依次回退：
          1) wmic ... SystemRestore call CreateRestorePoint
          2) 先用 Enable-ComputerRestore 启用系统还原，再重试 Checkpoint-Computer
        全部失败时返回 (False, 可读中文原因)，绝不抛异常。
        """
        last_reason = ""

        # 1) PowerShell Checkpoint-Computer
        try:
            script = ("Checkpoint-Computer -Description '%s' "
                      "-RestorePointType 'MODIFY_SETTINGS'" % description.replace("'", "''"))
            proc = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                capture_output=True, timeout=180
            )
            if proc.returncode == 0:
                return True, "系统还原点创建成功"
            last_reason = _decode_output(proc) or "Checkpoint-Computer 执行失败"
            low = last_reason.lower()
            if "disabled" in low or "系统还原" in last_reason or "未启用" in last_reason:
                last_reason = "系统还原功能未启用，" + last_reason
        except Exception as e:
            last_reason = f"Checkpoint-Computer 调用异常: {e}"

        # 2) wmic / SystemRestore（部分系统已移除 wmic，失败属正常）
        try:
            proc = subprocess.run(
                ["wmic", "/namespace:\\\\root\\default", "path", "SystemRestore",
                 "call", "CreateRestorePoint", description, "0", "100"],
                capture_output=True, timeout=180
            )
            out = _decode_output(proc)
            if proc.returncode == 0 and ("success" in out.lower() or "successful" in out.lower()):
                return True, "系统还原点创建成功（wmic 方式）"
            last_reason = last_reason or out or "wmic 创建还原点失败"
        except FileNotFoundError:
            last_reason = last_reason or "系统未提供 wmic 命令"
        except Exception as e:
            last_reason = last_reason or f"wmic 调用异常: {e}"

        # 3) 先启用系统还原，再重试
        try:
            subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command",
                 "Enable-ComputerRestore -Drive 'C:\\'"],
                capture_output=True, timeout=120
            )
            script = ("Checkpoint-Computer -Description '%s' "
                      "-RestorePointType 'MODIFY_SETTINGS'" % description.replace("'", "''"))
            proc = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                capture_output=True, timeout=180
            )
            if proc.returncode == 0:
                return True, "已启用系统还原并成功创建还原点"
            last_reason = _decode_output(proc) or last_reason or "启用系统还原后创建仍失败"
        except Exception as e:
            last_reason = last_reason or f"启用系统还原失败: {e}"

        if not last_reason:
            last_reason = "创建系统还原点失败（可能需要管理员权限，或当天已创建过还原点）"
        return False, last_reason

    # ==================== 清理注册表项 ====================

    def _resolve_static(self, subkey):
        """在静态白名单中查找匹配的清理策略（取最长前缀）"""
        try:
            key = subkey.lower().rstrip("\\")
            best = None
            for prefix, strategy in _ALLOWED_RULES:
                if key == prefix or key.startswith(prefix + "\\"):
                    if best is None or len(prefix) > best[1]:
                        best = (strategy, len(prefix))
            return best[0] if best else None
        except Exception:
            return None

    def _resolve_strategy(self, subkey):
        """解析清理策略：优先静态白名单，其次扫描阶段登记过的键"""
        strategy = self._resolve_static(subkey)
        if strategy:
            return strategy
        try:
            return self._scanned_keys.get(subkey.lower().rstrip("\\"))
        except Exception:
            return None

    def _delete_key(self, hive, subkey):
        """删除注册表键：优先 winreg，失败回退 reg.exe delete /f"""
        # DeleteKeyEx（64 位视图）
        try:
            winreg.DeleteKeyEx(hive, subkey, winreg.KEY_WOW64_64KEY)
            return True
        except Exception:
            pass
        # DeleteKey（默认视图）
        try:
            winreg.DeleteKey(hive, subkey)
            return True
        except Exception:
            pass
        # 回退 reg.exe
        try:
            hive_name = _HIVE_NAMES.get(hive)
            if hive_name:
                proc = subprocess.run(
                    ["reg", "delete", hive_name + "\\" + subkey, "/f"],
                    capture_output=True, timeout=30
                )
                return proc.returncode == 0
        except Exception:
            pass
        return False

    def _delete_value(self, hive, subkey, value_name):
        """删除注册表值：优先 winreg，失败回退 reg.exe delete /v /f"""
        key = self._open_key(hive, subkey, winreg.KEY_SET_VALUE)
        if key is not None:
            try:
                winreg.DeleteValue(key, value_name)
                return True
            except Exception:
                pass
            finally:
                try:
                    winreg.CloseKey(key)
                except OSError:
                    pass
        try:
            hive_name = _HIVE_NAMES.get(hive)
            if hive_name:
                proc = subprocess.run(
                    ["reg", "delete", hive_name + "\\" + subkey, "/v", value_name, "/f"],
                    capture_output=True, timeout=30
                )
                return proc.returncode == 0
        except Exception:
            pass
        return False

    def _clean_key_by_strategy(self, hive, subkey, strategy):
        """按策略清理某个键下的无效项，返回实际删除的条目数"""
        removed = 0
        if strategy in ("value_name_path", "value_data_path",
                        "value_data_exe_path", "run_command", "fonts_values"):
            # 值型垃圾：先收集后删除（边遍历边删会导致索引错位）
            try:
                junk = self._collect_junk_values(hive, subkey, strategy)
            except Exception:
                return 0
            for name in junk:
                if self._delete_value(hive, subkey, name):
                    removed += 1
            return removed

        if strategy in ("apppaths_subkeys", "uninstall_subkeys"):
            # 子键型垃圾：删除无效子键
            try:
                junk = self._collect_junk_subkeys(hive, subkey, strategy)
            except Exception:
                return 0
            for name in junk:
                if self._delete_key(hive, subkey + "\\" + name):
                    removed += 1
            return removed

        return 0

    def clean_registry_items(self, items):
        """
        清理用户勾选的注册表垃圾项（items 为 scan_registry_junk 返回的子集）。

        实现：从 .path 解析出根键与子路径，仅当该键命中白名单（静态规则或扫描阶段
        确认过的键）时才执行清理；值型垃圾删除对应无效值，子键型垃圾删除无效子键
        （winreg.DeleteValue/DeleteKey，失败回退 reg.exe /f）。
        不在白名单里的键一律跳过，并在日志中说明“出于安全考虑跳过”。

        需要管理员权限的部分：删除 HKLM 下的键/值；HKCU 下的无需管理员。
        返回 (成功清理的条目数, 日志列表)。绝不抛异常。
        """
        logs = []
        cleaned_items = 0
        total = len(items) if items else 0
        if total == 0:
            self._emit(100, "没有需要清理的注册表项")
            return 0, logs

        for idx, r in enumerate(items):
            try:
                self._emit(int(idx / total * 100), f"正在清理注册表: {getattr(r, 'source', '')}")
            except Exception:
                pass
            try:
                hive, subkey = _split_reg_path(getattr(r, "path", ""))
                if hive is None or not subkey:
                    logs.append(f"[跳过] 无法解析注册表路径: {getattr(r, 'path', '')}")
                    continue
                strategy = self._resolve_strategy(subkey)
                if not strategy:
                    logs.append(f"[跳过] 出于安全考虑跳过（不在可清理白名单内）: {r.path}")
                    continue
                if not _is_admin() and hive == winreg.HKEY_LOCAL_MACHINE:
                    # 非管理员时仍尝试，失败会在下面记录；这里不预先跳过
                    pass
                removed = self._clean_key_by_strategy(hive, subkey, strategy)
                if removed > 0:
                    cleaned_items += 1
                    logs.append(f"[成功] {r.source}: 清理 {removed} 个无效项 （{r.path}）")
                else:
                    logs.append(f"[无变化] {r.source}: 未发现可清理项或权限不足 （{r.path}）")
            except Exception as e:
                logs.append(f"[失败] {getattr(r, 'path', '')}: {e}")

        self._emit(100, f"注册表清理完成，成功清理 {cleaned_items} 项")
        return cleaned_items, logs

    def clean_registry_compact(self):
        """
        注册表压缩说明（本方法不执行任何实际操作）。

        Windows 不支持在系统运行期间压缩注册表 hive 文件，因此这里不会假装能压缩，
        而是明确返回 (False, 说明)。压缩 hive 需要在 WinPE 或恢复模式下离线操作；
        正常情况下，清理无效项后 hive 会在系统重启后由 Windows 自动整理部分冗余空间。
        """
        return False, (
            "Windows 不支持在系统运行期间压缩注册表 hive 文件，本工具不提供“在线压缩”，"
            "以免损坏系统。若确需压缩，请在 WinPE 或恢复模式下离线执行"
            "（例如：离线加载 hive 后导出并重新导入，或使用注册表编辑器离线整理）。"
            "替代方案：清理无效注册表项后，hive 会在系统重启后由 Windows 自动整理冗余空间；"
            "通常重启一次即可看到一定程度的体积回落。"
        )

    # ==================== 隐私开关（注册表级） ====================

    def get_privacy_switches(self):
        """
        返回当前系统隐私相关开关状态。每项为 dict：
        {"id", "name", "desc", "enabled", "root", "key", "value", "data", "need_admin"}
        其中 enabled 表示“是否已达到隐私保护状态”（即注册表当前值等于目标值）。
        读取失败或键不存在时按默认值处理，并在 desc 中标注。绝不抛异常。
        """
        switches = []
        try:
            admin = _is_admin()
        except Exception:
            admin = False

        for definition in _PRIVACY_DEFS:
            try:
                item = dict(definition)
                hive = _ROOT_MAP.get(str(item.get("root", "")).upper())
                cur_value = None
                value_exists = False
                key_exists = False
                if hive is not None:
                    cur_value, value_exists, key_exists = _read_dword(
                        hive, item["key"], item["value"]
                    )
                item["enabled"] = bool(value_exists and cur_value == item["data"])
                item["need_admin"] = bool(item.get("need_admin", False))

                desc = item["desc"]
                if not key_exists:
                    desc += "（当前系统版本未提供该注册表键，可能不支持此设置）"
                elif not value_exists:
                    desc += "（该项尚未设置，系统当前使用默认值）"
                elif item["enabled"]:
                    desc += "（当前已处于保护状态）"
                else:
                    desc += "（当前未处于保护状态，可一键收紧）"
                item["desc"] = desc
                switches.append(item)
            except Exception:
                continue
        return switches

    def apply_privacy_switches(self, switches):
        """
        应用用户勾选的隐私开关（来自 get_privacy_switches 的子集）。
        通过 winreg 写入/创建 DWORD 值；需要管理员而当前权限不足的项记录日志并跳过。
        返回 (成功项数, 日志列表)。绝不抛异常。
        """
        logs = []
        success = 0
        try:
            admin = _is_admin()
        except Exception:
            admin = False

        for sw in (switches or []):
            try:
                name = sw.get("name") or sw.get("id") or "未命名开关"
                hive = _ROOT_MAP.get(str(sw.get("root", "")).upper())
                key = sw.get("key")
                value = sw.get("value")
                data = sw.get("data", 0)
                need_admin = bool(sw.get("need_admin", False))

                if hive is None or not key or not value:
                    logs.append(f"[跳过] 无效的开关定义: {name}")
                    continue
                if need_admin and not admin:
                    logs.append(f"[跳过] “{name}”需要管理员权限，当前非管理员运行，未应用")
                    continue

                if _write_dword(hive, key, value, data):
                    success += 1
                    logs.append(f"[成功] 已应用: {name} = {data}")
                else:
                    logs.append(f"[失败] 无法写入注册表: {name}（可能权限不足或键被保护）")
            except Exception as e:
                logs.append(f"[失败] {sw.get('name', '未知')}: {e}")

        self._emit(100, f"隐私开关应用完成，成功 {success} 项")
        return success, logs


def _decode_output(proc):
    """把子进程输出解码为字符串（兼容 GBK/UTF-8）"""
    try:
        data = (proc.stdout or b"") + b"\n" + (proc.stderr or b"")
        if isinstance(data, bytes):
            for enc in ("gbk", "utf-8", "mbcs"):
                try:
                    return data.decode(enc, errors="strict").strip()
                except Exception:
                    continue
            return data.decode("gbk", errors="ignore").strip()
        return str(data).strip()
    except Exception:
        return ""
