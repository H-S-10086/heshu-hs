# -*- coding: utf-8 -*-
"""
磁盘空间分析引擎
提供四类能力：
  1. 驱动器清单（容量 / 类型 / 文件系统）
  2. 目录树体积分析（一次遍历同时产出：目录树、文件类型占用、最大文件排行）
  3. 重复文件查找（三段式收敛：大小分组 -> 首部哈希 -> 全量哈希）
  4. Treemap 矩形树图布局计算（Squarified 算法，只算坐标，渲染交给 UI）

说明：本模块只做计算，不删除任何文件。
"""

import os
import ctypes
import hashlib
from collections import defaultdict

from scanner import get_size, format_size


# ==================== 文件类型分类 ====================

FILE_TYPE_MAP = {
    "视频": ['.mp4', '.mkv', '.avi', '.mov', '.wmv', '.flv', '.rmvb', '.ts', '.m4v',
             '.webm', '.mpg', '.mpeg', '.3gp', '.rm', '.vob'],
    "图片": ['.jpg', '.jpeg', '.png', '.gif', '.bmp', '.webp', '.tif', '.tiff',
             '.heic', '.raw', '.psd', '.svg', '.ico', '.cr2', '.nef'],
    "音频": ['.mp3', '.wav', '.flac', '.aac', '.ogg', '.wma', '.m4a', '.ape', '.mid'],
    "文档": ['.pdf', '.doc', '.docx', '.xls', '.xlsx', '.ppt', '.pptx', '.txt', '.md',
             '.csv', '.rtf', '.odt', '.ods', '.epub'],
    "安装包": ['.exe', '.msi', '.msix', '.appx', '.iso', '.apk', '.dmg', '.pkg', '.cab'],
    "压缩包": ['.zip', '.rar', '.7z', '.tar', '.gz', '.bz2', '.xz', '.lz4', '.zst'],
    "程序与库": ['.dll', '.sys', '.lib', '.so', '.dylib', '.pdb', '.node', '.ocx', '.drv'],
    "数据与缓存": ['.dat', '.db', '.sqlite', '.sqlite3', '.log', '.tmp', '.cache',
                  '.bin', '.idx', '.bak', '.old'],
    "网页与脚本": ['.html', '.htm', '.js', '.css', '.json', '.xml', '.py', '.java',
                  '.cs', '.cpp', '.h', '.php', '.vue', '.ts'],
    "字体": ['.ttf', '.otf', '.woff', '.woff2', '.fon', '.ttc', '.pfb'],
}

# 反查表：扩展名 -> 分类名
_EXT_TO_CATEGORY = {}
for _cat, _exts in FILE_TYPE_MAP.items():
    for _e in _exts:
        _EXT_TO_CATEGORY[_e] = _cat


def category_of(filename):
    """按扩展名判定文件类型分类"""
    ext = os.path.splitext(filename)[1].lower()
    if not ext:
        return "无扩展名"
    return _EXT_TO_CATEGORY.get(ext, "其他")


# ==================== Treemap Squarified 布局算法 ====================

def _worst_ratio(row, short_side):
    """
    计算某一行的最差长宽比（越接近 1 越方正）。
    row 内元素为面积；short_side 是当前矩形短边长度。
    """
    if short_side <= 0 or not row:
        return float('inf')
    total = sum(row)
    if total <= 0:
        return float('inf')
    thickness = total / float(short_side)
    if thickness <= 0:
        return float('inf')
    worst = 0.0
    for v in row:
        length = v / thickness
        if length <= 0:
            return float('inf')
        r = thickness / length
        if length / thickness > r:
            r = length / thickness
        if r > worst:
            worst = r
    return worst


def layout_treemap(items, x, y, width, height, min_area_ratio=0.0005):
    """
    把一组带 size 的条目铺进矩形 (x, y, width, height)，返回矩形列表。

    参数
      items: 形如 [{"size": int, ...}, ...]，会按 size 降序排列
      min_area_ratio: 面积占比低于该值的条目会被丢弃（默认 0.05%），避免画面上出现无数细条

    返回
      [{"x","y","w","h","item","size"}]，坐标为浮点像素
    """
    if not items or width <= 0 or height <= 0:
        return []

    valid = [it for it in items if it.get("size", 0) > 0]
    if not valid:
        return []
    valid.sort(key=lambda it: it["size"], reverse=True)

    total = sum(it["size"] for it in valid)
    min_size = total * min_area_ratio
    valid = [it for it in valid if it["size"] >= min_size]
    if not valid:
        valid = [max(items, key=lambda it: it.get("size", 0))]
    total = sum(it["size"] for it in valid)

    # 归一化到矩形面积
    rect_area = float(width) * float(height)
    scale = rect_area / float(total)
    areas = [it["size"] * scale for it in valid]

    rects = []
    cx, cy, cw, ch = float(x), float(y), float(width), float(height)
    i = 0
    while i < len(areas):
        if cw <= 0.5 or ch <= 0.5:
            break
        short_side = cw if cw < ch else ch
        row = [areas[i]]
        best = _worst_ratio(row, short_side)
        j = i + 1
        while j < len(areas):
            trial = row + [areas[j]]
            ratio = _worst_ratio(trial, short_side)
            if ratio > best:
                break
            row = trial
            best = ratio
            j += 1

        row_total = sum(row)
        if row_total <= 0:
            break

        if cw >= ch:
            # 竖切一条：宽度 = 行面积 / 高度
            strip_w = row_total / ch
            offset = 0.0
            for k, v in enumerate(row):
                h = v / strip_w if strip_w > 0 else 0
                rects.append({
                    "x": cx, "y": cy + offset, "w": strip_w, "h": h,
                    "item": valid[i + k], "size": valid[i + k]["size"],
                })
                offset += h
            cx += strip_w
            cw -= strip_w
        else:
            # 横切一条：高度 = 行面积 / 宽度
            strip_h = row_total / cw
            offset = 0.0
            for k, v in enumerate(row):
                w = v / strip_h if strip_h > 0 else 0
                rects.append({
                    "x": cx + offset, "y": cy, "w": w, "h": strip_h,
                    "item": valid[i + k], "size": valid[i + k]["size"],
                })
                offset += w
            cy += strip_h
            ch -= strip_h

        i = j

    return rects


# ==================== 主引擎 ====================

class DiskMapEngine:
    """磁盘空间分析与重复文件查找"""

    # 遍历过程中记录的"大文件"阈值
    LARGEST_MIN_SIZE = 50 * 1024 * 1024      # 50 MB
    LARGEST_KEEP = 200                        # 只保留前 200 个大文件

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

    # ---------- 驱动器清单 ----------

    def list_drives(self):
        """
        列出所有可用驱动器。
        返回 [{"drive":"C:\\","letter":"C","total":int,"used":int,"free":int,
               "percent":float,"type":"SSD/HDD/可移动/未知","fs":"NTFS", "is_system":bool}]
        """
        result = []
        try:
            mask = ctypes.windll.kernel32.GetLogicalDrives()
        except Exception:
            mask = 0

        if not mask:
            # 兜底：直接猜常见盘符
            letters = ['C', 'D', 'E', 'F']
        else:
            letters = []
            for i in range(26):
                if mask & (1 << i):
                    letters.append(chr(ord('A') + i))

        drive_type_map = {0: "未知", 1: "无根目录", 2: "可移动", 3: "固定磁盘",
                          4: "网络", 5: "光驱", 6: "RAM盘"}

        for letter in letters:
            root = letter + ":\\"
            try:
                usage = os.statvfs(root) if hasattr(os, 'statvfs') else None
            except Exception:
                usage = None

            total = free = 0
            try:
                import shutil as _sh
                total, used, free = _sh.disk_usage(root)
            except Exception:
                continue

            if total <= 0:
                continue

            # 驱动器类型
            drive_kind = "未知"
            try:
                dt = ctypes.windll.kernel32.GetDriveTypeW(ctypes.c_wchar_p(root))
                drive_kind = drive_type_map.get(dt, "未知")
            except Exception:
                pass

            # 只保留固定磁盘与可移动磁盘
            if drive_kind not in ("固定磁盘", "可移动", "未知"):
                continue

            # 文件系统类型
            fs_name = ""
            try:
                buf = ctypes.create_unicode_buffer(261)
                fs_buf = ctypes.create_unicode_buffer(261)
                ctypes.windll.kernel32.GetVolumeInformationW(
                    ctypes.c_wchar_p(root), buf, 261, None, None, None, fs_buf, 261)
                fs_name = fs_buf.value
            except Exception:
                pass

            result.append({
                "drive": root,
                "letter": letter,
                "total": total,
                "used": total - free,
                "free": free,
                "percent": round((total - free) / total * 100, 1) if total else 0,
                "type": "SSD/HDD(待检测)",
                "fs": fs_name,
                "is_system": letter.upper() == 'C',
                "drive_kind": drive_kind,
            })

        self._emit(100, "驱动器信息读取完成")
        return result

    # ---------- 目录树体积分析 ----------

    def analyze(self, root, max_depth=3, min_child_ratio=0.01, largest_min=None):
        """
        一次性遍历，同时产出：
          - tree: 目录树（最多 max_depth 层）
          - types: 文件类型占用统计
          - largest: 大文件排行
          - 文件/目录计数

        参数
          max_depth: 目录树保留层数（0 表示只有根节点）
          min_child_ratio: 子节点体积占父节点比例低于此值时合并到"其他"，默认 1%
          largest_min: 大文件排行阈值（字节），默认 50MB

        返回
          {"path","size","file_count","dir_count","error",
           "tree": {name,path,size,children:[...]},
           "types":[{"name","size","percent","count"}],
           "largest":[{"path","name","size","dir"}]}
        """
        self.reset_stop()
        if largest_min is None:
            largest_min = self.LARGEST_MIN_SIZE

        self._emit(1, f"正在分析 {root} ...")
        types_counter = defaultdict(int)
        types_count = defaultdict(int)
        largest = []
        counters = {"files": 0, "dirs": 0}
        errors = []

        def walk(path, depth):
            total = 0
            children = []
            try:
                with os.scandir(path) as it:
                    for entry in it:
                        if self._stop:
                            break
                        try:
                            if entry.is_dir(follow_symlinks=False):
                                sub_total, sub_children = walk(entry.path, depth + 1)
                                total += sub_total
                                counters["dirs"] += 1
                                if depth < max_depth:
                                    children.append({
                                        "name": entry.name,
                                        "path": entry.path,
                                        "size": sub_total,
                                        "children": sub_children,
                                        "is_dir": True,
                                    })
                            else:
                                st = entry.stat(follow_symlinks=False)
                                sz = st.st_size
                                total += sz
                                counters["files"] += 1
                                cat = category_of(entry.name)
                                types_counter[cat] += sz
                                types_count[cat] += 1
                                if sz >= largest_min:
                                    largest.append({
                                        "path": entry.path,
                                        "name": entry.name,
                                        "size": sz,
                                        "dir": path,
                                    })
                        except (OSError, PermissionError):
                            continue
            except (OSError, PermissionError) as e:
                errors.append(str(e))

            if depth < max_depth and children:
                if self._stop:
                    return total, []
                children.sort(key=lambda x: x["size"], reverse=True)
                head_total = total if total > 0 else 1
                kept, rest_size = [], 0
                for c in children:
                    if c["size"] / head_total >= min_child_ratio:
                        kept.append(c)
                    else:
                        rest_size += c["size"]
                if rest_size > 0:
                    kept.append({
                        "name": f"其他较小项目({format_size(rest_size)})",
                        "path": path,
                        "size": rest_size,
                        "children": [],
                        "is_dir": False,
                    })
                children = kept
            else:
                children = []

            return total, children

        root_total, root_children = walk(root, 0)

        # 进度只在收尾时给（递归遍历无法精确报进度）
        self._emit(90, "正在汇总统计...")

        type_list = []
        for name, size in types_counter.items():
            type_list.append({
                "name": name,
                "size": size,
                "count": types_count[name],
                "percent": round(size / root_total * 100, 2) if root_total else 0,
            })
        type_list.sort(key=lambda x: x["size"], reverse=True)

        largest.sort(key=lambda x: x["size"], reverse=True)
        largest = largest[:self.LARGEST_KEEP]

        self._emit(100, f"分析完成，共 {counters['files']} 个文件")

        return {
            "path": root,
            "size": root_total,
            "file_count": counters["files"],
            "dir_count": counters["dirs"],
            "error": "; ".join(errors[:3]) if errors else "",
            "stopped": self._stop,
            "tree": {
                "name": os.path.basename(root.rstrip("\\/")) or root,
                "path": root,
                "size": root_total,
                "children": root_children,
                "is_dir": True,
            },
            "types": type_list,
            "largest": largest,
        }

    # ---------- 重复文件查找 ----------

    def _hash_head(self, path, size=8192):
        """读取文件首部若干字节做哈希"""
        h = hashlib.blake2b(digest_size=16)
        with open(path, 'rb') as f:
            h.update(f.read(size))
        return h.hexdigest()

    def _hash_full(self, path, block=1024 * 1024):
        """全文件哈希（分块读取，避免占内存）"""
        h = hashlib.blake2b(digest_size=16)
        with open(path, 'rb') as f:
            while True:
                if self._stop:
                    return None
                chunk = f.read(block)
                if not chunk:
                    break
                h.update(chunk)
        return h.hexdigest()

    def find_duplicates(self, roots, min_size=1024 * 1024, max_files=80000,
                        skip_dirs=None):
        """
        查找重复文件（三段式收敛，避免对大文件做无谓的全量哈希）。

        参数
          roots: 要扫描的根目录列表（字符串或列表）
          min_size: 小于该体积的文件不参与（默认 1MB）
          max_files: 收集文件数上限，防止超大磁盘卡死
          skip_dirs: 需要跳过的目录名集合

        返回
          {"groups":[{"size","count","wasted","files","hash"}],
           "scanned":int,"total_groups":int,"total_wasted":int,
           "skipped":int,"stopped":bool,"message":str}
        """
        self.reset_stop()
        if isinstance(roots, str):
            roots = [roots]
        if skip_dirs is None:
            skip_dirs = {"$Recycle.Bin", "System Volume Information", "Windows",
                         "WinSxS", "node_modules", ".git", "Program Files",
                         "Program Files (x86)", "$WinREAgent"}

        self._emit(2, "正在收集候选文件...")

        size_map = defaultdict(list)
        scanned = 0
        skipped = 0

        for root in roots:
            if not os.path.exists(root):
                continue
            for dirpath, dirnames, filenames in os.walk(root, onerror=lambda e: None):
                if self._stop or scanned >= max_files:
                    break
                dirnames[:] = [d for d in dirnames if d not in skip_dirs]
                for fn in filenames:
                    if scanned >= max_files:
                        break
                    fp = os.path.join(dirpath, fn)
                    try:
                        sz = os.path.getsize(fp)
                    except (OSError, PermissionError):
                        skipped += 1
                        continue
                    if sz < min_size:
                        continue
                    size_map[sz].append(fp)
                    scanned += 1

        # 只保留同体积出现 2 次以上的
        candidates = [(sz, paths) for sz, paths in size_map.items() if len(paths) > 1]
        candidates.sort(key=lambda x: x[0] * (len(x[1]) - 1), reverse=True)

        if not candidates:
            self._emit(100, "未发现重复文件")
            return {"groups": [], "scanned": scanned, "total_groups": 0,
                    "total_wasted": 0, "skipped": skipped, "stopped": False,
                    "message": "未发现重复文件"}

        self._emit(20, f"发现 {len(candidates)} 组同体积文件，正在比对内容...")

        groups = []
        total_cand = len(candidates)
        for idx, (sz, paths) in enumerate(candidates):
            if self._stop:
                break
            percent = 20 + int(idx / total_cand * 75)
            if idx % 5 == 0:
                self._emit(percent, f"正在比对第 {idx + 1}/{total_cand} 组...")

            # 第二段：首部哈希
            head_map = defaultdict(list)
            for p in paths:
                try:
                    head_map[self._hash_head(p)].append(p)
                except (OSError, PermissionError):
                    skipped += 1
            stage2 = [ps for ps in head_map.values() if len(ps) > 1]

            # 第三段：全量哈希
            for ps in stage2:
                if self._stop:
                    break
                full_map = defaultdict(list)
                for p in ps:
                    try:
                        fh = self._hash_full(p)
                    except (OSError, PermissionError):
                        skipped += 1
                        continue
                    if fh is None:
                        break
                    full_map[fh].append(p)
                for fh, dup_paths in full_map.items():
                    if len(dup_paths) > 1:
                        dup_paths.sort()
                        groups.append({
                            "size": sz,
                            "count": len(dup_paths),
                            "wasted": sz * (len(dup_paths) - 1),
                            "files": dup_paths,
                            "hash": fh,
                        })

        groups.sort(key=lambda g: g["wasted"], reverse=True)
        total_wasted = sum(g["wasted"] for g in groups)
        self._emit(100, f"查找完成，发现 {len(groups)} 组重复文件")

        return {
            "groups": groups,
            "scanned": scanned,
            "total_groups": len(groups),
            "total_wasted": total_wasted,
            "skipped": skipped,
            "stopped": self._stop,
            "message": f"共扫描 {scanned} 个文件，发现 {len(groups)} 组重复，可释放 {format_size(total_wasted)}",
        }

    # ---------- 删除选定文件（供重复文件界面使用） ----------

    def delete_files(self, paths, keep_first=True):
        """
        删除给定文件。keep_first=True 时每组保留第一个（按路径排序后的第一个）。

        返回 {"deleted":int,"freed":int,"failed":[(path, reason)]}
        """
        deleted = 0
        freed = 0
        failed = []
        for p in paths:
            try:
                sz = os.path.getsize(p)
                os.remove(p)
                deleted += 1
                freed += sz
            except (OSError, PermissionError) as e:
                failed.append((p, str(e)))
        return {"deleted": deleted, "freed": freed, "failed": failed}

    # ---------- 汇总给主程序表格使用 ----------

    def scan_disk_report(self):
        """
        生成"磁盘空间体检"条目（用于主程序结果表格）。
        返回 ScanResult 列表：驱动器剩余空间不足、超大文件、目录体积异常。
        """
        from scanner import ScanResult
        results = []
        try:
            drives = self.list_drives()
        except Exception:
            drives = []

        for d in drives:
            if d["total"] <= 0:
                continue
            free_percent = d["free"] / d["total"] * 100
            if free_percent < 10:
                tip = ("系统盘空间严重不足" if d["is_system"] else "该磁盘剩余空间不足")
                results.append(ScanResult(
                    category="磁盘空间",
                    source=f"{d['drive']} {tip}",
                    path=d["drive"],
                    size=d["used"],
                    description=(f"总容量 {format_size(d['total'])}，已用 {d['percent']}%，"
                                 f"仅剩 {format_size(d['free'])}。建议清理垃圾或大文件。"),
                ))
        self._emit(100, "磁盘空间体检完成")
        return results
