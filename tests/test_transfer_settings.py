# -*- coding: utf-8 -*-
"""接收目录持久化回归测试"""
import os
import tempfile
import unittest
import urllib.parse

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


class PairDeeplinkTest(unittest.TestCase):
    def test_pair_deeplink_fields(self):
        from page_transfer import TransferPage
        url = TransferPage._pair_deeplink(
            "192.168.1.5", 8080, "123456", "测试电脑")
        self.assertTrue(url.startswith("cmlink://pair?"), url)
        query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        self.assertEqual(query["ip"][0], "192.168.1.5")
        self.assertEqual(query["port"][0], "8080")
        self.assertEqual(query["code"][0], "123456")
        self.assertEqual(query["name"][0], "测试电脑")


if __name__ == "__main__":
    unittest.main()
