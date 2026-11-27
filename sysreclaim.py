# -*- coding: utf-8 -*-
"""
深层空间回收引擎
覆盖那些"普通清理工具够不到"的空间大户：
  - Windows.old 与系统升级残留（$WinREAgent / $GetCurrent / Panther）
  - WinSxS 组件存储（DISM AnalyzeComponentStore / StartComponentCleanup）
  - 驱动仓库中的历史驱动包（DriverStore / pnputil）
  - 系统还原点与卷影副本（vssadmin / Get-ComputerRestorePoint）
  - 失效快捷方式、空文件夹、冗余右键菜单项

说明：本模块只给出真实可查证的结果；取不到数据时返回空列表并说明原因，绝不编造。
"""

import os
import csv
import io
import re
import ctypes
import shutil
import subprocess
from datetime import datetime

from scanner import ScanResult, get_size, format_size


def is_admin():
    """是否拥有管理员权限"""
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _run(cmd, timeout=120, shell=False):
    """
    执行命令并返回 (成功, 标准输出, 标准错误)。
    统一做编码兜底：先按 utf-8 尝试，失败再按 gbk 重试。
    """
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=timeout, shell=shell)
    except (subprocess.TimeoutExpired, OSError, ValueError) as e:
        return False, "", str(e)

    out = proc.stdout or b""
    err = proc.stderr or b""
    text_out = text_err = ""
    for enc in ("utf-8", "gbk", "latin-1"):
        try:
            text_out = out.decode(enc, errors="ignore")
            text_err = err.decode(enc, errors="ignore")
            break
        except Exception:
            continue
    return proc.returncode == 0, text_out, text_err


def _powershell(script, timeout=120):
    """执行 PowerShell 脚本并返回 (成功, 输出)"""
    cmd = ["powershell", "-NoProfile", "-NonInteractive", "-Command", script]
    ok, out, err = _run(cmd, timeout=timeout)
    return ok, (out if ok else (err or out))


def _ps_csv(script, timeout=120):
    """
    执行 PowerShell 并把输出当作 CSV 解析成 dict 列表。
    失败或解析不到时返回空列表。
    """
    ok, text = _powershell(
        script + " | ConvertTo-Csv -NoTypeInformation", timeout=timeout)
    if not ok or not text.strip():
        return []
    try:
        reader = csv.DictReader(io.StringIO(text))
        return [dict(row) for row in reader]
    except Exception:
        return []


class SysReclaimEngine:
    """深层空间回收"""

    # 系统升级残留目录（相对 C:\）
    UPGRADE_LEFTOVER_DIRS = [
        ("$WinREAgent", "Windows 恢复环境升级残留，升级完成后可安全删除"),
        ("$GetCurrent", "Windows 升级临时目录，升级完成后可安全删除"),
        ("$Windows.~BT", "Windows 升级准备文件，可安全删除"),
        ("$Windows.~WS", "Windows 升级工作目录，可安全删除"),
    ]

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

    # ==================== Windows.old 与升级残留 ====================

    def scan_windows_old(self):
        """扫描 C:\\Windows.old 并返回 ScanResult 列表"""
        self._emit(5, "正在检查 Windows.old ...")
        path = "C:\\Windows.old"
        if not os.path.exists(path):
            return []
        size = get_size(path)
        if size <= 0:
            return []
        return [ScanResult(
            category="深度回收",
            source="旧版Windows备份(Windows.old)",
            path=path,
            size=size,
            description=("系统升级时保留的旧版本 Windows 全部文件，用于回滚。"
                         "确认不需要回滚到旧版本后可删除，通常可释放数 GB 到数十 GB。"
                         "注意：删除后无法再通过系统设置回退到升级前的版本。"),
        )]

    def scan_upgrade_leftovers(self):
        """扫描系统升级残留目录"""
        self._emit(10, "正在检查升级残留目录...")
        results = []
        for name, desc in self.UPGRADE_LEFTOVER_DIRS:
            path = os.path.join("C:\\", name)
            if not os.path.exists(path):
                continue
            size = get_size(path)
            if size <= 0:
                continue
            results.append(ScanResult(
                category="深度回收",
                source=f"升级残留目录 {name}",
                path=path,
                size=size,
                description=desc,
            ))

        # 升级日志目录
        for log_name, log_desc in [("Panther", "系统升级过程日志"),
                                   ("Logs\\CBS", "组件安装日志"),
                                   ("Logs\\DISM", "DISM 操作日志")]:
            path = os.path.join("C:\\Windows", log_name)
            if not os.path.exists(path):
                continue
            size = get_size(path)
            if size > 0:
                results.append(ScanResult(
                    category="深度回收",
                    source=f"升级日志 {log_name}",
                    path=path,
                    size=size,
                    description=f"{log_desc}，用于排查升级问题，确认无异常后可清理。",
                ))
        return results

    def delete_windows_old(self):
        """
        删除 Windows.old 与升级残留。
        由于这些目录带特殊 ACL，直接 rmtree 常失败；失败时返回真实原因（不假装成功）。
        """
        if not is_admin():
            return False, "需要管理员权限，请以管理员身份重启程序"

        freed = 0
        failed = []
        targets = ["C:\\Windows.old"] + [
            os.path.join("C:\\", n) for n, _ in self.UPGRADE_LEFTOVER_DIRS
        ]
        for path in targets:
            if not os.path.exists(path):
                continue
            size = get_size(path)
            try:
                # 先尝试清除只读属性再删除
                _run(["attrib", "-r", "-h", "-s", "/s", "/d", path], timeout=120)
                shutil.rmtree(path, ignore_errors=False)
                freed += size
            except Exception as e:
                failed.append(f"{path}: {e}")

        if failed and freed == 0:
            return False, ("删除失败，这些目录受系统保护。建议改用 Windows 自带的"
                           "「设置 > 系统 > 存储 > 临时文件」或运行 cleanmgr 勾选"
                           "「以前的 Windows 安装」来清理。详细错误：" + failed[0])
        msg = f"已释放 {format_size(freed)}"
        if failed:
            msg += f"；{len(failed)} 项失败（多为权限保护目录）"
        return True, msg

    # ==================== WinSxS 组件存储 ====================

    def analyze_component_store(self):
        """
        用 DISM 分析组件存储（WinSxS）。
        返回 {"success":bool,"message":str,"component_store_size":int,
              "backup_size":int,"reclaimable":int,"recommend_cleanup":bool,"raw":str}
        """
        self._emit(18, "正在分析 WinSxS 组件存储（需要管理员权限，请稍候）...")
        if not is_admin():
            return {"success": False, "message": "需要管理员权限才能分析组件存储",
                    "component_store_size": 0, "backup_size": 0, "reclaimable": 0,
                    "recommend_cleanup": False, "raw": ""}

        ok, out, err = _run(["dism", "/Online", "/Cleanup-Image", "/AnalyzeComponentStore"],
                            timeout=600)
        text = out + "\n" + err
        if not ok and not text.strip():
            return {"success": False, "message": "DISM 调用失败",
                    "component_store_size": 0, "backup_size": 0, "reclaimable": 0,
                    "recommend_cleanup": False, "raw": text}

        def _pick(pattern_list):
            """从 DISM 输出里抓取体积数字（单位 GB/MB）"""
            for pat in pattern_list:
                m = re.search(pat, text, re.IGNORECASE)
                if m:
                    try:
                        val = float(m.group(1))
                        unit = m.group(2) if m.lastindex and m.lastindex >= 2 else ""
                        if unit and unit.upper().startswith("G"):
                            return int(val * 1024 ** 3)
                        return int(val * 1024 ** 2)
                    except Exception:
                        continue
            return 0

        store_sz = _pick([r"组件存储.*?:\s*([\d.]+)\s*(GB|MB)",
                          r"Component Store.*?:\s*([\d.]+)\s*(GB|MB)"])
        backup_sz = _pick([r"备份.*?:\s*([\d.]+)\s*(GB|MB)",
                           r"Backups.*?:\s*([\d.]+)\s*(GB|MB)"])
        reclaim = _pick([r"可回收.*?:\s*([\d.]+)\s*(GB|MB)",
                         r"Reclaimable.*?:\s*([\d.]+)\s*(GB|MB)"])

        recommend = bool(re.search(r"(建议|recommended).{0,30}(清理|cleanup)", text, re.IGNORECASE))

        return {
            "success": True,
            "message": ("组件存储分析完成。可在需要时执行「清理组件存储」释放空间。"
                        if store_sz > 0 else "DISM 已执行，但未能解析出具体体积数值"),
            "component_store_size": store_sz,
            "backup_size": backup_sz,
            "reclaimable": reclaim,
            "recommend_cleanup": recommend,
            "raw": text[:2000],
        }

    def clean_component_store(self, reset_base=False):
        """
        执行组件存储清理。
        reset_base=True 会额外执行 /ResetBase（清理所有被取代的组件，之后无法卸载已安装更新）。
        这是高耗时操作（可能十几分钟到数十分钟）。
        """
        if not is_admin():
            return False, "需要管理员权限，请以管理员身份重启程序"

        args = ["dism", "/Online", "/Cleanup-Image", "/StartComponentCleanup"]
        if reset_base:
            args.append("/ResetBase")

        self._emit(30, "正在清理组件存储（耗时较长，请耐心等待）...")
        ok, out, err = _run(args, timeout=3600)
        text = out + "\n" + err
        if ok:
            return True, "组件存储清理完成。"
        return False, "组件存储清理失败或超时。DISM 输出：" + text[-500:]

    def scan_component_store(self):
        """把组件存储情况转成 ScanResult（仅在分析成功且体积可观时产出）"""
        info = self.analyze_component_store()
        if not info["success"]:
            return []
        size = info["component_store_size"] or 0
        if size <= 0:
            return []
        desc = (f"WinSxS 组件存储当前约 {format_size(size)}。"
                f"其中备份部分约 {format_size(info['backup_size'])}，"
                f"可回收约 {format_size(info['reclaimable'])}。"
                "清理会删除被取代的旧组件版本；若勾选 ResetBase 则无法再卸载已安装的更新。")
        return [ScanResult(
            category="深度回收",
            source="WinSxS 组件存储",
            path="C:\\Windows\\WinSxS",
            size=info["reclaimable"] or size,
            description=desc,
        )]

    # ==================== 历史驱动包 ====================

    def scan_old_drivers(self):
        """
        列出驱动仓库中的第三方驱动包（oem*.inf）。
        返回 {"success":bool,"message":str,"store_size":int,"drivers":[dict]}
        """
        self._emit(40, "正在枚举驱动仓库...")
        store_path = os.path.join("C:\\Windows", "System32", "DriverStore", "FileRepository")
        store_size = get_size(store_path) if os.path.exists(store_path) else 0

        if not is_admin():
            return {"success": False, "message": "需要管理员权限才能枚举驱动包",
                    "store_size": store_size, "drivers": []}

        ok, text = _powershell("pnputil /enum-drivers", timeout=180)
        if not ok or not text.strip():
            return {"success": False, "message": "pnputil 调用失败",
                    "store_size": store_size, "drivers": []}

        drivers = []
        cur = {}
        for line in text.splitlines():
            line = line.strip()
            if not line:
                if cur.get("published_name"):
                    drivers.append(cur)
                cur = {}
                continue
            if ":" not in line and "：" not in line:
                continue
            key, _, val = line.partition(":") if ":" in line else line.partition("：")
            key = key.strip().lower()
            val = val.strip()
            if "published name" in key or "发布名称" in key:
                cur["published_name"] = val
            elif "original name" in key or "原始名称" in key:
                cur["original_name"] = val
            elif "provider" in key or "提供程序" in key or "提供商" in key:
                cur["provider"] = val
            elif "class" in key or "类" in key:
                cur["class"] = val
            elif "version" in key or "版本" in key:
                cur["version"] = val
            elif "date" in key or "日期" in key:
                cur["date"] = val
        if cur.get("published_name"):
            drivers.append(cur)

        return {
            "success": True,
            "message": f"共发现 {len(drivers)} 个第三方驱动包，驱动仓库体积 {format_size(store_size)}",
            "store_size": store_size,
            "drivers": drivers,
        }

    def remove_old_driver(self, published_name):
        """
        移除指定驱动包（pnputil /delete-driver ... /uninstall /force）。
        危险：若该驱动正被设备使用，可能导致设备失效。请仅在明确知道该驱动已不再需要时使用。
        """
        if not is_admin():
            return False, "需要管理员权限"
        if not published_name or not re.match(r"^oem\d+\.inf$", published_name.lower()):
            return False, "驱动包名称格式不正确（应为 oemXX.inf）"
        ok, out, err = _run(
            ["pnputil", "/delete-driver", published_name, "/uninstall", "/force"],
            timeout=180)
        if ok:
            return True, f"已移除驱动包 {published_name}"
        return False, f"移除失败：{(err or out)[:300]}"

    def scan_driver_store(self):
        """把驱动仓库情况转成 ScanResult"""
        info = self.scan_old_drivers()
        if not info["success"] or info["store_size"] <= 0:
            return []
        return [ScanResult(
            category="磁盘优化",
            source="驱动仓库历史驱动包(DriverStore)",
            path="C:\\Windows\\System32\\DriverStore\\FileRepository",
            size=info["store_size"],
            description=(f"共 {len(info['drivers'])} 个第三方驱动包，占用 {format_size(info['store_size'])}。"
                         "其中未被当前设备使用的历史版本可移除；请勿移除正在使用的驱动，"
                         "以免设备失效。建议逐个确认后再移除。"),
        )]

    # ==================== 系统还原点与卷影副本 ====================

    def scan_restore_points(self):
        """
        列出系统还原点。
        返回 {"success":bool,"message":str,"points":[dict],"storage":[dict]}
        """
        self._emit(55, "正在读取系统还原点...")

        points = _ps_csv(
            "Get-ComputerRestorePoint | Select-Object SequenceNumber,Description,"
            "CreationTime,RestorePointType", timeout=120)

        norm = []
        for p in points:
            norm.append({
                "id": (p.get("SequenceNumber") or "").strip(),
                "description": (p.get("Description") or "").strip(),
                "time": (p.get("CreationTime") or "").strip(),
                "type": (p.get("RestorePointType") or "").strip(),
            })

        # 卷影存储占用（需要管理员）
        storage = []
        if is_admin():
            ok, out, err = _run(["vssadmin", "list", "shadowstorage"], timeout=120)
            text = out + "\n" + err
            if text.strip():
                used = re.search(r"(已用|Used).{0,10}?([\d.]+)\s*(GB|MB|TB)", text, re.IGNORECASE)
                alloc = re.search(r"(已分配|Allocated).{0,10}?([\d.]+)\s*(GB|MB|TB)", text, re.IGNORECASE)
                maximum = re.search(r"(最大|Maximum).{0,10}?([\d.]+)\s*(GB|MB|TB)", text, re.IGNORECASE)

                def _to_bytes(m):
                    if not m:
                        return 0
                    try:
                        v = float(m.group(2))
                    except Exception:
                        return 0
                    unit = m.group(3).upper()
                    factor = {"TB": 1024 ** 4, "GB": 1024 ** 3, "MB": 1024 ** 2}.get(unit, 1024 ** 3)
                    return int(v * factor)

                storage.append({
                    "used": _to_bytes(used),
                    "allocated": _to_bytes(alloc),
                    "maximum": _to_bytes(maximum),
                })

        if not norm and not storage:
            return {"success": False,
                    "message": ("未检测到系统还原点，或该功能未启用。"
                                "可在「系统属性 > 系统保护」中开启。"),
                    "points": [], "storage": []}

        return {"success": True,
                "message": f"共 {len(norm)} 个还原点",
                "points": norm, "storage": storage}

    def delete_restore_points(self, mode="oldest"):
        """
        删除系统还原点。
        mode: "oldest" 删除最旧的一个 / "all" 删除全部（保留最近一个通常更安全）
        需要管理员权限。
        """
        if not is_admin():
            return False, "需要管理员权限，请以管理员身份重启程序"
        if mode == "all":
            ok, out, err = _run(
                ["vssadmin", "delete", "shadows", "/for=C:", "/all", "/quiet"], timeout=300)
        else:
            ok, out, err = _run(
                ["vssadmin", "delete", "shadows", "/for=C:", "/oldest", "/quiet"], timeout=300)
        if ok:
            return True, "还原点已删除" if mode == "all" else "已删除最旧的还原点"
        return False, f"删除失败：{(err or out)[:300]}"

    def set_restore_space_limit(self, percent=5):
        """限制还原点可占用的最大磁盘空间比例"""
        if not is_admin():
            return False, "需要管理员权限"
        if percent < 1 or percent > 100:
            return False, "比例需在 1-100 之间"
        ok, out, err = _run(
            ["vssadmin", "resize", "shadowstorage", "/for=C:",
             f"/maxsize={percent}%"], timeout=120)
        if ok:
            return True, f"已将还原点最大占用限制为 {percent}%"
        return False, f"设置失败：{(err or out)[:300]}"

    def scan_restore_point_report(self):
        """把还原点情况转成 ScanResult"""
        info = self.scan_restore_points()
        if not info["success"]:
            return []
        used = info["storage"][0]["used"] if info["storage"] else 0
        if used <= 0 and len(info["points"]) <= 1:
            return []
        size = used if used > 0 else 0
        return [ScanResult(
            category="深度回收",
            source="系统还原点与卷影副本",
            path="系统还原点",
            size=size,
            description=(f"当前有 {len(info['points'])} 个系统还原点，"
                         f"占用约 {format_size(size) if size else '未知'}。"
                         "删除旧还原点可释放空间，但会失去回滚能力。建议保留最近一个。"),
        )]

    # ==================== 失效快捷方式 ====================

    def _resolve_lnk_targets(self, lnk_paths):
        """
        批量解析 .lnk 目标路径。
        做法：把路径清单写入临时文件，用一次 PowerShell 调用批量读取，
        避免为每个文件启动一次进程。
        返回 {lnk_path: target_path}
        """
        if not lnk_paths:
            return {}
        tmp_list = os.path.join(os.environ.get("TEMP", "C:\\Windows\\Temp"),
                                "cleanmaster_lnk_list.txt")
        try:
            with open(tmp_list, "w", encoding="utf-8") as f:
                f.write("\n".join(lnk_paths))
        except Exception:
            return {}

        script = (
            "$sh = New-Object -ComObject WScript.Shell;"
            f"Get-Content -LiteralPath '{tmp_list}' -Encoding UTF8 | ForEach-Object {{"
            "  $p = $_;"
            "  if ($p -and (Test-Path -LiteralPath $p)) {"
            "    try { $t = $sh.CreateShortcut($p).TargetPath;"
            "      \"$p`t$t\" } catch {}"
            "  }"
            "}}"
        )
        ok, text = _powershell(script, timeout=180)
        mapping = {}
        if ok:
            for line in text.splitlines():
                if "\t" in line:
                    k, _, v = line.partition("\t")
                    mapping[k.strip()] = v.strip()
        try:
            os.remove(tmp_list)
        except Exception:
            pass
        return mapping

    def scan_invalid_shortcuts(self):
        """扫描指向不存在目标的快捷方式"""
        self._emit(68, "正在检查失效快捷方式...")
        targets = []
        candidate_dirs = [
            os.path.join(os.environ.get("APPDATA", ""), "Microsoft", "Windows", "Recent"),
            os.path.join(os.environ.get("USERPROFILE", ""), "Desktop"),
            os.path.join(os.environ.get("USERPROFILE", ""), "OneDrive", "桌面"),
            os.path.join(os.environ.get("APPDATA", ""), "Microsoft", "Windows",
                         "Start Menu", "Programs"),
            os.path.join(os.environ.get("PROGRAMDATA", ""), "Microsoft", "Windows",
                         "Start Menu", "Programs"),
        ]
        for d in candidate_dirs:
            if not os.path.isdir(d):
                continue
            try:
                with os.scandir(d) as it:
                    for e in it:
                        if e.is_file() and e.name.lower().endswith(".lnk"):
                            targets.append(e.path)
            except (OSError, PermissionError):
                continue

        if not targets:
            return []

        mapping = self._resolve_lnk_targets(targets)
        invalid = []
        for lnk, tgt in mapping.items():
            # 目标为空或不存在，且不是网址/特殊目标
            if not tgt:
                continue
            if tgt.lower().startswith(("http://", "https://", "shell:", "::{", "mailto:")):
                continue
            if not os.path.exists(tgt):
                invalid.append((lnk, tgt))

        results = []
        for lnk, tgt in invalid:
            results.append(ScanResult(
                category="系统清理",
                source="失效快捷方式",
                path=lnk,
                size=0,
                description=f"该快捷方式指向的目标已不存在：{tgt}。可安全删除。",
            ))
        self._emit(78, f"发现 {len(results)} 个失效快捷方式")
        return results

    # ==================== 空文件夹 ====================

    def scan_empty_folders(self, roots=None, max_scan_dirs=200000):
        """扫描完全为空的文件夹（递归判定：自身无文件且所有子目录也为空）"""
        self._emit(82, "正在查找空文件夹...")
        if roots is None:
            up = os.environ.get("USERPROFILE", "")
            roots = [os.path.join(up, d) for d in
                     ("Desktop", "Documents", "Downloads", "Pictures", "Music", "Videos")]
            roots = [r for r in roots if os.path.isdir(r)]

        skip = {"$Recycle.Bin", "Windows", "WinSxS", "System Volume Information",
                "node_modules", ".git", "$WinREAgent"}
        empty_dirs = set()
        scanned = 0

        for root in roots:
            # 先 topdown 剪枝收集目录（跳过集真正生效），再自底向上判定
            walked = []
            for dirpath, dirnames, filenames in os.walk(
                    root, topdown=True, onerror=lambda e: None):
                if self._stop:
                    break
                dirnames[:] = [d for d in dirnames if d not in skip]
                walked.append((dirpath, filenames))
                scanned += 1
                if scanned > max_scan_dirs:
                    break
            if self._stop:
                break

            for dirpath, filenames in reversed(walked):
                if filenames or dirpath == root:
                    continue          # 根目录本身不删；有文件也不算空
                try:
                    entries = os.listdir(dirpath)
                except (OSError, PermissionError):
                    continue
                is_empty = True
                for name in entries:
                    full = os.path.join(dirpath, name)
                    if os.path.isdir(full):
                        if full not in empty_dirs:
                            is_empty = False
                            break
                    else:
                        is_empty = False
                        break
                if is_empty:
                    empty_dirs.add(dirpath)

        results = []
        for path in sorted(empty_dirs, key=len, reverse=True):
            results.append(ScanResult(
                category="系统清理",
                source="空文件夹",
                path=path,
                size=0,
                description="该文件夹自身没有文件，且所有子目录也为空，可安全删除。",
            ))
        self._emit(88, f"发现 {len(results)} 个空文件夹")
        return results

    def clean_empty_folders(self, paths):
        """删除空文件夹（只删确实为空的，非空的一律跳过）"""
        deleted = 0
        failed = []
        # 深的先删：父目录只有在子目录删完后才会变空
        for p in sorted(paths, key=len, reverse=True):
            try:
                if os.path.isdir(p) and not os.listdir(p):
                    os.rmdir(p)
                    deleted += 1
            except (OSError, PermissionError) as e:
                failed.append((p, str(e)))
        return deleted, failed

    # ==================== 右键菜单管理 ====================

    # 右键菜单所在的注册表位置（根键, 子路径, 界面显示名）
    CONTEXT_MENU_LOCATIONS = [
        ("HKEY_CURRENT_USER", r"Software\Classes\*\shell", "文件右键菜单(当前用户)"),
        ("HKEY_CURRENT_USER", r"Software\Classes\*\shellex\ContextMenuHandlers", "文件右键扩展(当前用户)"),
        ("HKEY_CURRENT_USER", r"Software\Classes\Directory\shell", "文件夹右键菜单(当前用户)"),
        ("HKEY_CURRENT_USER", r"Software\Classes\Directory\shellex\ContextMenuHandlers", "文件夹右键扩展(当前用户)"),
        ("HKEY_CURRENT_USER", r"Software\Classes\Directory\Background\shell", "桌面空白处右键(当前用户)"),
        ("HKEY_CURRENT_USER", r"Software\Classes\Folder\shell", "目录对象右键(当前用户)"),
        ("HKEY_CURRENT_USER", r"Software\Classes\Drive\shell", "驱动器右键(当前用户)"),
        ("HKEY_CURRENT_USER", r"Software\Classes\AllFilesystemObjects\shellex\ContextMenuHandlers", "所有文件对象扩展(当前用户)"),
        ("HKEY_LOCAL_MACHINE", r"SOFTWARE\Classes\*\shell", "文件右键菜单(全机)"),
        ("HKEY_LOCAL_MACHINE", r"SOFTWARE\Classes\*\shellex\ContextMenuHandlers", "文件右键扩展(全机)"),
        ("HKEY_LOCAL_MACHINE", r"SOFTWARE\Classes\Directory\shell", "文件夹右键菜单(全机)"),
        ("HKEY_LOCAL_MACHINE", r"SOFTWARE\Classes\Directory\shellex\ContextMenuHandlers", "文件夹右键扩展(全机)"),
        ("HKEY_LOCAL_MACHINE", r"SOFTWARE\Classes\Directory\Background\shell", "桌面空白处右键(全机)"),
        ("HKEY_LOCAL_MACHINE", r"SOFTWARE\Classes\Folder\shell", "目录对象右键(全机)"),
        ("HKEY_LOCAL_MACHINE", r"SOFTWARE\Classes\Folder\shellex\ContextMenuHandlers", "目录对象扩展(全机)"),
        ("HKEY_LOCAL_MACHINE", r"SOFTWARE\Classes\Drive\shell", "驱动器右键(全机)"),
    ]

    _ROOT_MAP = {"HKEY_CURRENT_USER": None, "HKEY_LOCAL_MACHINE": None}

    def _hkey(self, root_name):
        """把根键字符串映射为 winreg 常量"""
        import winreg
        return {"HKEY_CURRENT_USER": winreg.HKEY_CURRENT_USER,
                "HKEY_LOCAL_MACHINE": winreg.HKEY_LOCAL_MACHINE}.get(root_name, winreg.HKEY_CURRENT_USER)

    def scan_context_menu(self):
        """枚举右键菜单项"""
        import winreg
        self._emit(90, "正在枚举右键菜单项...")
        items = []
        for root_name, sub, loc_name in self.CONTEXT_MENU_LOCATIONS:
            path = root_name + "\\" + sub
            try:
                key = winreg.OpenKey(self._hkey(root_name), sub, 0, winreg.KEY_READ)
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
                    if idx > 500:
                        break
                    display = name
                    try:
                        sub_key = winreg.OpenKey(self._hkey(root_name),
                                                 sub + "\\" + name, 0, winreg.KEY_READ)
                        try:
                            val, _ = winreg.QueryValueEx(sub_key, "")
                            if val:
                                display = str(val)
                        except (FileNotFoundError, OSError):
                            pass
                        try:
                            clsid, _ = winreg.QueryValueEx(sub_key, "CLSID")
                        except (FileNotFoundError, OSError):
                            clsid = ""
                        winreg.CloseKey(sub_key)
                    except (FileNotFoundError, PermissionError, OSError):
                        clsid = ""

                    items.append({
                        "root": root_name,
                        "key_path": path + "\\" + name,
                        "sub": sub + "\\" + name,
                        "name": name,
                        "display": display,
                        "clsid": clsid,
                        "location": loc_name,
                        "need_admin": root_name == "HKEY_LOCAL_MACHINE",
                    })
            finally:
                try:
                    winreg.CloseKey(key)
                except Exception:
                    pass

        self._emit(94, f"发现 {len(items)} 个右键菜单项")
        return items

    def backup_registry_key(self, root_name, sub_path, backup_dir=None):
        """用 reg.exe 导出单个注册表键作为备份"""
        if backup_dir is None:
            backup_dir = os.path.join(os.environ.get("USERPROFILE", ""),
                                      "CleanMasterBackup", "contextmenu")
        os.makedirs(backup_dir, exist_ok=True)
        safe = re.sub(r"[^A-Za-z0-9_\-]", "_", sub_path)[:80]
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        out_file = os.path.join(backup_dir, f"{safe}_{stamp}.reg")
        ok, out, err = _run(["reg", "export", root_name + "\\" + sub_path, out_file, "/y"],
                            timeout=60)
        if ok and os.path.exists(out_file):
            return True, out_file
        return False, (err or out)[:200] or "导出失败"

    def delete_context_menu_entry(self, item):
        """删除一个右键菜单项（删除前自动备份）"""
        if item.get("need_admin") and not is_admin():
            return False, "该菜单项属于全机范围，需要管理员权限"

        ok, info = self.backup_registry_key(item["root"], item["sub"])
        if not ok:
            return False, f"备份失败，为安全起见已取消删除：{info}"

        ok2, out, err = _run(["reg", "delete",
                              item["root"] + "\\" + item["sub"], "/f"], timeout=60)
        if ok2:
            return True, f"已删除「{item['display']}」，备份在 {info}"
        return False, (err or out)[:200]

    # ==================== 统一扫描入口 ====================

    def scan_all_deep(self):
        """执行全部深度回收扫描，返回 ScanResult 列表"""
        self.reset_stop()
        results = []
        results += self.scan_windows_old()
        if self._stop:
            return results
        results += self.scan_upgrade_leftovers()
        if self._stop:
            return results
        results += self.scan_component_store()
        if self._stop:
            return results
        results += self.scan_driver_store()
        if self._stop:
            return results
        results += self.scan_restore_point_report()
        if self._stop:
            return results
        results += self.scan_invalid_shortcuts()
        if self._stop:
            return results
        results += self.scan_empty_folders()
        self._emit(100, f"深度回收扫描完成，共 {len(results)} 项")
        return results
