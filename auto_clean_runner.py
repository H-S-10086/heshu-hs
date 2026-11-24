# -*- coding: utf-8 -*-
"""
自动清理执行脚本（供计划任务调用，无界面）

用法：
    python auto_clean_runner.py [--quick|--full] [--log 日志路径]

设计说明：
  - 只清理"安全可删除"的系统垃圾，不触碰注册表、不做任何不可逆的高危操作
  - 所有动作写入日志文件，可在 用户目录\\CleanMasterBackup\\auto_clean.log 查看
  - 计划任务由「启动项管理」页的「自动清理计划」创建
"""

import os
import sys
import argparse
from datetime import datetime

from scanner import ScanEngine, format_size
from cleaner import CleanEngine


def default_log_path():
    base = os.path.join(os.environ.get("USERPROFILE", os.environ.get("TEMP", "")),
                        "CleanMasterBackup")
    try:
        os.makedirs(base, exist_ok=True)
    except Exception:
        base = os.environ.get("TEMP", ".")
    return os.path.join(base, "auto_clean.log")


def write_log(log_path, text):
    """追加一行日志"""
    try:
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {text}\n")
    except Exception:
        pass


def run(mode="quick", log_path=None, quiet=False):
    """
    执行自动清理，返回释放的字节数。

    mode="quick" 只清理系统垃圾与回收站；mode="full" 额外包含微信/QQ 缓存。
    """
    log_path = log_path or default_log_path()
    write_log(log_path, f"=== 自动清理开始（模式：{mode}） ===")

    engine = ScanEngine()
    if mode == "full":
        results = engine.scan_all()
    else:
        results = engine.scan_quick()

    # 只保留安全类别；不自动处理大文件、注册表与深度回收项
    safe_categories = {"系统垃圾", "微信专清", "QQ专清", "隐私清理"}
    safe_results = [r for r in results if getattr(r, "category", "") in safe_categories]

    skipped = [r for r in results if getattr(r, "category", "") not in safe_categories]

    total_before = sum(getattr(r, "size", 0) for r in safe_results)
    write_log(log_path, f"扫描到 {len(results)} 项，其中可安全清理 {len(safe_results)} 项，"
                        f"预计 {format_size(total_before)}")

    cleaner = CleanEngine()
    freed = cleaner.clean_results(safe_results)

    for r in safe_results:
        write_log(log_path, f"  已清理：{getattr(r, 'source', '')} "
                            f"({format_size(getattr(r, 'size', 0))})")

    if skipped:
        write_log(log_path, f"  跳过 {len(skipped)} 项需人工确认的类别："
                            + "、".join(sorted({getattr(r, 'category', '') for r in skipped})))

    write_log(log_path, f"=== 自动清理结束，实际释放 {format_size(freed)} ===")

    if not quiet:
        print(f"自动清理完成，释放 {format_size(freed)}")
        print(f"日志：{log_path}")
    return freed


def main(argv=None):
    parser = argparse.ArgumentParser(description="合枢 HS - 自动清理执行脚本")
    parser.add_argument("--full", action="store_true",
                        help="执行完整清理（含微信/QQ 缓存）")
    parser.add_argument("--quick", action="store_true",
                        help="执行快速清理（默认）")
    parser.add_argument("--log", default=None, help="日志文件路径")
    parser.add_argument("--quiet", action="store_true", help="不输出到控制台")
    args = parser.parse_args(argv)

    mode = "full" if args.full else ("quick" if args.quick else "quick")
    try:
        run(mode=mode, log_path=args.log, quiet=args.quiet)
        return 0
    except Exception as e:
        write_log(args.log or default_log_path(), f"自动清理失败：{e}")
        if not args.quiet:
            print(f"自动清理失败：{e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
