# -*- coding: utf-8 -*-
"""
启动项 / 服务 / 计划任务 管理引擎（对标 Sysinternals Autoruns 的启动项全景 + 开机耗时分析）

设计约束：
1. 仅使用 Python 3.12 标准库（winreg / ctypes / subprocess / os / json / datetime 等），
   不引入任何第三方依赖，方便 PyInstaller 单文件打包。
2. 不含任何 tkinter / UI 代码，只提供纯数据接口与 (成功, 中文信息) 元组。
3. 所有系统调用均做容错处理，PermissionError / FileNotFoundError / OSError /
   subprocess.SubprocessError 一律捕获，异常绝不向外冒泡。
4. 所有列表数据都来自真实系统查询；查不到时返回空列表并在日志中说明，绝不伪造数据。
"""

import os
import re
import sys
import csv
import io
import json
import time
import uuid
import base64
import struct
import ctypes
import winreg
import datetime
import subprocess
from ctypes import wintypes

from scanner import ScanResult, get_size, format_size


# ============================================================================
# 模块级常量与工具
# ============================================================================

# 注册表根键名 -> winreg 句柄
_ROOT_MAP = {
    "HKEY_LOCAL_MACHINE": winreg.HKEY_LOCAL_MACHINE,
    "HKEY_CURRENT_USER": winreg.HKEY_CURRENT_USER,
    "HKEY_CLASSES_ROOT": winreg.HKEY_CLASSES_ROOT,
    "HKEY_USERS": winreg.HKEY_USERS,
}

# 自动清理计划任务的固定名称
AUTO_TASK_NAME = "C盘清理大师-自动清理"

# 备份根目录（相对于用户主目录）
BACKUP_SUBDIR = os.path.join("CleanMasterBackup", "startup")

# 路径 / 命令解析用的正则
_PATH_RE = re.compile(r'^([a-zA-Z]:\\|\\\\)')
_QUOTED_RE = re.compile(r'^"([^"]+)"')
_NOQUOTE_RE = re.compile(
    r'^(.*?\.(?:exe|com|bat|cmd|ps1|vbs|scr|msi|hta|js|jar|py|url|ini|lnk|dll))'
    r'(?=["\s,;]|$)', re.IGNORECASE)
_TIME_RE = re.compile(r'^\d{1,2}:\d{2}$')


def _is_admin():
    """判断当前进程是否具有管理员权限"""
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _decode_bytes(data):
    """把 subprocess 返回的字节流解码成文本（兼容 UTF-8 / GBK / OEM 代码页）"""
    if isinstance(data, str):
        return data
    if not data:
        return ""
    for enc in ("utf-8", "gbk", "latin-1"):
        try:
            return data.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode("utf-8", "ignore")


def _clean_ps_error(text):
    """
    清理 PowerShell 的错误输出：
    去掉 CLIXML 包装、_x000D__x000A_ 转义与冗长的源码回显，只保留可读的错误信息。
    """
    if not text:
        return ""
    text = text.strip()
    if text.startswith("#< CLIXML"):
        # 提取 <S S="Error">...</S> 中的文本
        parts = re.findall(r'<S S="Error">(.*?)</S>', text, re.DOTALL)
        text = " ".join(parts) if parts else ""
    text = text.replace("_x000D__x000A_", " ")
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    # 丢掉 "At line:..", "+ ...", "CategoryInfo", "FullyQualifiedErrorId" 这类噪音行
    useful = []
    for ln in lines:
        low = ln.lower()
        if ln.startswith("+") or low.startswith("at line:") or \
           low.startswith("categoryinfo") or low.startswith("fullyqualifiederrorid"):
            continue
        useful.append(ln)
    result = " ".join(useful).strip()
    return result[:300]


def _looks_like_path(text):
    """粗略判断字符串是否像磁盘路径（如 C:\\... 或 \\\\server\\...）"""
    return bool(text) and bool(_PATH_RE.match(text))


def _extract_exe_path(command):
    """
    从启动命令中解析出可执行文件路径。
    处理带引号、带参数、%环境变量%、rundll32/regsvr32 等常见形式。
    """
    if not command:
        return ""
    try:
        cmd = os.path.expandvars(command.strip())
    except (OSError, ValueError):
        cmd = command.strip()

    m = _QUOTED_RE.match(cmd)
    if m:
        cand = m.group(1).strip()
    else:
        m2 = _NOQUOTE_RE.match(cmd)
        if m2:
            cand = m2.group(1).strip()
        else:
            cand = cmd.split(" ")[0].strip().strip('"').rstrip(",;")

    base = os.path.basename(cand).lower()
    # rundll32 / regsvr32 的真实目标是其参数中的 dll
    if base in ("rundll32.exe", "regsvr32.exe"):
        rest = cmd[len(cand):].strip()
        m3 = _QUOTED_RE.match(rest)
        if m3:
            cand = m3.group(1).strip()
        else:
            for token in re.split(r'[\s,]+', rest):
                token = token.strip().strip('"')
                if token and not token.startswith("-") and not token.startswith("/"):
                    cand = token
                    break
    return cand


def _approved_sub_for(reg_key):
    """根据 Run/RunOnce 键路径推断 StartupApproved 子键名"""
    if not reg_key:
        return None
    if "WOW6432Node" in reg_key:
        return "Run32"
    if reg_key.endswith("RunOnce"):
        return "RunOnce"
    if reg_key.endswith("Run"):
        return "Run"
    return None


def _make_approved_blob(enabled):
    """
    构造 StartupApproved 的 REG_BINARY 值（12 字节）：
    首 4 字节 0x02 表示启用、0x03 表示禁用，其余 8 字节为该状态的 FILETIME 时间戳。
    """
    flag = 0x02 if enabled else 0x03
    try:
        ft = int((time.time() + 11644473600) * 10000000)
    except (OverflowError, ValueError):
        ft = 0
    return struct.pack("<I", flag) + struct.pack("<Q", ft)


def _suspicious_reasons(exe_path, command, publisher):
    """
    综合判断启动项是否可疑，返回可疑原因列表。
    判定偏保守，宁可少报也不误报。
    """
    reasons = []
    if not exe_path:
        return reasons
    low = (command or "").lower()
    p = exe_path.lower().replace("/", "\\")

    # 位于临时目录的常驻启动项
    temp_dirs = [
        (os.environ.get("TEMP") or "").lower().replace("/", "\\"),
        (os.environ.get("TMP") or "").lower().replace("/", "\\"),
        "\\appdata\\local\\temp\\",
    ]
    for d in temp_dirs:
        if d and d in p:
            reasons.append("程序位于临时目录，多为临时释放型/可疑程序")
            break

    # PowerShell 编码命令
    if "powershell" in low and ("-encodedcommand" in low or " -enc " in low or " -e " in low):
        reasons.append("使用 PowerShell 编码命令启动，常见于混淆脚本")

    # 脚本宿主 + 远程内容
    if any(t in low for t in ("mshta", "wscript", "cscript", "regsvr32", "certutil", "rundll32")):
        if "http://" in low or "https://" in low or "\\\\" in low:
            reasons.append("使用系统工具执行远程/网络内容")

    if "http://" in low or "https://" in low:
        reasons.append("启动命令中包含网络地址")

    # 无发布者信息且位于普通用户可写目录
    if not publisher and os.path.isfile(exe_path):
        for d in ("\\appdata\\", "\\users\\public\\", "\\programdata\\", "\\downloads\\"):
            if d in p:
                reasons.append("无发布者（版本）信息且位于普通用户可写目录")
                break

    # 去重并保持顺序
    seen = set()
    unique = []
    for r in reasons:
        if r not in seen:
            seen.add(r)
            unique.append(r)
    return unique


# ---------------------------- 文件版本信息（发布者） ----------------------------

_PUBLISHER_CACHE = {}
_VERSION_DLL = None
_VERSION_DLL_READY = False


class _LANGANDCODEPAGE(ctypes.Structure):
    """version.dll 返回的语言/代码页结构"""
    _fields_ = [("wLanguage", wintypes.WORD), ("wCodePage", wintypes.WORD)]


def _init_version_dll():
    """延迟加载 version.dll 并声明函数签名，失败时返回 None"""
    global _VERSION_DLL, _VERSION_DLL_READY
    if _VERSION_DLL_READY:
        return _VERSION_DLL
    _VERSION_DLL_READY = True
    try:
        dll = ctypes.WinDLL("version.dll")
        dll.GetFileVersionInfoSizeW.restype = wintypes.DWORD
        dll.GetFileVersionInfoSizeW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(wintypes.DWORD)]
        dll.GetFileVersionInfoW.restype = wintypes.BOOL
        dll.GetFileVersionInfoW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID]
        dll.VerQueryValueW.restype = wintypes.BOOL
        dll.VerQueryValueW.argtypes = [wintypes.LPCVOID, wintypes.LPCWSTR,
                                       ctypes.POINTER(wintypes.LPVOID), ctypes.POINTER(wintypes.UINT)]
        _VERSION_DLL = dll
    except (OSError, AttributeError):
        _VERSION_DLL = None
    return _VERSION_DLL


def _get_file_publisher(path):
    """
    读取文件版本信息中的 CompanyName 作为发布者。
    读不到一律返回空字符串，绝不用文件名瞎猜。
    """
    if not path:
        return ""
    cache_key = path.lower()
    if cache_key in _PUBLISHER_CACHE:
        return _PUBLISHER_CACHE[cache_key]

    company = ""
    try:
        if not os.path.isfile(path):
            _PUBLISHER_CACHE[cache_key] = ""
            return ""
        dll = _init_version_dll()
        if not dll:
            return ""

        dummy = wintypes.DWORD(0)
        size = dll.GetFileVersionInfoSizeW(path, ctypes.byref(dummy))
        if not size:
            _PUBLISHER_CACHE[cache_key] = ""
            return ""

        buf = ctypes.create_string_buffer(size)
        if not dll.GetFileVersionInfoW(path, 0, size, buf):
            _PUBLISHER_CACHE[cache_key] = ""
            return ""

        trans_ptr = wintypes.LPVOID()
        trans_len = wintypes.UINT()
        if not dll.VerQueryValueW(buf, "\\VarFileInfo\\Translation",
                                  ctypes.byref(trans_ptr), ctypes.byref(trans_len)):
            _PUBLISHER_CACHE[cache_key] = ""
            return ""
        if trans_len.value < ctypes.sizeof(_LANGANDCODEPAGE) or not trans_ptr:
            _PUBLISHER_CACHE[cache_key] = ""
            return ""

        lp = ctypes.cast(trans_ptr, ctypes.POINTER(_LANGANDCODEPAGE))
        lang = lp.contents.wLanguage
        codepage = lp.contents.wCodePage
        sub = "\\StringFileInfo\\%04x%04x\\CompanyName" % (lang, codepage)

        val_ptr = wintypes.LPVOID()
        val_len = wintypes.UINT()
        if not dll.VerQueryValueW(buf, sub, ctypes.byref(val_ptr), ctypes.byref(val_len)):
            _PUBLISHER_CACHE[cache_key] = ""
            return ""
        if not val_ptr:
            _PUBLISHER_CACHE[cache_key] = ""
            return ""
        company = ctypes.wstring_at(val_ptr).strip()
    except (OSError, ValueError, TypeError, ctypes.ArgumentError):
        company = ""

    _PUBLISHER_CACHE[cache_key] = company
    return company


# ============================================================================
# 服务建议规则（保守判定，只对明确安全的服务给出建议）
# ============================================================================

# 明确建议可改为手动的服务（键为服务名小写）
_MANUAL_SERVICE_RULES = {
    "xblauthmanager": "Xbox Live 身份验证管理器，仅在玩 Xbox 游戏时需要，可改为手动",
    "xblgamesave": "Xbox 游戏存档服务，普通办公用户不使用，可改为手动",
    "xboxgipsvc": "Xbox 手柄输入服务，不接手柄时不需要，可改为手动",
    "xboxnetapisvc": "Xbox 网络 API 服务，非游戏场景可改为手动",
    "fax": "传真服务，绝大多数用户不使用，可改为手动",
    "remoteregistry": "远程注册表服务，仅远程管理时需要，可改为手动（更安全）",
    "remoteaccess": "路由和远程访问服务，普通用户不使用，可改为手动",
    "spooler": "打印后台处理程序，不常打印时可改为手动（打印时系统会自动启动）",
    "w32time": "Windows 时间同步服务，可改为手动（系统需要时会自动触发校时）",
    "walletService": "钱包服务，国内用户基本不使用",
}

# 建议可直接禁用的第三方/可选服务（键为服务名小写）
_DISABLE_SERVICE_RULES = {
    "bonjourservice": "Apple Bonjour 局域网设备发现服务，多数用户不需要（禁用后会影响 iTunes/AirPlay 的局域网发现）",
}

# 必须保留的系统关键服务示例（仅用于辅助判定，命中时不建议改动）
_KEEP_SERVICE_NAMES = {
    "rpcss", "dcomlaunch", "eventlog", "plugplay", "power", "profileservice",
    "schedule", "themes", "lanmanworkstation", "lanmanserver", "dhcp",
    "dnscache", "audiosrv", "audioendpointbuilder", "wudf",
    "windefend", "securityhealthservice", "bfe", "mpssvc",
}


def _suggest_for_service(name, display, path, publisher):
    """
    给出服务的处置建议。返回 (suggest, reason)。
    判定非常保守：只有命中明确白名单规则时才建议改动，否则一律“建议保留”。
    """
    lname = (name or "").lower()
    ldisplay = (display or "").lower()
    lpub = (publisher or "").lower()

    if lname in _DISABLE_SERVICE_RULES:
        return "可禁用", _DISABLE_SERVICE_RULES[lname]

    if lname in _MANUAL_SERVICE_RULES:
        return "可改为手动", _MANUAL_SERVICE_RULES[lname]

    if lname in _KEEP_SERVICE_NAMES:
        return "建议保留", "系统关键服务，禁用可能影响系统正常运行"

    # Xbox 系列以显示名兜底判断
    if "xbox" in ldisplay:
        return "可改为手动", "Xbox 相关服务，非游戏场景可改为手动"

    # 传真服务以显示名兜底判断
    if "fax" in ldisplay or "传真" in display:
        return "可改为手动", "传真服务，多数用户不使用，可改为手动"

    # 第三方软件更新服务：非微软发布、且名称/显示名含更新字样
    if publisher and "microsoft" not in lpub:
        if any(k in lname for k in ("update", "updater")) or \
           any(k in ldisplay for k in ("update", "update", "更新", "升级")):
            return "可改为手动", ("第三方软件更新服务（发布者：%s），非系统必需，"
                                  "改为手动可减少后台占用，但可能延迟该软件的自动更新" % publisher)

    return "建议保留", "系统或常用软件服务，未发现明确的不必要依据，建议保留"


# ============================================================================
# 启动项引擎
# ============================================================================

class StartupEngine:
    """启动项 / 服务 / 计划任务 管理引擎"""

    def __init__(self, progress_callback=None):
        self.progress_callback = progress_callback
        # 计划任务查询结果缓存：(时间戳, 结果)，避免重复执行 schtasks
        self._task_cache = None
        # 记录枚举过程中的提示信息
        self._notes = []

    # ------------------------------ 基础工具 ------------------------------

    def _emit(self, percent, message):
        """上报进度（percent 0-100），回调异常不影响引擎"""
        if not self.progress_callback:
            return
        try:
            self.progress_callback(percent, message)
        except Exception:
            pass

    def _log(self, message):
        """记录一条普通日志（用 0 进度上报，仅带消息）"""
        self._notes.append(message)
        self._emit(0, message)

    def _run_command(self, args, timeout=30):
        """
        执行外部命令。返回 (是否成功, 标准输出文本, 错误说明)。
        超时 / 命令不存在 / 其他异常均友好返回，不抛出。
        """
        try:
            proc = subprocess.run(args, capture_output=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            return False, "", "命令执行超时（超过 %d 秒）" % timeout
        except (OSError, subprocess.SubprocessError) as exc:
            return False, "", "无法执行命令：%s" % exc
        out = _decode_bytes(proc.stdout)
        err = _decode_bytes(proc.stderr)
        if proc.returncode != 0:
            return False, out, err or ("命令返回码 %d" % proc.returncode)
        return True, out, err

    def _run_powershell(self, script, timeout=30):
        """执行 PowerShell 脚本片段（自动把输出编码设为 UTF-8，并清理错误输出的噪音）"""
        prefix = "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8;"
        ok, out, err = self._run_command(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", prefix + script],
            timeout=timeout)
        return ok, out, _clean_ps_error(err)

    def _backup_dir(self):
        """返回备份目录（不存在时创建），失败返回 None"""
        try:
            home = os.path.expanduser("~")
        except (OSError, RuntimeError):
            home = os.environ.get("USERPROFILE", "")
        if not home:
            return None
        path = os.path.join(home, BACKUP_SUBDIR)
        try:
            os.makedirs(path, exist_ok=True)
        except (PermissionError, OSError):
            return None
        return path

    # ------------------------- 启动项构造与状态读取 -------------------------

    def _read_startup_approved(self, root_name, approved_sub, value_name):
        """
        读取 StartupApproved 中的启用状态。
        返回 True(启用) / False(禁用) / None(无记录，视为启用)。
        """
        root = _ROOT_MAP.get(root_name)
        if root is None or not approved_sub:
            return None
        sub_path = (r"SOFTWARE\Microsoft\Windows\CurrentVersion\Explorer"
                    r"\StartupApproved\%s" % approved_sub)
        try:
            key = winreg.OpenKey(root, sub_path, 0, winreg.KEY_READ)
        except (FileNotFoundError, PermissionError, OSError):
            return None
        try:
            data, vtype = winreg.QueryValueEx(key, value_name)
        except (FileNotFoundError, PermissionError, OSError):
            return None
        finally:
            try:
                winreg.CloseKey(key)
            except OSError:
                pass
        if vtype != winreg.REG_BINARY or not isinstance(data, (bytes, bytearray)) or len(data) == 0:
            return None
        # 首字节最低位为 1 表示禁用（0x02 启用，0x03 禁用）
        return (data[0] & 0x01) == 0

    def _build_item(self, location, name, command, source, reg_root=None, reg_key=None,
                    reg_value=None, startup_folder_file=None, enabled=True):
        """构造统一的启动项 dict，并完成路径解析 / 存在性 / 大小 / 发布者 / 风险判定"""
        command = command or ""
        exe = _extract_exe_path(command) if command else ""
        is_real_path = _looks_like_path(exe)

        if is_real_path:
            try:
                path_exists = os.path.exists(exe)
            except (OSError, ValueError):
                path_exists = False
        else:
            # 非真实路径（如 COM 处理程序、GUID、空命令）无法判定，按“存在”处理避免误报
            path_exists = True

        size = 0
        publisher = ""
        if is_real_path and path_exists:
            try:
                size = get_size(exe)
            except (OSError, PermissionError):
                size = 0
            publisher = _get_file_publisher(exe)

        reasons = _suspicious_reasons(exe, command, publisher) if is_real_path else []

        if is_real_path and not path_exists:
            risk = "失效"
        elif reasons:
            risk = "可疑"
        else:
            risk = "正常"

        item_id = "%s|%s|%s" % (location, reg_key or startup_folder_file or "", name)
        return {
            # ---- 规范要求的字段 ----
            "id": item_id,
            "location": location,
            "name": name,
            "command": command,
            "publisher": publisher,
            "enabled": bool(enabled),
            "path_exists": bool(path_exists),
            "size": size,
            "risk": risk,
            "source": source or "",
            "reg_root": reg_root,
            "reg_key": reg_key,
            "reg_value": reg_value,
            "startup_folder_file": startup_folder_file,
            # ---- 供后续处置与说明使用 ----
            "exe_path": exe,
            "suspect_reasons": reasons,
        }

    # ------------------------------ 各类枚举 ------------------------------

    def _enum_run_key(self, root_name, sub_key, location):
        """枚举单个注册表 Run / RunOnce 键下的所有自启动值"""
        result = []
        root = _ROOT_MAP.get(root_name)
        if root is None:
            return result
        try:
            key = winreg.OpenKey(root, sub_key, 0, winreg.KEY_READ)
        except (FileNotFoundError, PermissionError, OSError):
            # 键不存在或无权限属于正常情况（部分系统没有 RunOnce）
            return result
        try:
            approved_sub = _approved_sub_for(sub_key)
            index = 0
            while index < 500:
                try:
                    value_name, value_data, _vtype = winreg.EnumValue(key, index)
                except OSError:
                    break
                index += 1
                if not isinstance(value_data, str):
                    value_data = str(value_data)
                command = value_data.strip()

                enabled = True
                state = self._read_startup_approved(root_name, approved_sub, value_name) \
                    if approved_sub else None
                if state is not None:
                    enabled = state

                result.append(self._build_item(
                    location=location,
                    name=value_name or "(默认值)",
                    command=command,
                    source="注册表自启动项 %s\\%s" % (root_name, sub_key),
                    reg_root=root_name,
                    reg_key=sub_key,
                    reg_value=value_name,
                    enabled=enabled,
                ))
        finally:
            try:
                winreg.CloseKey(key)
            except OSError:
                pass
        return result

    def _resolve_lnk_targets(self, lnk_paths):
        """批量解析 .lnk 快捷方式的目标路径，返回 {lnk路径: 目标路径}"""
        mapping = {}
        if not lnk_paths:
            return mapping
        quoted = ",".join("'" + p.replace("'", "''") + "'" for p in lnk_paths)
        script = ("$shell=New-Object -ComObject WScript.Shell;"
                  "$list=@(" + quoted + ");"
                  "foreach($p in $list){ $t='';"
                  "try{$t=$shell.CreateShortcut($p).TargetPath}catch{};"
                  "Write-Output ($p + [char]9 + $t) }")
        ok, out, _err = self._run_powershell(script, timeout=20)
        if not ok:
            return mapping
        for line in out.splitlines():
            if "\t" not in line:
                continue
            src, _sep, target = line.partition("\t")
            src = src.strip()
            target = target.strip()
            if src:
                mapping[src] = target
        return mapping

    def _scan_startup_folders(self):
        """扫描当前用户与全局的“启动”文件夹"""
        result = []
        candidates = [
            ("启动文件夹 (当前用户)",
             os.path.join(os.environ.get("APPDATA", ""),
                          r"Microsoft\Windows\Start Menu\Programs\Startup"),
             "HKEY_CURRENT_USER"),
            ("启动文件夹 (所有用户)",
             os.path.join(os.environ.get("ProgramData", r"C:\ProgramData"),
                          r"Microsoft\Windows\Start Menu\Programs\StartUp"),
             "HKEY_LOCAL_MACHINE"),
        ]
        for location, folder, root_name in candidates:
            if not folder or not os.path.isdir(folder):
                self._log("启动文件夹不存在，跳过：%s" % folder)
                continue
            try:
                entries = os.listdir(folder)
            except (PermissionError, OSError) as exc:
                self._log("无法读取启动文件夹 %s：%s" % (folder, exc))
                continue
            entries = [e for e in entries[:200]
                       if os.path.isfile(os.path.join(folder, e))
                       and e.lower() not in ("desktop.ini", "thumbs.db")]

            lnk_paths = [os.path.join(folder, e) for e in entries if e.lower().endswith(".lnk")]
            targets = self._resolve_lnk_targets(lnk_paths)

            for fname in entries:
                full = os.path.join(folder, fname)
                command = full
                if fname.lower().endswith(".lnk"):
                    target = targets.get(full, "")
                    if target:
                        command = target

                # 已移动到 __disabled__ 子目录的文件不会出现在这里；
                # 这里只能依据 StartupApproved 判断启用状态
                enabled = True
                state = self._read_startup_approved(root_name, "StartupFolder", fname)
                if state is not None:
                    enabled = state

                item = self._build_item(
                    location=location,
                    name=fname,
                    command=command,
                    source="启动文件夹 %s" % folder,
                    reg_root=root_name,
                    startup_folder_file=full,
                    enabled=enabled,
                )
                result.append(item)

            # 同时把已被禁用到 __disabled__ 目录的项列出来，便于恢复
            disabled_dir = os.path.join(folder, "__disabled__")
            if os.path.isdir(disabled_dir):
                try:
                    d_entries = [e for e in os.listdir(disabled_dir)[:200]
                                 if os.path.isfile(os.path.join(disabled_dir, e))]
                except (PermissionError, OSError):
                    d_entries = []
                d_lnk = [os.path.join(disabled_dir, e) for e in d_entries
                         if e.lower().endswith(".lnk")]
                d_targets = self._resolve_lnk_targets(d_lnk)
                for fname in d_entries:
                    full = os.path.join(disabled_dir, fname)
                    command = full
                    if fname.lower().endswith(".lnk") and d_targets.get(full):
                        command = d_targets[full]
                    result.append(self._build_item(
                        location=location,
                        name=fname,
                        command=command,
                        source="启动文件夹 %s（已禁用）" % disabled_dir,
                        reg_root=root_name,
                        startup_folder_file=full,
                        enabled=False,
                    ))
        return result

    def _scan_winlogon(self):
        """检查 Winlogon 的 Userinit / Shell，被改写则标记可疑"""
        result = []
        sub = r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon"
        try:
            key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, sub, 0, winreg.KEY_READ)
        except (FileNotFoundError, PermissionError, OSError):
            self._log("无法读取 Winlogon 注册表项（可能权限不足），跳过")
            return result
        try:
            expects = {"Userinit": "userinit.exe", "Shell": "explorer.exe"}
            for value_name, expect_base in expects.items():
                try:
                    data, _t = winreg.QueryValueEx(key, value_name)
                except (FileNotFoundError, PermissionError, OSError):
                    continue
                command = str(data).strip()
                item = self._build_item(
                    location="登录脚本 Winlogon",
                    name=value_name,
                    command=command,
                    source="Winlogon 登录项 %s" % sub,
                    reg_root="HKEY_LOCAL_MACHINE",
                    reg_key=sub,
                    reg_value=value_name,
                )
                exe = item.get("exe_path") or ""
                base = os.path.basename(exe).lower() if exe else ""
                if base != expect_base:
                    if item["risk"] == "正常":
                        item["risk"] = "可疑"
                    item["suspect_reasons"].append(
                        "Winlogon 的 %s 值与系统默认(%s)不一致，可能被第三方或恶意程序劫持"
                        % (value_name, expect_base))
                result.append(item)
        finally:
            try:
                winreg.CloseKey(key)
            except OSError:
                pass
        return result

    def _scan_auto_services_for_startup(self, limit=200):
        """枚举 Start=2（自动启动）的服务，最多列出 limit 条"""
        result = []
        base = r"SYSTEM\CurrentControlSet\Services"
        try:
            root_key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, base, 0, winreg.KEY_READ)
        except (FileNotFoundError, PermissionError, OSError):
            self._log("无法读取服务注册表项（需要管理员权限），跳过自动启动服务")
            return result
        total_auto = 0
        try:
            index = 0
            while index < 5000:
                try:
                    svc_name = winreg.EnumKey(root_key, index)
                except OSError:
                    break
                index += 1
                try:
                    sk = winreg.OpenKey(root_key, svc_name, 0, winreg.KEY_READ)
                except (FileNotFoundError, PermissionError, OSError):
                    continue
                try:
                    try:
                        start, _ = winreg.QueryValueEx(sk, "Start")
                    except (FileNotFoundError, OSError):
                        continue
                    if start != 2:
                        continue
                    # 只列用户态“服务”，跳过内核/文件系统驱动（Type 不含 0x30 即为驱动）
                    try:
                        svc_type, _ = winreg.QueryValueEx(sk, "Type")
                    except (FileNotFoundError, OSError):
                        svc_type = 0x20
                    if not (int(svc_type) & 0x30):
                        continue
                    total_auto += 1

                    try:
                        image, _ = winreg.QueryValueEx(sk, "ImagePath")
                    except (FileNotFoundError, OSError):
                        image = ""
                    try:
                        disp, _ = winreg.QueryValueEx(sk, "DisplayName")
                    except (FileNotFoundError, OSError):
                        disp = svc_name

                    if len(result) >= limit:
                        continue
                    item = self._build_item(
                        location="自动启动服务",
                        name=svc_name,
                        command=str(image),
                        source="自动启动服务 Start=2（%s\\%s）" % (base, svc_name),
                        reg_root="HKEY_LOCAL_MACHINE",
                        reg_key=base + "\\" + svc_name,
                        reg_value="Start",
                    )
                    item["display_name"] = str(disp)
                    result.append(item)
                finally:
                    try:
                        winreg.CloseKey(sk)
                    except OSError:
                        pass
        finally:
            try:
                winreg.CloseKey(root_key)
            except OSError:
                pass

        if total_auto > limit:
            self._log("自动启动服务共 %d 个，已超出上限，仅列出前 %d 个（可在“服务管理”页查看完整列表）"
                      % (total_auto, limit))
        return result

    def _scan_shell_execute_hooks(self):
        """枚举 ShellExecuteHooks（浏览器壳扩展劫持点）"""
        result = []
        sub = r"SOFTWARE\Microsoft\Windows\CurrentVersion\Explorer\ShellExecuteHooks"
        try:
            key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, sub, 0, winreg.KEY_READ)
        except (FileNotFoundError, PermissionError, OSError):
            return result
        try:
            index = 0
            while index < 100:
                try:
                    name, data, _t = winreg.EnumValue(key, index)
                except OSError:
                    break
                index += 1
                command = str(data).strip()
                if not command:
                    continue
                result.append(self._build_item(
                    location="Shell扩展加载点",
                    name=name or "(默认值)",
                    command=command,
                    source="ShellExecuteHooks 壳扩展加载点（易被浏览器劫持）",
                    reg_root="HKEY_LOCAL_MACHINE",
                    reg_key=sub,
                    reg_value=name,
                ))
        finally:
            try:
                winreg.CloseKey(key)
            except OSError:
                pass
        return result

    def _scan_tasks_for_startup(self, limit=300):
        """把非微软自带的计划任务纳入启动项全景"""
        result = []
        for task in self.scan_scheduled_tasks():
            if task.get("is_microsoft"):
                continue
            if len(result) >= limit:
                self._log("非微软计划任务超过 %d 个，仅列出前 %d 个" % (limit, limit))
                break
            item = self._build_item(
                location="计划任务",
                name=task.get("name", ""),
                command=task.get("command", ""),
                source="计划任务（路径：%s，状态：%s，计划：%s）"
                       % (task.get("path", ""), task.get("status", ""), task.get("schedule", "")),
                reg_root=None,
                reg_key=None,
                reg_value=None,
                enabled=(task.get("status", "").lower() != "disabled"),
            )
            item["task_name"] = task.get("name", "")
            result.append(item)
        return result

    # ======================= 启动项全景枚举（核心） =======================

    def scan_startup_items(self):
        """
        枚举所有自启动位置，返回启动项 dict 列表。
        覆盖：Run/RunOnce（HKLM/HKCU/32位兼容）、用户与全局启动文件夹、
        Winlogon 登录项、自动启动服务、ShellExecuteHooks、计划任务。
        """
        items = []
        self._emit(1, "正在枚举启动项...")

        reg_targets = [
            ("HKEY_LOCAL_MACHINE", r"SOFTWARE\Microsoft\Windows\CurrentVersion\Run",
             "注册表Run (HKLM)"),
            ("HKEY_LOCAL_MACHINE", r"SOFTWARE\Microsoft\Windows\CurrentVersion\RunOnce",
             "注册表RunOnce (HKLM)"),
            ("HKEY_CURRENT_USER", r"SOFTWARE\Microsoft\Windows\CurrentVersion\Run",
             "注册表Run (HKCU)"),
            ("HKEY_CURRENT_USER", r"SOFTWARE\Microsoft\Windows\CurrentVersion\RunOnce",
             "注册表RunOnce (HKCU)"),
            ("HKEY_LOCAL_MACHINE",
             r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Run",
             "注册表Run (HKLM 32位兼容)"),
        ]
        for idx, (root_name, sub_key, location) in enumerate(reg_targets):
            self._emit(3 + idx * 4, "正在读取 %s ..." % location)
            items.extend(self._enum_run_key(root_name, sub_key, location))

        self._emit(25, "正在读取启动文件夹...")
        items.extend(self._scan_startup_folders())

        self._emit(32, "正在检查 Winlogon 登录项...")
        items.extend(self._scan_winlogon())

        self._emit(38, "正在枚举自动启动服务（数量可能较多，请稍候）...")
        items.extend(self._scan_auto_services_for_startup())

        self._emit(72, "正在检查 Shell 扩展加载点...")
        items.extend(self._scan_shell_execute_hooks())

        self._emit(78, "正在枚举计划任务...")
        items.extend(self._scan_tasks_for_startup())

        self._emit(100, "启动项枚举完成，共发现 %d 项" % len(items))
        return items

    # ============================ 启用 / 禁用 ============================

    def _set_registry_item_enabled(self, item, enabled):
        """通过 StartupApproved 二进制值启用/禁用注册表启动项"""
        reg_key = item.get("reg_key") or ""
        if "RunOnce" in reg_key:
            return False, "RunOnce 项属于一次性启动任务，系统不支持通过 StartupApproved 禁用，请直接删除该项"
        sub = _approved_sub_for(reg_key)
        if not sub:
            return False, "该启动项不支持启用/禁用操作（请在对应的服务页或计划任务页处理）"

        reg_root = item.get("reg_root") or "HKEY_CURRENT_USER"
        root = _ROOT_MAP.get(reg_root)
        if root is None:
            return False, "不支持的注册表根键：%s" % reg_root

        value_name = item.get("reg_value") or ""
        sub_path = (r"SOFTWARE\Microsoft\Windows\CurrentVersion\Explorer"
                    r"\StartupApproved\%s" % sub)
        blob = _make_approved_blob(enabled)
        try:
            key = winreg.CreateKeyEx(root, sub_path, 0, winreg.KEY_SET_VALUE)
        except PermissionError:
            return False, "需要管理员权限才能修改系统级(HKLM)启动项状态"
        except OSError as exc:
            return False, "无法创建启用状态记录键：%s" % exc
        try:
            winreg.SetValueEx(key, value_name, 0, winreg.REG_BINARY, blob)
        except PermissionError:
            return False, "需要管理员权限才能修改系统级(HKLM)启动项状态"
        except OSError as exc:
            return False, "写入启用状态失败：%s" % exc
        finally:
            try:
                winreg.CloseKey(key)
            except OSError:
                pass

        action = "已启用" if enabled else "已禁用"
        return True, "%s启动项“%s”" % (action, item.get("name", ""))

    def _set_folder_item_enabled(self, item, enabled):
        """通过移动到同目录 __disabled__ 子文件夹来启用/禁用启动文件夹中的文件"""
        path = item.get("startup_folder_file")
        if not path:
            return False, "缺少启动文件夹文件路径，无法操作"
        folder = os.path.dirname(path)
        base = os.path.basename(path)
        disabled_dir = os.path.join(folder, "__disabled__")
        already_disabled = os.path.basename(folder) == "__disabled__"

        if enabled:
            if not already_disabled:
                return True, "启动项“%s”当前已处于启用状态" % base
            target = os.path.join(os.path.dirname(folder), base)
            try:
                os.replace(path, target)
            except (PermissionError, OSError) as exc:
                return False, "恢复启动项失败：%s" % exc
            item["startup_folder_file"] = target
            return True, "已启用启动项“%s”" % base
        else:
            if already_disabled:
                return True, "启动项“%s”当前已处于禁用状态" % base
            try:
                os.makedirs(disabled_dir, exist_ok=True)
                os.replace(path, os.path.join(disabled_dir, base))
            except (PermissionError, OSError) as exc:
                return False, "禁用启动项失败：%s" % exc
            item["startup_folder_file"] = os.path.join(disabled_dir, base)
            return True, "已禁用启动项“%s”（已移动到 __disabled__ 目录，可随时恢复）" % base

    def set_startup_item_enabled(self, item, enabled):
        """启用/禁用指定启动项，返回 (成功, 中文信息)"""
        if not item:
            return False, "启动项信息为空"
        try:
            if item.get("startup_folder_file"):
                return self._set_folder_item_enabled(item, enabled)

            location = item.get("location") or ""
            if location in ("自动启动服务",):
                return False, "服务类启动项请在“服务管理”页单独操作"
            if location == "计划任务":
                name = item.get("task_name") or item.get("name") or ""
                return self.set_scheduled_task_enabled(name, enabled)

            reg_key = item.get("reg_key") or ""
            if "CurrentVersion\\Run" in reg_key and "ShellExecuteHooks" not in reg_key:
                return self._set_registry_item_enabled(item, enabled)
            if "ShellExecuteHooks" in reg_key:
                return False, "Shell 扩展加载点不建议通过此方式禁用，请使用“删除”处理"
            if "Winlogon" in reg_key:
                return False, "Winlogon 登录项涉及系统登录流程，不建议禁用，如需修改请在注册表编辑器中谨慎操作"
            return False, "该启动项类型不支持启用/禁用操作"
        except (PermissionError, FileNotFoundError, OSError) as exc:
            return False, "操作失败：%s" % exc

    # ============================== 删除 / 恢复 ==============================

    def delete_startup_item(self, item):
        """
        删除注册表启动值或启动文件夹中的文件。
        删除前会把原值/原文件备份到 用户目录\\CleanMasterBackup\\startup\\，以便恢复。
        """
        if not item:
            return False, "启动项信息为空"
        bdir = self._backup_dir()
        if not bdir:
            return False, "无法创建备份目录，为安全起见已取消删除"
        now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        bid = uuid.uuid4().hex[:12]

        try:
            # ---------- 情况一：启动文件夹里的文件 ----------
            if item.get("startup_folder_file"):
                src = item["startup_folder_file"]
                if not os.path.isfile(src):
                    return False, "文件不存在或已被删除：%s" % src
                base_name = os.path.basename(src)
                backup_file = os.path.join(bdir, "startup_%s_%s" % (bid, base_name))
                try:
                    with open(src, "rb") as fin:
                        payload = fin.read()
                    with open(backup_file, "wb") as fout:
                        fout.write(payload)
                    os.remove(src)
                except (PermissionError, OSError) as exc:
                    return False, "删除启动项文件失败：%s" % exc

                meta = {
                    "id": bid,
                    "type": "file",
                    "name": item.get("name", base_name),
                    "command": item.get("command", ""),
                    "backup_file": backup_file,
                    "original_path": src,
                    "time": now_str,
                }
                if not self._write_backup_meta(bdir, bid, meta):
                    return False, "备份记录写入失败，文件已移动到备份目录：%s" % backup_file
                return True, "已删除并备份启动项文件“%s”" % base_name

            # ---------- 情况二：注册表启动值 ----------
            reg_key = item.get("reg_key") or ""
            if not reg_key:
                return False, "不支持的启动项类型，无法删除"
            if ("CurrentVersion\\Run" not in reg_key) and ("ShellExecuteHooks" not in reg_key):
                return False, "该项属于系统关键位置（服务/Winlogon），不支持直接删除，请在对应功能页处理"

            reg_root = item.get("reg_root") or "HKEY_CURRENT_USER"
            root = _ROOT_MAP.get(reg_root)
            if root is None:
                return False, "不支持的注册表根键：%s" % reg_root
            value_name = item.get("reg_value")
            if value_name is None:
                return False, "缺少注册表值名，无法删除"

            try:
                key = winreg.OpenKey(root, reg_key, 0, winreg.KEY_READ | winreg.KEY_SET_VALUE)
            except PermissionError:
                return False, "需要管理员权限才能删除系统级(HKLM)启动项"
            except (FileNotFoundError, OSError) as exc:
                return False, "无法打开注册表项：%s" % exc
            try:
                try:
                    data, vtype = winreg.QueryValueEx(key, value_name)
                except (FileNotFoundError, PermissionError, OSError) as exc:
                    return False, "读取原启动项失败：%s" % exc

                meta = {
                    "id": bid,
                    "type": "registry",
                    "name": item.get("name", value_name or "(默认值)"),
                    "command": item.get("command", ""),
                    "reg_root": reg_root,
                    "reg_key": reg_key,
                    "reg_value": value_name,
                    "value_type": int(vtype),
                    "time": now_str,
                }
                if isinstance(data, (bytes, bytearray)):
                    meta["is_binary"] = True
                    meta["data_b64"] = base64.b64encode(bytes(data)).decode("ascii")
                    meta["data"] = None
                else:
                    meta["is_binary"] = False
                    meta["data"] = data

                if not self._write_backup_meta(bdir, bid, meta):
                    return False, "备份记录写入失败，为安全起见已取消删除"

                try:
                    winreg.DeleteValue(key, value_name)
                except PermissionError:
                    return False, "需要管理员权限才能删除该注册表启动项"
                except OSError as exc:
                    return False, "删除注册表启动项失败：%s" % exc
            finally:
                try:
                    winreg.CloseKey(key)
                except OSError:
                    pass

            return True, "已删除并备份启动项“%s”" % item.get("name", "")
        except (PermissionError, FileNotFoundError, OSError) as exc:
            return False, "删除失败：%s" % exc

    def _write_backup_meta(self, bdir, bid, meta):
        """写备份元数据 json，成功返回 True"""
        if not bdir:
            return False
        try:
            path = os.path.join(bdir, "startup_%s.json" % bid)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(meta, f, ensure_ascii=False, indent=2)
            return True
        except (OSError, ValueError, TypeError):
            return False

    def recover_startup_item(self, backup_id):
        """从备份恢复此前删除的启动项"""
        bdir = self._backup_dir()
        if not bdir:
            return False, "备份目录不可用"
        if not backup_id:
            return False, "备份 ID 无效"
        meta_path = os.path.join(bdir, "startup_%s.json" % backup_id)
        try:
            with open(meta_path, "r", encoding="utf-8") as f:
                meta = json.load(f)
        except (FileNotFoundError, OSError, ValueError):
            return False, "找不到备份记录：%s" % backup_id

        btype = meta.get("type")
        try:
            # -------- 恢复文件 --------
            if btype == "file":
                backup_file = meta.get("backup_file")
                original = meta.get("original_path")
                if not backup_file or not os.path.isfile(backup_file):
                    return False, "备份文件已丢失，无法恢复"
                try:
                    parent = os.path.dirname(original or "")
                    if parent:
                        os.makedirs(parent, exist_ok=True)
                    with open(backup_file, "rb") as fin:
                        payload = fin.read()
                    with open(original, "wb") as fout:
                        fout.write(payload)
                except (PermissionError, OSError) as exc:
                    return False, "恢复文件失败：%s" % exc
                return True, "已恢复启动项文件“%s”" % os.path.basename(original or backup_file)

            # -------- 恢复注册表值 --------
            if btype == "registry":
                reg_root = meta.get("reg_root") or "HKEY_CURRENT_USER"
                root = _ROOT_MAP.get(reg_root)
                if root is None:
                    return False, "备份中的注册表根键无效"
                reg_key = meta.get("reg_key") or ""
                reg_value = meta.get("reg_value") or ""
                vtype = int(meta.get("value_type", winreg.REG_SZ))
                if meta.get("is_binary"):
                    data = base64.b64decode(meta.get("data_b64") or "")
                else:
                    data = meta.get("data")
                    if data is None:
                        data = ""
                try:
                    key = winreg.CreateKeyEx(root, reg_key, 0, winreg.KEY_SET_VALUE)
                except PermissionError:
                    return False, "需要管理员权限才能恢复系统级(HKLM)启动项"
                except OSError as exc:
                    return False, "无法创建注册表项：%s" % exc
                try:
                    winreg.SetValueEx(key, reg_value, 0, vtype, data)
                except PermissionError:
                    return False, "需要管理员权限才能恢复该启动项"
                except OSError as exc:
                    return False, "恢复注册表值失败：%s" % exc
                finally:
                    try:
                        winreg.CloseKey(key)
                    except OSError:
                        pass
                return True, "已恢复启动项“%s”" % meta.get("name", reg_value)

            return False, "未知的备份类型：%s" % btype
        except (PermissionError, FileNotFoundError, OSError) as exc:
            return False, "恢复失败：%s" % exc

    def list_startup_backups(self):
        """列出所有可恢复的启动项备份"""
        result = []
        bdir = self._backup_dir()
        if not bdir:
            return result
        try:
            names = os.listdir(bdir)
        except (PermissionError, OSError):
            return result
        for fname in names:
            if not fname.startswith("startup_") or not fname.endswith(".json"):
                continue
            try:
                with open(os.path.join(bdir, fname), "r", encoding="utf-8") as f:
                    meta = json.load(f)
            except (OSError, ValueError):
                continue
            result.append({
                "id": meta.get("id", ""),
                "name": meta.get("name", ""),
                "time": meta.get("time", ""),
                "type": "启动文件夹文件" if meta.get("type") == "file" else "注册表启动项",
            })
        result.sort(key=lambda x: x.get("time", ""), reverse=True)
        return result

    # ============================= 开机耗时分析 =============================

    def analyze_boot_time(self):
        """
        从 Windows 事件日志读取最近的开机耗时（Diagnostics-Performance / 事件 100）。
        取不到数据时如实返回 success=False，绝不伪造数据。
        """
        self._emit(5, "正在读取系统开机耗时记录...")
        # 使用 SilentlyContinue：日志不存在或没有记录时不产生错误输出，只返回空结果
        script = (
            "$ErrorActionPreference='SilentlyContinue';"
            "$ev=Get-WinEvent -FilterHashtable @{"
            "LogName='Microsoft-Windows-Diagnostics-Performance/Operational';Id=100"
            "} -MaxEvents 10;"
            "if($ev){$ev | Select-Object -Property TimeCreated,"
            "@{n='BootMs';e={$_.Properties[0].Value}} | ConvertTo-Csv -NoTypeInformation};"
            "exit 0"
        )
        ok, out, err = self._run_powershell(script, timeout=25)
        if not ok:
            msg = (err or "").strip()
            if not msg:
                msg = "该日志未启用或没有记录"
            return {
                "success": False,
                "records": [],
                "avg_ms": 0,
                "message": "无法读取开机性能日志：%s" % msg,
            }
        if not out.strip():
            return {
                "success": False,
                "records": [],
                "avg_ms": 0,
                "message": "该日志未启用或没有记录（系统尚未采集到开机耗时数据）",
            }

        records = []
        try:
            rows = list(csv.reader(io.StringIO(out)))
        except (csv.Error, ValueError):
            rows = []
        for row in rows:
            if len(row) < 2:
                continue
            first = row[0].strip().lower()
            if first in ("timecreated", "时间", ""):
                continue
            try:
                ms = int(float(row[1].strip().strip('"')))
            except (TypeError, ValueError):
                continue
            if ms <= 0:
                continue
            records.append({"time": row[0].strip(), "boot_ms": ms})

        if not records:
            return {
                "success": False,
                "records": [],
                "avg_ms": 0,
                "message": "未找到开机性能记录（该日志可能未启用，或系统尚未记录开机耗时）",
            }

        avg_ms = int(sum(r["boot_ms"] for r in records) / len(records))
        self._emit(100, "已读取到 %d 条开机耗时记录" % len(records))
        return {
            "success": True,
            "records": records[:10],
            "avg_ms": avg_ms,
            "message": "已读取最近 %d 次开机耗时记录，平均耗时约 %.1f 秒"
                       % (len(records), avg_ms / 1000.0),
        }

    # ================================ 服务管理 ================================

    def scan_services(self, only_auto=True):
        """
        列出服务（only_auto=True 时只返回自动启动 Start=2 的服务）。
        优先使用 PowerShell Get-CimInstance，失败时回退到直接读注册表。
        返回空列表时会在日志中说明原因。
        """
        self._emit(5, "正在读取系统服务列表...")
        services = []

        script = (
            "Get-CimInstance -ClassName Win32_Service -Property Name,DisplayName,State,"
            "StartMode,PathName | Select-Object Name,DisplayName,State,StartMode,PathName "
            "| ConvertTo-Csv -NoTypeInformation"
        )
        ok, out, err = self._run_powershell(script, timeout=40)
        if ok and out.strip():
            try:
                rows = list(csv.reader(io.StringIO(out)))
            except (csv.Error, ValueError):
                rows = []
            header = [h.strip().lower() for h in rows[0]] if rows else []

            def col(row, name):
                if name in header:
                    idx = header.index(name)
                    if idx < len(row):
                        return row[idx].strip()
                return ""

            for row in rows:
                if not row or (row[0].strip().lower() in ("name",)):
                    continue
                start_mode = col(row, "startmode")
                if only_auto and start_mode.lower() != "auto":
                    continue
                services.append(self._make_service_dict(
                    name=col(row, "name"),
                    display=col(row, "displayname"),
                    state=col(row, "state"),
                    start_mode=start_mode,
                    path=col(row, "pathname"),
                ))
        else:
            self._log("PowerShell 读取服务失败（%s），改用注册表方式读取"
                      % ((err or "").strip()[:120] or "无返回"))
            services = self._scan_services_from_registry(only_auto)

        if not services:
            self._log("未能读取到服务列表（可能需要管理员权限）")
        self._emit(100, "服务读取完成，共 %d 个" % len(services))
        return services

    def _scan_services_from_registry(self, only_auto):
        """回退方案：直接枚举 HKLM\\SYSTEM\\CurrentControlSet\\Services"""
        result = []
        base = r"SYSTEM\CurrentControlSet\Services"
        try:
            root_key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, base, 0, winreg.KEY_READ)
        except (FileNotFoundError, PermissionError, OSError) as exc:
            self._log("无法读取服务注册表项：%s" % exc)
            return result

        start_map = {0: "Boot", 1: "System", 2: "Auto", 3: "Manual", 4: "Disabled"}
        try:
            index = 0
            while index < 3000:
                try:
                    svc_name = winreg.EnumKey(root_key, index)
                except OSError:
                    break
                index += 1
                try:
                    sk = winreg.OpenKey(root_key, svc_name, 0, winreg.KEY_READ)
                except (FileNotFoundError, PermissionError, OSError):
                    continue
                try:
                    try:
                        start, _ = winreg.QueryValueEx(sk, "Start")
                    except (FileNotFoundError, OSError):
                        continue
                    start_mode = start_map.get(int(start), "Unknown")
                    if only_auto and start_mode != "Auto":
                        continue
                    try:
                        image, _ = winreg.QueryValueEx(sk, "ImagePath")
                    except (FileNotFoundError, OSError):
                        image = ""
                    try:
                        disp, _ = winreg.QueryValueEx(sk, "DisplayName")
                    except (FileNotFoundError, OSError):
                        disp = svc_name
                    result.append(self._make_service_dict(
                        name=svc_name, display=str(disp), state="",
                        start_mode=start_mode, path=str(image)))
                finally:
                    try:
                        winreg.CloseKey(sk)
                    except OSError:
                        pass
        finally:
            try:
                winreg.CloseKey(root_key)
            except OSError:
                pass
        return result

    def _make_service_dict(self, name, display, state, start_mode, path):
        """组装单个服务的信息，并给出保守的处置建议"""
        mode_map = {"auto": "自动", "manual": "手动", "disabled": "禁用",
                    "boot": "引导", "system": "系统", "unknown": "未知"}
        start_cn = mode_map.get((start_mode or "").lower(), start_mode or "未知")

        state_lower = (state or "").lower()
        if state_lower == "running":
            state_cn = "运行中"
        elif state_lower == "stopped":
            state_cn = "已停止"
        else:
            state_cn = state or "未知"

        exe = _extract_exe_path(path)
        publisher = _get_file_publisher(exe) if _looks_like_path(exe) else ""
        suggest, reason = _suggest_for_service(name, display, exe, publisher)

        return {
            "name": name,
            "display": display,
            "state": state_cn,
            "start_type": start_cn,
            "path": exe or path,
            "publisher": publisher,
            "suggest": suggest,
            "reason": reason,
        }

    def set_service_start_type(self, service_name, start_type):
        """修改服务的启动类型：auto / manual / disabled（需要管理员权限）"""
        mapping = {"auto": "auto", "manual": "demand", "disabled": "disabled"}
        sc_start = mapping.get((start_type or "").lower())
        if not sc_start:
            return False, "不支持的启动类型：%s（可选 auto / manual / disabled）" % start_type
        if not service_name:
            return False, "服务名为空"
        if not _is_admin():
            return False, "需要管理员权限"

        # sc.exe 要求 "start=" 与取值分开传参
        ok, out, err = self._run_command(
            ["sc.exe", "config", service_name, "start=", sc_start], timeout=20)
        if ok:
            cn = {"auto": "自动", "demand": "手动", "disabled": "禁用"}[sc_start]
            return True, "已把服务“%s”的启动类型设置为“%s”" % (service_name, cn)
        msg = (err or out).strip()
        if "拒绝访问" in msg or "Access is denied" in msg or "5" == msg.strip():
            return False, "需要管理员权限"
        return False, "修改服务启动类型失败：%s" % msg[:200]

    # ============================== 计划任务管理 ==============================

    # schtasks 的列名在不同语言下会本地化，这里同时兼容英文与简体中文表头
    _TASK_COLUMNS = {
        "name": ("taskname", "任务名"),
        "status": ("status", "状态"),
        "schedule": ("schedule", "计划"),
        "command": ("task to run", "要运行的任务"),
        "next_run": ("next run time", "下次运行时间"),
    }

    def _parse_tasks_csv(self, out):
        """解析 schtasks /fo csv /v 的输出，返回 dict 列表"""
        tasks = []
        try:
            rows = list(csv.reader(io.StringIO(out)))
        except (csv.Error, ValueError):
            return tasks
        if not rows:
            return tasks

        header = [h.strip().lower() for h in rows[0]]

        def col(row, key):
            for candidate in self._TASK_COLUMNS[key]:
                if candidate in header:
                    idx = header.index(candidate)
                    if idx < len(row):
                        return row[idx].strip()
            return ""

        for row in rows:
            if not row:
                continue
            first = row[0].strip().lower()
            if first in ("hostname", "taskname", "主机名", "任务名"):
                continue
            name = col(row, "name")
            if not name:
                continue
            path = name
            is_ms = name.startswith("\\Microsoft") or name.startswith("\\Windows")
            tasks.append({
                "name": name,
                "path": path,
                "status": col(row, "status"),
                "schedule": col(row, "schedule"),
                "command": col(row, "command"),
                "next_run": col(row, "next_run"),
                "is_microsoft": is_ms,
            })
            if len(tasks) >= 300:
                self._log("计划任务数量超过 300 个，仅返回前 300 个")
                break
        return tasks

    def scan_scheduled_tasks(self):
        """列出计划任务（含微软自带，用 is_microsoft 标注）"""
        now = time.time()
        if self._task_cache and (now - self._task_cache[0]) < 60:
            self._emit(50, "正在使用缓存的计划任务列表...")
            return self._task_cache[1]

        self._emit(20, "正在读取计划任务（schtasks）...")
        ok, out, err = self._run_command(
            ["schtasks", "/query", "/fo", "csv", "/v"], timeout=45)
        if not ok or not out.strip():
            self._log("读取计划任务失败：%s" % ((err or "").strip()[:200] or "无返回"))
            self._task_cache = (now, [])
            return []

        tasks = self._parse_tasks_csv(out)
        if not tasks:
            self._log("未能解析出任何计划任务")
        self._task_cache = (now, tasks)
        self._emit(100, "计划任务读取完成，共 %d 个" % len(tasks))
        return tasks

    def set_scheduled_task_enabled(self, task_name, enabled):
        """启用/禁用计划任务"""
        if not task_name:
            return False, "任务名为空"
        args = ["schtasks", "/change", "/tn", task_name,
                "/enable" if enabled else "/disable"]
        ok, out, err = self._run_command(args, timeout=25)
        if ok:
            return True, ("已启用计划任务“%s”" if enabled else "已禁用计划任务“%s”") % task_name
        return False, "操作计划任务失败：%s" % ((err or out).strip()[:200] or "未知错误")

    def delete_scheduled_task(self, task_name):
        """删除计划任务"""
        if not task_name:
            return False, "任务名为空"
        ok, out, err = self._run_command(
            ["schtasks", "/delete", "/tn", task_name, "/f"], timeout=25)
        if ok:
            return True, "已删除计划任务“%s”" % task_name
        return False, "删除计划任务失败：%s" % ((err or out).strip()[:200] or "未知错误")

    # ============================ 定时自动清理 ============================

    def create_auto_clean_task(self, script_path, when="weekly", time_str="12:00"):
        """
        创建定时自动清理计划任务。
        when: daily / weekly（每周日）；time_str: HH:MM。
        """
        if not script_path or not os.path.isfile(script_path):
            return False, "脚本文件不存在：%s" % script_path
        if when not in ("daily", "weekly"):
            return False, "不支持的计划周期：%s（仅支持 daily / weekly）" % when
        if not _TIME_RE.match(time_str or ""):
            return False, "时间格式不正确，应为 HH:MM（例如 12:00）"
        if not _is_admin():
            return False, "创建计划任务需要管理员权限，请以管理员身份运行本程序"

        exe = sys.executable or "python"
        # /tr 值需要同时给 exe 与脚本加引号，subprocess 会自动完成外层转义
        tr_value = '"%s" "%s"' % (exe, script_path)

        args = ["schtasks", "/create", "/tn", AUTO_TASK_NAME, "/tr", tr_value]
        if when == "daily":
            args += ["/sc", "daily"]
        else:
            args += ["/sc", "weekly", "/d", "SUN"]
        args += ["/st", time_str, "/f"]

        ok, out, err = self._run_command(args, timeout=30)
        if ok:
            period = "每天" if when == "daily" else "每周日"
            return True, "已创建自动清理计划任务“%s”（%s %s 自动运行）" % (AUTO_TASK_NAME, period, time_str)

        msg = (err or out).strip() or "未知错误"
        return False, ("创建自动清理计划任务失败：%s\n"
                       "实际执行的命令：%s" % (msg[:300], " ".join(args)))

    def delete_auto_clean_task(self):
        """删除定时自动清理计划任务"""
        ok, out, err = self._run_command(
            ["schtasks", "/delete", "/tn", AUTO_TASK_NAME, "/f"], timeout=25)
        if ok:
            return True, "已删除自动清理计划任务“%s”" % AUTO_TASK_NAME
        msg = (err or out).strip() or "未知错误"
        if "找不到" in msg or "cannot find" in msg.lower():
            return False, "自动清理计划任务不存在"
        return False, "删除自动清理计划任务失败：%s" % msg[:200]

    def get_auto_clean_task_status(self):
        """查询自动清理计划任务的状态，返回 {exists, status, next_run}"""
        result = {"exists": False, "status": "", "next_run": ""}
        ok, out, err = self._run_command(
            ["schtasks", "/query", "/tn", AUTO_TASK_NAME, "/fo", "csv", "/v"], timeout=25)
        if not ok or not out.strip():
            return result
        tasks = self._parse_tasks_csv(out)
        if not tasks:
            return result
        task = tasks[0]
        result["exists"] = True
        result["status"] = task.get("status", "")
        next_run = task.get("next_run", "")
        if next_run in ("N/A", "不可用", ""):
            next_run = "未计划"
        result["next_run"] = next_run
        return result

    # ====================== 汇总为 ScanResult（供主程序表格） ======================

    def scan_startup_junk(self):
        """
        把“失效/可疑”的启动项转成 ScanResult 列表，供主程序结果表格展示。
        category 统一为“启动项问题”。
        """
        self._emit(0, "正在汇总启动项问题...")
        results = []
        try:
            items = self.scan_startup_items()
        except (PermissionError, OSError) as exc:
            self._emit(100, "启动项问题汇总失败：%s" % exc)
            return results

        for it in items:
            risk = it.get("risk")
            if risk == "正常":
                continue

            target = it.get("exe_path") or ""
            size = it.get("size", 0) or 0
            if not size and target and _looks_like_path(target) and os.path.exists(target):
                size = get_size(target)

            if risk == "失效":
                source = "失效启动项(文件不存在)"
                description = (
                    "这是一条“无效启动项”：它指向的程序文件已经不存在，只会在开机时白白浪费一点时间。\n"
                    "所在位置：%s\n启动项名称：%s\n启动命令：%s\n解析出的程序路径：%s\n"
                    "处理建议：可以直接删除该启动项（删除前会自动备份，可随时恢复）。"
                    % (it.get("location", "-"), it.get("name", "-"),
                       it.get("command", "-") or "-", target or "未能解析出路径")
                )
                if not it.get("enabled"):
                    description += "\n（该项当前已是禁用状态，删除记录可让启动列表更干净）"
            else:
                reasons = it.get("suspect_reasons") or []
                source = "可疑启动项(%s)" % it.get("location", "未知位置")
                description = (
                    "这条启动项存在可疑特征，建议核实后再决定是否保留。\n"
                    "所在位置：%s\n启动项名称：%s\n发布者：%s\n启动命令：%s\n可疑原因：%s\n"
                    "处理建议：先确认来源，如非自己安装的软件可禁用或删除（删除前会自动备份）。"
                    % (it.get("location", "-"), it.get("name", "-"),
                       it.get("publisher") or "无发布者（版本）信息",
                       it.get("command", "-") or "-",
                       "；".join(reasons) if reasons else "来源位置异常")
                )

            results.append(ScanResult(
                category="启动项问题",
                source=source,
                path=it.get("command") or it.get("reg_key")
                     or it.get("startup_folder_file") or "",
                size=size,
                description=description,
            ))

        self._emit(100, "启动项问题汇总完成，共 %d 项" % len(results))
        return results
