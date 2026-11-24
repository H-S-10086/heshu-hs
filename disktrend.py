# -*- coding: utf-8 -*-
"""
磁盘占用历史趋势引擎
在每次启动与每次清理后记录各盘的占用快照，积累成历史，回答
"我的空间是被谁一点点吃掉的 / 这次清理到底释放了多少 / 按现在的速度还能撑多久"。

说明：只记录真实的磁盘容量数据（shutil.disk_usage），绝不编造。
"""

import os
import json
import shutil
from datetime import datetime, timedelta

from scanner import format_size


def _default_history_dir():
    base = os.path.join(os.environ.get("USERPROFILE", os.environ.get("TEMP", ".")),
                        "CleanMasterBackup")
    try:
        os.makedirs(base, exist_ok=True)
    except Exception:
        base = os.environ.get("TEMP", ".")
    return base


class DiskTrendEngine:
    """磁盘占用历史趋势"""

    # 快照最小间隔：默认 6 小时。避免一天内频繁启动产生大量重复点。
    DEFAULT_MIN_INTERVAL_HOURS = 6

    def __init__(self, progress_callback=None, history_dir=None):
        self.progress_callback = progress_callback
        self._dir = history_dir or _default_history_dir()

    def _emit(self, percent, message):
        if self.progress_callback:
            self.progress_callback(percent, message)

    # ---------- 存储 ----------

    def _history_file(self):
        return os.path.join(self._dir, "disk_history.json")

    def _load_all(self):
        """读取全部历史，返回 {盘符: [快照...]}"""
        path = self._history_file()
        if not os.path.exists(path):
            return {}
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data
        except Exception:
            pass
        return {}

    def _save_all(self, data):
        """写回全部历史"""
        path = self._history_file()
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
            return True
        except Exception:
            return False

    # ---------- 记录 ----------

    def record_snapshot(self, drive="C", min_interval_hours=None, force=False,
                        reason=""):
        """
        记录一次快照。

        参数
          drive: 盘符（如 "C" 或 "C:"）
          min_interval_hours: 与上一条快照的最小间隔，小于该间隔则跳过（去重）。
                              None 表示用默认值。
          force: 强制记录（例如清理完成后调用，即使间隔未到也记录，以反映清理效果）
          reason: 本次记录的原因（启动 / 清理后 / 手动），用于界面提示

        返回 {"recorded": bool, "snapshot": dict 或 None, "message": str}
        """
        if min_interval_hours is None:
            min_interval_hours = self.DEFAULT_MIN_INTERVAL_HOURS

        letter = drive.rstrip(":\\").upper()
        root = letter + ":\\"
        try:
            total, used, free = shutil.disk_usage(root)
        except Exception as e:
            return {"recorded": False, "snapshot": None,
                    "message": f"无法读取 {letter}: 盘容量信息：{e}"}

        now = datetime.now()
        data = self._load_all()
        points = data.get(letter, [])

        if points and not force:
            try:
                last_time = datetime.fromisoformat(points[-1]["t"])
            except Exception:
                last_time = None
            if last_time is not None:
                if (now - last_time) < timedelta(hours=min_interval_hours):
                    return {"recorded": False, "snapshot": None,
                            "message": (f"距上次记录不足 {min_interval_hours} 小时，"
                                        f"本次跳过（上次：{last_time.strftime('%m-%d %H:%M')}）")}

        snapshot = {"t": now.isoformat(timespec="seconds"),
                    "total": int(total), "used": int(used), "free": int(free),
                    "reason": reason}
        points.append(snapshot)
        # 防止历史无限膨胀：只保留最近 500 条
        if len(points) > 500:
            points = points[-500:]
        data[letter] = points

        if not self._save_all(data):
            return {"recorded": False, "snapshot": None, "message": "写入历史文件失败"}

        return {"recorded": True, "snapshot": snapshot,
                "message": f"已记录 {letter}: 盘快照（{reason or '手动'}）："
                           f"剩余 {format_size(free)}"}

    # ---------- 读取与分析 ----------

    def get_history(self, drive="C"):
        """
        返回某盘的全部快照（按时间升序）。
        每项 {"t": ISO时间, "total": int, "used": int, "free": int, "reason": str, "dt": datetime}
        """
        letter = drive.rstrip(":\\").upper()
        points = self._load_all().get(letter, [])
        out = []
        for p in points:
            try:
                dt = datetime.fromisoformat(p["t"])
            except Exception:
                continue
            out.append({
                "t": p["t"],
                "total": p.get("total", 0),
                "used": p.get("used", 0),
                "free": p.get("free", 0),
                "reason": p.get("reason", ""),
                "dt": dt,
            })
        out.sort(key=lambda x: x["dt"])
        return out

    def analyze(self, drive="C"):
        """
        对某盘做趋势分析。

        返回 {"capacity": int, "points": int, "first": dict|None, "last": dict|None,
              "span_days": float, "free_change_bytes": int, "rate_bytes_per_day": float,
              "days_until_full": float|None, "direction": "正在消耗"/"正在释放"/"基本平稳",
              "biggest_clean": dict|None, "message": str}
        """
        history = self.get_history(drive)
        if not history:
            return {"capacity": 0, "points": 0, "first": None, "last": None,
                    "span_days": 0.0, "free_change_bytes": 0, "rate_bytes_per_day": 0.0,
                    "days_until_full": None, "direction": "基本平稳",
                    "biggest_clean": None,
                    "message": "还没有历史数据。本工具会在每次启动和每次清理后自动记录快照，"
                               "积累几天后这里就能看到趋势。"}

        capacity = history[-1]["total"]
        first, last = history[0], history[-1]
        span = (last["dt"] - first["dt"]).total_seconds()
        span_days = max(span / 86400.0, 0)
        free_change = last["free"] - first["free"]

        if span_days > 0.01:
            rate = free_change / span_days  # 每天剩余空间变化（正=在释放，负=在消耗）
        else:
            rate = 0.0

        # 找出"单次释放最多"的一段（通常是某次清理后）
        biggest = None
        biggest_gain = 0
        for i in range(1, len(history)):
            gain = history[i]["free"] - history[i - 1]["free"]
            if gain > biggest_gain:
                biggest_gain = gain
                biggest = {"time": history[i]["t"],
                           "gain": gain,
                           "reason": history[i].get("reason", "")}

        # 预计还能撑多久（只在"正在消耗"时有意义）
        days_until_full = None
        direction = "基本平稳"
        # 变化幅度小于总容量的 0.1% 视为平稳
        if capacity > 0 and abs(free_change) < capacity * 0.001:
            direction = "基本平稳"
        elif rate < 0:
            direction = "正在消耗"
            days_until_full = last["free"] / (-rate) if -rate > 0 else None
        else:
            direction = "正在释放"

        return {
            "capacity": capacity,
            "points": len(history),
            "first": first,
            "last": last,
            "span_days": round(span_days, 2),
            "free_change_bytes": free_change,
            "rate_bytes_per_day": rate,
            "days_until_full": days_until_full,
            "direction": direction,
            "biggest_clean": biggest if biggest_gain > 0 else None,
            "message": "",
        }

    def clear_history(self, drive=None):
        """清空历史（drive=None 时清空全部）"""
        data = self._load_all()
        if drive is None:
            data = {}
        else:
            data.pop(drive.rstrip(":\\").upper(), None)
        ok = self._save_all(data)
        return ok, "历史已清空" if ok else "清空失败"
