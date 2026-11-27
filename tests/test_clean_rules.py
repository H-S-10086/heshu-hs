# -*- coding: utf-8 -*-
"""批次 1 回归测试：paths 列表、真实释放量、社交规则、空文件夹递归。"""
import os
import tempfile
import unittest

import scanner
from cleaner import CleanEngine
from sysreclaim import SysReclaimEngine


def touch(path, content=b"x"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(content)
    return len(content)


class PathsAndFreedTest(unittest.TestCase):
    def test_paths_list_cleans_all_not_first_five(self):
        with tempfile.TemporaryDirectory(prefix="hs_paths_") as tmp:
            paths = []
            total = 0
            # 7 个目录：旧实现只会删前 5 个（path 里只拼前 5 个）
            for i in range(7):
                d = os.path.join(tmp, "d%d" % i)
                os.makedirs(d)
                total += touch(os.path.join(d, "f.bin"), b"a" * 10)
                paths.append(d)
            r = scanner.ScanResult(
                category="微信专清", source="测试", path="; ".join(paths),
                paths=paths, size=total, social=True)
            engine = CleanEngine()
            freed = engine.clean_results([r])
            self.assertEqual(freed, total)
            for p in paths:
                self.assertFalse(os.listdir(p), p)
            self.assertEqual(engine.last_skipped, [])

    def test_locked_file_not_counted_as_freed(self):
        with tempfile.TemporaryDirectory(prefix="hs_lock_") as tmp:
            d = os.path.join(tmp, "cache")
            os.makedirs(d)
            free_size = touch(os.path.join(d, "a.bin"), b"a" * 32)
            locked = os.path.join(d, "b.bin")
            touch(locked, b"b" * 64)
            handle = open(locked, "r+b")
            try:
                engine = CleanEngine()
                freed = engine.clean_directory_contents(d)
                # 只统计真正删掉的 a.bin；b.bin 被占用，应保留并记录
                self.assertEqual(freed, free_size)
                self.assertTrue(os.path.exists(locked))
                self.assertTrue(any(p == locked for p, _ in engine.last_skipped))
            finally:
                handle.close()


class SocialRulesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="hs_social_")
        self.old_env = {k: os.environ.get(k) for k in
                        ("USERPROFILE", "APPDATA", "LOCALAPPDATA")}
        os.environ["USERPROFILE"] = self.tmp.name
        os.environ["APPDATA"] = os.path.join(self.tmp.name, "AppData", "Roaming")
        os.environ["LOCALAPPDATA"] = os.path.join(self.tmp.name, "AppData", "Local")
        self.root = self.tmp.name

    def tearDown(self):
        for k, v in self.old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        scanner.set_social_include_files(False)
        self.tmp.cleanup()

    def _build_tree(self):
        u = self.root
        # 微信 3.x
        touch(os.path.join(u, "Documents", "WeChat Files", "wxid_a", "Image", "a.jpg"))
        touch(os.path.join(u, "Documents", "WeChat Files", "wxid_a",
                           "FileStorage", "File", "recv.docx"))
        touch(os.path.join(u, "Documents", "WeChat Files", "wxid_a",
                           "Attachment", "att.pdf"))
        # 微信 4.0
        touch(os.path.join(u, "Documents", "xwechat_files", "wxid_b", "msg", "msg.db"))
        touch(os.path.join(u, "Documents", "xwechat_files", "wxid_b", "image", "b.jpg"))
        touch(os.path.join(u, "Documents", "xwechat_files", "wxid_b",
                           "attachment", "c.pdf"))
        # QQ 经典
        touch(os.path.join(u, "Documents", "Tencent Files", "10001", "Image", "q.jpg"))
        touch(os.path.join(u, "Documents", "Tencent Files", "10001", "FileRecv", "f.zip"))
        # QQ NT
        touch(os.path.join(u, "Documents", "Tencent Files", "10002",
                           "nt_qq", "nt_db", "msg.db"))
        touch(os.path.join(u, "Documents", "Tencent Files", "10002",
                           "nt_qq", "nt_data", "Pic", "p.jpg"))
        touch(os.path.join(u, "Documents", "Tencent Files", "10002",
                           "nt_qq", "nt_data", "File", "f.bin"))
        # AppData 配置
        touch(os.path.join(u, "AppData", "Roaming", "Tencent", "WeChat", "config.ini"))
        touch(os.path.join(u, "AppData", "Roaming", "Tencent", "QQ", "config.ini"))

    def _scan_paths(self):
        engine = scanner.ScanEngine()
        engine.scan_wechat()
        engine.scan_qq()
        clean = []
        hints = []
        for r in engine.results:
            if getattr(r, "cleanable", True):
                clean.extend(r.paths)
            else:
                hints.append(r)
        return clean, hints

    def test_default_excludes_received_files(self):
        self._build_tree()
        scanner.set_social_include_files(False)
        paths, hints = self._scan_paths()
        joined = "\n".join(paths)
        self.assertIn("Image", joined)
        self.assertIn(os.path.join("nt_data", "Pic"), joined)
        self.assertNotIn("FileStorage" + os.sep + "File", joined)
        self.assertNotIn("Attachment", joined)
        self.assertNotIn("FileRecv", joined)
        self.assertNotIn(os.path.join("nt_data", "File"), joined)
        # 消息库永不进入清理列表
        self.assertNotIn("nt_db", joined)
        self.assertNotIn(os.sep + "msg" + os.sep, joined)
        # AppData 只提示、不可清理
        self.assertTrue(hints)
        self.assertTrue(all(not h.cleanable for h in hints))
        self.assertTrue(any("AppData" in h.source or "配置" in h.source for h in hints))

    def test_opt_in_includes_received_files_but_never_msg_db(self):
        self._build_tree()
        scanner.set_social_include_files(True)
        paths, _hints = self._scan_paths()
        joined = "\n".join(paths)
        self.assertIn(os.path.join("FileStorage", "File"), joined)
        self.assertIn("Attachment", joined)
        self.assertIn("FileRecv", joined)
        self.assertIn(os.path.join("nt_data", "File"), joined)
        self.assertNotIn("nt_db", joined)
        self.assertNotIn(os.sep + "msg" + os.sep, joined)


class EmptyFoldersTest(unittest.TestCase):
    def test_recursive_empty_folders(self):
        with tempfile.TemporaryDirectory(prefix="hs_empty_") as tmp:
            os.makedirs(os.path.join(tmp, "a", "b"))
            os.makedirs(os.path.join(tmp, "d", "e"))
            touch(os.path.join(tmp, "c", "file.txt"))
            os.makedirs(os.path.join(tmp, "skip", "node_modules", "pkg"))

            engine = SysReclaimEngine()
            results = engine.scan_empty_folders(roots=[tmp])
            found = {os.path.normcase(r.path) for r in results}
            target = {
                os.path.normcase(os.path.join(tmp, "a")),
                os.path.normcase(os.path.join(tmp, "a", "b")),
                os.path.normcase(os.path.join(tmp, "d")),
                os.path.normcase(os.path.join(tmp, "d", "e")),
            }
            self.assertTrue(target.issubset(found), (target, found))
            self.assertNotIn(os.path.normcase(tmp), found)
            self.assertNotIn(os.path.normcase(os.path.join(tmp, "c")), found)
            self.assertNotIn(os.path.normcase(os.path.join(tmp, "skip")), found)

            deleted, failed = engine.clean_empty_folders([r.path for r in results])
            self.assertEqual(failed, [])
            self.assertEqual(deleted, len(target))
            self.assertFalse(os.path.exists(os.path.join(tmp, "a")))
            self.assertFalse(os.path.exists(os.path.join(tmp, "d")))
            self.assertTrue(os.path.isdir(os.path.join(tmp, "skip", "node_modules", "pkg")))


if __name__ == "__main__":
    unittest.main()
