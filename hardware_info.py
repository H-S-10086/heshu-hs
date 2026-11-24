# -*- coding: utf-8 -*-
"""
硬件信息检测引擎
- 仅使用 Python 标准库（ctypes / subprocess / os / re / csv / io / datetime 等）
  以及 scanner 中的 ScanResult / get_size / format_size，不引入任何第三方依赖（便于 PyInstaller 单文件打包）。
- 所有系统调用均带超时并做容错，捕获 PermissionError / FileNotFoundError / OSError /
  subprocess.SubprocessError / UnicodeDecodeError，绝不让异常冒泡。
- 解析优先使用 PowerShell 的 `ConvertTo-Csv -NoTypeInformation`，字段名按英文关键名匹配，
  因此在中英文系统上都能工作；解析不到就返回空值并说明原因。
- 硬性要求：任何读取不到的数值一律用 None / 空列表表示，绝不伪造硬件数据。
"""

import os
import re
import io
import csv
import ctypes
import shutil
import tempfile
import subprocess
from datetime import datetime

from scanner import ScanResult, get_size, format_size


# Windows 下避免弹出控制台窗口
_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)

# SMBIOS 内存类型代码 -> 中文名称（WMI Win32_PhysicalMemory.SMBIOSMemoryType）
_SMBIOS_MEMORY_TYPE = {
    0: "未知", 1: "其他", 2: "DRAM", 3: "同步DRAM", 4: "缓存DRAM",
    5: "EDO", 6: "EDRAM", 7: "VRAM", 8: "SRAM", 9: "RAM", 10: "ROM",
    11: "Flash", 12: "EEPROM", 13: "FEPROM", 14: "EPROM", 15: "CDRAM",
    16: "3DRAM", 17: "SDRAM", 18: "SGRAM", 19: "RDRAM", 20: "DDR",
    21: "DDR2", 22: "DDR2 FB-DIMM", 24: "DDR3", 25: "FBD2", 26: "DDR4",
    27: "LPDDR", 28: "LPDDR2", 29: "LPDDR3", 30: "LPDDR4",
    34: "DDR5", 35: "LPDDR5",
}

# 物理磁盘介质类型 -> 展示用类型
_MEDIA_TYPE_MAP = {
    "SSD": "SSD",
    "HDD": "HDD",
    "SCM": "SCM",
    "Unspecified": "未知",
    "FileBackedVirtual": "虚拟磁盘",
}

# 磁盘健康状态 -> 中文
_HEALTH_MAP = {
    "Healthy": "健康",
    "Warning": "警告",
    "Unhealthy": "异常",
    "Unknown": "未知",
}


class HardwareEngine:
    """硬件信息检测引擎（纯引擎逻辑，不含任何 UI 代码）"""

    def __init__(self, progress_callback=None):
        self.progress_callback = progress_callback

    def _emit(self, percent, message):
        """上报进度，交由主程序更新界面"""
        if self.progress_callback:
            try:
                self.progress_callback(percent, message)
            except Exception:
                # 进度回调出错不应影响引擎主流程
                pass

    # ==================== 底层系统调用封装 ====================

    @staticmethod
    def _decode_bytes(data):
        """容错解码子进程输出：优先 utf-8，其次 gbk，最后 latin-1（永不抛异常）"""
        if data is None:
            return ""
        if isinstance(data, str):
            return data
        for enc in ("utf-8", "gbk", "latin-1"):
            try:
                return data.decode(enc)
            except (UnicodeDecodeError, LookupError):
                continue
        return data.decode("utf-8", errors="ignore")

    def _run_cmd(self, args, timeout=20, input_bytes=None, shell=False):
        """执行外部命令。

        返回 (returncode, stdout_text, stderr_text)；执行失败返回 (None, "", 错误说明)。
        所有异常都被捕获，绝不冒泡；timeout 单位为秒。
        """
        try:
            proc = subprocess.run(
                args,
                input=input_bytes,
                capture_output=True,
                timeout=timeout,
                shell=shell,
                creationflags=_CREATE_NO_WINDOW,
            )
        except subprocess.TimeoutExpired:
            return None, "", f"命令执行超时（>{timeout}秒）"
        except subprocess.SubprocessError as e:
            return None, "", f"子进程错误: {e}"
        except (OSError, ValueError) as e:
            return None, "", f"命令无法执行: {e}"
        out = self._decode_bytes(proc.stdout)
        err = self._decode_bytes(proc.stderr)
        return proc.returncode, out, err

    def _run_powershell(self, script, timeout=20):
        """执行 PowerShell 命令并返回标准输出文本，失败返回 None。

        统一使用 -NoProfile -NonInteractive 降低启动开销并避免交互式挂起，
        并在脚本前设置控制台输出编码为 UTF-8，保证中文不乱码。
        """
        prelude = "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8;"
        cmd = [
            "powershell.exe", "-NoProfile", "-NonInteractive",
            "-Command", prelude + script,
        ]
        rc, out, _err = self._run_cmd(cmd, timeout=timeout)
        if rc is None:
            return None
        return out

    def _parse_csv(self, text):
        """把 PowerShell ConvertTo-Csv 的输出解析为 list[dict]。

        解析失败或为空时返回空列表；值统一去首尾空白。
        """
        if not text:
            return []
        text = text.replace("\ufeff", "").strip()
        if not text:
            return []
        try:
            rows = list(csv.reader(io.StringIO(text)))
        except (csv.Error, UnicodeDecodeError, OSError):
            return []
        rows = [r for r in rows if r and any(str(c).strip() for c in r)]
        if len(rows) < 2:
            return []
        header = [h.strip() for h in rows[0]]
        result = []
        for row in rows[1:]:
            if len(row) < len(header):
                row = row + [""] * (len(header) - len(row))
            item = {}
            for i, key in enumerate(header):
                item[key] = str(row[i]).strip() if i < len(row) else ""
            result.append(item)
        return result

    def _run_ps_csv(self, script, timeout=25):
        """执行 PowerShell 并把 CSV 输出解析为 list[dict]，失败返回空列表"""
        return self._parse_csv(self._run_powershell(script, timeout=timeout))

    @staticmethod
    def _to_int(value):
        """容错转整数，失败返回 None（不伪造）"""
        if value is None:
            return None
        s = str(value).strip()
        if s == "":
            return None
        try:
            return int(float(s))
        except (ValueError, TypeError):
            return None

    @staticmethod
    def _to_float(value):
        """容错转浮点数，失败返回 None（不伪造）"""
        if value is None:
            return None
        s = str(value).strip()
        if s == "":
            return None
        try:
            return float(s)
        except (ValueError, TypeError):
            return None

    @staticmethod
    def _is_admin():
        """判断当前进程是否以管理员身份运行（封装自 ctypes.windll.shell32.IsUserAnAdmin）"""
        try:
            return bool(ctypes.windll.shell32.IsUserAnAdmin())
        except (AttributeError, OSError):
            return False

    @staticmethod
    def _strip_colon(drive):
        """把 'C:' / 'C' 统一规整成盘符字母大写形式，如 'C'"""
        if not drive:
            return "C"
        return str(drive).rstrip(":\\/").upper()[:1] or "C"

    @staticmethod
    def _cim_datetime(value):
        """把 CIM 时间（如 20240115000000.000000+480）转成可读时间字符串，失败返回原值"""
        if not value:
            return None
        s = str(value).strip()
        # 形如 20240115000000.000000+480，前 14 位为 YYYYMMDDHHMMSS
        if len(s) >= 14 and s[:14].isdigit():
            try:
                dt = datetime.strptime(s[:14], "%Y%m%d%H%M%S")
                return dt.strftime("%Y-%m-%d %H:%M:%S")
            except ValueError:
                return s
        return s

    @staticmethod
    def _smart_available(smart_row):
        """判断某条 SMART 预测查询结果是否包含有效数据（内部辅助函数）"""
        if not smart_row:
            return False
        return bool(str(smart_row.get("PredictFailure", "")).strip()
                    or str(smart_row.get("InstanceName", "")).strip())

    # ==================== 硬件信息总览 ====================

    def get_hardware_info(self):
        """返回分组的中文硬件信息字典（对标鲁大师 / Speccy 的总览）"""
        info = {
            "cpu": self._get_cpu_info(),
            "memory": self._get_memory_info(),
            "disk": self._get_disk_list(),
            "gpu": self._get_gpu_info(),
            "motherboard": self._get_motherboard_info(),
            "os": self._get_os_info(),
            "battery": self._get_battery_brief(),
        }
        return info

    def _get_cpu_info(self):
        """CPU 信息：名称 / 核心数 / 线程数 / 基准频率 / 当前占用率"""
        self._emit(5, "正在读取 CPU 信息...")
        rows = self._run_ps_csv(
            "Get-CimInstance Win32_Processor -ErrorAction SilentlyContinue | "
            "Select-Object Name,NumberOfCores,NumberOfLogicalProcessors,MaxClockSpeed,LoadPercentage | "
            "ConvertTo-Csv -NoTypeInformation"
        )
        result = {"名称": None, "核心数": None, "线程数": None, "基准频率": None, "当前占用率": None}
        if not rows:
            return result
        r = rows[0]
        mhz = self._to_int(r.get("MaxClockSpeed"))
        load = self._to_int(r.get("LoadPercentage"))
        result["名称"] = r.get("Name") or None
        result["核心数"] = self._to_int(r.get("NumberOfCores"))
        result["线程数"] = self._to_int(r.get("NumberOfLogicalProcessors"))
        result["基准频率"] = f"{mhz} MHz" if mhz is not None else None
        result["当前占用率"] = f"{load}%" if load is not None else None
        return result

    def _get_memory_info(self):
        """内存信息：总容量 / 已用 / 可用 / 使用率 / 内存条明细"""
        self._emit(12, "正在读取内存信息...")
        info = {"总容量": None, "已用": None, "可用": None, "使用率": None, "内存条": []}

        # 内存条明细（Win32_PhysicalMemory）
        modules = self._run_ps_csv(
            "Get-CimInstance Win32_PhysicalMemory -ErrorAction SilentlyContinue | "
            "Select-Object Capacity,Speed,Manufacturer,SMBIOSMemoryType | "
            "ConvertTo-Csv -NoTypeInformation"
        )
        total = 0
        for m in modules:
            cap = self._to_int(m.get("Capacity"))
            if not cap or cap <= 0:
                continue
            total += cap
            speed = self._to_int(m.get("Speed"))
            type_code = self._to_int(m.get("SMBIOSMemoryType"))
            type_name = _SMBIOS_MEMORY_TYPE.get(
                type_code, f"类型代码 {type_code}" if type_code is not None else "未知"
            )
            info["内存条"].append({
                "容量": format_size(cap),
                "频率": f"{speed} MHz" if speed else "未知",
                "厂商": m.get("Manufacturer") or "未知",
                "类型": type_name,
            })

        # 使用率 / 可用：来自 Win32_OperatingSystem（KB）
        os_rows = self._run_ps_csv(
            "Get-CimInstance Win32_OperatingSystem -ErrorAction SilentlyContinue | "
            "Select-Object TotalVisibleMemorySize,FreePhysicalMemory | "
            "ConvertTo-Csv -NoTypeInformation"
        )
        if os_rows:
            total_kb = self._to_int(os_rows[0].get("TotalVisibleMemorySize"))
            free_kb = self._to_int(os_rows[0].get("FreePhysicalMemory"))
            if total_kb and total_kb > 0:
                free_bytes = (free_kb or 0) * 1024
                total_bytes = total_kb * 1024
                used_bytes = total_bytes - free_bytes
                info["总容量"] = format_size(total_bytes)
                info["已用"] = format_size(used_bytes)
                info["可用"] = format_size(free_bytes)
                info["使用率"] = f"{used_bytes / total_bytes * 100:.1f}%"
        elif total > 0:
            # 拿不到使用率时至少给出物理总量
            info["总容量"] = format_size(total)
        return info

    def _get_disk_list(self):
        """磁盘列表：型号 / 容量 / 类型(SSD/HDD) / 接口 / 健康状态"""
        self._emit(22, "正在读取磁盘信息...")
        disks = self._run_ps_csv(
            "Get-CimInstance Win32_DiskDrive -ErrorAction SilentlyContinue | "
            "Select-Object Model,Size,InterfaceType | ConvertTo-Csv -NoTypeInformation"
        )
        # Get-PhysicalDisk 提供 SSD/HDD 与健康状态（无存储服务时可能失败，失败留空）
        phys = self._run_ps_csv(
            "Get-PhysicalDisk -ErrorAction SilentlyContinue | "
            "Select-Object DeviceId,FriendlyName,MediaType,Size,HealthStatus | "
            "ConvertTo-Csv -NoTypeInformation"
        )

        result = []
        for idx, d in enumerate(disks):
            model = d.get("Model") or "未知"
            size = self._to_int(d.get("Size"))
            entry = {
                "型号": model,
                "容量": format_size(size) if size else "未知",
                "类型": "未知",
                "接口": d.get("InterfaceType") or "未知",
                "健康状态": "未知",
            }
            # 优先按型号名匹配物理磁盘，补充介质类型与健康状态
            matched = None
            for p in phys:
                pname = p.get("FriendlyName") or ""
                if pname and model and (pname in model or model in pname):
                    matched = p
                    break
            # 退化：按顺序匹配
            if matched is None and idx < len(phys):
                matched = phys[idx]
            if matched is not None:
                entry["类型"] = _MEDIA_TYPE_MAP.get(matched.get("MediaType", ""), "未知")
                entry["健康状态"] = _HEALTH_MAP.get(matched.get("HealthStatus", ""), "未知")
            result.append(entry)
        return result

    def _get_gpu_info(self):
        """显卡列表：名称 / 显存 / 驱动版本"""
        self._emit(45, "正在读取显卡信息...")
        rows = self._run_ps_csv(
            "Get-CimInstance Win32_VideoController -ErrorAction SilentlyContinue | "
            "Select-Object Name,AdapterRAM,DriverVersion | ConvertTo-Csv -NoTypeInformation"
        )
        result = []
        for r in rows:
            name = r.get("Name") or "未知"
            vram = self._to_int(r.get("AdapterRAM"))
            # AdapterRAM 为 32 位值，超过 4GB 时可能被截断/为 0，这里如实展示，不做推测
            result.append({
                "名称": name,
                "显存": format_size(vram) if vram and vram > 0 else "未知",
                "驱动版本": r.get("DriverVersion") or "未知",
            })
        return result

    def _get_motherboard_info(self):
        """主板 / BIOS：厂商 / 型号 / BIOS 版本"""
        self._emit(58, "正在读取主板信息...")
        info = {"厂商": None, "型号": None, "BIOS版本": None}
        board = self._run_ps_csv(
            "Get-CimInstance Win32_BaseBoard -ErrorAction SilentlyContinue | "
            "Select-Object Manufacturer,Product | ConvertTo-Csv -NoTypeInformation"
        )
        if board:
            info["厂商"] = board[0].get("Manufacturer") or None
            info["型号"] = board[0].get("Product") or None
        bios = self._run_ps_csv(
            "Get-CimInstance Win32_BIOS -ErrorAction SilentlyContinue | "
            "Select-Object SMBIOSBIOSVersion,Version | ConvertTo-Csv -NoTypeInformation"
        )
        if bios:
            info["BIOS版本"] = bios[0].get("SMBIOSBIOSVersion") or bios[0].get("Version") or None
        return info

    def _get_os_info(self):
        """操作系统：名称 / 版本 / 内部版本号 / 安装时间 / 架构"""
        self._emit(68, "正在读取系统信息...")
        info = {"系统名称": None, "版本": None, "内部版本号": None, "安装时间": None, "架构": None}
        rows = self._run_ps_csv(
            "Get-CimInstance Win32_OperatingSystem -ErrorAction SilentlyContinue | "
            "Select-Object Caption,Version,BuildNumber,InstallDate,OSArchitecture | "
            "ConvertTo-Csv -NoTypeInformation"
        )
        if rows:
            r = rows[0]
            info["系统名称"] = r.get("Caption") or None
            info["版本"] = r.get("Version") or None
            info["内部版本号"] = r.get("BuildNumber") or None
            info["安装时间"] = self._cim_datetime(r.get("InstallDate"))
            info["架构"] = r.get("OSArchitecture") or None
        return info

    def _get_battery_brief(self):
        """电池简要信息（总览用，避免生成电池报告拖慢整体速度）"""
        rows = self._run_ps_csv(
            "Get-CimInstance Win32_Battery -ErrorAction SilentlyContinue | "
            "Select-Object Name,EstimatedChargeRemaining,BatteryStatus | "
            "ConvertTo-Csv -NoTypeInformation"
        )
        if not rows:
            return {"存在": False}
        r = rows[0]
        charge = self._to_int(r.get("EstimatedChargeRemaining"))
        return {
            "存在": True,
            "名称": r.get("Name") or "未知",
            "电量百分比": f"{charge}%" if charge is not None else None,
            "状态": self._battery_status_text(self._to_int(r.get("BatteryStatus"))),
        }

    @staticmethod
    def _battery_status_text(code):
        """Win32_Battery.BatteryStatus 状态码 -> 中文说明"""
        mapping = {
            1: "放电中", 2: "已接通电源", 3: "已充满", 4: "低电量",
            5: "电量临界", 6: "充电中", 7: "充电中(高)", 8: "充电中(低)",
            9: "充电中(临界)", 10: "未定义", 11: "部分充电",
        }
        if code is None:
            return "未知"
        return mapping.get(code, f"未知状态({code})")

    # ==================== 实时占用率 ====================

    def get_live_usage(self):
        """实时占用率（供主程序每 2 秒轮询；所有调用 timeout <= 5 秒，保证快速返回）"""
        result = {
            "cpu_percent": 0.0, "mem_percent": 0.0,
            "mem_used": 0, "mem_total": 0,
            "disk_read_mbps": 0.0, "disk_write_mbps": 0.0,
        }
        # 一次性取回 CPU / 内存 / 磁盘读写，减少 PowerShell 启动次数
        combined = (
            "$cpu=(Get-CimInstance Win32_Processor -ErrorAction SilentlyContinue | "
            "Measure-Object -Property LoadPercentage -Average -ErrorAction SilentlyContinue).Average;"
            "$os=Get-CimInstance Win32_OperatingSystem -ErrorAction SilentlyContinue;"
            "$pd=Get-CimInstance Win32_PerfFormattedData_PerfDisk_PhysicalDisk -ErrorAction SilentlyContinue | "
            "Where-Object {$_.Name -eq '_Total'};"
            "[PSCustomObject]@{CpuPercent=$cpu;MemTotalKb=$os.TotalVisibleMemorySize;"
            "MemFreeKb=$os.FreePhysicalMemory;DiskRead=$pd.DiskReadBytesPerSec;"
            "DiskWrite=$pd.DiskWriteBytesPerSec} | ConvertTo-Csv -NoTypeInformation"
        )
        rows = self._run_ps_csv(combined, timeout=5)

        if not rows:
            # 组合查询失败（可能耗时类接口超时），退回只取 CPU / 内存的轻量查询
            rows = self._run_ps_csv(
                "$cpu=(Get-CimInstance Win32_Processor -ErrorAction SilentlyContinue | "
                "Measure-Object -Property LoadPercentage -Average -ErrorAction SilentlyContinue).Average;"
                "$os=Get-CimInstance Win32_OperatingSystem -ErrorAction SilentlyContinue;"
                "[PSCustomObject]@{CpuPercent=$cpu;MemTotalKb=$os.TotalVisibleMemorySize;"
                "MemFreeKb=$os.FreePhysicalMemory} | ConvertTo-Csv -NoTypeInformation",
                timeout=5,
            )
        if not rows:
            return result

        r = rows[0]
        cpu = self._to_float(r.get("CpuPercent"))
        if cpu is not None:
            result["cpu_percent"] = round(cpu, 1)

        total_kb = self._to_int(r.get("MemTotalKb"))
        free_kb = self._to_int(r.get("MemFreeKb"))
        if total_kb and total_kb > 0:
            total_bytes = total_kb * 1024
            used_bytes = total_bytes - (free_kb or 0) * 1024
            result["mem_total"] = total_bytes
            result["mem_used"] = used_bytes
            result["mem_percent"] = round(used_bytes / total_bytes * 100, 1)

        # 磁盘读写速率（Win32_PerfFormattedData 已是每秒速率，单位字节），取不到保持 0.0
        read = self._to_float(r.get("DiskRead"))
        write = self._to_float(r.get("DiskWrite"))
        if read is not None:
            result["disk_read_mbps"] = round(read / 1024 / 1024, 2)
        if write is not None:
            result["disk_write_mbps"] = round(write / 1024 / 1024, 2)
        return result

    # ==================== 温度监控 ====================

    def get_temperatures(self):
        """读取温度。

        说明：Windows 没有标准通用接口可直接读取硬件温度。这里尝试 WMI 命名空间
        root\\wmi 的 MSAcpi_ThermalZoneTemperature（多数机型不支持或需要管理员权限），
        值单位为 1/10 开尔文，换算：摄氏度 = value/10 - 273.15。
        读不到就返回 value=None / status="不可用"，绝不伪造温度数值。
        """
        self._emit(85, "正在读取温度传感器...")
        rows = self._run_ps_csv(
            "Get-WmiObject -Namespace root/wmi -Class MSAcpi_ThermalZoneTemperature "
            "-ErrorAction SilentlyContinue | "
            "Select-Object InstanceName,CurrentTemperature | ConvertTo-Csv -NoTypeInformation",
            timeout=25,
        )
        result = []
        for i, r in enumerate(rows):
            raw = self._to_float(r.get("CurrentTemperature"))
            if raw is None or raw <= 0:
                continue
            celsius = round(raw / 10.0 - 273.15, 1)
            if celsius < 20 or celsius > 130:
                # 明显不合理的换算结果，判为无效，不展示
                continue
            name = "CPU 温度" if i == 0 else f"温度传感器 {i + 1}"
            result.append({
                "name": name,
                "value": celsius,
                "unit": "°C",
                "status": self._temp_status(celsius),
                "desc": r.get("InstanceName") or "",
            })
        if not result:
            return [{
                "name": "CPU 温度",
                "value": None,
                "unit": "°C",
                "status": "不可用",
                "desc": "无法读取温度：需要管理员权限或该机型不支持 ACPI 温度传感器。",
            }]
        return result

    @staticmethod
    def _temp_status(celsius):
        """温度状态判定"""
        if celsius is None:
            return "不可用"
        if celsius >= 85:
            return "高"
        if celsius >= 70:
            return "偏高"
        return "正常"

    # ==================== 磁盘健康 SMART ====================

    def get_disk_health(self):
        """磁盘健康信息（SMART / 存储状态）。

        按优先级尝试：Get-PhysicalDisk -> Get-Disk -> root\\wmi SMART 预测；
        温度 / 通电时长 / 磨损来自 Get-StorageReliabilityCounter（通常需要管理员权限，拿不到填 None）。
        全部失败时返回空列表，不伪造。
        """
        self._emit(60, "正在读取磁盘健康信息...")
        result = []

        phys = self._run_ps_csv(
            "Get-PhysicalDisk -ErrorAction SilentlyContinue | "
            "Select-Object DeviceId,FriendlyName,MediaType,Size,HealthStatus,OperationalStatus,BusType | "
            "ConvertTo-Csv -NoTypeInformation"
        )
        rel = self._run_ps_csv(
            "Get-PhysicalDisk -ErrorAction SilentlyContinue | "
            "Get-StorageReliabilityCounter -ErrorAction SilentlyContinue | "
            "Select-Object DeviceId,Temperature,PowerOnHours,Wear,ReadErrorsTotal | "
            "ConvertTo-Csv -NoTypeInformation",
            timeout=25,
        )
        rel_map = {}
        for r in rel:
            key = str(r.get("DeviceId", "")).strip()
            if key:
                rel_map[key] = r

        # SMART 预测状态（root\wmi），多数机型只能给到首个物理盘
        smart_rows = self._run_ps_csv(
            "Get-WmiObject -Namespace root/wmi -Class MSStorageDriver_FailurePredictStatus "
            "-ErrorAction SilentlyContinue | "
            "Select-Object InstanceName,PredictFailure,Reason | ConvertTo-Csv -NoTypeInformation",
            timeout=20,
        )

        if phys:
            for idx, p in enumerate(phys):
                dev = str(p.get("DeviceId", "")).strip()
                rc = rel_map.get(dev, {})
                if idx < len(smart_rows):
                    smart = smart_rows[idx]
                elif smart_rows:
                    smart = smart_rows[0]
                else:
                    smart = {}
                result.append(self._build_disk_health_entry(
                    model=p.get("FriendlyName") or "未知",
                    media=p.get("MediaType", ""),
                    size=self._to_int(p.get("Size")),
                    health_status=p.get("HealthStatus", ""),
                    operational=p.get("OperationalStatus", ""),
                    bus=p.get("BusType", ""),
                    rel=rc,
                    smart=smart,
                ))
            return result

        # 回退：Get-Disk
        disks = self._run_ps_csv(
            "Get-Disk -ErrorAction SilentlyContinue | "
            "Select-Object Number,FriendlyName,Health,OperationalStatus,BusType,TotalSize | "
            "ConvertTo-Csv -NoTypeInformation"
        )
        for idx, d in enumerate(disks):
            smart = smart_rows[idx] if idx < len(smart_rows) else {}
            result.append(self._build_disk_health_entry(
                model=d.get("FriendlyName") or f"磁盘 {d.get('Number', '')}",
                media="",
                size=self._to_int(d.get("TotalSize")),
                health_status=d.get("Health") or "",
                operational=d.get("OperationalStatus", ""),
                bus=d.get("BusType", ""),
                rel={},
                smart=smart,
            ))
        return result

    def _build_disk_health_entry(self, model, media, size, health_status, operational, bus, rel, smart):
        """组装单块磁盘的健康信息字典"""
        health_cn = _HEALTH_MAP.get(health_status, "未知")
        temp = self._to_float(rel.get("Temperature"))
        power_hours = self._to_int(rel.get("PowerOnHours"))
        wear = self._to_int(rel.get("Wear"))
        read_errors = self._to_int(rel.get("ReadErrorsTotal"))

        wear_parts = []
        if power_hours is not None:
            wear_parts.append(f"已通电 {power_hours} 小时")
        if wear is not None:
            wear_parts.append(f"磨损 {wear}%")
        if read_errors is not None:
            wear_parts.append(f"读取错误 {read_errors} 次")

        # SMART 预测
        smart_predict = ""
        predict = str(smart.get("PredictFailure", "")).strip().lower() if smart else ""
        reason = str(smart.get("Reason", "")).strip() if smart else ""
        if predict in ("true", "1"):
            smart_predict = f"SMART 预测存在故障（原因代码 {reason or '未知'}）"
            if health_cn in ("健康", "未知"):
                health_cn = "警告"
        elif predict in ("false", "0"):
            smart_predict = "SMART 预测无故障"

        detail_parts = []
        if operational:
            detail_parts.append(f"运行状态:{operational}")
        if bus:
            detail_parts.append(f"总线:{bus}")
        detail_parts.extend(wear_parts)
        if not rel:
            detail_parts.append(
                "温度/磨损/通电时长不可用（Get-StorageReliabilityCounter 需管理员权限或该机型不支持）"
            )
        if not self._smart_available(smart):
            detail_parts.append("SMART 预测不可用")

        return {
            "model": model,
            "type": _MEDIA_TYPE_MAP.get(media, "未知") if media else "未知",
            "size": format_size(size) if size else "未知",
            "health": health_cn,
            "smart_predict": smart_predict,
            "temperature": temp,
            "wear_or_hours": "；".join(wear_parts),
            "status_detail": "；".join(detail_parts) if detail_parts else "无补充信息",
        }

    # ==================== SSD 判断与优化 ====================

    def check_disk_for_ssd(self):
        """判断各盘符对应的物理磁盘类型，并给出 TRIM 状态。

        返回 [{"drive": "C:", "type": "SSD"/"HDD"/"未知",
               "trim_enabled": bool 或 None, "is_system": bool}, ...]
        """
        self._emit(88, "正在检测磁盘类型与 TRIM 状态...")
        trim_enabled = self._query_trim_enabled()

        physicals = self._run_ps_csv(
            "Get-PhysicalDisk -ErrorAction SilentlyContinue | "
            "Select-Object DeviceId,FriendlyName,MediaType,BusType | ConvertTo-Csv -NoTypeInformation"
        )
        disks = self._run_ps_csv(
            "Get-Disk -ErrorAction SilentlyContinue | "
            "Select-Object Number,FriendlyName | ConvertTo-Csv -NoTypeInformation"
        )
        partitions = self._run_ps_csv(
            "Get-Partition -ErrorAction SilentlyContinue | "
            "Select-Object DriveLetter,DiskNumber | ConvertTo-Csv -NoTypeInformation"
        )

        # DeviceId / 盘号 -> 介质类型（Get-PhysicalDisk 的 DeviceId 通常即物理盘号）
        phys_by_dev = {}
        phys_by_name = {}
        for p in physicals:
            dev = str(p.get("DeviceId", "")).strip()
            if dev:
                phys_by_dev[dev] = p.get("MediaType", "")
            name = (p.get("FriendlyName") or "").strip()
            if name:
                phys_by_name[name] = p.get("MediaType", "")
        # Get-Disk 盘号 -> 型号名
        disk_name_by_num = {}
        for d in disks:
            num = str(d.get("Number", "")).strip()
            if num:
                disk_name_by_num[num] = (d.get("FriendlyName") or "").strip()

        result = []
        for part in partitions:
            # ConvertTo-Csv 对空字符（未分配盘符的分区）会输出空串或 NUL 字符，这里只保留字母盘符
            letter = re.sub(r"[^A-Za-z]", "", str(part.get("DriveLetter") or ""))
            if not letter:
                continue
            num = str(part.get("DiskNumber", "")).strip()
            media = phys_by_dev.get(num, "")
            if not media:
                # 用 Get-Disk 型号名去 Get-PhysicalDisk 匹配
                name = disk_name_by_num.get(num, "")
                if name:
                    media = phys_by_name.get(name, "")
            result.append({
                "drive": f"{letter.upper()}:",
                "type": _MEDIA_TYPE_MAP.get(media, "未知") if media else "未知",
                "trim_enabled": trim_enabled,
                "is_system": letter.upper() == "C",
            })

        if result:
            return result

        # Get-Partition 失败时：逐个枚举存在的盘符，只给类型（无法关联物理盘）
        for letter in "CDEFGH":
            root = f"{letter}:\\"
            if os.path.exists(root):
                result.append({
                    "drive": f"{letter}:",
                    "type": "未知",
                    "trim_enabled": trim_enabled,
                    "is_system": letter == "C",
                })
        return result

    def _query_trim_enabled(self):
        """通过 fsutil 查询 TRIM 是否启用。

        'DisableDeleteNotify = 0' 表示 TRIM 已启用，= 1 表示已禁用；查询失败返回 None。
        """
        rc, out, _err = self._run_cmd(
            ["fsutil", "behavior", "query", "DisableDeleteNotify"], timeout=15
        )
        if rc != 0 or not out:
            return None
        m = re.search(r"DisableDeleteNotify\s*=\s*(\d+)", out)
        if not m:
            return None
        return m.group(1) == "0"

    def optimize_disk_for_ssd(self):
        """在确认系统盘为 SSD 的前提下启用 TRIM。

        返回 (成功布尔, 中文说明)。需要管理员权限。
        """
        self._emit(92, "正在执行 SSD 优化...")
        if not self._is_admin():
            return False, "需要管理员权限"

        drives = self.check_disk_for_ssd()
        sys_drive = next((d for d in drives if d.get("is_system")), None)
        if sys_drive is None:
            return False, "无法定位系统盘，操作已取消"
        dtype = sys_drive.get("type")
        if dtype == "HDD":
            return False, "检测到机械硬盘，无需执行 TRIM 优化"
        if dtype != "SSD":
            return False, "无法确认系统盘为 SSD，为安全起见未执行任何操作"

        rc, out, err = self._run_cmd(
            ["fsutil", "behavior", "set", "DisableDeleteNotify", "0"], timeout=15
        )
        if rc == 0:
            return True, (
                "已启用 NTFS TRIM（DisableDeleteNotify=0）。"
                "建议在系统空闲时对系统盘执行 'defrag C: /L' 进行重修剪(Retrim)，"
                "可进一步维持 SSD 性能；此操作由 Windows 自带工具完成，安全无损。"
            )
        return False, f"启用 TRIM 失败：{err.strip() or out.strip() or '未知错误'}"

    # ==================== 磁盘错误检查 ====================

    def scan_disk_errors(self, drive="C:"):
        """检查磁盘错误相关信息（不会直接运行 chkdsk，避免锁盘）。"""
        self._emit(94, f"正在检查 {drive} 磁盘状态...")
        letter = self._strip_colon(drive)
        result = {
            "drive": f"{letter}:",
            "read_errors": "不可用",
            "file_system": None,
            "total_space": None,
            "free_space": None,
            "dirty_bit": None,
            "message": "",
            "success": False,
        }

        # 卷信息
        rows = self._run_ps_csv(
            f"Get-Volume -DriveLetter {letter} -ErrorAction SilentlyContinue | "
            "Select-Object DriveLetter,FileSystem,Size,SizeRemaining,HealthStatus | "
            "ConvertTo-Csv -NoTypeInformation"
        )
        if rows:
            r = rows[0]
            result["file_system"] = r.get("FileSystem") or None
            result["total_space"] = self._to_int(r.get("Size"))
            result["free_space"] = self._to_int(r.get("SizeRemaining"))
            result["success"] = True
            health = r.get("HealthStatus") or ""
            result["message"] = f"卷状态：{health or '未知'}；文件系统：{result['file_system'] or '未知'}"

        # dirty bit
        rc, out, _err = self._run_cmd(["fsutil", "dirty", "query", f"{letter}:"], timeout=10)
        if rc == 0 and out:
            if re.search(r"is\s+NOT\s+Dirty", out, re.IGNORECASE):
                result["dirty_bit"] = False
            elif re.search(r"is\s+Dirty", out, re.IGNORECASE):
                result["dirty_bit"] = True

        # 读取错误次数（依赖可靠性计数器，通常需管理员权限或单一物理盘）
        rel = self._run_ps_csv(
            "Get-PhysicalDisk -ErrorAction SilentlyContinue | "
            "Get-StorageReliabilityCounter -ErrorAction SilentlyContinue | "
            "Select-Object DeviceId,ReadErrorsTotal | ConvertTo-Csv -NoTypeInformation",
            timeout=20,
        )
        if len(rel) == 1:
            total_errors = self._to_int(rel[0].get("ReadErrorsTotal"))
            if total_errors is not None:
                result["read_errors"] = f"{total_errors} 次"
        elif len(rel) > 1:
            result["read_errors"] = "不可用（存在多块物理磁盘，无法唯一定位）"

        if not result["success"]:
            result["message"] = "无法获取卷信息（可能没有权限或盘符不存在）"
        return result

    def schedule_disk_check(self, drive="C:"):
        """计划在下次重启时对磁盘执行 chkdsk /F /R（需要管理员权限）。

        为避免锁盘卡死，通过管道自动应答 Y，并设置超时。
        """
        self._emit(96, f"正在计划 {drive} 磁盘检查...")
        if not self._is_admin():
            return False, "需要管理员权限"
        letter = self._strip_colon(drive)
        cmd = f"echo Y | chkdsk {letter}: /F /R"
        rc, out, err = self._run_cmd(cmd, timeout=30, shell=True)
        text = (out or "") + (err or "")
        lowered = text.lower()
        if rc == 0 and ("schedule" in lowered or "计划" in text or "重启" in text or "restart" in lowered):
            return True, (
                f"已计划在下次重启时对 {letter}: 执行 chkdsk /F /R。"
                "请重启电脑以完成磁盘检查与修复（耗时可能较长，请保持供电）。"
            )
        if rc == 0:
            # 命令成功执行但未匹配到明确提示，如实反馈原始输出
            return True, f"chkdsk 已执行，输出：{text.strip()[:200] or '（无输出）'}"
        return False, f"计划磁盘检查失败：{err.strip() or out.strip() or '未知错误（可能磁盘被占用）'}"

    # ==================== 电池 ====================

    def get_battery_info(self):
        """电池信息（优先解析 powercfg 电池报告 XML，失败退回 Win32_Battery）。

        读取不到的字段一律为 None，绝不伪造。
        """
        self._emit(97, "正在读取电池信息...")
        result = {
            "exists": False,
            "design_capacity": None,
            "full_capacity": None,
            "health_percent": None,
            "cycle_count": None,
            "current_charge": None,
            "status": "未知",
            "message": "",
            "charge_limit_supported": False,
            "charge_limit_current": None,
        }

        design, full, cycle = self._parse_battery_report_xml()
        if design is not None or full is not None:
            result["exists"] = True
            result["design_capacity"] = design
            result["full_capacity"] = full
            result["cycle_count"] = cycle
            if design and full and design > 0:
                result["health_percent"] = round(full / design * 100, 1)

        # 基础状态（电量 / 是否接通电源）来自 Win32_Battery
        rows = self._run_ps_csv(
            "Get-CimInstance Win32_Battery -ErrorAction SilentlyContinue | "
            "Select-Object Name,EstimatedChargeRemaining,BatteryStatus,DesignCapacity,FullChargeCapacity | "
            "ConvertTo-Csv -NoTypeInformation"
        )
        if rows:
            result["exists"] = True
            r = rows[0]
            result["current_charge"] = self._to_int(r.get("EstimatedChargeRemaining"))
            result["status"] = self._battery_status_text(self._to_int(r.get("BatteryStatus")))
            if result["design_capacity"] is None:
                result["design_capacity"] = self._to_int(r.get("DesignCapacity"))
            if result["full_capacity"] is None:
                result["full_capacity"] = self._to_int(r.get("FullChargeCapacity"))
            if (result["health_percent"] is None and result["design_capacity"]
                    and result["full_capacity"] and result["design_capacity"] > 0):
                result["health_percent"] = round(
                    result["full_capacity"] / result["design_capacity"] * 100, 1
                )

        if not result["exists"]:
            result["message"] = "未检测到电池（台式机或电池驱动未上报）。"
        else:
            notes = []
            if result["health_percent"] is None:
                notes.append("设计与满充容量不可用，无法计算健康度")
            if result["cycle_count"] is None:
                notes.append("循环次数不可用")
            result["message"] = "；".join(notes) if notes else "电池信息读取成功"
        return result

    def _parse_battery_report_xml(self):
        """生成并解析 powercfg 电池报告 XML，返回 (设计容量, 满充容量, 循环次数)。

        任一项缺失即为 None；生成/解析失败整体返回 (None, None, None)。
        """
        tmp_path = os.path.join(tempfile.gettempdir(), f"_hw_battery_{os.getpid()}.xml")
        try:
            rc, _out, _err = self._run_cmd(
                ["powercfg", "/batteryreport", "/output", tmp_path, "/xml"], timeout=25
            )
            if rc != 0 or not os.path.exists(tmp_path):
                return None, None, None
            try:
                with open(tmp_path, "rb") as f:
                    raw = f.read()
            except (OSError, PermissionError):
                return None, None, None
            text = self._decode_bytes(raw)
            design = self._xml_first_int(text, "DesignCapacity")
            full = self._xml_first_int(text, "FullChargeCapacity")
            cycle = self._xml_first_int(text, "CycleCount")
            return design, full, cycle
        finally:
            try:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
            except (OSError, PermissionError):
                pass

    @staticmethod
    def _xml_first_int(text, tag):
        """从 XML 文本中用正则取出首个 <tag>数字</tag>（忽略命名空间前缀），失败返回 None"""
        if not text:
            return None
        m = re.search(rf"<{tag}[^>]*>\s*(\d+)\s*</{tag}>", text)
        if not m:
            return None
        try:
            return int(m.group(1))
        except (ValueError, TypeError):
            return None

    def set_battery_charge_limit(self, percent):
        """设置充电阈值。

        充电阈值属于厂商私有驱动功能（联想/华硕/华为等各有接口），Windows 无统一接口，
        因此本工具不假装支持，直接返回明确的中文说明。
        """
        return False, (
            "充电阈值由电脑厂商私有驱动控制，本工具暂不支持通用修改，"
            "请在厂商管家软件（如联想电脑管家、MyASUS、华为电脑管家等）中设置。"
        )

    # ==================== 汇总为 ScanResult ====================

    def scan_hardware_issues(self):
        """把值得提醒用户的硬件问题汇总为 ScanResult 列表。

        注意：这些不是可删除的垃圾，主程序会单独展示，size 一律给 0，
        真实数据（容量/百分比等）写入 description，避免误导用户去"清理"。
        """
        self._emit(80, "正在汇总硬件健康提醒...")
        results = []

        # 1) 硬盘健康状态非"健康"（仅对已知的警告/异常产生条目，读不到不臆测）
        health_list = self.get_disk_health()
        for d in health_list:
            if d.get("health") in ("警告", "异常"):
                results.append(ScanResult(
                    category="硬件健康",
                    source="硬盘健康度异常",
                    path=d.get("model") or "未知磁盘",
                    size=0,
                    description=(
                        f"磁盘「{d.get('model')}」健康状态为「{d.get('health')}」，"
                        f"容量 {d.get('size')}，SMART：{d.get('smart_predict') or '无'}。"
                        f"建议备份重要数据并检测硬盘，必要时更换。"
                    ),
                ))

        # 2) SSD 未启用 TRIM
        ssd_list = self.check_disk_for_ssd()
        for s in ssd_list:
            if s.get("type") == "SSD" and s.get("trim_enabled") is False:
                results.append(ScanResult(
                    category="磁盘优化",
                    source="SSD TRIM 未启用",
                    path=s.get("drive"),
                    size=0,
                    description=(
                        f"盘符 {s.get('drive')} 为 SSD，但 TRIM 已被禁用（DisableDeleteNotify=1）。"
                        f"启用 TRIM 有助于维持 SSD 性能与寿命，可在管理员权限下执行 SSD 优化。"
                    ),
                ))

        # 3) 电池健康低于 80%
        battery = self.get_battery_info()
        hp = battery.get("health_percent")
        if hp is not None and hp < 80:
            results.append(ScanResult(
                category="硬件健康",
                source="电池健康度偏低",
                path="系统电池",
                size=0,
                description=(
                    f"电池健康度约 {hp}%（设计 {battery.get('design_capacity')} mWh / "
                    f"满充 {battery.get('full_capacity')} mWh）。健康度偏低会缩短续航，"
                    f"建议插电使用或考虑更换电池。"
                ),
            ))

        # 4) 系统盘剩余空间低于 10%（用标准库读取，稳定可靠）
        try:
            usage = shutil.disk_usage("C:\\")
            if usage.total > 0:
                ratio = usage.free / usage.total
                if ratio < 0.10:
                    results.append(ScanResult(
                        category="磁盘优化",
                        source="系统盘剩余空间不足",
                        path="C:\\",
                        size=0,
                        description=(
                            f"C 盘可用空间仅 {format_size(usage.free)} / 共 {format_size(usage.total)}"
                            f"（约 {ratio * 100:.1f}%），低于 10% 会影响系统运行。"
                            f"建议清理垃圾或转移大文件。"
                        ),
                    ))
        except (OSError, PermissionError):
            pass

        # 5) 温度偏高（>=85°C）
        for t in self.get_temperatures():
            val = t.get("value")
            if val is not None and val >= 85:
                results.append(ScanResult(
                    category="硬件健康",
                    source="CPU温度偏高",
                    path=t.get("name") or "CPU",
                    size=0,
                    description=(
                        f"{t.get('name')} 当前约 {val}°C（{t.get('status')}）。"
                        f"持续高温可能导致降频，建议清理散热灰尘、检查硅脂与风扇。"
                    ),
                ))

        self._emit(100, "硬件健康检查完成。")
        return results
