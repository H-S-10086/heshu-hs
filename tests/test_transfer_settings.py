# -*- coding: utf-8 -*-
"""接收目录持久化回归测试"""
import os
import tempfile
import unittest

from file_transfer import TransferEngine


class TransferSettingsTest(unittest.TestCase):
    def test_save_dir_persists_across_engines(self):
        with tempfile.TemporaryDirectory(prefix="hs_settings_") as tmp:
            old = os.environ.get("USERPROFILE")
            os.environ["USERPROFILE"] = tmp
            try:
                e1 = TransferEngine(port=0)
                target = os.path.join(tmp, "自定义接收目录")
                ok, path = e1.set_save_dir(target)
                self.assertTrue(ok, path)
                self.assertTrue(os.path.isdir(target))

                # 模拟切换页面 / 重启：新引擎应读取上次保存的目录
                e2 = TransferEngine(port=0)
                self.assertEqual(os.path.normcase(e2.save_dir),
                                 os.path.normcase(target))
            finally:
                if old is None:
                    os.environ.pop("USERPROFILE", None)
                else:
                    os.environ["USERPROFILE"] = old


if __name__ == "__main__":
    unittest.main()
