# -*- coding: utf-8 -*-
r"""
内核驱动服务检测引擎

背景与价值
----------
Windows 的服务分两大类：
  · 用户态服务（Win32_Service）—— 就是任务管理器"服务"页看到的那些
  · 内核驱动服务（Type=1 内核驱动 / Type=2 文件系统驱动）—— **WMI 的
    Win32_Service 类完全看不到它们**

实测本机有 583 个内核驱动服务，而常规服务枚举方式一个都读不到。
这类驱动恰恰是最容易被忽略的残留来源：虚拟声卡（如 WO Mic）、虚拟摄像头、
网游反作弊、加密狗、光驱模拟器等软件都会注册内核驱动，卸载不干净时
驱动服务、驱动文件、DriverStore 副本会长期滞留。

本模块只做**只读诊断**，绝不提供禁用/删除操作。
原因：禁用正在使用的内核驱动会导致设备失灵、软件异常，严重时蓝屏。
真要清理驱动，必须走"先卸载软件 → 停服务 → pnputil 删除驱动包"的完整链路，
这超出本工具的安全边界，因此这里只负责把问题**看清楚**。

数据全部来自真实系统，取不到就如实标注，不编造。
"""

import os
import re
import csv
import io
import ctypes
import subprocess
import winreg
from ctypes import wintypes
from datetime import datetime

from scanner import ScanResult, get_size, format_size


def is_admin():
    """是否拥有管理员权限"""
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


# ==================== 服务类型常量 ====================

SERVICE_TYPE_MAP = {
    0x00000001: "内核驱动",
    0x00000002: "文件系统驱动",
    0x00000004: "适配器",
    0x00000008: "文件系统驱动(旧)",
    0x00000010: "独立进程服务",
    0x00000020: "共享进程服务",
    0x00000100: "交互式进程服务",
}

# 需要关注的驱动类型：1=内核驱动，2=文件系统驱动
DRIVER_TYPES = (0x01, 0x02)

START_TYPE_MAP = {
    0: "引导启动(Boot)",
    1: "系统启动(System)",
    2: "自动(Auto)",
    3: "手动(Manual)",
    4: "已禁用(Disabled)",
}

# 状态码
STATE_MAP = {
    1: "已停止",
    2: "启动中",
    3: "停止中",
    4: "运行中",
    5: "继续中",
    6: "暂停中",
    7: "已暂停",
}

# 已知的虚拟设备类驱动特征（用于给用户额外提示）
# 注意：关键词要足够具体。像 "audio"/"camera" 这种泛词会把大量正常驱动
# 也匹配进来（实测泛词匹配导致 583 个驱动里 348 个被误标），因此只保留
# 明确指向虚拟设备/特殊用途驱动的词。
VIRTUAL_DEVICE_HINTS = (
    ("虚拟音频", ("voicemeeter", "vb-audio", "vbaudio", "virtualaudio", "womic",
                "wovad", "virtual cable", "vb_cable", "scream", "vac",
                "virtual mic", "virtualmic")),
    ("虚拟摄像头", ("virtualcam", "vcam", "splitcam", "manycam", "obs-virtual",
                 "virtual camera", "virtualcamera")),
    ("反作弊/安全", ("easyanticheat", "battleye", "vgk", "vanguard", "xigncode",
                  "nprotect", "tenprotect", "gameguard", "anticheat")),
    ("虚拟光驱/磁盘", ("vcdrom", "dtlite", "daemontools", "virtualdrive", "imdisk",
                   "winfsp", "dokan", "subst")),
    ("网络过滤/VPN", ("wintun", "wireguard", "openvpn", "npcap", "winpcap", "tap0901")),
    ("硬件监控", ("winring0", "winring", "inpout", "cpuz", "gpu-z", "hwinfo",
                "iomap", "msio", "nvidiaclock")),
)

# 签名主体为"硬件兼容性发布者"的，说明是第三方驱动但通过了微软 WHQL 认证。
# 这与"微软自家驱动"是两回事，必须区分开。
WHQL_SIGNER_HINT = "hardware compatibility"


class _SERVICE_STATUS(ctypes.Structure):
    """QueryServiceStatus 需要的结构体"""
    _fields_ = [("dwServiceType", wintypes.DWORD),
                ("dwCurrentState", wintypes.DWORD),
                ("dwControlsAccepted", wintypes.DWORD),
                ("dwWin32ExitCode", wintypes.DWORD),
                ("dwServiceSpecificExitCode", wintypes.DWORD),
                ("dwCheckPoint", wintypes.DWORD),
                ("dwWaitHint", wintypes.DWORD)]


class DriverEngine:
    """内核驱动服务检测"""

    # 第三方驱动的缓存上限（避免在异常系统上卡死）
    MAX_DRIVERS = 1200

    def __init__(self, progress_callback=None):
        self.progress_callback = progress_callback
        self._stop = False
        self._publisher_cache = {}

    def _emit(self, percent, message):
        if self.progress_callback:
            self.progress_callback(percent, message)

    def stop(self):
        self._stop = True

    def reset_stop(self):
        self._stop = False

    # ==================== 路径解析 ====================

    @staticmethod
    def _resolve_image_path(image_path):
        r"""
        把服务 ImagePath 解析为真实文件路径。

        常见的几种写法：
          \SystemRoot\System32\drivers\xxx.sys
          \??\C:\Windows\System32\drivers\xxx.sys
          C:\Windows\System32\drivers\xxx.sys
          System32\drivers\xxx.sys
          %SystemRoot%\System32\drivers\xxx.sys
        """
        if not image_path:
            return ""
        p = str(image_path).strip().strip('"')
        if not p:
            return ""

        # 只取 .sys 路径部分，丢掉后面的命令行参数
        m = re.match(r'^(.*?\.sys)', p, re.IGNORECASE)
        if m:
            p = m.group(1)
        else:
            p = p.split(" ")[0]

        win_dir = os.environ.get("SystemRoot", r"C:\Windows")

        # 统一处理各类前缀（用切片判断，避免转义地狱）
        low = p.lower()
        if low.startswith("\\systemroot\\"):
            p = p[len("\\systemroot\\"):]
            p = os.path.join(win_dir, p)
        elif low.startswith("\\??\\"):
            p = p[len("\\??\\"):]
        elif low.startswith("system32\\") or low.startswith("system32/"):
            p = os.path.join(win_dir, p)

        # 极端情况下仍可能带一层 \SystemRoot 前缀
        low = p.lower()
        if low.startswith("\\systemroot\\"):
            p = os.path.join(win_dir, p[len("\\systemroot\\"):])

        try:
            p = os.path.expandvars(p)
        except Exception:
            pass
        return p

    # ==================== 状态查询 ====================

    def _query_states(self, names):
        """
        用 ctypes 批量查询服务状态（比逐个调用 sc.exe 快得多）。
        返回 {服务名: (状态字符串, 状态码)}

        注意：必须显式声明 argtypes/restype。ctypes 默认把返回值当 32 位 int，
        而句柄在 64 位系统是 64 位指针，不声明会被截断导致句柄无效、
        所有状态查询都静默失败（这个坑实测踩过一次）。
        """
        result = {}
        advapi = ctypes.windll.advapi32
        SC_MANAGER_CONNECT = 0x0001
        SERVICE_QUERY_STATUS = 0x0004

        advapi.OpenSCManagerW.restype = wintypes.HANDLE
        advapi.OpenSCManagerW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR,
                                          wintypes.DWORD]
        advapi.OpenServiceW.restype = wintypes.HANDLE
        advapi.OpenServiceW.argtypes = [wintypes.HANDLE, wintypes.LPCWSTR,
                                        wintypes.DWORD]
        advapi.QueryServiceStatus.argtypes = [wintypes.HANDLE,
                                              ctypes.POINTER(_SERVICE_STATUS)]
        advapi.CloseServiceHandle.argtypes = [wintypes.HANDLE]

        try:
            scm = advapi.OpenSCManagerW(None, None, SC_MANAGER_CONNECT)
        except Exception:
            return result
        if not scm:
            return result

        try:
            for name in names:
                if self._stop:
                    break
                try:
                    handle = advapi.OpenServiceW(scm, name, SERVICE_QUERY_STATUS)
                except Exception:
                    handle = 0
                if not handle:
                    continue
                try:
                    st = _SERVICE_STATUS()
                    if advapi.QueryServiceStatus(handle, ctypes.byref(st)):
                        code = int(st.dwCurrentState)
                        result[name] = (STATE_MAP.get(code, "未知"), code)
                except Exception:
                    pass
                finally:
                    try:
                        advapi.CloseServiceHandle(handle)
                    except Exception:
                        pass
        finally:
            try:
                advapi.CloseServiceHandle(scm)
            except Exception:
                pass
        return result

    # ==================== 发布者与签名 ====================

    def _get_publisher(self, path):
        """读取文件版本信息里的公司名（带缓存）"""
        if not path:
            return ""
        key = path.lower()
        if key in self._publisher_cache:
            return self._publisher_cache[key]
        company = ""
        try:
            from startup_manager import _get_file_publisher
            company = _get_file_publisher(path) or ""
        except Exception:
            company = ""
        self._publisher_cache[key] = company
        return company

    def _verify_signatures(self, paths, timeout=120):
        """
        批量验证数字签名（只在需要时调用，避免 583 个文件逐个验证）。

        用一次 PowerShell 调用处理全部路径，返回 {路径: {"status":..,"signer":..}}。
        失败时返回空字典，调用方应降级为"未验证"而不是编造结果。
        """
        if not paths:
            return {}

        # 写临时清单文件，避免命令行过长
        tmp = os.path.join(os.environ.get("TEMP", "."),
                           "cleanmaster_sigcheck_%d.txt" % os.getpid())
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                f.write("\n".join(paths))
        except Exception:
            return {}

        script = (
            "$ErrorActionPreference='SilentlyContinue';"
            f"Get-Content -LiteralPath '{tmp}' -Encoding UTF8 | ForEach-Object {{"
            "  $p=$_; if ($p -and (Test-Path -LiteralPath $p)) {"
            "    try {"
            "      $s = Get-AuthenticodeSignature -LiteralPath $p;"
            "      $subj = if ($s.SignerCertificate) { $s.SignerCertificate.Subject } else { '' };"
            "      \"$p`t$($s.Status)`t$subj\""
            "    } catch { \"$p`tERROR`t\" }"
            "  }"
            "}"
        )
        result = {}
        try:
            proc = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                capture_output=True, timeout=timeout)
            raw = proc.stdout or b""
            text = ""
            for enc in ("utf-8", "gbk", "latin-1"):
                try:
                    text = raw.decode(enc, errors="ignore")
                    break
                except Exception:
                    continue
            for line in text.splitlines():
                parts = line.split("\t")
                if len(parts) >= 2:
                    path = parts[0].strip()
                    status = parts[1].strip()
                    signer = parts[2].strip() if len(parts) > 2 else ""
                    result[path.lower()] = {"status": status, "signer": signer}
        except Exception:
            pass
        finally:
            try:
                os.remove(tmp)
            except Exception:
                pass
        return result

    @staticmethod
    def _signer_short(signer_subject):
        """从证书主题里提取简短的签名者名称"""
        if not signer_subject:
            return ""
        m = re.search(r"CN=([^,]+)", signer_subject)
        return m.group(1).strip() if m else signer_subject[:60]

    @staticmethod
    def _is_ddk_default_publisher(publisher):
        """
        判断发布者是否为驱动编译器的默认占位值，而非真实厂商。

        驱动开发者若没在自己的 .rc 里填公司名，Windows DDK 会自动写入
        "Windows (R) Win 7 DDK provider" 这类默认值。它不代表微软出品，
        也不能作为"可信"的依据（WO Mic 的驱动就是这样）。
        """
        if not publisher:
            return False
        low = publisher.lower()
        markers = ("ddk provider", "wdk provider", "windows (r) win",
                   "microsoft windows operating system", "unknown")
        return any(m in low for m in markers)

    # ==================== 主扫描 ====================

    def scan_kernel_drivers(self, only_third_party=False, verify_signature=True):
        """
        枚举内核驱动服务。

        返回 list[dict]，每项：
          {"name","display","type","type_cn","start_type","start_code",
           "state","running","image_path","image_exists","image_size",
           "description","group","publisher","signed","signer",
           "is_third_party","is_system_dir","category_hint","advice"}
        """
        self.reset_stop()
        self._emit(3, "正在枚举内核驱动服务...")

        base = r"SYSTEM\CurrentControlSet\Services"
        try:
            root_key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, base, 0, winreg.KEY_READ)
        except (FileNotFoundError, PermissionError, OSError) as e:
            self._emit(100, f"无法读取服务注册表：{e}")
            return []

        raw = []
        try:
            index = 0
            while index < self.MAX_DRIVERS:
                if self._stop:
                    break
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
                        type_val = int(winreg.QueryValueEx(sk, "Type")[0])
                    except (FileNotFoundError, OSError, ValueError, TypeError):
                        continue
                    # 只保留驱动类（去掉 0x10/0x20 等标志位再判断低字节）
                    if (type_val & 0xFF) not in DRIVER_TYPES:
                        continue

                    def _val(name, default=""):
                        try:
                            v = winreg.QueryValueEx(sk, name)[0]
                            return v if v is not None else default
                        except (FileNotFoundError, OSError):
                            return default

                    raw.append({
                        "name": svc_name,
                        "display": str(_val("DisplayName", svc_name)),
                        "type": type_val,
                        "start_code": _val("Start", 3),
                        "image_path": str(_val("ImagePath", "")),
                        "description": str(_val("Description", "")),
                        "group": str(_val("Group", "")),
                    })
                finally:
                    try:
                        winreg.CloseKey(sk)
                    except Exception:
                        pass
        finally:
            try:
                winreg.CloseKey(root_key)
            except Exception:
                pass

        if not raw:
            self._emit(100, "未枚举到内核驱动服务")
            return []

        self._emit(20, f"已枚举 {len(raw)} 个驱动服务，正在查询运行状态...")

        # 查询状态
        states = self._query_states([r["name"] for r in raw])
        self._emit(45, f"状态查询完成（{len(states)} 个），正在整理文件与发布者信息...")

        win_dir = os.path.normcase(os.environ.get("SystemRoot", r"C:\Windows"))
        drivers_dir = os.path.normcase(os.path.join(win_dir, "System32", "drivers"))

        items = []
        third_party_paths = []
        for idx, r in enumerate(raw):
            if self._stop:
                break
            if idx % 100 == 0:
                self._emit(45 + int(idx / max(1, len(raw)) * 30),
                           f"正在整理驱动信息 {idx}/{len(raw)}...")

            image = self._resolve_image_path(r["image_path"])
            exists = bool(image) and os.path.isfile(image)
            size = 0
            if exists:
                try:
                    size = os.path.getsize(image)
                except (OSError, PermissionError):
                    size = 0

            # 没有 ImagePath 的内核驱动是"内核内置驱动"（如 Ntfs、Null、Beep、Wof、
            # fastfat 等），它们由 ntoskrnl 直接加载，根本没有独立 .sys 文件。
            # 这类必须判为系统驱动，否则会被误报成"文件缺失的第三方残留"。
            is_builtin = not image

            publisher = self._get_publisher(image) if exists else ""
            pub_low = publisher.lower()
            is_ms = ("microsoft" in pub_low)
            is_ddk_default = self._is_ddk_default_publisher(publisher)

            is_system_dir = False
            if image:
                try:
                    is_system_dir = os.path.normcase(image).startswith(drivers_dir)
                except Exception:
                    is_system_dir = False

            state, _code = states.get(r["name"], ("未知", 0))
            start_cn = START_TYPE_MAP.get(int(r["start_code"]) if str(r["start_code"]).isdigit() else 3,
                                          "未知")

            # 分类提示：只用名称/显示名/发布者判断，不掺入完整路径，
            # 否则 "c:\windows\system32\drivers\..." 里的子串会造成大量误匹配
            hint = ""
            probe = (r["name"] + " " + r["display"] + " " + publisher).lower()
            for label, keys in VIRTUAL_DEVICE_HINTS:
                if any(k in probe for k in keys):
                    hint = label
                    break

            # 初步判定：哪些需要做签名验证来最终确认归属
            #   微软官方发布者 → 直接判系统驱动，省去验证开销
            #   DDK 默认占位 / 空发布者 / 非系统目录 → 必须验签后才能定性
            if is_builtin or is_ms:
                needs_sig = False
            else:
                needs_sig = exists

            item = {
                "name": r["name"],
                "display": r["display"],
                "type": r["type"],
                "type_cn": SERVICE_TYPE_MAP.get(r["type"] & 0xFF, "未知"),
                "start_type": start_cn,
                "start_code": r["start_code"],
                "state": state,
                "running": state == "运行中",
                "image_path": image,
                "image_exists": exists,
                "image_size": size,
                "is_builtin": is_builtin,
                "description": r["description"],
                "group": r["group"],
                "publisher": publisher,
                "is_ddk_default_publisher": is_ddk_default,
                "signed": None,       # 后面按需填
                "signer": "",
                "is_third_party": False,      # 待签名验证后最终确定
                "is_system_dir": is_system_dir,
                "category_hint": hint,
                "advice": "",
                "needs_sig": needs_sig,
            }
            items.append(item)
            if needs_sig:
                third_party_paths.append(image)

        # ---------- 批量验证签名，并据此最终定性 ----------
        sig_map = {}
        if verify_signature and third_party_paths and not self._stop:
            uniq = list(dict.fromkeys(third_party_paths))
            self._emit(80, f"正在验证 {len(uniq)} 个驱动的数字签名...")
            sig_map = self._verify_signatures(uniq)

        for it in items:
            # 内核内置驱动直接定性为系统驱动
            if it.get("is_builtin"):
                it["is_third_party"] = False
                continue

            # 驱动文件缺失：无法验签，只能按路径判断。
            # 有 ImagePath 但文件不存在，且路径不在系统驱动目录，
            # 基本可以确定是第三方软件卸载后遗留的服务记录。
            if not it["image_exists"]:
                it["is_third_party"] = bool(it["image_path"]) and not it["is_system_dir"]
                it["signed"] = False
                it["signer"] = ""
                continue

            # 微软官方发布者：直接判系统驱动，无需验签
            if not it.get("needs_sig"):
                it["is_third_party"] = False
                continue

            info = sig_map.get(it["image_path"].lower()) if it["image_path"] else None
            if info:
                status = (info.get("status") or "").lower()
                signer = self._signer_short(info.get("signer", ""))
                it["signed"] = (status == "valid")
                it["signer"] = signer
                signer_low = signer.lower()

                if status != "valid":
                    # 未签名 / 签名无效 → 第三方驱动（且带风险）
                    it["is_third_party"] = True
                elif WHQL_SIGNER_HINT in signer_low:
                    # 由"微软硬件兼容性发布者"签名：这是第三方驱动通过了 WHQL 认证。
                    # 关键区别：它**不是**微软自己的驱动，而是第三方厂商的驱动。
                    it["is_third_party"] = True
                elif "microsoft" in signer_low:
                    # 微软官方签名 → 系统驱动
                    it["is_third_party"] = False
                else:
                    # 其他签名者 → 第三方
                    it["is_third_party"] = True
            else:
                # 未能完成验签：退回到保守判断（不在系统驱动目录就按第三方提示）
                it["is_third_party"] = not it["is_system_dir"]

        # 生成建议
        for it in items:
            it["advice"] = self._advice_for(it)

        if only_third_party:
            items = [i for i in items if i["is_third_party"]]

        items.sort(key=lambda x: (not x["is_third_party"], not x["running"],
                                  x["display"].lower()))

        self._emit(100, f"扫描完成，共 {len(items)} 个驱动服务")
        return items

    def _advice_for(self, it):
        """给出保守的处置建议（只做提示，不执行任何操作）"""
        if it.get("is_builtin"):
            return "Windows 内核内置驱动（无独立 .sys 文件），系统正常运行所必需，请勿改动。"
        if not it["image_exists"]:
            return ("驱动文件已不存在，属于软件卸载后残留的服务记录。"
                    "若确认对应软件已卸载，可用管理员命令 sc delete 清理该服务名。")
        if it["is_third_party"] and it["running"]:
            return ("第三方驱动且正在运行，说明对应软件或设备正在使用，"
                    "请勿禁用或删除，否则会导致设备失灵或软件异常。")
        if it["is_third_party"] and not it["running"]:
            return ("第三方驱动当前未运行。若确认对应软件已卸载，"
                    "应先卸载软件，再用 pnputil 移除驱动包。")
        if it["running"]:
            return "系统驱动正在运行，属正常状态，无需处理。"
        return "系统驱动，正常状态，无需处理。"

    # ==================== 统计与问题汇总 ====================

    def get_driver_summary(self, items=None):
        """驱动统计信息"""
        if items is None:
            items = self.scan_kernel_drivers()
        total = len(items)
        third = [i for i in items if i["is_third_party"]]
        builtin = [i for i in items if i.get("is_builtin")]
        running = [i for i in items if i["running"]]
        # "文件缺失"只统计那些本来应该有独立文件的（排除内核内置驱动）
        missing = [i for i in items if not i["image_exists"] and not i.get("is_builtin")]
        third_missing = [i for i in third if not i["image_exists"]]
        unsigned_third = [i for i in third if i.get("signed") is False]
        virtual = [i for i in items if i.get("category_hint")]
        return {
            "total": total,
            "third_party": len(third),
            "builtin": len(builtin),
            "running": len(running),
            "missing_file": len(missing),
            "third_party_missing": len(third_missing),
            "unsigned_third": len(unsigned_third),
            "virtual_device": len(virtual),
        }

    def scan_driver_issues(self):
        """
        生成"值得提醒用户的驱动问题"（供主程序结果表格）。
        只报告明确的问题，不把正常驱动当垃圾。
        """
        self._emit(2, "正在检查内核驱动...")
        items = self.scan_kernel_drivers(only_third_party=True)
        results = []
        for it in items:
            if self._stop:
                break
            if it.get("is_builtin"):
                continue
            # 1) 驱动文件缺失 = 卸载残留的服务记录
            if not it["image_exists"]:
                results.append(ScanResult(
                    category="驱动残留",
                    source="残留驱动服务（驱动文件已丢失）",
                    path=f"{it['name']} → {it['image_path'] or '(无路径)'}",
                    size=0,
                    description=(
                        f"服务「{it['display']}」({it['name']}) 注册为{it['type_cn']}，"
                        f"启动类型{it['start_type']}，但驱动文件已不存在，"
                        "通常是软件卸载后遗留的无效服务记录。"
                        f"本工具不会自动删除，需管理员执行：sc delete {it['name']}"),
                ))
                continue
            # 2) 第三方驱动未签名（潜在风险，仅提示）
            if it.get("signed") is False:
                results.append(ScanResult(
                    category="驱动残留",
                    source="第三方驱动未签名",
                    path=it["image_path"],
                    size=it["image_size"],
                    description=(
                        f"驱动「{it['display']}」({it['name']}) 没有有效的数字签名，"
                        f"发布者：{it['publisher'] or '未知'}，状态：{it['state']}。"
                        "常见于虚拟声卡、虚拟摄像头、硬件监控、反作弊等软件。"
                        "未签名驱动存在兼容性与安全风险，请确认来源可信。"),
                ))
        self._emit(100, f"驱动检查完成，发现 {len(results)} 项值得注意")
        return results
