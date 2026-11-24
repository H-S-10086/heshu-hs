# -*- coding: utf-8 -*-
"""
文件粉碎 / 空闲空间擦除引擎
------------------------------------------------------------
把主程序原有的"隐私清理"（仅删除）升级为"不可恢复级隐私链路"：

  1) 文件粉碎：按业界通行的多遍覆写算法把文件内容物理覆盖后删除，
     使普通数据恢复软件无法还原。
  2) 空闲空间擦除：把磁盘上"已删除文件残留的数据块"用覆写数据盖掉，
     从而防止被恢复。

设计原则（务必遵守）：
  - 只使用 Python 3.12 标准库 + 项目内 scanner 模块，绝不引入第三方依赖
    （项目使用 PyInstaller 单文件打包）。
  - 本模块只包含引擎逻辑，不含任何 tkinter / UI 代码。
  - 粉碎是不可逆操作：所有对外方法都提供 dry_run 预览或明确的确认语义，
    绝不允许误删"目录本身以外"的内容。
  - 所有常见异常（PermissionError / OSError / FileNotFoundError）均被捕获，
    绝不让异常向上冒泡导致主程序崩溃。
  - 模块被 import 时不产生任何副作用（仅定义常量与类）。
"""

import os
import stat
import time
import ctypes
import shutil
import secrets
import datetime

from scanner import ScanResult, get_size, format_size


# ============================================================
# 覆写算法常量
# ============================================================
# 说明：以下遍数均为业界通行标准的真实遍数，未做缩水命名。
# 其中 gutmann 采用"简化模式"（见该条 desc 与下方 _algorithm_passes 注释），
# 遍数足 35 遍，但固定模式并非 Gutmann 论文原始的 35 种不同模式，已如实标注。
ALGORITHMS = {
    "quick":    {"name": "快速覆写(1遍随机)",       "passes": 1,  "desc": "写 1 遍随机数据，速度最快，可防普通恢复软件"},
    "dod":      {"name": "DoD 5220.22-M(3遍)",       "passes": 3,  "desc": "美国国防部标准，随机-补码-随机"},
    "dod_ece":  {"name": "DoD 5220.22-M ECE(7遍)",   "passes": 7,  "desc": "DoD 增强版，7 遍覆写（随机/补码/随机 一轮半再加最后一遍随机）"},
    "gutmann":  {"name": "Gutmann(35遍)",            "passes": 35, "desc": "35 遍覆写（简化模式：仅用 0x00/0xFF/0x55/0xAA 等固定模式轮换，并非 Gutmann 论文原始的 35 种不同模式），耗时很长"},
    "schneier": {"name": "Schneier(7遍)",            "passes": 7,  "desc": "Bruce Schneier 提出的 7 遍方案：0x00、0xFF 后接 5 遍随机"},
    "zero":     {"name": "全零覆写(1遍)",            "passes": 1,  "desc": "写 1 遍 0x00，速度最快但防护最弱"},
}

# 覆写块大小（1MB）：按块写入，避免把大文件一次性读进内存
BLOCK_SIZE = 1024 * 1024

# 空闲空间擦除时保留的磁盘余量（200MB）
FREE_SPACE_RESERVE = 200 * 1024 * 1024

# 空闲空间擦除临时文件名（有辨识度，便于识别与清理）
FREE_SPACE_TMP_NAME = "__shred_free_space_tmp__.dat"

# 按位取反查找表：用 bytes.translate 以 C 语言速度完成 b ^ 0xFF
_INVERT_TABLE = bytes(255 - i for i in range(256))


def _invert(data):
    """对字节串做按位取反（等价于 bytes(b ^ 0xFF)）"""
    return data.translate(_INVERT_TABLE)


class ShredEngine:
    """文件粉碎 / 空闲空间擦除引擎"""

    def __init__(self, progress_callback=None):
        self.progress_callback = progress_callback
        self._stop = False          # 中断标志
        self._logs = []             # 本次操作的日志（每次调用前重置）

    # ==================== 进度与中断 ====================

    def _emit(self, percent, message):
        """报告进度（0~100）。回调异常被吞掉，避免影响引擎运行。"""
        if not self.progress_callback:
            return
        try:
            p = int(percent)
            if p < 0:
                p = 0
            elif p > 100:
                p = 100
            self.progress_callback(p, message)
        except Exception:
            # UI 回调里的异常不应导致粉碎流程崩溃
            pass

    def stop(self):
        """请求停止：置位停止标志，覆写循环会检查该标志并尽快退出。"""
        self._stop = True

    def reset_stop(self):
        """清除停止标志，用于开始新一轮操作。"""
        self._stop = False

    def _log(self, message):
        """记录一条带时间戳的日志。"""
        try:
            ts = datetime.datetime.now().strftime("%H:%M:%S")
        except Exception:
            ts = "--:--:--"
        self._logs.append(f"[{ts}] {message}")

    # ==================== 算法信息 ====================

    def get_algorithms(self):
        """把 ALGORITHMS 转成列表，供 UI 下拉框使用。

        返回：[{"id", "name", "passes", "desc"}, ...]
        """
        result = []
        for alg_id, meta in ALGORITHMS.items():
            result.append({
                "id": alg_id,
                "name": meta["name"],
                "passes": meta["passes"],
                "desc": meta["desc"],
            })
        return result

    def _algorithm_passes(self, algorithm):
        """返回指定算法的"每一遍覆写模式"列表。

        模式类型（kind）含义：
            random  随机数据（secrets）
            invert  随机数据的按位取反（实现"补码"遍）
            zero    0x00
            one     0xFF
            p55     0x55
            paa     0xAA
        """
        alg = algorithm.lower() if isinstance(algorithm, str) else "dod"
        if alg not in ALGORITHMS:
            alg = "dod"

        if alg == "quick":
            return ["random"]
        if alg == "zero":
            return ["zero"]
        if alg == "dod":
            # DoD 5220.22-M：随机 → 补码 → 随机
            return ["random", "invert", "random"]
        if alg == "dod_ece":
            # DoD ECE：三遍（随机/补码/随机）再重复一轮，最后再加 1 遍随机，共 7 遍
            return ["random", "invert", "random",
                    "random", "invert", "random",
                    "random"]
        if alg == "schneier":
            # Schneier 方案：先 0x00、0xFF，再 5 遍随机，共 7 遍
            return ["zero", "one"] + ["random"] * 5
        if alg == "gutmann":
            # 【简化实现说明】Gutmann 论文原始方案为 35 遍各不相同的固定/随机模式；
            # 这里简化为：前 4 遍随机、中间 27 遍用 0x00/0xFF/0x55/0xAA/随机 轮换、
            # 最后 4 遍随机，总遍数确为 35 遍。固定模式种类少于原始论文，
            # 因此在 desc 与注释中已如实说明为"简化模式"，不夸大防护能力。
            fixed_cycle = ["zero", "one", "p55", "paa", "random"]
            passes = ["random"] * 4
            for i in range(27):
                passes.append(fixed_cycle[i % len(fixed_cycle)])
            passes += ["random"] * 4
            return passes  # 4 + 27 + 4 = 35

        return ["random", "invert", "random"]

    # ==================== 覆写实现 ====================

    def _make_block(self, kind, size):
        """按模式生成一块待写入的数据（长度 = size）。"""
        if kind == "random":
            return secrets.token_bytes(size)
        if kind == "zero":
            return b"\x00" * size
        if kind == "one":
            return b"\xff" * size
        if kind == "p55":
            return b"\x55" * size
        if kind == "paa":
            return b"\xaa" * size
        if kind == "invert":
            # 补码模式：先生成随机数据，再整体按位取反
            return _invert(secrets.token_bytes(size))
        # 兜底按随机处理
        return secrets.token_bytes(size)

    def _overwrite_handle(self, fh, size, algorithm, progress_base=0.0,
                          progress_span=100.0, label=""):
        """对已打开的文件句柄按算法逐遍覆写。

        每遍都：seek(0) → 逐块写入该遍 pattern → flush → os.fsync()。
        返回 True 表示全部完成；false 表示被中断或已提前失败。
        """
        if size <= 0:
            return True  # 空文件无需覆写

        passes = self._algorithm_passes(algorithm)
        n_passes = len(passes)
        total = size * n_passes   # 本次覆写需要写入的总字节数
        done = 0

        for idx, kind in enumerate(passes, start=1):
            if self._stop:
                return False
            fh.seek(0)
            remaining = size
            while remaining > 0:
                if self._stop:
                    return False
                n = min(BLOCK_SIZE, remaining)
                fh.write(self._make_block(kind, n))
                remaining -= n
                done += n
                pct = progress_base + (done / total) * progress_span
                self._emit(int(pct),
                           f"{label}第 {idx}/{n_passes} 遍覆写中（{kind}）...")
            # 保证本遍数据真正落盘
            fh.flush()
            os.fsync(fh.fileno())

        return True

    def _overwrite_file(self, path, algorithm, progress_base=0, progress_span=100):
        """核心：覆写单个文件。

        返回 True 表示该文件已按算法完整覆写；False 表示失败或被中断。
        所有 IO 异常均在内部捕获，不向外冒泡。
        """
        try:
            size = os.path.getsize(path)
        except FileNotFoundError:
            self._log(f"文件不存在，跳过：{path}")
            return False
        except (OSError, PermissionError) as e:
            self._log(f"无法读取文件大小：{path}（{e}）")
            return False

        try:
            with open(path, "r+b") as fh:
                return self._overwrite_handle(
                    fh, size, algorithm,
                    progress_base=progress_base,
                    progress_span=progress_span,
                    label=os.path.basename(path) + " ",
                )
        except FileNotFoundError:
            self._log(f"文件不存在，跳过：{path}")
            return False
        except (PermissionError, OSError) as e:
            self._log(f"覆写失败：{path}（{e}）")
            return False
        except Exception as e:
            # 兜底：任何意外异常都不允许冒泡
            self._log(f"覆写异常：{path}（{e}）")
            return False

    def _truncate_and_delete(self, path):
        """覆写完成后：截断为 0 字节并删除文件。

        删除采取逐级兜底策略：
          1) 直接 os.remove
          2) 失败（只读/占用）→ 去只读属性再删
          3) 仍失败 → 重命名为随机名后再删
          4) 仍失败 → MoveFileExW + MOVEFILE_DELAY_UNTIL_REBOOT(0x4)，标记重启后删除
        返回 True 表示"文件已不存在"或"已成功标记重启后删除"。
        """
        # ---- 1) 截断为 0 ----
        try:
            with open(path, "r+b") as fh:
                fh.truncate(0)
                fh.flush()
                os.fsync(fh.fileno())
        except FileNotFoundError:
            return True  # 已经不在了，视为成功
        except (OSError, PermissionError):
            pass  # 截断失败不阻断后续删除尝试

        # ---- 2) 直接删除 ----
        try:
            os.remove(path)
            return True
        except FileNotFoundError:
            return True
        except (OSError, PermissionError):
            pass

        # ---- 3) 去只读属性后删除 ----
        try:
            os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
            os.remove(path)
            return True
        except (OSError, PermissionError):
            pass

        # ---- 4) 随机重命名后再删除（绕过部分占用/命名锁） ----
        try:
            directory = os.path.dirname(path)
            new_name = os.path.join(directory,
                                    "__shred_" + secrets.token_hex(8) + ".tmp")
            os.rename(path, new_name)
            os.remove(new_name)
            return True
        except (OSError, PermissionError):
            pass

        # ---- 5) 兜底：标记为重启后删除 ----
        try:
            kernel32 = ctypes.windll.kernel32
            # MoveFileExW(src, dst, flags)；dst 传 None 表示只删除源。
            # 0x4 = MOVEFILE_DELAY_UNTIL_REBOOT，由系统在下次重启时删除。
            res = kernel32.MoveFileExW(ctypes.c_wchar_p(path), None, 0x4)
            if res:
                self._log(f"无法立即删除，已标记为重启后删除：{path}")
                return True
        except Exception:
            pass

        self._log(f"删除失败：{path}")
        return False

    # ==================== 对外：粉碎文件列表 ====================

    def shred_files(self, paths, algorithm="dod", dry_run=False):
        """粉碎一批文件（或目录内的全部文件）。

        参数：
            paths      : 文件/目录路径列表；目录会被递归处理，但保留目录本身
            algorithm  : 覆写算法 id（见 ALGORITHMS），默认 dod
            dry_run    : True 时只做预览统计，绝不写入/删除任何数据

        返回 dict：
            {"success_count", "fail_count", "total_bytes", "failures",
             "logs", "stopped", "dry_run"}
        """
        self._logs = []
        result = {
            "success_count": 0,
            "fail_count": 0,
            "total_bytes": 0,
            "failures": [],
            "logs": self._logs,
            "stopped": False,
            "dry_run": bool(dry_run),
        }

        # ---- 收集待处理文件（目录只取其内部文件，绝不删除目录本身） ----
        targets = []
        for p in (paths or []):
            try:
                if os.path.isdir(p):
                    for dirpath, _dirnames, filenames in os.walk(
                            p, onerror=lambda e: None):
                        for f in filenames:
                            targets.append(os.path.join(dirpath, f))
                elif os.path.isfile(p):
                    targets.append(p)
                else:
                    result["failures"].append(
                        {"path": p, "reason": "路径不存在或不是文件/目录"})
                    result["fail_count"] += 1
            except (OSError, PermissionError) as e:
                result["failures"].append({"path": p, "reason": f"扫描异常：{e}"})
                result["fail_count"] += 1

        total_files = len(targets)
        if total_files == 0:
            self._log("没有可粉碎的文件。")
            self._emit(100, "没有可粉碎的文件")
            return result

        # ---- 预览模式：只统计，不写盘 ----
        if dry_run:
            for t in targets:
                try:
                    result["total_bytes"] += os.path.getsize(t)
                    result["success_count"] += 1
                except (OSError, PermissionError):
                    result["fail_count"] += 1
                    result["failures"].append(
                        {"path": t, "reason": "无法读取文件大小"})
            msg = (f"预览：将用「{ALGORITHMS.get(algorithm, {}).get('name', algorithm)}」"
                   f"粉碎 {total_files} 个文件，约 {format_size(result['total_bytes'])}"
                   f"（未写入任何数据）")
            self._log(msg)
            self._emit(100, msg)
            return result

        # ---- 实际粉碎 ----
        alg_name = ALGORITHMS.get(algorithm, {}).get("name", algorithm)
        self._log(f"开始粉碎：算法={alg_name}，文件数={total_files}")
        span = 100.0 / total_files

        for i, t in enumerate(targets):
            if self._stop:
                result["stopped"] = True
                self._log("已收到停止指令，提前结束。")
                break

            try:
                size = os.path.getsize(t)
            except (OSError, PermissionError):
                size = 0

            ok = self._overwrite_file(t, algorithm,
                                      progress_base=i * span, progress_span=span)
            if ok:
                removed = self._truncate_and_delete(t)
                if removed and not os.path.exists(t):
                    result["success_count"] += 1
                    result["total_bytes"] += size
                    self._log(f"已粉碎：{t}（{format_size(size)}）")
                else:
                    result["fail_count"] += 1
                    result["failures"].append(
                        {"path": t, "reason": "覆写完成但删除失败"})
                    self._log(f"覆写完成但删除失败：{t}")
            else:
                result["fail_count"] += 1
                reason = "被中断" if self._stop else "覆写失败"
                result["failures"].append({"path": t, "reason": reason})
                self._log(f"{reason}：{t}")

        # 覆写途中被中断时，循环可能来不及走回顶部判断，这里统一回填停止标志
        if self._stop:
            result["stopped"] = True

        self._emit(100,
                   f"粉碎结束：成功 {result['success_count']}，失败 {result['fail_count']}")
        return result

    def shred_path(self, path, algorithm="dod"):
        """粉碎单个文件或单个目录（目录内的文件全部粉碎，保留目录结构）。

        返回 (是否全部成功, 信息字符串)。
        """
        try:
            if not os.path.exists(path):
                return False, f"路径不存在：{path}"
            r = self.shred_files([path], algorithm=algorithm, dry_run=False)
            ok = (r["fail_count"] == 0 and r["success_count"] > 0
                  and not r["stopped"])
            info = (f"成功 {r['success_count']} 个，失败 {r['fail_count']} 个，"
                    f"共 {format_size(r['total_bytes'])}")
            if r["stopped"]:
                info += "（已中断）"
            return ok, info
        except Exception as e:
            return False, f"粉碎异常：{e}"

    # ==================== 空闲空间擦除 ====================

    def write_free_space(self, drive="C:", algorithm="zero", dry_run=False):
        """空闲空间擦除：把磁盘上已删除文件残留的数据块物理覆盖，防恢复。

        【重要警告】
          - 这是高耗时操作：需先写满剩余空间再覆写，机械硬盘可能需要
            几十分钟到几小时，且期间磁盘可用空间会被占满。
          - 不要在 SSD 上频繁使用：大量写入会消耗固态硬盘寿命（写入放大）。
          - 该操作同样不可逆，但只影响"已删除文件"的残留数据，不影响现存文件。

        流程：
          1) shutil.disk_usage(drive) 取剩余空间，目标写入量 = 剩余空间 - 200MB
          2) 在目标盘创建临时文件，分块写入直到接近写满
          3) fsync 落盘后，重新打开该临时文件按 algorithm 逐遍覆写
          4) 删除临时文件

        返回 dict：
            {"success", "written_bytes", "free_before", "free_after",
             "seconds", "logs", "message", "stopped"}
        """
        self._logs = []
        result = {
            "success": False,
            "written_bytes": 0,
            "free_before": 0,
            "free_after": 0,
            "seconds": 0.0,
            "logs": self._logs,
            "message": "",
            "stopped": False,
        }

        # 规范化盘符，确保带反斜杠
        drive = (drive or "C:").strip()
        if drive and not drive.endswith(("\\", "/")):
            drive += "\\"

        try:
            usage_before = shutil.disk_usage(drive)
            free_before = usage_before.free
        except (OSError, PermissionError) as e:
            result["message"] = f"无法读取磁盘剩余空间：{e}"
            self._log(result["message"])
            return result

        result["free_before"] = free_before
        target = free_before - FREE_SPACE_RESERVE  # 保留 200MB 余量
        if target <= 0:
            result["message"] = (f"剩余空间不足：{format_size(free_before)}，"
                                 f"保留 {format_size(FREE_SPACE_RESERVE)} 后无可写入空间。")
            self._log(result["message"])
            return result

        # ---- 预览模式：只计算，不写盘 ----
        if dry_run:
            result["success"] = True
            result["written_bytes"] = target
            result["message"] = (f"预览：将向 {drive} 写入约 {format_size(target)} "
                                 f"（剩余空间 {format_size(free_before)} - 保留 200MB），"
                                 f"未写入任何数据。")
            self._log(result["message"])
            self._emit(100, result["message"])
            return result

        tmp_path = os.path.join(drive, FREE_SPACE_TMP_NAME)
        start = time.time()
        written = 0
        fill_block = 4 * 1024 * 1024  # 填充阶段用 4MB 块

        try:
            # ---- 阶段一：写满空闲空间（进度 0~60） ----
            try:
                fh = open(tmp_path, "wb")
            except (OSError, PermissionError) as e:
                result["message"] = f"无法创建临时文件：{tmp_path}（{e}）"
                self._log(result["message"])
                return result

            try:
                while written < target:
                    if self._stop:
                        result["stopped"] = True
                        break
                    n = min(fill_block, target - written)
                    fh.write(secrets.token_bytes(n))  # 随机填充
                    written += n
                    self._emit(int(written / target * 60),
                               f"写入空闲空间 {format_size(written)} / "
                               f"{format_size(target)} ...")
                fh.flush()
                os.fsync(fh.fileno())  # 确保写满的数据真正落盘
            except (OSError, PermissionError) as e:
                self._log(f"写入临时文件出错：{e}")
            finally:
                try:
                    fh.close()
                except Exception:
                    pass

            result["written_bytes"] = written

            # ---- 阶段二：按算法覆写临时文件（进度 60~98） ----
            if written > 0 and not result["stopped"]:
                try:
                    with open(tmp_path, "r+b") as fh2:
                        self._overwrite_handle(
                            fh2, written, algorithm,
                            progress_base=60.0, progress_span=38.0,
                            label="空闲空间",
                        )
                except (OSError, PermissionError) as e:
                    self._log(f"覆写空闲空间出错：{e}")
        finally:
            # ---- 阶段三：无论如何都要尽力删除临时文件 ----
            try:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
                    self._log(f"已删除临时文件：{tmp_path}")
            except (OSError, PermissionError):
                self._log(f"临时文件删除失败，请手动清理：{tmp_path}")

        result["seconds"] = round(time.time() - start, 2)
        try:
            result["free_after"] = shutil.disk_usage(drive).free
        except (OSError, PermissionError):
            result["free_after"] = 0

        if result["stopped"]:
            result["success"] = False
            result["message"] = (f"已被中断：已写入 {format_size(written)}，"
                                 f"临时文件已清理。")
        else:
            result["success"] = True
            result["message"] = (f"空闲空间擦除完成：写入 {format_size(written)}，"
                                 f"耗时 {result['seconds']} 秒。")
        self._log(result["message"])
        self._emit(100, result["message"])
        return result

    # ==================== 磁盘擦除（危险操作） ====================

    def wipe_disk(self, drive):
        """整盘擦除。

        【明确说明】为避免数据风险，本工具**不提供**一键整盘擦除。
        这里刻意不做任何真实的裸盘写入，只返回明确的提示。
        """
        return (False,
                "整盘擦除需要重新初始化磁盘，本工具建议您使用 Windows 官方工具 "
                "diskpart clean 命令，或在磁盘管理中进行格式化。"
                "为避免数据风险，本工具不提供一键整盘擦除。")

    # ==================== 隐私文件清单（衔接已有隐私清理） ====================

    def scan_privacy_shred_targets(self):
        """扫描"建议粉碎而非普通删除"的敏感文件，转成 ScanResult 列表。

        category 统一为 "文件粉碎"。仅为真实存在的内容产出条目，
        若一个都没有则返回空列表。该方法只读扫描，不做任何删除。
        """
        results = []

        def _add(source, path, size, description):
            if size <= 0:
                return
            results.append(ScanResult(
                category="文件粉碎",
                source=source,
                path=path,
                size=size,
                description=description,
            ))

        # ---- 1) 最近文档目录里的 .lnk 文件 ----
        try:
            recent = os.path.join(os.environ.get("APPDATA", ""),
                                  "Microsoft", "Windows", "Recent")
            if os.path.isdir(recent):
                cnt = 0
                for f in os.listdir(recent):
                    if not f.lower().endswith(".lnk"):
                        continue
                    fp = os.path.join(recent, f)
                    s = get_size(fp)
                    _add("最近文档记录(.lnk)",
                         fp, s,
                         "「最近文档」快捷方式记录了您最近打开过的文件路径，"
                         "属于隐私痕迹，建议粉碎而非普通删除以防被枚举还原。")
                    cnt += 1
                    if cnt >= 100:
                        break
        except (OSError, PermissionError):
            pass

        # ---- 2) Windows 剪贴板历史目录 ----
        try:
            clipboard = os.path.join(os.environ.get("LOCALAPPDATA", ""),
                                     "Microsoft", "Windows", "Clipboard")
            if os.path.isdir(clipboard):
                s = get_size(clipboard)
                _add("剪贴板历史", clipboard, s,
                     "剪贴板历史中可能残留您复制过的密码、验证码等敏感内容，"
                     "建议粉碎以防被提取。")
        except (OSError, PermissionError):
            pass

        # ---- 3) 回收站（提示改用空闲空间擦除） ----
        try:
            recycle_total = 0
            recycle_paths = []
            for drive in ["C", "D", "E", "F"]:
                rp = os.path.join(drive + ":\\", "$Recycle.Bin")
                if os.path.isdir(rp):
                    recycle_paths.append(rp)
                    recycle_total += get_size(rp)
            _add("回收站残留", "; ".join(recycle_paths) or "回收站(无)", recycle_total,
                 "回收站中的文件仅被标记删除，数据仍留在磁盘上，普通清空无法防恢复；"
                 "建议配合「空闲空间擦除」处理。")
        except (OSError, PermissionError):
            pass

        # ---- 4) Documents 下的 *.tmp / *.bak 敏感残留 ----
        try:
            docs = os.path.join(os.environ.get("USERPROFILE", ""), "Documents")
            if os.path.isdir(docs):
                cnt = 0
                for f in os.listdir(docs):
                    low = f.lower()
                    if not (low.endswith(".tmp") or low.endswith(".bak")):
                        continue
                    fp = os.path.join(docs, f)
                    if not os.path.isfile(fp):
                        continue
                    s = get_size(fp)
                    _add("文档临时/备份残留",
                         fp, s,
                         "文档目录下的临时文件/备份文件常含正文片段，"
                         "建议粉碎以防内容泄露。")
                    cnt += 1
                    if cnt >= 100:
                        break
        except (OSError, PermissionError):
            pass

        # ---- 5) 微信 / QQ 缓存图片目录 ----
        try:
            wechat_root = os.path.join(os.environ.get("USERPROFILE", ""),
                                       "Documents", "WeChat Files")
            if os.path.isdir(wechat_root):
                for wx_id in os.listdir(wechat_root):
                    wx_dir = os.path.join(wechat_root, wx_id)
                    if not os.path.isdir(wx_dir):
                        continue
                    for sub in ["Image", "Video", "FileStorage"]:
                        sp = os.path.join(wx_dir, sub)
                        if os.path.isdir(sp):
                            s = get_size(sp)
                            _add("微信缓存(图片/视频/文件)",
                                 sp, s,
                                 "微信接收/缓存的图片、视频、文件可能含隐私内容，"
                                 "建议粉碎而非普通删除。")
        except (OSError, PermissionError):
            pass

        try:
            qq_root = os.path.join(os.environ.get("USERPROFILE", ""),
                                   "Documents", "Tencent Files")
            if os.path.isdir(qq_root):
                for qq_id in os.listdir(qq_root):
                    qq_dir = os.path.join(qq_root, qq_id)
                    if not os.path.isdir(qq_dir):
                        continue
                    for sub in ["Image", "Video", "FileRecv"]:
                        sp = os.path.join(qq_dir, sub)
                        if os.path.isdir(sp):
                            s = get_size(sp)
                            _add("QQ缓存(图片/视频/文件)",
                                 sp, s,
                                 "QQ接收/缓存的图片、视频、文件可能含隐私内容，"
                                 "建议粉碎而非普通删除。")
        except (OSError, PermissionError):
            pass

        # ---- 6) 浏览器历史 / 下载记录残留 ----
        try:
            local = os.environ.get("LOCALAPPDATA", "")
            browsers = [
                ("Chrome 历史/下载记录", os.path.join(
                    local, "Google", "Chrome", "User Data", "Default", "History")),
                ("Edge 历史/下载记录", os.path.join(
                    local, "Microsoft", "Edge", "User Data", "Default", "History")),
            ]
            for source, fp in browsers:
                if os.path.isfile(fp):
                    s = get_size(fp)
                    _add(source, fp, s,
                         "浏览器历史/下载记录数据库，包含您访问过的网址与下载记录，"
                         "建议粉碎以防被取证还原。")
        except (OSError, PermissionError):
            pass

        return results


if __name__ == "__main__":
    # 仅在直接运行本文件时执行的演示（被 import 时不产生任何副作用）
    engine = ShredEngine()
    print("可用覆写算法：")
    for a in engine.get_algorithms():
        print(f"  {a['id']:9s} {a['name']:24s} {a['passes']:>2d} 遍  {a['desc']}")
    print("\n隐私粉碎候选（预览，仅统计数量）：")
    pr = engine.scan_privacy_shred_targets()
    print(f"  共 {len(pr)} 项，合计 {format_size(sum(x.size for x in pr))}")
    print("\n提示：真正的粉碎请通过 shred_files / shred_path 调用。")
