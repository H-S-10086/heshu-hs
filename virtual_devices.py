# -*- coding: utf-8 -*-
r"""
虚拟设备专项管理引擎

背景
----
"虚拟设备"指没有真实硬件、由软件模拟出来的设备，典型代表：
  · 虚拟麦克风 / 虚拟声卡 —— WO Mic（手机当麦克风）、VoiceMeeter、VB-Audio Cable、NVIDIA 虚拟音频
  · 虚拟网卡 —— OpenVPN TAP、WireGuard Wintun
  · 虚拟摄像头 —— OBS 虚拟摄像头、ManyCam
  · 虚拟光驱 —— DAEMON Tools、WinCDEmu

这类软件的特点：会在系统里注册**根枚举设备**（InstanceId 以 ROOT\ 开头）和内核驱动，
用户平时完全感觉不到它们的存在，卸载后设备与驱动却常常残留。

识别原理（关键）
----------------
真实硬件设备的 InstanceId 形如：
    HDAUDIO\FUNC_01&VEN_10EC...   USB\VID_0BDA...   PCI\VEN_8086...
而软件模拟的设备一律挂在 ROOT 下：
    ROOT\MEDIA\0000          → WO Mic Device
    ROOT\UNNAMED_DEVICE\0000 → NVIDIA Virtual Audio Device
    ROOT\WINTUN\0000         → Wintun Userspace Tunnel
这是判断"虚拟设备"最可靠的依据。

性能说明
--------
本模块全部通过**直接读注册表**获取数据，实测：
    Enum\ROOT 全量枚举        0.002 秒
    Enum\SWD\MMDEVAPI 枚举    0.001 秒
作为对比，用 Get-PnpDeviceProperty 逐个取属性需要 155 秒（20 个设备 × 5 个属性），
因此这里刻意不调用 PowerShell。

安全边界
--------
只做诊断，不提供任何"一键卸载设备/删除驱动"操作。
移除虚拟设备需要走 pnputil / 设备管理器，属于高风险操作，本工具只给指引。
"""

import os
import re
import ctypes
import winreg

from scanner import ScanResult, get_size, format_size


# ==================== 系统核心虚拟设备白名单 ====================
# 这些也挂在 ROOT 下，但属于 Windows 自身的基础组件（ACPI、总线枚举器、
# 卷管理器等），不是用户安装的软件，绝不能当作"虚拟设备残留"报告。
SYSTEM_ROOT_DEVICES = {
    "ACPI_HAL", "BasicDisplay", "BasicRender", "CAD", "CompositeBus",
    "hvservice", "KDNIC", "mssmbios", "NdisVirtualBus", "RDPBUS",
    "spaceport", "SYSTEM", "UMBUS", "vdrvroot", "VID", "volmgr",
    "SWD", "ROOTDEVICE", "DISPLAY", "PCI", "STORAGE",
}

# 音频端点类 GUID（MMDEVAPI）
CLASS_AUDIO_ENDPOINT = "{c166523c-fe0c-4a94-a586-f1a80cfbbf3e}"
CLASS_MEDIA = "{4d36e96c-e325-11ce-bfc1-08002be10318}"
CLASS_NET = "{4d36e972-e325-11ce-bfc1-08002be10318}"
CLASS_CAMERA = "{ca3e7ab9-b4c3-4ae6-8251-579ef933890f}"
CLASS_IMAGE = "{6bdd1fc6-810f-11d0-bec7-08002be2092f}"
CLASS_CDROM = "{4d36e965-e325-11ce-bfc1-08002be10318}"
CLASS_VOLUME = "{71a27cdd-812a-11d0-bec7-08002be2092f}"
CLASS_SYSTEM = "{4d36e97d-e325-11ce-bfc1-08002be10318}"

# 类 GUID → 中文分类名
CLASS_NAME_MAP = {
    CLASS_MEDIA: "多媒体设备",
    CLASS_AUDIO_ENDPOINT: "音频端点",
    CLASS_NET: "网络适配器",
    CLASS_CAMERA: "摄像头",
    CLASS_IMAGE: "影像设备",
    CLASS_CDROM: "光驱",
    CLASS_VOLUME: "存储卷",
    CLASS_SYSTEM: "系统设备",
}

# 已知虚拟设备驱动的特征词 → (分类, 可能的所属软件)
KNOWN_VIRTUAL_DRIVERS = {
    "wovad": ("虚拟音频", "WO Mic"),
    "womic": ("虚拟音频", "WO Mic"),
    "nvvad": ("虚拟音频", "NVIDIA 虚拟音频"),
    "nvvad_waveextensible": ("虚拟音频", "NVIDIA 虚拟音频"),
    "vbaudio_cable": ("虚拟音频", "VB-Audio Virtual Cable"),
    "vbaudio_voicemeeter": ("虚拟音频", "VoiceMeeter"),
    "vbaudio_voicemeetervaio": ("虚拟音频", "VoiceMeeter"),
    "scream": ("虚拟音频", "Scream 虚拟音频"),
    "virtualaudio": ("虚拟音频", "虚拟音频设备"),
    "vac": ("虚拟音频", "Virtual Audio Cable"),
    "tap0901": ("虚拟网卡", "OpenVPN / TAP-Windows"),
    "tap": ("虚拟网卡", "TAP 虚拟网卡"),
    "wintun": ("虚拟网卡", "WireGuard / Wintun"),
    "wireguard": ("虚拟网卡", "WireGuard"),
    "npcap": ("网络过滤", "Npcap"),
    "winpcap": ("网络过滤", "WinPcap"),
    "vcdrom": ("虚拟光驱", "虚拟光驱"),
    "dtlite": ("虚拟光驱", "DAEMON Tools Lite"),
    "sptd": ("虚拟光驱", "DAEMON Tools SPTD"),
    "wincdemu": ("虚拟光驱", "WinCDEmu"),
    "imdisk": ("虚拟磁盘", "ImDisk"),
    "winfsp": ("虚拟磁盘", "WinFsp"),
    "dokan": ("虚拟磁盘", "Dokan"),
    "obs-virtualcam": ("虚拟摄像头", "OBS 虚拟摄像头"),
    "obs_virtualcam": ("虚拟摄像头", "OBS 虚拟摄像头"),
    "manycam": ("虚拟摄像头", "ManyCam"),
    "splitcam": ("虚拟摄像头", "SplitCam"),
    "easyanticheat": ("反作弊", "Easy Anti-Cheat"),
    "battleye": ("反作弊", "BattlEye"),
    "vgk": ("反作弊", "Vanguard"),
}

# 名称特征词（当驱动名无法匹配时，用设备名兜底判断）
NAME_HINTS = (
    ("虚拟音频", ("virtual audio", "虚拟音频", "virtual cable", "voicemeeter",
                "vb-audio", "virtual mic", "虚拟麦克风", "womic", "wo mic")),
    ("虚拟摄像头", ("virtual camera", "virtualcam", "虚拟摄像头", "manycam",
                 "splitcam", "obs virtual")),
    ("虚拟光驱", ("virtual cd", "虚拟光驱", "virtual dvd", "daemon tools")),
    ("虚拟网卡", ("tap-windows", "wintun", "wireguard", "virtual ethernet",
               "虚拟网卡", "openvpn")),
    ("虚拟磁盘", ("imdisk", "winfsp", "dokan", "virtual disk", "虚拟磁盘")),
)


class VirtualDeviceEngine:
    """虚拟设备枚举、软件归属与残留判定"""

    MAX_DEVICES = 400

    def __init__(self, progress_callback=None):
        self.progress_callback = progress_callback
        self._stop = False
        self._software_cache = None
        self._class_cache = {}

    def _emit(self, percent, message):
        if self.progress_callback:
            self.progress_callback(percent, message)

    def stop(self):
        self._stop = True

    def reset_stop(self):
        self._stop = False

    # ==================== 工具 ====================

    @staticmethod
    def _parse_device_desc(desc):
        r"""
        解析设备描述字符串。

        注册表里的原始值形如：
            @oem109.inf,%wovad.devicedesc%;WO Mic Device
        格式为：@<inf文件>,<资源键>;<实际显示名>
        最后一段分号之后才是人能看懂的名称。
        """
        if not desc:
            return "", ""
        s = str(desc)
        inf = ""
        if s.startswith("@"):
            head = s[1:].split(";")[0]
            inf = head.split(",")[0]
        # 取分号后的显示名
        if ";" in s:
            display = s.split(";")[-1].strip()
        else:
            display = s.lstrip("@").strip()
        return inf, display

    def _read_values(self, sub_key):
        """读取一个注册表键的全部值，返回 dict"""
        out = {}
        try:
            key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, sub_key, 0, winreg.KEY_READ)
        except (FileNotFoundError, PermissionError, OSError):
            return out
        try:
            i = 0
            while True:
                try:
                    name, value, _type = winreg.EnumValue(key, i)
                except OSError:
                    break
                i += 1
                out[name] = value
        finally:
            try:
                winreg.CloseKey(key)
            except Exception:
                pass
        return out

    def _get_class_instance(self, driver_ref):
        """
        通过设备键里的 Driver 值（形如 {4d36e96c-...}\\0001）
        读取驱动类实例，拿到 ProviderName / DriverVersion / InfPath。
        """
        if not driver_ref:
            return {}
        key = r"SYSTEM\CurrentControlSet\Control\Class\\" + str(driver_ref)
        if key in self._class_cache:
            return self._class_cache[key]
        vals = self._read_values(key)
        info = {
            "provider": str(vals.get("ProviderName", "") or ""),
            "driver_version": str(vals.get("DriverVersion", "") or ""),
            "inf_path": str(vals.get("InfPath", "") or ""),
            "driver_desc": str(vals.get("DriverDesc", "") or ""),
            "mfg": str(vals.get("Mfg", "") or ""),
        }
        self._class_cache[key] = info
        return info

    # ==================== 枚举 ====================

    def scan_root_devices(self):
        """
        枚举 ROOT 下的根设备（软件模拟设备）。
        返回 list[dict]
        """
        base = r"SYSTEM\CurrentControlSet\Enum\ROOT"
        try:
            root = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, base, 0, winreg.KEY_READ)
        except (FileNotFoundError, PermissionError, OSError):
            return []

        devices = []
        try:
            ci = 0
            while ci < self.MAX_DEVICES:
                if self._stop:
                    break
                try:
                    class_key = winreg.EnumKey(root, ci)
                except OSError:
                    break
                ci += 1
                try:
                    ck = winreg.OpenKey(root, class_key, 0, winreg.KEY_READ)
                except (FileNotFoundError, PermissionError, OSError):
                    continue
                try:
                    ii = 0
                    while ii < 50:
                        try:
                            inst = winreg.EnumKey(ck, ii)
                        except OSError:
                            break
                        ii += 1
                        vals = self._read_values(base + "\\" + class_key + "\\" + inst)
                        if not vals:
                            continue
                        desc_inf, desc_name = self._parse_device_desc(vals.get("DeviceDesc", ""))
                        friendly = str(vals.get("FriendlyName", "") or "")
                        name = friendly or desc_name or class_key
                        devices.append({
                            "instance_id": f"ROOT\\{class_key}\\{inst}",
                            "class_key": class_key,
                            "name": name,
                            "raw_desc": str(vals.get("DeviceDesc", "") or ""),
                            "inf": desc_inf,
                            "service": str(vals.get("Service", "") or ""),
                            "driver_ref": str(vals.get("Driver", "") or ""),
                            "config_flags": int(vals.get("ConfigFlags", 0) or 0),
                            "hardware_id": vals.get("HardwareID", []),
                            "endpoints": [],
                            "kind": "root",
                        })
                finally:
                    try:
                        winreg.CloseKey(ck)
                    except Exception:
                        pass
        finally:
            try:
                winreg.CloseKey(root)
            except Exception:
                pass

        # 补充驱动类信息
        for d in devices:
            info = self._get_class_instance(d["driver_ref"])
            d["provider"] = info.get("provider", "")
            d["driver_version"] = info.get("driver_version", "")
            d["driver_inf"] = info.get("inf_path", "") or d["inf"]
        return devices

    def scan_audio_endpoints(self):
        """
        枚举音频端点（麦克风 / 扬声器）。
        InstanceId 前缀含义：
            {0.0.0.xxxx} → 播放端点（扬声器）
            {0.0.1.xxxx} → 录制端点（麦克风）
        """
        base = r"SYSTEM\CurrentControlSet\Enum\SWD\MMDEVAPI"
        try:
            root = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, base, 0, winreg.KEY_READ)
        except (FileNotFoundError, PermissionError, OSError):
            return []

        endpoints = []
        try:
            i = 0
            while i < self.MAX_DEVICES:
                if self._stop:
                    break
                try:
                    inst = winreg.EnumKey(root, i)
                except OSError:
                    break
                i += 1
                vals = self._read_values(base + "\\" + inst)
                if not vals:
                    continue
                friendly = str(vals.get("FriendlyName", "") or "")
                if not friendly:
                    continue
                low = inst.lower()
                if low.startswith("{0.0.1."):
                    direction = "录音(麦克风)"
                elif low.startswith("{0.0.0."):
                    direction = "播放(扬声器)"
                else:
                    direction = "其他"
                endpoints.append({
                    "instance_id": f"SWD\\MMDEVAPI\\{inst}",
                    "name": friendly,
                    "direction": direction,
                    "driver_ref": str(vals.get("Driver", "") or ""),
                    "kind": "endpoint",
                })
        finally:
            try:
                winreg.CloseKey(root)
            except Exception:
                pass
        return endpoints

    # ==================== 分类与归属 ====================

    def _classify_device(self, dev):
        """
        判断设备分类。返回 (分类名, 是否系统核心设备, 匹配依据)
        """
        class_key = dev.get("class_key", "")
        service = (dev.get("service") or "").lower()
        name_low = (dev.get("name") or "").lower()

        # 1) 系统核心虚拟总线：直接排除
        if class_key in SYSTEM_ROOT_DEVICES:
            return ("系统虚拟总线", True, f"ROOT\\{class_key} 属于 Windows 基础组件")

        # 2) 已知虚拟驱动
        for key, (cat, _owner) in KNOWN_VIRTUAL_DRIVERS.items():
            if key and (service == key or service.startswith(key) or key in service):
                return (cat, False, f"驱动 {service} 属于已知虚拟设备")

        # 3) 名称特征
        for cat, hints in NAME_HINTS:
            if any(h in name_low for h in hints):
                return (cat, False, "设备名称含虚拟设备特征")

        # 4) 按设备类判断
        if class_key.upper() in ("MEDIA",):
            return ("虚拟音频", False, "ROOT 下的多媒体设备（无真实硬件）")
        if class_key.upper() in ("NET",):
            return ("虚拟网卡", False, "ROOT 下的网络适配器（无真实硬件）")
        if class_key.upper() in ("CAMERA", "IMAGE"):
            return ("虚拟摄像头", False, "ROOT 下的影像设备")
        if class_key.upper() in ("CDROM",):
            return ("虚拟光驱", False, "ROOT 下的光驱设备")
        if class_key.upper() in ("VOLUME", "STORAGE"):
            return ("虚拟磁盘", False, "ROOT 下的存储设备")

        return ("其他虚拟设备", False, "ROOT 下的软件模拟设备")

    def _owner_from_known(self, service):
        """从已知驱动映射表里取所属软件名"""
        s = (service or "").lower()
        for key, (_cat, owner) in KNOWN_VIRTUAL_DRIVERS.items():
            if key and (s == key or s.startswith(key) or key in s):
                return owner
        return ""

    def _match_installed_software(self, dev, software_list):
        """
        把设备归属到已安装的软件。
        依次尝试：厂商名匹配 → 设备名匹配 → 已知驱动映射。
        返回 (软件名, 是否已安装, 匹配依据)
        """
        provider = (dev.get("provider") or "").strip().lower()
        name = (dev.get("name") or "").strip().lower()
        service = (dev.get("service") or "").lower()

        # 逐个比对已装软件
        if provider:
            for s in software_list:
                pub = (s.get("publisher") or "").strip().lower()
                if not pub:
                    continue
                # 厂商名互相包含（"Wolicheng Tech Ltd." vs "Wolicheng Tech"）
                if pub in provider or provider in pub:
                    return (s.get("name", ""), True, f"厂商匹配：{s.get('publisher')}")

        if name:
            for s in software_list:
                sname = (s.get("name") or "").strip().lower()
                if not sname:
                    continue
                if sname in name or name in sname:
                    return (s.get("name", ""), True, f"名称匹配：{s.get('name')}")

        # 已知映射（说明这是有明确来源的虚拟设备，但列表里找不到对应软件）
        known = self._owner_from_known(service)
        if known:
            # 再宽松地查一次：软件名里是否含厂商关键字
            for s in software_list:
                sname = (s.get("name") or "").lower()
                if known.lower() in sname or sname in known.lower():
                    return (s.get("name", ""), True, f"已知驱动映射：{known}")
            return (known, False, f"已知虚拟驱动映射到「{known}」，但软件列表中未找到")

        if provider:
            return (provider, False, f"厂商「{provider}」不在已安装软件列表中")
        return ("", False, "")

    # ==================== 主入口 ====================

    def scan_devices(self, include_system=False):
        """
        扫描虚拟设备。

        参数
          include_system: 是否包含 Windows 系统虚拟总线（默认不含，避免噪声）

        返回 list[dict]，每项：
          {"instance_id","name","category","kind","service","provider",
           "driver_version","inf","disabled","driver_running","driver_file",
           "driver_exists","endpoints","owner","owner_installed","match_reason",
           "is_orphan","status_text","advice"}
        """
        self.reset_stop()
        self._emit(3, "正在读取已安装软件列表...")

        # 已安装软件（用于归属判断）
        try:
            from software_manager import SoftwareManager
            software_list = SoftwareManager().list_installed_software()
        except Exception:
            software_list = []

        self._emit(25, "正在枚举根设备（虚拟设备）...")
        root_devices = self.scan_root_devices()
        self._emit(50, f"已发现 {len(root_devices)} 个根设备，正在枚举音频端点...")
        endpoints = self.scan_audio_endpoints()
        self._emit(65, "正在关联驱动状态...")

        # 驱动服务状态
        driver_states = {}
        try:
            from drivers import DriverEngine
            de = DriverEngine()
            names = [d["service"] for d in root_devices if d.get("service")]
            if names:
                driver_states = de._query_states(names)
        except Exception:
            driver_states = {}

        # 驱动文件路径（用于判断是否还在）
        driver_files = {}
        try:
            from drivers import DriverEngine
            de2 = DriverEngine()
            for d in root_devices:
                svc = d.get("service")
                if not svc:
                    continue
                vals = self._read_values(
                    r"SYSTEM\CurrentControlSet\Services\\" + svc)
                img = str(vals.get("ImagePath", "") or "")
                if img:
                    driver_files[svc] = de2._resolve_image_path(img)
        except Exception:
            pass

        # 音频端点按名称里的括号归属到父设备
        for ep in endpoints:
            m = re.search(r"[（(]([^）)]+)[）)]", ep["name"])
            ep["parent_hint"] = m.group(1).strip() if m else ""

        results = []
        for d in root_devices:
            if self._stop:
                break
            category, is_system, reason = self._classify_device(d)
            if is_system and not include_system:
                continue

            dev_low = (d.get("name") or "").lower()
            # 关联端点：端点的父设备提示名包含在设备名里，或反之
            linked = []
            for ep in endpoints:
                hint = (ep.get("parent_hint") or "").lower()
                if hint and (hint in dev_low or dev_low in hint):
                    linked.append(ep)

            svc = d.get("service", "")
            state = driver_states.get(svc, ("未知", 0))[0]
            running = state == "运行中"
            df = driver_files.get(svc, "")
            exists = bool(df) and os.path.isfile(df)
            disabled = bool(d.get("config_flags", 0) & 0x1)

            owner, owner_installed, match_reason = self._match_installed_software(
                d, software_list)

            # 残留判定：设备还在，但驱动文件丢失，或所属软件已不在列表中
            is_orphan = False
            if not exists and svc:
                is_orphan = True
            elif owner and not owner_installed:
                is_orphan = True

            if disabled:
                status_text = "已禁用"
            elif running:
                status_text = "运行中"
            elif svc:
                status_text = state
            else:
                status_text = "已安装"

            item = {
                "instance_id": d["instance_id"],
                "name": d["name"],
                "category": category,
                "kind": "device",
                "service": svc,
                "provider": d.get("provider", ""),
                "driver_version": d.get("driver_version", ""),
                "inf": d.get("driver_inf") or d.get("inf", ""),
                "disabled": disabled,
                "driver_running": running,
                "driver_file": df,
                "driver_exists": exists,
                "endpoints": linked,
                "owner": owner,
                "owner_installed": owner_installed,
                "match_reason": match_reason,
                "is_orphan": is_orphan,
                "status_text": status_text,
                "advice": "",
            }
            item["advice"] = self._advice_for(item)
            results.append(item)

        results.sort(key=lambda x: (not x["is_orphan"], x["category"], x["name"].lower()))
        self._emit(100, f"扫描完成，共 {len(results)} 个虚拟设备")
        return results

    def _advice_for(self, it):
        """生成保守的处置建议"""
        if it["is_orphan"]:
            if not it["driver_exists"] and it["service"]:
                return (f"所属软件的驱动文件已丢失，但设备与驱动服务仍注册在系统里，"
                        f"属于卸载残留。建议先用管理员身份执行 "
                        f"pnputil /enum-drivers 找到对应驱动包后删除，"
                        f"再执行 sc delete {it['service']}。请勿直接删除正在使用的设备。")
            if it["owner"]:
                return (f"该虚拟设备属于「{it['owner']}」，但当前已安装软件列表中找不到它，"
                        "说明软件可能已卸载而设备未清理干净。"
                        "可在「设备管理器」中右键卸载该设备（勾选删除驱动程序），或用 pnputil 移除驱动包。")
            return ("未能确定该虚拟设备属于哪个软件。建议在「设备管理器 > 声音、视频和游戏控制器」"
                    "中查看其详细信息后再决定是否卸载。")
        if it["disabled"]:
            return "该设备当前已被禁用。若仍需要它工作，可在设备管理器中重新启用。"
        if it["owner_installed"]:
            return (f"该虚拟设备属于已安装的「{it['owner']}」，且正在正常工作，"
                    "无需处理。如需移除，请先卸载该软件。")
        return "该虚拟设备工作正常，无需处理。"

    # ==================== 汇总到 ScanResult ====================

    def scan_virtual_device_issues(self):
        """
        生成"值得提醒用户的虚拟设备问题"（供主程序结果表格）。
        只报告残留，不把正常设备当垃圾。
        """
        self._emit(2, "正在检查虚拟设备...")
        devices = self.scan_devices()
        results = []
        for it in devices:
            if self._stop:
                break
            if not it["is_orphan"]:
                continue
            eps = "、".join(e["name"] for e in it["endpoints"][:3])
            detail = f"设备：{it['name']}"
            if it["service"]:
                detail += f"　驱动服务：{it['service']}"
            if it["provider"]:
                detail += f"　厂商：{it['provider']}"
            if eps:
                detail += f"　关联端点：{eps}"
            results.append(ScanResult(
                category="虚拟设备残留",
                source=f"{it['category']}残留",
                path=it["instance_id"],
                size=0,
                description=(detail + "。" + it["advice"]),
            ))
        self._emit(100, f"虚拟设备检查完成，发现 {len(results)} 项残留")
        return results

    def get_summary(self, devices=None):
        """统计信息"""
        if devices is None:
            devices = self.scan_devices()
        cats = {}
        for d in devices:
            cats[d["category"]] = cats.get(d["category"], 0) + 1
        return {
            "total": len(devices),
            "orphan": sum(1 for d in devices if d["is_orphan"]),
            "running": sum(1 for d in devices if d["driver_running"]),
            "categories": cats,
        }
