# -*- coding: utf-8 -*-
"""
一键体检评分引擎
把分散在各模块的检查项聚合成一个健康分与结论清单，对标"开机小助手 / 电脑体检"体验。

设计原则：
  - 只使用真实可读到的数据；读不到的维度按"未知"处理并给中性分，绝不编造
  - 每个检查项都给出具体数值、状态、扣分原因和可执行建议
  - 慢速检查（硬盘健康、温度、垃圾量）与快速检查分离，便于 UI 分层调用
"""

import os
import shutil
import tempfile
from datetime import datetime

from scanner import get_size, format_size


def _disk_free(letter="C"):
    """获取磁盘容量信息"""
    try:
        total, used, free = shutil.disk_usage(letter + ":\\")
        return total, used, free
    except Exception:
        return 0, 0, 0


def _memory_usage():
    """读取物理内存占用（KB -> 字节）"""
    try:
        import ctypes

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
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat)):
            return stat.ullTotalPhys, stat.ullTotalPhys - stat.ullAvailPhys, stat.dwMemoryLoad
    except Exception:
        pass
    return 0, 0, 0


class HealthEngine:
    """一键体检"""

    def __init__(self, progress_callback=None):
        self.progress_callback = progress_callback
        self._stop = False

    def _emit(self, percent, message):
        if self.progress_callback:
            self.progress_callback(percent, message)

    def stop(self):
        self._stop = True

    # ==================== 单项检查 ====================

    def _check_disk_space(self):
        total, used, free = _disk_free("C")
        if total <= 0:
            return {"name": "系统盘空间", "score": 15, "max_score": 30, "status": "未知",
                    "detail": "无法读取 C 盘容量信息", "suggestion": "请检查磁盘是否可访问"}
        free_percent = free / total * 100
        if free_percent >= 30:
            score, status = 30, "优秀"
            tip = "空间充足，保持良好"
        elif free_percent >= 20:
            score, status = 22, "良好"
            tip = "空间尚可，建议清理系统垃圾"
        elif free_percent >= 10:
            score, status = 14, "一般"
            tip = "建议立即清理垃圾并搬运大文件"
        elif free_percent >= 5:
            score, status = 6, "较差"
            tip = "空间告急！建议关闭休眠、转移虚拟内存、清理深度垃圾"
        else:
            score, status = 0, "危险"
            tip = "空间严重不足，可能导致系统卡顿甚至无法更新"
        return {"name": "系统盘空间", "score": score, "max_score": 30, "status": status,
                "detail": f"总容量 {format_size(total)}，已用 {used / total * 100:.1f}%，"
                          f"剩余 {format_size(free)}（{free_percent:.1f}%）",
                "suggestion": tip}

    def _check_memory(self):
        total, used, percent = _memory_usage()
        if total <= 0:
            return {"name": "内存占用", "score": 8, "max_score": 15, "status": "未知",
                    "detail": "无法读取内存状态", "suggestion": "可尝试使用内存清理功能释放占用"}
        if percent <= 50:
            score, status, tip = 15, "优秀", "内存充裕，无需处理"
        elif percent <= 65:
            score, status, tip = 12, "良好", "内存占用正常"
        elif percent <= 80:
            score, status, tip = 8, "一般", "建议关闭不常用的后台程序"
        elif percent <= 90:
            score, status, tip = 4, "较差", "内存吃紧，建议使用内存清理并检查启动项"
        else:
            score, status, tip = 0, "危险", "内存严重不足，系统会频繁卡顿，建议清理内存与后台程序"
        return {"name": "内存占用", "score": score, "max_score": 15, "status": status,
                "detail": f"总容量 {format_size(total)}，已用 {percent}%（{format_size(used)}）",
                "suggestion": tip}

    def _check_temp_junk(self):
        """快速估算常见垃圾体积（只测临时目录与回收站，保证速度）"""
        total = 0
        details = []
        paths = [
            (os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "Temp"), "系统临时"),
            (tempfile.gettempdir(), "用户临时"),
        ]
        for path, label in paths:
            if os.path.exists(path):
                size = get_size(path)
                if size > 0:
                    total += size
                    details.append(f"{label} {format_size(size)}")

        recycle = 0
        for drive in ("C", "D", "E", "F"):
            rp = os.path.join(drive + ":\\", "$Recycle.Bin")
            if os.path.exists(rp):
                recycle += get_size(rp)
        if recycle > 0:
            total += recycle
            details.append(f"回收站 {format_size(recycle)}")

        if total <= 0:
            return {"name": "可清理垃圾", "score": 15, "max_score": 15, "status": "优秀",
                    "detail": "未检测到明显垃圾", "suggestion": "保持良好，建议定期清理"}
        gb = total / (1024 ** 3)
        if gb < 1:
            score, status, tip = 15, "优秀", "垃圾量很少，无需急于清理"
        elif gb < 5:
            score, status, tip = 12, "良好", "建议执行一键扫描清理"
        elif gb < 15:
            score, status, tip = 8, "一般", "建议执行一键清理，可释放可观空间"
        elif gb < 30:
            score, status, tip = 4, "较差", "垃圾大量堆积，建议立即深度清理"
        else:
            score, status, tip = 0, "危险", "垃圾量异常巨大，建议立即深度清理并检查大文件"
        return {"name": "可清理垃圾", "score": score, "max_score": 15, "status": status,
                "detail": "、".join(details) + f"，合计 {format_size(total)}",
                "suggestion": tip}

    def _check_startup(self):
        """启动项数量（依赖 startup_manager，缺失时中性处理）"""
        try:
            from startup_manager import StartupEngine
            all_items = StartupEngine().scan_startup_items()
        except Exception as e:
            return {"name": "开机启动项", "score": 8, "max_score": 15, "status": "未知",
                    "detail": f"无法读取启动项：{e}",
                    "suggestion": "请检查启动项管理功能是否可用"}

        # 只统计"真正的开机自启项"。自动启动服务与计划任务数量庞大且属于系统正常组成部分，
        # 计入评分会严重失真，因此排除（它们各自有独立的服务页与计划任务页管理）。
        excluded_locations = ("自动启动服务", "计划任务")
        items = [i for i in all_items if i.get("location") not in excluded_locations]

        if not items:
            return {"name": "开机启动项", "score": 15, "max_score": 15, "status": "优秀",
                    "detail": "未检测到开机自启项", "suggestion": "启动项干净"}
        enabled = [i for i in items if i.get("enabled")]
        invalid = [i for i in items if i.get("risk") == "失效"]
        count = len(enabled)
        if count <= 5:
            score, status, tip = 15, "优秀", "启动项数量健康"
        elif count <= 10:
            score, status, tip = 12, "良好", "启动项略多，可适当精简"
        elif count <= 20:
            score, status, tip = 8, "一般", "建议禁用不常用的自启项以加快开机"
        elif count <= 35:
            score, status, tip = 4, "较差", "启动项过多，明显拖慢开机速度"
        else:
            score, status, tip = 0, "危险", "启动项极多，强烈建议批量禁用"
        extra = f"，其中 {len(invalid)} 项已失效" if invalid else ""
        return {"name": "开机启动项", "score": score, "max_score": 15, "status": status,
                "detail": f"共 {len(items)} 项开机自启配置，启用中 {count} 项{extra}",
                "suggestion": tip}

    def _check_disk_health(self):
        """硬盘健康（依赖 hardware_info）"""
        try:
            from hardware_info import HardwareEngine
            health = HardwareEngine().get_disk_health()
        except Exception as e:
            return {"name": "硬盘健康", "score": 6, "max_score": 10, "status": "未知",
                    "detail": f"无法读取硬盘健康信息：{e}",
                    "suggestion": "建议安装厂商工具查看 SMART 信息"}
        if not health:
            return {"name": "硬盘健康", "score": 6, "max_score": 10, "status": "未知",
                    "detail": "未获取到硬盘健康数据（可能需要管理员权限）",
                    "suggestion": "建议以管理员身份重新体检"}
        bad = [h for h in health if str(h.get("health", "")).find("健康") < 0
               and str(h.get("health", "")) not in ("Unknown", "未知", "")]
        if bad:
            return {"name": "硬盘健康", "score": 2, "max_score": 10, "status": "异常",
                    "detail": "；".join(f"{h.get('model', '未知')}: {h.get('health', '')}"
                                       for h in bad),
                    "suggestion": "请立即备份重要数据，硬盘可能存在故障风险"}
        return {"name": "硬盘健康", "score": 10, "max_score": 10, "status": "优秀",
                "detail": f"共 {len(health)} 块磁盘，状态正常",
                "suggestion": "保持定期备份习惯"}

    def _check_temperature(self):
        """温度（依赖 hardware_info）"""
        try:
            from hardware_info import HardwareEngine
            temps = HardwareEngine().get_temperatures()
        except Exception as e:
            return {"name": "运行温度", "score": 3, "max_score": 5, "status": "未知",
                    "detail": f"无法读取温度：{e}", "suggestion": "可使用厂商工具查看温度"}
        values = [t for t in temps if t.get("value") is not None]
        if not values:
            return {"name": "运行温度", "score": 3, "max_score": 5, "status": "未知",
                    "detail": "该机型或当前权限下无法读取温度传感器",
                    "suggestion": "如需温度监控，请以管理员身份运行或使用厂商工具"}
        hottest = max(values, key=lambda t: t["value"])
        v = hottest["value"]
        if v < 70:
            score, status, tip = 5, "优秀", "温度正常"
        elif v < 80:
            score, status, tip = 4, "良好", "温度略高，注意散热"
        elif v < 90:
            score, status, tip = 2, "一般", "温度偏高，建议清理机箱/风扇灰尘"
        else:
            score, status, tip = 0, "危险", "温度过高，可能导致降频或死机，请立即检查散热"
        return {"name": "运行温度", "score": score, "max_score": 5, "status": status,
                "detail": f"最高读数 {v:.0f}°C（{hottest.get('name', '传感器')}）",
                "suggestion": tip}

    def _check_system_maintenance(self):
        """系统维护项：休眠文件、虚拟内存位置、还原点占用"""
        score = 10
        notes = []
        # 休眠文件
        hiber = "C:\\hiberfil.sys"
        if os.path.exists(hiber):
            size = get_size(hiber)
            if size > 0:
                score -= 4
                notes.append(f"休眠文件占用 {format_size(size)}（关闭休眠可释放）")
        else:
            notes.append("休眠文件已关闭")
        # 虚拟内存
        pagefile = "C:\\pagefile.sys"
        if os.path.exists(pagefile):
            size = get_size(pagefile)
            if size > 0:
                score -= 3
                notes.append(f"虚拟内存在 C 盘，占用 {format_size(size)}（可转移到其他盘）")
        else:
            notes.append("虚拟内存不在 C 盘")
        # Windows.old
        if os.path.exists("C:\\Windows.old"):
            size = get_size("C:\\Windows.old")
            if size > 0:
                score -= 3
                notes.append(f"存在 Windows.old 旧系统备份 {format_size(size)}（可清理）")

        score = max(0, score)
        status = "优秀" if score >= 8 else ("良好" if score >= 5 else ("一般" if score >= 3 else "较差"))
        return {"name": "系统维护项", "score": score, "max_score": 10, "status": status,
                "detail": "；".join(notes) if notes else "未发现可优化项",
                "suggestion": "建议关闭休眠、转移虚拟内存、清理旧系统备份以释放 C 盘空间"
                              if score < 8 else "系统维护状态良好"}

    # ==================== 汇总 ====================

    def run_checkup(self, include_slow=True):
        """
        执行完整体检。
        include_slow=False 时跳过硬盘健康与温度检查（这两项需要调用 PowerShell，较慢）。

        返回 {"score":int,"grade":str,"items":[dict],"checked_at":str,"total_max":int}
        """
        self._stop = False
        items = []

        self._emit(5, "检查系统盘空间...")
        items.append(self._check_disk_space())
        if self._stop:
            return self._build_result(items)

        self._emit(20, "检查内存占用...")
        items.append(self._check_memory())
        if self._stop:
            return self._build_result(items)

        self._emit(35, "估算可清理垃圾...")
        items.append(self._check_temp_junk())
        if self._stop:
            return self._build_result(items)

        self._emit(50, "检查系统维护项...")
        items.append(self._check_system_maintenance())
        if self._stop:
            return self._build_result(items)

        self._emit(65, "统计开机启动项...")
        items.append(self._check_startup())
        if self._stop:
            return self._build_result(items)

        if include_slow:
            self._emit(80, "读取硬盘健康状态...")
            items.append(self._check_disk_health())
            if self._stop:
                return self._build_result(items)

            self._emit(92, "读取温度传感器...")
            items.append(self._check_temperature())

        self._emit(100, "体检完成")
        return self._build_result(items)

    def _build_result(self, items):
        total = sum(i["score"] for i in items)
        max_total = sum(i["max_score"] for i in items)
        # 归一化到百分制，保证不同检查项数量下分数可比
        score = int(round(total / max_total * 100)) if max_total else 0
        if score >= 90:
            grade = "优秀"
        elif score >= 75:
            grade = "良好"
        elif score >= 60:
            grade = "一般"
        elif score >= 40:
            grade = "较差"
        else:
            grade = "危险"

        # 从低分项生成待处理建议
        problems = sorted(items, key=lambda x: x["score"] / max(x["max_score"], 1))
        actions = [{"name": p["name"], "suggestion": p["suggestion"],
                    "score_ratio": round(p["score"] / max(p["max_score"], 1), 2)}
                   for p in problems if p["score"] / max(p["max_score"], 1) < 0.7]

        return {
            "score": score,
            "grade": grade,
            "items": items,
            "actions": actions,
            "total_score": total,
            "total_max": max_total,
            "checked_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
