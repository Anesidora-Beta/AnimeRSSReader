# -*- coding: utf-8 -*-
"""规则层测试：认网盘、抽链接、补提取码。

这一层是纯函数，所以用例可以写得很具体 —— 每条规则一组正反例，
改域名时改坏哪一条会立刻显形。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import rules  # noqa: E402


class Classify(unittest.TestCase):
    def test_bt_schemes(self):
        cases = {
            "magnet:?xt=urn:btih:abc&dn=x": "magnet",
            "MAGNET:?xt=urn:btih:abc": "magnet",
            "ed2k://|file|a.mkv|123|HASH|/": "ed2k",
            "thunder://QUFodHRwOi8v": "thunder",
        }
        for url, want in cases.items():
            self.assertEqual(rules.classify(url)[0], want, url)

    def test_cn_pans(self):
        cases = {
            "https://pan.baidu.com/s/1abc": "baidu",
            "https://yun.baidu.com/s/1abc": "baidu",
            "https://www.alipan.com/s/abc": "alipan",
            "https://www.aliyundrive.com/s/abc": "alipan",
            "https://pan.quark.cn/s/abc": "quark",
            "https://quark.cn/s/abc": "quark",
            "https://www.123pan.com/s/abc": "pan123",
            "https://123684.com/s/abc": "pan123",
            "https://123865.com/s/abc": "pan123",
            "https://123912.com/s/abc": "pan123",
            "https://pan.xunlei.com/s/abc": "xunlei",
            "https://115.com/s/abc": "pan115",
            "https://115cdn.com/s/abc": "pan115",
            "https://cloud.189.cn/t/abc": "tianyi",
            "https://caiyun.139.com/m/i?abc": "cmcc",
            "https://share.weiyun.com/abc": "weiyun",
            "https://drive.uc.cn/s/abc": "uc",
            "https://wwa.lanzouj.com/iabc": "lanzou",
            "https://wws.lanzoui.com/iabc": "lanzou",
            "https://mypikpak.com/s/abc": "pikpak",
            "https://www.terabox.com/s/abc": "terabox",
            "https://www.cowtransfer.com/s/abc": "cowtransfer",
            "https://www.wenshushu.cn/f/abc": "wenshushu",
        }
        for url, want in cases.items():
            self.assertEqual(rules.classify(url)[0], want, url)

    def test_overseas(self):
        cases = {
            "https://drive.google.com/file/d/x/view": "gdrive",
            "https://docs.google.com/spreadsheets/d/x/edit": "gdrive",
            "https://1drv.ms/u/s!abc": "onedrive",
            "https://mega.nz/file/abc": "mega",
            "https://www.dropbox.com/s/abc": "dropbox",
        }
        for url, want in cases.items():
            self.assertEqual(rules.classify(url)[0], want, url)

    def test_file_and_torrent_suffix(self):
        self.assertEqual(
            rules.classify("https://acg.rip/t/366344.torrent")[0], "torrent")
        self.assertEqual(
            rules.classify("https://x.com/a.mkv?token=1")[0], "file")
        self.assertEqual(
            rules.classify("https://x.com/a.ZIP")[0], "file")

    def test_plain_page_is_web(self):
        for url in ("https://bgm.tv/subject/1", "https://t.me/+abc",
                    "https://jq.qq.com/?_wv=1027"):
            self.assertEqual(rules.classify(url)[0], "web", url)

    def test_unknown_scheme_falls_back_to_web(self):
        """认不出来一律 web：宁可只让「打开/复制」，也不猜。"""
        self.assertEqual(rules.classify("ftp://x/y")[0], "web")
        self.assertEqual(rules.classify("javascript:alert(1)")[0], "web")
        self.assertEqual(rules.classify("")[0], "web")

    def test_nothing_raises_on_weird_input(self):
        for url in ("   ", "\x00", "https://", "magnet:" * 50,
                    "https://a.com/" + "x" * 4000):
            rules.classify(url)                # 只要不抛异常即可

    def test_trailing_punctuation_stripped(self):
        pid, url = rules.classify("https://pan.baidu.com/s/1abc。")
        self.assertEqual(pid, "baidu")
        self.assertFalse(url.endswith("。"))

    def test_html_entity_unescaped(self):
        _, url = rules.classify("https://a.com/x?a=1&amp;b=2")
        self.assertIn("&b=2", url)

    def test_every_provider_has_metadata(self):
        for p in rules.PROVIDERS:
            for key in ("id", "name", "badge", "kind", "color", "note"):
                self.assertIn(key, p, p.get("id"))
            self.assertIn(p["kind"], ("bt", "pan", "file", "web"))


class Extract(unittest.TestCase):
    def item(self, **kw):
        base = {"id": "x", "title": "t", "summary": "", "magnet": "",
                "torrent": "", "link": "", "links": []}
        base.update(kw)
        return base

    def test_order_magnet_then_torrent_then_links(self):
        it = self.item(
            magnet="magnet:?xt=urn:btih:aaa",
            torrent="https://x.com/a.torrent",
            links=["https://pan.baidu.com/s/1abc", "https://bgm.tv/subject/1"],
            link="https://bangumi.moe/torrent/1")
        got = [x["pid"] for x in rules.extract(it)]
        self.assertEqual(got[:4], ["magnet", "torrent", "baidu", "web"])

    def test_dedupe(self):
        it = self.item(magnet="magnet:?xt=urn:btih:aaa",
                       links=["magnet:?xt=urn:btih:aaa"],
                       summary="再贴一次 magnet:?xt=urn:btih:aaa")
        self.assertEqual(len(rules.extract(it)), 1)

    def test_include_page_false_drops_web(self):
        it = self.item(links=["https://bgm.tv/subject/1"],
                       link="https://bangumi.moe/torrent/1",
                       summary="看看 https://t.me/+abc")
        self.assertEqual(rules.extract(it, include_page=False), [])

    def test_summary_urls_extracted(self):
        it = self.item(summary=(
            "下载见 https://pan.quark.cn/s/abc ，备用 https://www.alipan.com/s/xyz 。"
            "群 https://t.me/+qqq"))
        pids = [x["pid"] for x in rules.extract(it)]
        self.assertIn("quark", pids)
        self.assertIn("alipan", pids)
        # 中文标点不能吃进 URL
        for x in rules.extract(it):
            self.assertNotIn("，", x["url"])
            self.assertNotIn("。", x["url"])

    def test_pwd_from_summary(self):
        """真实数据里最常见的样子：链接不带 pwd，提取码写在摘要。"""
        it = self.item(
            links=["https://pan.baidu.com/s/1bKEvn"],
            summary="提取码: orio\n解压密码：zzzz")
        links = rules.extract(it)
        baidu = [x for x in links if x["pid"] == "baidu"][0]
        self.assertIn("pwd=orio", baidu["url"])
        self.assertEqual(baidu["pwd"], "orio")

    def test_pwd_kept_when_already_present(self):
        it = self.item(links=["https://pan.baidu.com/s/1abc?pwd=1234"],
                       summary="提取码: abcd")
        baidu = rules.extract(it)[0]
        self.assertIn("pwd=1234", baidu["url"])
        self.assertNotIn("abcd", baidu["url"])

    def test_pwd_disabled(self):
        it = self.item(links=["https://pan.baidu.com/s/1abc"],
                       summary="提取码: orio")
        self.assertEqual(rules.extract(it, allow_pwd=False)[0]["pwd"], "")

    def test_pwd_disabled_still_scans_summary(self):
        """关掉「自动补提取码」只该关掉补码，不该连摘要里的链接都不抽了。

        这两件事共用同一段文本，一度被一起清空 —— 用户关掉补码后，
        条目里的链接会凭空少掉一半。
        """
        it = self.item(summary="盘：https://pan.baidu.com/s/1abc 提取码: orio")
        got = rules.extract(it, allow_pwd=False)
        self.assertTrue(got, "摘要里的链接被整段丢掉了")
        self.assertEqual(got[0]["pid"], "baidu")
        self.assertEqual(got[0]["pwd"], "")

    def test_pwd_not_added_to_other_pans(self):
        it = self.item(links=["https://pan.quark.cn/s/abc"],
                       summary="提取码: orio")
        self.assertEqual(rules.extract(it)[0]["pwd"], "")

    def test_empty_item(self):
        self.assertEqual(rules.extract(None), [])
        self.assertEqual(rules.extract({}), [])


class Grouping(unittest.TestCase):
    def test_bt_before_pan(self):
        it = {"magnet": "magnet:?xt=urn:btih:a",
              "links": ["https://pan.quark.cn/s/abc",
                        "https://pan.baidu.com/s/1x"],
              "summary": "", "title": "", "torrent": "", "link": ""}
        groups = rules.group(rules.extract(it))
        self.assertEqual(groups[0]["kind"], "bt")
        self.assertEqual([g["pid"] for g in groups][1:], ["baidu", "quark"])

    def test_multiple_urls_in_one_group(self):
        it = {"links": ["https://pan.quark.cn/s/a", "https://pan.quark.cn/s/b"],
              "magnet": "", "torrent": "", "summary": "", "title": "", "link": ""}
        groups = rules.group(rules.extract(it))
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["count"], 2)
        self.assertEqual(len(groups[0]["urls"]), 2)

    def test_pwd_carried_to_group(self):
        it = {"links": ["https://pan.baidu.com/s/1abc"], "summary": "提取码: qwer",
              "magnet": "", "torrent": "", "title": "", "link": ""}
        groups = rules.group(rules.extract(it))
        self.assertEqual(groups[0]["pwd"], "qwer")

    def test_group_empty(self):
        self.assertEqual(rules.group([]), [])


class Size(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(rules.parse_size("1.5 GB"), int(1.5 * 1024 ** 3))
        self.assertEqual(rules.parse_size("800MB"), 800 * 1024 ** 2)
        self.assertEqual(rules.parse_size("1024"), 1024)
        self.assertEqual(rules.parse_size(""), 0)
        self.assertEqual(rules.parse_size("未知"), 0)


class Defaults(unittest.TestCase):
    def test_bt_defaults_to_system_handler(self):
        self.assertEqual(rules.target_of("magnet")["mode"], "open")

    def test_pan_defaults_to_browser(self):
        self.assertEqual(rules.target_of("baidu")["mode"], "web")

    def test_unknown_pid_does_not_crash(self):
        t = rules.target_of("nope")
        self.assertIn("mode", t)


if __name__ == "__main__":
    unittest.main()
