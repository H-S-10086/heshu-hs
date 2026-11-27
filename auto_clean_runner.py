# -*- coding: utf-8 -*-
"""
自动清理执行脚本（供计划任务调用，无界面）

用法：
    python auto_clean_runner.py [--quick|--full] [--include-social]
                                [--social-min-age-days 30] [--log 日志路径]

设计说明：
  - 默认只清"系统垃圾"，不碰社交缓存、隐私清理、注册表与不可逆操作
  - 需要清理微信/QQ 缓存时必须显式 --include-social，并且只删早于
    --social-min-age-days（默认 30 天）的文件；已接收文件/附件默认不在
    扫描范围内（交互界面需额外勾选）
  - 所有动作写入日志文件，可在 用户目录\\CleanMasterBackup\\auto_clean.log 查看
  - 计划任务由「启动项管理」页的「自动清理计划」创建
"""

import os
import sys
import argparse
from datetime import datetime

from scanner import ScanEngine, format_size, set_social_include_files
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


def run(mode="quick", log_path=None, quiet=False, include_social=False,
        social_min_age_days=30):
    """
    执行自动清理，返回实际释放的字节数。

    默认只清系统垃圾；社交缓存必须显式 include_social=True，
    且只删除超过 social_min_age_days 天的文件。
    """
    log_path = log_path or default_log_path()
    write_log(log_path, f"=== 自动清理开始（模式：{mode}，"
                        f"社交缓存：{'开' if include_social else '关'}） ===")

    # 无人值守永远不碰"已接收文件/附件"，只允许清理默认缓存目录
    set_social_include_files(False)

    engine = ScanEngine()
    if mode == "full":
        results = engine.scan_all()
    else:
        results = engine.scan_quick()

    # 默认安全集合只有系统垃圾；社交缓存需要显式开关，隐私清理永不由计划任务执行
    safe_categories = {"系统垃圾"}
    if include_social:
        safe_categories.update({"微信专清", "QQ专清"})

    safe_results = [r for r in results
                    if getattr(r, "category", "") in safe_categories
                    and getattr(r, "cleanable", True)]
    skipped = [r for r in results
               if getattr(r, "category", "") not in safe_categories
               or not getattr(r, "cleanable", True)]

    total_before = sum(getattr(r, "size", 0) for r in safe_results)
    write_log(log_path, f"扫描到 {len(results)} 项，其中可安全清理 {len(safe_results)} 项，"
                        f"预计 {format_size(total_before)}")
    if include_social:
        write_log(log_path, f"社交缓存只清理早于 {int(social_min_age_days)} 天的文件")

    cleaner = CleanEngine()
    freed = cleaner.clean_results(
        safe_results,
        social_min_age_days=int(social_min_age_days) if include_social else 0)

    for r in safe_results:
        write_log(log_path, f"  已清理：{getattr(r, 'source', '')} "
                            f"({format_size(getattr(r, 'size', 0))})")

    skipped_files = getattr(cleaner, "last_skipped", [])
    if skipped_files:
        write_log(log_path, f"  跳过 {len(skipped_files)} 个被占用/无权限文件，"
                            "请关闭微信/QQ/浏览器后重试")

    if skipped:
        write_log(log_path, f"  跳过 {len(skipped)} 项需人工确认的类别："
                            + "、".join(sorted({getattr(r, 'category', '') for r in skipped})))

    write_log(log_path, f"=== 自动清理结束，实际释放 {format_size(freed)} ===")

    if not quiet:
        print(f"自动清理完成，实际释放 {format_size(freed)}")
        print(f"日志：{log_path}")
    return freed


def main(argv=None):
    parser = argparse.ArgumentParser(description="合枢 HS - 自动清理执行脚本")
    parser.add_argument("--full", action="store_true",
                        help="完整扫描（默认仍只清系统垃圾，社交缓存需 --include-social）")
    parser.add_argument("--quick", action="store_true",
                        help="快速扫描（默认）")
    parser.add_argument("--include-social", action="store_true",
                        help="允许清理微信/QQ 缓存（需配合 --social-min-age-days，默认 30 天）")
    parser.add_argument("--social-min-age-days", type=int, default=30,
                        help="社交缓存只清理早于该天数的文件（默认 30）")
    parser.add_argument("--log", default=None, help="日志文件路径")
    parser.add_argument("--quiet", action="store_true", help="不输出到控制台")
    args = parser.parse_args(argv)

    mode = "full" if args.full else ("quick" if args.quick else "quick")
    try:
        run(mode=mode, log_path=args.log, quiet=args.quiet,
            include_social=args.include_social,
            social_min_age_days=max(1, args.social_min_age_days))
        return 0
    except Exception as e:
        write_log(args.log or default_log_path(), f"自动清理失败：{e}")
        if not args.quiet:
            print(f"自动清理失败：{e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
