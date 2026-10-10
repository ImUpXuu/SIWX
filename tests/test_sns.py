import hashlib
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from siwx import sns, sns_cdn, sns_isaac64
from tests._base import IsolatedRootCase
from tests import fixtures as fx


class TestSnsPrimitives(unittest.TestCase):
    def test_official_isaac_vector(self):
        self.assertTrue(sns_isaac64.self_test())

    def test_signed_tid_roundtrip(self):
        tid = -3726233932341759299
        self.assertEqual(sns.sns_id_to_ms(tid), 1754821555777)
        self.assertEqual(sns.sns_id_to_seconds(tid), 1754821555)

    def test_media_url_uses_attribute_token(self):
        url = "http://shmmsns.qpic.cn/mmsns/abc/150?token=old&idx=0"
        got = sns_cdn.build_media_url(url, "attribute-token")
        self.assertTrue(got.startswith("https://shmmsns.qpic.cn/mmsns/abc/0?"))
        self.assertIn("token=attribute-token", got)
        self.assertIn("idx=1", got)
        self.assertNotIn("token=old", got)

    def test_cache_key_ignores_rotating_tokens(self):
        a = "https://h.example/mmsns/a/0?token=one&idx=1"
        b = "https://h.example/mmsns/a/0?token=two&idx=1"
        self.assertEqual(sns_cdn.cache_key(a), sns_cdn.cache_key(b))

    def test_parse_timeline_media_attributes(self):
        xml = (
            "<SnsDataItem><TimelineObject>"
            "<id>x</id><username>wxid_a</username><createTime>1700000000</createTime>"
            "<ContentObject><type>2</type><mediaList><media>"
            "<id>1</id><type>2</type><size width='100' height='80' totalSize='9'/>"
            "<url md5='a'*32 token='tok' key='123'>https://x/150</url>"
            "</media></mediaList></ContentObject>"
            "</TimelineObject></SnsDataItem>"
        ).replace("'a'*32", "'" + "a" * 32 + "'")
        feed = sns.parse_timeline(xml)
        self.assertEqual(feed["content_kind"], "image")
        self.assertEqual(feed["medias"][0]["token"], "tok")
        self.assertEqual(feed["medias"][0]["width"], 100)

    def test_host_fallback_candidates(self):
        url = "https://shmmsns.qpic.cn/mmsns/a/0?token=t&idx=1"
        cands = sns_cdn._host_candidates(url)
        self.assertEqual(cands[0], url)
        self.assertGreater(len(cands), 1)

    def test_disk_cache_roundtrip(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            url = "https://h.example/mmsns/a/0?token=x&idx=1"
            self.assertIsNone(sns_cdn.read_cached(d, url))
            p = sns_cdn.cached_path(d, url, "jpg")
            self.assertIsNotNone(p)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(b"\xff\xd8\xffDATA")
            data, ext, mime = sns_cdn.read_cached(d, url)
            self.assertEqual(data, b"\xff\xd8\xffDATA")
            self.assertEqual(ext, "jpg")

    def test_livephoto_parsed(self):
        """实况照片：liveMedia 是嵌套媒体，key 在 <enc key> 而不是 url@key。"""
        import xml.etree.ElementTree as ET
        xml = ("<media><type>2</type><size width='1920' height='1920' totalSize='398618'/>"
               "<url token='t1' key='k1' enc_idx='1'>http://h/mmsns/a/0</url>"
               "<LivePhoto><liveMedia>"
               "<id>0</id><type>6</type><subType>0</subType>"
               "<videoSize width='0' height='0'/>"
               "<url type='1' md5='" + "c" * 32 + "'>http://h/102/20202/snsvideodownload?encfilekey=x</url>"
               "<thumb type='1'>http://h/150/snsvideodownload?encfilekey=x</thumb>"
               "<size width='288' height='288' totalSize='9884'/>"
               "<videoDuration>2.37800002</videoDuration>"
               "<liveStillImageTimeMs>734</liveStillImageTimeMs>"
               "<enc key='1884729990'>1</enc>"
               "</liveMedia></LivePhoto></media>")
        d = sns._parse_media_el(ET.fromstring(xml))
        # 主图正常
        self.assertEqual(d["width"], 1920)
        self.assertEqual(d["key"], "k1")
        self.assertIsNone(d["enc_key"])          # 主图没有 <enc>

        # 单独解析 liveMedia
        lp_el = ET.fromstring(xml).find("LivePhoto/liveMedia")
        lp = sns._parse_media_el(lp_el)
        self.assertEqual(lp["type"], 6)
        self.assertEqual(lp["width"], 288)
        self.assertEqual(lp["height"], 288)
        self.assertEqual(lp["total_size"], 9884)
        self.assertEqual(lp["video_duration"], "2.37800002")
        self.assertEqual(lp["live_still_ms"], 734)
        # ⚠️ 关键：key 来自 <enc key>，不是 url@key
        self.assertEqual(lp["key"], "1884729990")
        self.assertEqual(lp["enc_key"], "1884729990")
        self.assertEqual(lp["md5"], "c" * 32)
        self.assertIn("snsvideodownload", lp["url"])

    def test_livephoto_attached_to_media(self):
        """parse_timeline 应把 LivePhoto 挂到对应 media 上。"""
        base = ("<SnsDataItem><TimelineObject><id>1</id><username>u</username>"
                "<createTime>1700000000</createTime><ContentObject><type>2</type>"
                "<mediaList><media><type>2</type>"
                "<url token='t' key='k'>http://h/mmsns/a/0</url>"
                "<LivePhoto><liveMedia><type>6</type>"
                "<url>" + "http://h/snsvideodownload?x" + "</url>"
                "<enc key='999'>1</enc></liveMedia></LivePhoto>"
                "</media></mediaList></ContentObject></TimelineObject></SnsDataItem>")
        feed = sns.parse_timeline(base)
        m = feed["medias"][0]
        self.assertIn("live_photo", m)
        self.assertEqual(m["live_photo"]["key"], "999")
        self.assertEqual(m["live_photo"]["type"], 6)

    def test_media_without_livephoto_has_no_key(self):
        base = ("<SnsDataItem><TimelineObject><id>1</id><username>u</username>"
                "<createTime>1700000000</createTime><ContentObject><type>2</type>"
                "<mediaList><media><type>2</type>"
                "<url token='t' key='k'>http://h/mmsns/a/0</url>"
                "</media></mediaList></ContentObject></TimelineObject></SnsDataItem>")
        m = sns.parse_timeline(base)["medias"][0]
        self.assertNotIn("live_photo", m)

    def test_strip_tail(self):
        body = b"\xff\xd8\xffDATA\xff\xd9"
        tail = b"\x75\xf0\xd3\x3c\x00\x00\x00\x00" + hashlib.md5(body).digest()
        self.assertEqual(sns_cdn.strip_wechat_tail(body + tail), body)


def _to_signed64(v: int) -> int:
    """把无符号 64 位值转成 SQLite 存储的有符号 int64。"""
    v &= 0xFFFFFFFFFFFFFFFF
    return v - (1 << 64) if v >= (1 << 63) else v


def _make_sns_db(path: Path, posts):
    """建一个最小的 sns.db（SnsTimeLine 只有 tid/user_name/content）。"""
    import sqlite3
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE SnsTimeLine(tid INTEGER PRIMARY KEY DESC, "
                "user_name TEXT, content TEXT, pack_info_buf TEXT)")
    for tid, user, text in posts:
        xml = (f"<SnsDataItem><TimelineObject><id>{tid}</id>"
               f"<username>{user}</username><createTime>1700000000</createTime>"
               f"<contentDesc>{text}</contentDesc>"
               f"<ContentObject><type>1</type><mediaList/></ContentObject>"
               f"</TimelineObject></SnsDataItem>")
        con.execute("INSERT INTO SnsTimeLine VALUES (?,?,?,?)", (tid, user, xml, ""))
    con.commit()
    con.close()


def _make_sns_db_raw(path: Path, posts):
    """建一个最小的 sns.db，posts = ``[(tid, user_name, xml)]``（原样存 XML）。"""
    import sqlite3
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE SnsTimeLine(tid INTEGER PRIMARY KEY DESC, "
                "user_name TEXT, content TEXT, pack_info_buf TEXT)")
    for tid, user, xml in posts:
        con.execute("INSERT INTO SnsTimeLine VALUES (?,?,?,?)", (tid, user, xml, ""))
    con.commit()
    con.close()


class TestSnsExport(unittest.TestCase):
    def setUp(self):
        import tempfile
        # Windows 上临时目录清理偶尔会被索引/杀软瞬时占用，不应让测试变红
        self._tmp = tempfile.TemporaryDirectory(prefix="siwx_sns_exp_",
                                                ignore_cleanup_errors=True)
        self.tmp = Path(self._tmp.name)
        self.db = self.tmp / "sns" / "sns.db"
        # 构造合法 tid：(createTime_ms << 23) | rand，再转有符号 int64 存储
        base = (1700000000 * 1000) << 23
        _make_sns_db(self.db, [(_to_signed64(base + 1), "wxid_a", "第一条动态 hello"),
                               (_to_signed64(base + 2), "wxid_b", "第二条动态 world")])

    def tearDown(self):
        self._tmp.cleanup()

    def test_export_json_and_markdown(self):
        from siwx import sns_export as E
        for fmt, suffix in (("json", ".json"), ("markdown", ".md"),
                            ("txt", ".txt"), ("html", ".html")):
            with self.subTest(fmt=fmt):
                r = E.run_sns_export(self.db, fx.DEMO_SELF, fmt=fmt,
                                     export_root=self.tmp / "out")
                self.assertTrue(r["ok"], r.get("error"))
                self.assertEqual(r["count"], 2)
                f = Path(r["file"])
                self.assertTrue(f.is_file())
                self.assertEqual(f.suffix, suffix)
                self.assertGreater(f.stat().st_size, 0)

    def test_export_keyword_filter(self):
        from siwx import sns_export as E
        r = E.run_sns_export(self.db, fx.DEMO_SELF, fmt="json",
                             export_root=self.tmp / "out", keyword="world")
        self.assertEqual(r["count"], 1)

    def test_export_author_filter(self):
        from siwx import sns_export as E
        r = E.run_sns_export(self.db, fx.DEMO_SELF, fmt="json",
                             export_root=self.tmp / "out", usernames=["wxid_a"])
        self.assertEqual(r["count"], 1)

    def test_export_rejects_unknown_format(self):
        from siwx import sns_export as E
        r = E.run_sns_export(self.db, fx.DEMO_SELF, fmt="yaml",
                             export_root=self.tmp / "out")
        self.assertFalse(r["ok"])

    def test_export_empty_result(self):
        """筛选后没有动态 = 空结果成功（旧契约 ok=False 会被 UI 渲染成红色"导出失败"）。"""
        from siwx import sns_export as E
        r = E.run_sns_export(self.db, fx.DEMO_SELF, fmt="json",
                             export_root=self.tmp / "out", keyword="不存在的内容")
        self.assertTrue(r["ok"])
        self.assertTrue(r["empty"])
        self.assertEqual(r["count"], 0)
        self.assertIsNone(r["file"])


_EMOJI_XML = (
    "<SnsDataItem><TimelineObject><id>1</id><username>wxid_a</username>"
    "<createTime>1700000000</createTime><contentDesc>带表情的评论</contentDesc>"
    "<ContentObject><type>1</type><mediaList/></ContentObject></TimelineObject>"
    "<LocalExtraInfo><comment_user_list><user_comment>"
    "<username>wxid_b</username><nickname>小明</nickname>"
    "<content>哈哈</content><create_time>1700000001</create_time><type>2</type>"
    "<emojilist><emojiinfo><md5>" + "a" * 32 + "</md5>"
    "<width>86</width><height>86</height><size>1782</size>"
    "<sns_emoji_data>"
    "<url>http://vweixinf.tc.qq.com/110/20401/stodownload?m=abc</url>"
    "<thumb_url>http://vweixinf.tc.qq.com/110/20401/thumb</thumb_url>"
    "<encrypt_url>http://wxapp.tc.qq.com/262/20304/stodownload?m=def</encrypt_url>"
    "<aes_key>" + "d0" * 16 + "</aes_key>"
    "<extern_md5>" + "e4" * 16 + "</extern_md5>"
    "</sns_emoji_data></emojiinfo></emojilist>"
    "<imagelist><imageinfo>"
    "<url token='ctok' key='12345' enc_idx='1' md5='" + "b" * 32 + "'>"
    "http://shmmsns.qpic.cn/mmcomment/abc/0</url>"
    "<thumb_url token='ctok2' key='12345'>http://shmmsns.qpic.cn/mmcomment/abc/60</thumb_url>"
    "<width>964</width><height>1208</height><file_size>96147</file_size>"
    "<media_id>14817026313653858851</media_id><md5>" + "b" * 32 + "</md5>"
    "</imageinfo></imagelist>"
    "</user_comment></comment_user_list>"
    "<like_user_list><user_comment><username>wxid_c</username>"
    "<nickname>小红</nickname></user_comment></like_user_list>"
    "</LocalExtraInfo></SnsDataItem>"
)


class TestSnsInteraction(unittest.TestCase):
    """评论 / 点赞 / 表情的解析。"""

    def test_comment_and_like_split(self):
        feed = sns.parse_timeline(_EMOJI_XML)
        self.assertEqual(len(feed["comments"]), 1, "有内容+表情的应归为评论")
        self.assertEqual(len(feed["likes"]), 1, "无内容无表情的应归为点赞")
        c = feed["comments"][0]
        self.assertEqual(c["nickname"], "小明")
        self.assertEqual(c["content"], "哈哈")

    def test_emoji_fields_extracted(self):
        feed = sns.parse_timeline(_EMOJI_XML)
        e = feed["comments"][0]["emojis"][0]
        self.assertEqual(e["md5"], "a" * 32)
        self.assertEqual(e["width"], 86)
        self.assertIn("vweixinf.tc.qq.com", e["url"])
        self.assertIn("wxapp.tc.qq.com", e["encrypt_url"])
        self.assertEqual(e["aes_key"], "d0" * 16)
        self.assertEqual(e["extern_md5"], "e4" * 16)
        # 兼容字段仍在
        self.assertEqual(feed["comments"][0]["emoji_md5"], "a" * 32)

    def test_emoji_cached_in_export(self):
        from siwx import sns_export as E
        feed = sns.parse_timeline(_EMOJI_XML)
        d = E._feed_to_dict(feed)
        self.assertEqual(len(d["comments"][0]["emojis"]), 1)
        self.assertEqual(d["comments"][0]["emojis"][0]["md5"], "a" * 32)

    def test_concurrency_clamped(self):
        from siwx import sns_export as E
        self.assertEqual(E.clamp_concurrency(None), E.DEFAULT_CONCURRENCY)
        self.assertEqual(E.clamp_concurrency("abc"), E.DEFAULT_CONCURRENCY)
        self.assertEqual(E.clamp_concurrency(0), 1)
        self.assertEqual(E.clamp_concurrency(-5), 1)
        self.assertEqual(E.clamp_concurrency(999), E.MAX_CONCURRENCY)
        self.assertEqual(E.clamp_concurrency(3), 3)
        self.assertEqual(E.clamp_concurrency("7"), 7)

    def test_fetch_emoji_rejects_empty(self):
        from siwx import sns_cdn
        r = sns_cdn.fetch_emoji({})
        self.assertFalse(r["ok"])

    def test_comment_image_parsed_like_media(self):
        """评论内嵌图片与主图结构一致，token/key/md5 都要解析出来。"""
        feed = sns.parse_timeline(_EMOJI_XML)
        imgs = feed["comments"][0]["images"]
        self.assertEqual(len(imgs), 1)
        im = imgs[0]
        self.assertEqual(im["width"], 964)
        self.assertEqual(im["height"], 1208)
        self.assertEqual(im["total_size"], 96147)
        self.assertEqual(im["md5"], "b" * 32)
        self.assertEqual(im["token"], "ctok")          # 下载必需
        self.assertEqual(im["key"], "12345")
        self.assertEqual(im["enc_idx"], 1)
        self.assertIn("/mmcomment/", im["url"])
        self.assertEqual(im["type"], 2)

    def test_location_zero_coords_filtered(self):
        """实测多数动态的 location 是 0,0 占位，不应当成有效位置。"""
        base = ("<SnsDataItem><TimelineObject><id>1</id><username>u</username>"
                "<createTime>1700000000</createTime>"
                "<ContentObject><type>1</type><mediaList/></ContentObject>{loc}"
                "</TimelineObject></SnsDataItem>")
        zero = base.format(loc="<location latitude='0' longitude='0'/>")
        self.assertIsNone(sns.parse_timeline(zero)["location"])

        named = base.format(
            loc="<location latitude='0' longitude='0' poiName='某地' poiAddress='某路1号'/>")
        loc = sns.parse_timeline(named)["location"]
        self.assertEqual(loc["name"], "某地")
        self.assertEqual(loc["address"], "某路1号")

        coord = base.format(loc="<location latitude='31.23' longitude='121.47'/>")
        self.assertIsNotNone(sns.parse_timeline(coord)["location"])

    def test_media_el_handles_both_shapes(self):
        """media（<size> 属性）与 imageinfo（扁平字段）都要能解析。"""
        import xml.etree.ElementTree as ET
        m = ET.fromstring(
            "<media><type>2</type>"
            "<size width='100' height='80' totalSize='9'/>"
            "<url token='t1' key='k1' enc_idx='1'>http://h/mmsns/a/0</url>"
            "<thumb>http://h/mmsns/a/150</thumb></media>")
        d = sns._parse_media_el(m)
        self.assertEqual((d["width"], d["height"], d["total_size"]), (100, 80, 9))
        self.assertEqual(d["token"], "t1")
        self.assertEqual(d["thumb_url"], "http://h/mmsns/a/150")   # <thumb>
        self.assertEqual(d["enc_idx"], 1)

        ii = ET.fromstring(
            "<imageinfo><width>50</width><height>40</height><file_size>7</file_size>"
            "<thumb_url>http://h/mmcomment/a/60</thumb_url>"
            "<url token='t2'>http://h/mmcomment/a/0</url></imageinfo>")
        d2 = sns._parse_media_el(ii)
        self.assertEqual((d2["width"], d2["height"], d2["total_size"]), (50, 40, 7))
        self.assertEqual(d2["token"], "t2")
        self.assertEqual(d2["thumb_url"], "http://h/mmcomment/a/60")  # <thumb_url>
        self.assertIsNone(d2["key"])


class TestSnsCardParsing(unittest.TestCase):
    """卡片类动态（链接 / 视频号 / 直播 / 音乐 / 笔记）。

    ⭐ 关键事实（实测 5684 条真实库，见 docs/sns-todo.md §1）：
    **不存在 ``<appmsg>`` 节点**，卡片字段是 ``<ContentObject>`` 的直接子元素；
    而且 type 编号与语义会漂移（type 42/47 是音乐、34 是直播、26 是笔记），
    所以 ``card["kind"]`` 必须由**实际字段**推导，不能只看 type。
    """

    @staticmethod
    def _wrap(ctype: str, co_inner: str, desc: str = "", extra: str = "") -> str:
        return ("<SnsDataItem><TimelineObject><id>1</id><username>wxid_a</username>"
                "<createTime>1700000000</createTime>"
                f"<contentDesc>{desc}</contentDesc>"
                f"<ContentObject><type>{ctype}</type>{co_inner}</ContentObject>"
                f"{extra}</TimelineObject></SnsDataItem>")

    def test_link_card_fields(self):
        xml = self._wrap("3",
                         "<title>占位直播标题甲</title>"
                         "<description>7月29日 19:30</description>"
                         "<contentUrl>https://mp.weixin.qq.com/s?__biz=abc</contentUrl>"
                         "<mediaList><media><type>2</type>"
                         "<url token='t' key='k'>http://h/mmsns/cov/0</url>"
                         "</media></mediaList>",
                         extra="<sourceNickName>占位来源甲</sourceNickName>")
        feed = sns.parse_timeline(xml)
        card = feed["card"]
        self.assertEqual(card["kind"], "link")
        self.assertEqual(card["title"], "占位直播标题甲")
        self.assertEqual(card["description"], "7月29日 19:30")
        self.assertIn("mp.weixin.qq.com", card["content_url"])
        self.assertEqual(card["source"], "占位来源甲")
        self.assertEqual(card["cover_url"], "http://h/mmsns/cov/0")

    def test_music_card_kind_from_fields_not_type(self):
        """type 42 在早期文档里叫 finder_live，实际是音乐 —— kind 必须按字段判定。"""
        xml = self._wrap("42",
                         "<title>占位歌曲甲</title><description>占位歌手甲、占位歌手乙</description>"
                         "<contentUrl>https://t1.kugou.com/abc</contentUrl>"
                         "<musicShareItem><mvSingerName>占位歌手甲</mvSingerName>"
                         "<mvAlbumName>占位歌曲甲</mvAlbumName>"
                         "<musicDuration>226000</musicDuration></musicShareItem>")
        card = sns.parse_timeline(xml)["card"]
        self.assertEqual(card["kind"], "music")
        self.assertEqual(card["music"]["singer"], "占位歌手甲")
        self.assertEqual(card["music"]["album"], "占位歌曲甲")
        self.assertEqual(card["music"]["duration_ms"], 226000)

    def test_finder_feed_card(self):
        xml = self._wrap(
            "28",
            "<description>占位视频文案甲</description>"
            "<finderFeed><objectId>14711924164078868603</objectId>"
            "<feedType>4</feedType><nickname>占位主播甲</nickname>"
            "<avatar>http://wx.qlogo.cn/finderhead/abc</avatar>"
            "<mediaCount>1</mediaCount>"
            "<username>v2_060000231003b20f@finder</username>"
            "<mediaList>"
            # 第一条只有封面（实测很多 finderFeed 的 media 没有 <url>），
            # 视频地址要单独往后找，不能只认第一条
            "<media><mediaType>4</mediaType>"
            "<thumbUrl>http://wxapp.tc.qq.com/251/20304/stodownload?filekey=t</thumbUrl>"
            "<coverUrl>http://wxapp.tc.qq.com/251/20304/stodownload?filekey=c</coverUrl>"
            # 实测尺寸字段可能是 "1080.0"（浮点字符串）
            "<width>1080.0</width><height>608.0</height>"
            "<videoPlayDuration>255</videoPlayDuration></media>"
            "<media><mediaType>4</mediaType>"
            "<url>http://wxapp.tc.qq.com/251/20302/stodownload?encfilekey=x</url>"
            "<width>1080</width><height>608</height>"
            "<videoPlayDuration>255</videoPlayDuration></media>"
            "</mediaList></finderFeed>")
        feed = sns.parse_timeline(xml)
        card = feed["card"]
        self.assertEqual(card["kind"], "finder")
        self.assertEqual(card["cover_width"], 1080)
        self.assertEqual(card["cover_height"], 608)
        self.assertEqual(card["duration_s"], 255)
        self.assertIn("filekey=c", card["cover_url"])
        # ⭐ 视频地址来自第二条 media（第一条没有 <url>）
        self.assertIn("encfilekey=x", card["video_url"])
        f = card["finder"]
        self.assertEqual(f["nickname"], "占位主播甲")
        self.assertEqual(f["media_count"], 1)
        self.assertEqual(f["medias"][0]["media_type"], 4)
        self.assertEqual(f["medias"][0]["duration_s"], 255)
        # finderFeed 的媒体**不是**主 mediaList，不应混进 medias
        self.assertEqual(feed["medias"], [])

    def test_finder_live_card(self):
        xml = self._wrap("34",
                         "<finderLive><finderLiveID>2042905931828856520</finderLiveID>"
                         "<nickname>占位媒体甲</nickname>"
                         "<coverUrl>https://wxapp.tc.qq.com/251/20304/stodownload?encfilekey=y</coverUrl>"
                         "<desc>占位活动甲</desc><liveStatus>1</liveStatus>"
                         "<media><coverUrl>https://wxapp.tc.qq.com/251/20304/stodownload?encfilekey=y</coverUrl>"
                         "<width>1440</width><height>1920</height></media></finderLive>")
        card = sns.parse_timeline(xml)["card"]
        self.assertEqual(card["kind"], "live")
        self.assertEqual(card["live"]["nickname"], "占位媒体甲")
        self.assertEqual(card["live"]["desc"], "占位活动甲")
        self.assertEqual(card["live"]["status"], 1)
        self.assertEqual(card["cover_width"], 1440)

    def test_note_card(self):
        xml = self._wrap("26",
                         "<title>占位笔记标题甲</title><description>note description</description>"
                         "<noteinfo><edittime>1770794672</edittime><datalist count='2'>"
                         "<dataitem datatype='1' dataid='a'><datadesc>正文第一段</datadesc></dataitem>"
                         "<dataitem datatype='2' dataid='b'><datasize>1234</datasize></dataitem>"
                         "</datalist></noteinfo>")
        card = sns.parse_timeline(xml)["card"]
        self.assertEqual(card["kind"], "note")
        self.assertEqual(card["note"]["text"], "正文第一段")
        self.assertEqual(card["note"]["image_count"], 1)
        self.assertEqual(card["note"]["edit_time"], 1770794672)

    def test_plain_text_post_has_no_card(self):
        xml = self._wrap("1", "<mediaList/>", desc="只有正文")
        feed = sns.parse_timeline(xml)
        self.assertIsNone(feed["card"])
        # 没有卡片字段的 type 7/54（实测 117 + 91 条）同样为 None
        self.assertIsNone(sns.parse_timeline(self._wrap("7", "<mediaList/>"))["card"])

    def test_public_card_shape_is_stable(self):
        """对外形状由 `public_card` 统一，内部字段改名不能漏到 API/导出。

        （开发中踩过：前端读 ``card.cover``，而 API 直接透传内部 ``cover_url`` →
        封面静默不显示，且当时的测试喂的是导出形状所以没发现。）
        """
        xml = self._wrap(
            "28",
            "<description>文案</description><finderFeed><nickname>昵称</nickname>"
            "<mediaCount>2</mediaCount>"
            "<mediaList><media><mediaType>4</mediaType><coverUrl>http://h/c</coverUrl>"
            "<url>http://h/v</url><width>100</width><height>80</height>"
            "<videoPlayDuration>12</videoPlayDuration></media></mediaList></finderFeed>")
        internal = sns.parse_timeline(xml)["card"]
        # 内部形状（只在本模块内使用）
        self.assertIn("content_url", internal)
        self.assertIn("duration_s", internal)

        pub = sns.public_card(internal)
        self.assertEqual(set(pub), {"kind", "title", "description", "source", "url", "cover",
                                    "cover_width", "cover_height", "duration", "finder"})
        self.assertEqual(pub["url"], internal["content_url"])
        self.assertEqual(pub["cover"], internal["cover_url"])
        self.assertEqual(pub["duration"], internal["duration_s"])
        self.assertEqual(pub["cover_width"], 100)
        f = pub["finder"]
        self.assertEqual(f["media_count"], 2)
        self.assertEqual(f["video_url"], internal["video_url"])
        self.assertEqual(f["media"], internal["finder"]["medias"])
        self.assertIsNone(sns.public_card(None))

    def test_search_text_covers_card_fields(self):
        """卡片动态的 contentDesc 常为空，搜索必须能命中标题/歌手/昵称。"""
        music = sns.parse_timeline(self._wrap(
            "42", "<title>占位歌曲甲</title>"
                  "<musicShareItem><mvSingerName>占位歌手甲</mvSingerName>"
                  "<mvAlbumName>占位歌曲甲</mvAlbumName></musicShareItem>"))
        self.assertEqual(music["content_desc"], "")
        self.assertIn("占位歌曲甲", sns.search_text(music))
        self.assertIn("占位歌手甲", sns.search_text(music))

        finder = sns.parse_timeline(self._wrap(
            "28", "<finderFeed><nickname>占位主播乙</nickname>"
                  "<mediaList><media><mediaType>4</mediaType>"
                  "<coverUrl>http://h/cover</coverUrl></media></mediaList></finderFeed>"))
        self.assertIn("占位主播乙", sns.search_text(finder))

    def test_search_text_covers_media_description_and_location(self):
        """实测 type 54 的正文只存在于 mediaList/media/description；位置也可搜。"""
        xml = self._wrap("54", "<mediaList><media><type>2</type>"
                              "<description>占位正文甲</description>"
                              "<url>http://h/mmsns/a/0</url></media></mediaList>",
                         extra="<location latitude='31.2' longitude='121.4' poiName='外滩'/>")
        feed = sns.parse_timeline(xml)
        blob = sns.search_text(feed)
        self.assertIn("占位正文甲", blob)
        self.assertIn("外滩", blob)


class TestSnsCardExport(unittest.TestCase):
    """卡片 / 位置 / 评论图 进四种导出格式。"""

    def setUp(self):
        import tempfile
        self._tmp = tempfile.TemporaryDirectory(prefix="siwx_sns_card_",
                                                ignore_cleanup_errors=True)
        self.tmp = Path(self._tmp.name)
        self.db = self.tmp / "sns" / "sns.db"

    def tearDown(self):
        self._tmp.cleanup()

    def _db(self):
        base = (1700000000 * 1000) << 23
        link = ("<SnsDataItem><TimelineObject><id>1</id><username>wxid_a</username>"
                "<createTime>1700000000</createTime><contentDesc>正文</contentDesc>"
                "<ContentObject><type>3</type><title>一篇好文</title>"
                "<description>摘要</description><contentUrl>https://mp.weixin.qq.com/s/x</contentUrl>"
                "<mediaList/></ContentObject>"
                "<location latitude='31.2' longitude='121.4' poiName='外滩' poiAddress='中山东一路'/>"
                "</TimelineObject>"
                "<LocalExtraInfo><comment_user_list><user_comment><username>wxid_b</username>"
                "<nickname>小明</nickname><content>好看</content><type>2</type>"
                "<imagelist><imageinfo><url token='t'>http://h/mmcomment/a/0</url>"
                "<width>100</width><height>80</height></imageinfo></imagelist>"
                "</user_comment></comment_user_list></LocalExtraInfo></SnsDataItem>")
        finder = ("<SnsDataItem><TimelineObject><id>2</id><username>wxid_c</username>"
                  "<createTime>1700000001</createTime><contentDesc>视频号内容</contentDesc>"
                  "<ContentObject><type>28</type><finderFeed>"
                  "<nickname>占位主播乙</nickname><mediaCount>1</mediaCount>"
                  "<mediaList><media><mediaType>4</mediaType>"
                  "<coverUrl>http://h/finder/cover</coverUrl>"
                  "<width>1080</width><height>608</height>"
                  "<videoPlayDuration>255</videoPlayDuration></media></mediaList>"
                  "</finderFeed></ContentObject></TimelineObject></SnsDataItem>")
        _make_sns_db_raw(self.db, [(_to_signed64(base + 2), "wxid_c", finder),
                                   (_to_signed64(base + 1), "wxid_a", link)])

    def test_keyword_matches_card_title(self):
        """真实缺陷回归：卡片标题可搜（此前只搜 contentDesc）。"""
        from siwx import sns_export as E
        self._db()
        r = E.run_sns_export(self.db, fx.DEMO_SELF, fmt="json",
                             export_root=self.tmp / "out", keyword="一篇好文")
        self.assertTrue(r["ok"], r.get("error"))
        self.assertEqual(r["count"], 1)

    def test_keyword_matches_finder_nickname(self):
        from siwx import sns_export as E
        self._db()
        r = E.run_sns_export(self.db, fx.DEMO_SELF, fmt="json",
                             export_root=self.tmp / "out", keyword="占位主播乙")
        self.assertEqual(r["count"], 1)

    def test_json_card_and_comment_image(self):
        import json as _json
        from siwx import sns_export as E
        self._db()
        r = E.run_sns_export(self.db, fx.DEMO_SELF, fmt="json",
                             export_root=self.tmp / "out")
        data = _json.loads(Path(r["file"]).read_text(encoding="utf-8"))
        posts = {p["author"]: p for p in data["posts"]}
        link = posts["wxid_a"]
        self.assertEqual(link["card"]["kind"], "link")
        self.assertEqual(link["card"]["title"], "一篇好文")
        self.assertEqual(link["card"]["url"], "https://mp.weixin.qq.com/s/x")
        self.assertEqual(link["location"]["name"], "外滩")
        self.assertEqual(link["comments"][0]["images"][0]["url"], "http://h/mmcomment/a/0")
        self.assertEqual(posts["wxid_c"]["card"]["finder"]["nickname"], "占位主播乙")

    def test_markdown_txt_html_card_blocks(self):
        from siwx import sns_export as E
        self._db()
        md = Path(E.run_sns_export(self.db, fx.DEMO_SELF, fmt="markdown",
                                   export_root=self.tmp / "out")["file"]).read_text(encoding="utf-8")
        self.assertIn("[一篇好文](https://mp.weixin.qq.com/s/x)", md)
        self.assertIn("📹 视频号 @占位主播乙", md)
        self.assertIn("📍 外滩", md)
        self.assertIn("![评论图](http://h/mmcomment/a/0)", md)

        txt = Path(E.run_sns_export(self.db, fx.DEMO_SELF, fmt="txt",
                                    export_root=self.tmp / "out")["file"]).read_text(encoding="utf-8")
        self.assertIn("🔗 一篇好文", txt)
        self.assertIn("📍 外滩 中山东一路", txt)
        self.assertIn("<评论图> http://h/mmcomment/a/0", txt)

        html = Path(E.run_sns_export(self.db, fx.DEMO_SELF, fmt="html",
                                     export_root=self.tmp / "out")["file"]).read_text(encoding="utf-8")
        self.assertIn('class="card card--link"', html)
        self.assertIn('class="card card--finder"', html)
        self.assertIn('href="https://mp.weixin.qq.com/s/x"', html)
        self.assertIn("http://h/finder/cover", html)
        self.assertIn("📍 外滩", html)
        self.assertIn('class="cm-img"', html)


class TestSnsTimeRange(unittest.TestCase):
    """时间范围过滤：tid 区间下推 + 秒级边界。"""

    def test_tid_bounds_cover_the_second(self):
        # 2026-09-30 12:00:00 这一秒内的所有随机低位都应落在 [lo, hi]
        sec = 1790740800
        lo, hi = sns.ts_to_tid_bounds(sec, sec)
        base = (sec * 1000) << 23
        self.assertLessEqual(lo, sns._to_signed64(base))
        self.assertLessEqual(sns._to_signed64(base + 999), hi)
        # 相邻秒必须被排除
        self.assertGreater(sns._to_signed64(((sec + 1) * 1000) << 23), hi)
        self.assertLess(sns._to_signed64((sec * 1000 - 1) << 23), lo)
        # 现代时间戳都在同一（负数）区，BETWEEN 语义才成立
        self.assertLess(lo, 0)
        self.assertLess(hi, 0)

    def test_tid_bounds_open_ended(self):
        lo, hi = sns.ts_to_tid_bounds(1790740800, None)
        self.assertIsNotNone(lo)
        self.assertIsNone(hi)
        lo2, hi2 = sns.ts_to_tid_bounds(None, 1790740800)
        self.assertIsNone(lo2)
        self.assertIsNotNone(hi2)
        self.assertEqual(sns.ts_to_tid_bounds(None, None), (None, None))

    def test_parse_ts_arg(self):
        self.assertEqual(sns.parse_ts_arg("1790740800"), 1790740800)
        self.assertEqual(sns.parse_ts_arg(1790740800), 1790740800)
        self.assertIsNone(sns.parse_ts_arg(""))
        self.assertIsNone(sns.parse_ts_arg("昨天"))
        # 日期字符串按本地时区解析
        import datetime as _dt
        got = sns.parse_ts_arg("2026-09-30")
        self.assertEqual(_dt.datetime.fromtimestamp(got).strftime("%Y-%m-%d"), "2026-09-30")
        self.assertEqual(sns.parse_ts_arg("2026-09-30"), sns.parse_ts_arg("2026/09/30"))


def _make_sns_db_times(path: Path, posts):
    """建库，posts = [(tid, user, xml)]（时间范围测试用，语义同 raw）。"""
    _make_sns_db_raw(path, posts)


class TestSnsApi(IsolatedRootCase):
    """API 层：账号隔离、分页、边界、异步导出任务。

    隔离统一到 tests/_base.IsolatedRootCase（此前本类只清 paths._PATH_CACHE，
    api_chat/插件注册表的模块级单例会在 discover 全量跑时跨套件残留）。
    """

    def setUp(self):
        super().setUp()
        self.acc = "wxid_api_test"
        db = self.tmp / "output" / self.acc / "sns" / "sns.db"
        base = (1700000000 * 1000) << 23
        _make_sns_db(db, [(_to_signed64(base + 2), "wxid_a", "hello world"),
                          (_to_signed64(base + 1), "wxid_b", "第二条")])

    @staticmethod
    def _client():
        from siwx.server import app
        return app.test_client()

    def test_accounts_lists_only_sns(self):
        d = self._client().get("/api/sns/accounts").get_json()
        self.assertEqual([a["wxid"] for a in d["accounts"]], [self.acc])
        self.assertEqual(d["accounts"][0]["count"], 2)

    def test_timeline_paging_and_filters(self):
        c = self._client()
        d = c.get(f"/api/sns/timeline?account={self.acc}&limit=1").get_json()
        self.assertEqual(len(d["timeline"]), 1)
        self.assertTrue(d["has_more"])
        self.assertIsNotNone(d["next_before_tid"])

        d2 = c.get(f"/api/sns/timeline?account={self.acc}&keyword=world").get_json()
        self.assertEqual(len(d2["timeline"]), 1)
        self.assertEqual(d2["timeline"][0]["user_name"], "wxid_a")

        d3 = c.get(f"/api/sns/timeline?account={self.acc}&username=wxid_b").get_json()
        self.assertEqual(len(d3["timeline"]), 1)

    def test_detail_and_errors(self):
        c = self._client()
        tid = c.get(f"/api/sns/timeline?account={self.acc}&limit=1").get_json()["timeline"][0]["tid"]
        self.assertEqual(c.get(f"/api/sns/detail?account={self.acc}&tid={tid}").status_code, 200)
        self.assertEqual(c.get(f"/api/sns/detail?account={self.acc}&tid=1").status_code, 404)
        self.assertEqual(c.get(f"/api/sns/detail?account={self.acc}&tid=abc").status_code, 400)

    def test_path_traversal_blocked(self):
        c = self._client()
        for bad in ("../..", "..", "a/b", "a\\b", ""):
            with self.subTest(account=bad):
                self.assertEqual(
                    c.get(f"/api/sns/timeline?account={bad}").status_code, 404)

    def test_export_validation(self):
        c = self._client()
        self.assertEqual(c.post("/api/sns/export",
                                json={"account": "nope", "format": "json"}).status_code, 404)
        self.assertEqual(c.post("/api/sns/export",
                                json={"account": self.acc, "format": "yaml"}).status_code, 400)

    def test_export_runs_as_job(self):
        """导出应走任务槽（异步），而不是同步阻塞请求。"""
        import time
        from siwx import server
        c = self._client()
        r = c.post("/api/sns/export", json={"account": self.acc, "format": "json"})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.get_json().get("started"))
        self.assertEqual(server._job["mode"], "sns_export")

        for _ in range(100):
            j = c.get("/api/job").get_json()
            if not j.get("running"):
                break
            time.sleep(0.1)
        self.assertFalse(j.get("running"), "任务未在预期时间内结束")
        self.assertTrue(j.get("ok"), j.get("logs"))
        rep = j.get("report") or {}
        self.assertEqual(rep.get("kind"), "sns_export")
        self.assertEqual(rep.get("count"), 2)

    def test_export_download_blocks_traversal(self):
        c = self._client()
        self.assertEqual(
            c.get("/api/sns/export/download?path=C:/Windows/win.ini").status_code, 403)
        self.assertEqual(c.get("/api/sns/export/download").status_code, 400)

    def test_friends_aggregated_by_sql(self):
        """发布者聚合走 user_name 列，不解析 XML。"""
        d = self._client().get(f"/api/sns/friends?account={self.acc}").get_json()
        fs = {f["username"]: f["count"] for f in d["friends"]}
        self.assertEqual(fs.get("wxid_a"), 1)
        self.assertEqual(fs.get("wxid_b"), 1)
        self.assertEqual(d["total"], 2)
        # names=0 时不带 display（或 display == username）
        d2 = self._client().get(
            f"/api/sns/friends?account={self.acc}&names=0").get_json()
        for f in d2["friends"]:
            self.assertEqual(f["display"], f["username"])

    def test_username_filter_pushed_to_sql(self):
        """指定 username 时应直接返回 limit 条，而不是「取候选再过滤」被稀释。"""
        c = self._client()
        d = c.get(f"/api/sns/timeline?account={self.acc}&username=wxid_a&limit=20").get_json()
        self.assertEqual(len(d["timeline"]), 1)
        self.assertEqual(d["timeline"][0]["user_name"], "wxid_a")

        # 不存在的发布者 → 空
        d2 = c.get(f"/api/sns/timeline?account={self.acc}&username=nobody&limit=20").get_json()
        self.assertEqual(len(d2["timeline"]), 0)

    def test_export_scoped_to_one_friend(self):
        """导出可限定单个发布者。"""
        import time
        from siwx import server
        c = self._client()
        c.post("/api/sns/export", json={"account": self.acc, "format": "json",
                                        "username": "wxid_a"})
        for _ in range(100):
            j = c.get("/api/job").get_json()
            if not j.get("running"):
                break
            time.sleep(0.1)
        self.assertTrue(j.get("ok"))
        self.assertEqual((j.get("report") or {}).get("count"), 1)

    def test_timeline_time_range_filter(self):
        """时间范围应下推成 tid 区间，并精确按秒过滤。"""
        acc = "wxid_range"
        db = self.tmp / "output" / acc / "sns" / "sns.db"
        days = [1700000000, 1700086400, 1700172800, 1700259200]      # 连续 4 天
        posts = []
        for i, ts in enumerate(days):
            xml = (f"<SnsDataItem><TimelineObject><id>{i}</id><username>wxid_a</username>"
                   f"<createTime>{ts}</createTime><contentDesc>第{i}天</contentDesc>"
                   "<ContentObject><type>1</type><mediaList/></ContentObject>"
                   "</TimelineObject></SnsDataItem>")
            posts.append((_to_signed64((ts * 1000) << 23), "wxid_a", xml))
        _make_sns_db_times(db, posts)
        c = self._client()

        d = c.get(f"/api/sns/timeline?account={acc}&limit=50").get_json()
        self.assertEqual(len(d["timeline"]), 4)

        # 只取中间两天（含端点）
        d2 = c.get(f"/api/sns/timeline?account={acc}&limit=50"
                   f"&start={days[1]}&end={days[2]}").get_json()
        self.assertEqual([p["ts"] for p in d2["timeline"]], [days[2], days[1]])

        # 单秒范围
        d3 = c.get(f"/api/sns/timeline?account={acc}&limit=50"
                   f"&start={days[3]}&end={days[3]}").get_json()
        self.assertEqual([p["ts"] for p in d3["timeline"]], [days[3]])

        # 只有下界 / 只有上界
        d4 = c.get(f"/api/sns/timeline?account={acc}&limit=50&start={days[2]}").get_json()
        self.assertEqual([p["ts"] for p in d4["timeline"]], [days[3], days[2]])
        d5 = c.get(f"/api/sns/timeline?account={acc}&limit=50&end={days[1]}").get_json()
        self.assertEqual([p["ts"] for p in d5["timeline"]], [days[1], days[0]])

        # 空区间
        d6 = c.get(f"/api/sns/timeline?account={acc}&limit=50"
                   f"&start={days[0] - 100}&end={days[0] - 50}").get_json()
        self.assertEqual(d6["timeline"], [])

        # 非法值按「不限」处理，不能 500
        d7 = c.get(f"/api/sns/timeline?account={acc}&limit=50&start=abc&end=昨天").get_json()
        self.assertEqual(len(d7["timeline"]), 4)

    def test_friends_avatar_flags_and_range(self):
        """friends 返回 has_avatar / first_ts / last_ts（供头像 + 排序用）。"""
        acc = "wxid_friends"
        acc_dir = self.tmp / "output" / acc
        db = acc_dir / "sns" / "sns.db"
        t0, t1 = 1700000000, 1700086400
        posts = []
        for i, (user, ts) in enumerate([("wxid_with", t0), ("wxid_with", t1),
                                        ("wxid_without", t1)]):
            xml = (f"<SnsDataItem><TimelineObject><id>{i}</id><username>{user}</username>"
                   f"<createTime>{ts}</createTime><contentDesc>x</contentDesc>"
                   "<ContentObject><type>1</type><mediaList/></ContentObject>"
                   "</TimelineObject></SnsDataItem>")
            posts.append((_to_signed64((ts * 1000) << 23) + i, user, xml))
        _make_sns_db_times(db, posts)

        # 造一个有头像的 head_image.db（image_buffer 为明文 JPEG）
        import sqlite3
        hi_dir = acc_dir / "head_image"
        hi_dir.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(hi_dir / "head_image.db")
        con.execute("CREATE TABLE head_image(username TEXT PRIMARY KEY, md5 TEXT, "
                    "image_buffer BLOB, update_time INTEGER)")
        con.execute("INSERT INTO head_image VALUES (?,?,?,?)",
                    ("wxid_with", "m", b"\xff\xd8\xffFAKE", 0))
        con.commit()
        con.close()

        d = self._client().get(f"/api/sns/friends?account={acc}&names=0").get_json()
        rows = {f["username"]: f for f in d["friends"]}
        self.assertTrue(rows["wxid_with"]["has_avatar"])
        self.assertFalse(rows["wxid_without"]["has_avatar"])
        self.assertEqual(rows["wxid_with"]["count"], 2)
        self.assertEqual(rows["wxid_with"]["first_ts"], t0)
        self.assertEqual(rows["wxid_with"]["last_ts"], t1)

        # names=0 时 display == username（不影响 has_avatar）
        self.assertEqual(rows["wxid_with"]["display"], "wxid_with")

    def test_emoji_requires_url(self):
        c = self._client()
        self.assertEqual(c.get("/api/sns/emoji").status_code, 400)
        self.assertEqual(c.get("/api/sns/emoji?emoji=notjson").status_code, 400)
        self.assertEqual(c.get('/api/sns/emoji?emoji={"foo":1}').status_code, 400)

    def test_timeline_keyword_matches_card_fields(self):
        """卡片标题/歌手可搜（此前只搜 contentDesc，卡片动态搜不到）。"""
        acc = "wxid_cards"
        db = self.tmp / "output" / acc / "sns" / "sns.db"
        base = (1700000000 * 1000) << 23
        link = ("<SnsDataItem><TimelineObject><id>9</id><username>wxid_a</username>"
                "<createTime>1700000000</createTime><contentDesc></contentDesc>"
                "<ContentObject><type>3</type><title>一篇好文</title>"
                "<contentUrl>https://mp.weixin.qq.com/s/x</contentUrl>"
                "<mediaList/></ContentObject></TimelineObject></SnsDataItem>")
        music = ("<SnsDataItem><TimelineObject><id>8</id><username>wxid_a</username>"
                 "<createTime>1700000000</createTime><contentDesc></contentDesc>"
                 "<ContentObject><type>42</type>"
                 "<musicShareItem><mvAlbumName>占位歌曲甲</mvAlbumName>"
                 "<mvSingerName>占位歌手甲</mvSingerName></musicShareItem>"
                 "<mediaList/></ContentObject></TimelineObject></SnsDataItem>")
        finder = ("<SnsDataItem><TimelineObject><id>7</id><username>wxid_a</username>"
                  "<createTime>1700000000</createTime><contentDesc></contentDesc>"
                  "<ContentObject><type>28</type><finderFeed><nickname>占位主播乙</nickname>"
                  "<mediaList><media><mediaType>4</mediaType>"
                  "<coverUrl>http://h/finder/cover</coverUrl>"
                  "<url>http://h/finder/v.mp4</url>"
                  "<videoPlayDuration>255</videoPlayDuration></media></mediaList>"
                  "</finderFeed></ContentObject></TimelineObject></SnsDataItem>")
        _make_sns_db_raw(db, [(_to_signed64(base + 9), "wxid_a", link),
                              (_to_signed64(base + 8), "wxid_a", music),
                              (_to_signed64(base + 7), "wxid_a", finder)])
        c = self._client()

        d = c.get(f"/api/sns/timeline?account={acc}&keyword=好文").get_json()
        self.assertEqual(len(d["timeline"]), 1)
        card = d["timeline"][0]["card"]
        self.assertEqual(card["kind"], "link")
        # ⭐ API 给的是 public_card 形状（url/cover/duration），不是内部字段名
        self.assertEqual(card["url"], "https://mp.weixin.qq.com/s/x")
        self.assertNotIn("content_url", card)

        d2 = c.get(f"/api/sns/timeline?account={acc}&keyword=占位歌手甲").get_json()
        self.assertEqual(len(d2["timeline"]), 1)
        self.assertEqual(d2["timeline"][0]["card"]["kind"], "music")
        self.assertEqual(d2["timeline"][0]["card"]["music"]["album"], "占位歌曲甲")

        d3 = c.get(f"/api/sns/timeline?account={acc}&keyword=占位主播乙").get_json()
        self.assertEqual(len(d3["timeline"]), 1)
        fc = d3["timeline"][0]["card"]
        self.assertEqual(fc["kind"], "finder")
        self.assertEqual(fc["finder"]["nickname"], "占位主播乙")
        self.assertEqual(fc["finder"]["video_url"], "http://h/finder/v.mp4")
        self.assertEqual(fc["cover"], "http://h/finder/cover")
        self.assertNotIn("cover_url", fc)

        # 详情接口同样是 public 形状
        tid = d3["timeline"][0]["tid"]
        post = c.get(f"/api/sns/detail?account={acc}&tid={tid}").get_json()["post"]
        self.assertEqual(post["card"]["finder"]["nickname"], "占位主播乙")

        # 负面用例：搜不到的词仍应为空
        d4 = c.get(f"/api/sns/timeline?account={acc}&keyword=不存在").get_json()
        self.assertEqual(len(d4["timeline"]), 0)


class TestSnsCdnDiagnosis(IsolatedRootCase):
    """CDN 拉不下来时：原因要对、要有日志、要看得见。

    背景（5684 条规模的样本库实测，2026-09-30）：
    * 视频 media 写成 ``<url key="0">`` + ``<enc key="929615230">``，
      旧实现把 ``"0"`` 当真密钥 → 密文 XOR 成乱码 → **100% 视频拉不下来**；
    * 视频域名上失败后又被后续 qpic 域名的 400 覆盖，报错方向全错；
    * 整条链路零日志 → 用户「图挂了但日志里什么都没有」。
    """

    def test_zero_placeholder_key_falls_back_to_enc_key(self):
        """真实 XML 片段：url@key="0" 是占位，真密钥在 <enc key>。"""
        import xml.etree.ElementTree as ET
        xml = ("<media><id>1</id><type>6</type><sub_type>0</sub_type>"
               "<url type='1' md5='" + "a" * 32 + "' key='0' enc_idx='0' "
               "videomd5='" + "b" * 32 + "'>http://h/102/20202/snsvideodownload?x=1</url>"
               "<size width='288' height='512' totalSize='6794'/>"
               "<videoDuration>18.83</videoDuration>"
               "<enc key='929615230'>1</enc></media>")
        d = sns._parse_media_el(ET.fromstring(xml))
        self.assertEqual(d["key"], "929615230")
        self.assertEqual(d["enc_key"], "929615230")

    def test_zero_and_empty_attrs_are_missing(self):
        import xml.etree.ElementTree as ET
        d = sns._parse_media_el(ET.fromstring(
            "<media><type>2</type><url token='0' key='0'>http://h/mmsns/a/0</url>"
            "<enc key='0'>0</enc></media>"))
        self.assertIsNone(d["key"])
        self.assertIsNone(d["token"])
        self.assertIsNone(d["enc_key"])
        self.assertIsNone(sns._meta_attr(None, "", "  ", "0", "none", "NULL"))
        self.assertEqual(sns._meta_attr("0", "42"), "42")

    def test_thumb_token_attribute_parsed(self):
        import xml.etree.ElementTree as ET
        d = sns._parse_media_el(ET.fromstring(
            "<media><type>2</type>"
            "<thumb token='tt'>http://h/mmsns/a/150</thumb>"
            "<url token='ut' key='5'>http://h/mmsns/a/0</url></media>"))
        self.assertEqual(d["token"], "ut")
        self.assertEqual(d["thumb_token"], "tt")

    def test_host_fallback_only_for_qpic(self):
        """视频域名不该被换成 qpic —— 只会多拿几个 400 把真实原因盖掉。"""
        vid = "https://shzjwxsns.video.qq.com/102/20202/snsvideodownload?encfilekey=x"
        self.assertEqual(sns_cdn._host_candidates(vid), [vid])
        img = "https://shmmsns.qpic.cn/mmsns/a/0?token=t&idx=1"
        self.assertEqual(len(sns_cdn._host_candidates(img)), 3)

    def test_is_wechat_cdn(self):
        self.assertTrue(sns_cdn.is_wechat_cdn("https://shmmsns.qpic.cn/mmsns/a/0"))
        self.assertTrue(sns_cdn.is_wechat_cdn("https://shzjwxsns.video.qq.com/x"))
        self.assertTrue(sns_cdn.is_wechat_cdn("https://wx.qlogo.cn/x"))
        # 视频号封面（实测 1606 个）与 c2c 视频域名必须放行
        self.assertTrue(sns_cdn.is_wechat_cdn("http://wxapp.tc.qq.com/251/20304/stodownload?x=1"))
        self.assertTrue(sns_cdn.is_wechat_cdn(
            "http://snsvideo.c2c.wechat.com/102/20202/snsvideodownload?x=1"))
        # 外链卡片：不是媒体，拒绝（顺带堵掉任意 URL 代理）
        self.assertFalse(sns_cdn.is_wechat_cdn("https://b23.tv/LgNWM4c"))
        self.assertFalse(sns_cdn.is_wechat_cdn("https://y.music.163.com/m/song?id=1"))
        self.assertFalse(sns_cdn.is_wechat_cdn("https://live.bilibili.com/21738461"))
        self.assertFalse(sns_cdn.is_wechat_cdn("http://evil.com/qpic.cn/x"))
        self.assertFalse(sns_cdn.is_wechat_cdn(""))
        # 日志里不能出现 token
        self.assertEqual(
            sns_cdn.safe_url("https://h.qpic.cn/mmsns/a/0?token=SECRET&idx=1"),
            "h.qpic.cn/mmsns/a/0")

    def test_fetch_media_rejects_non_cdn_and_logs(self):
        with self.assertLogs("siwx", level="WARNING") as cm:
            r = sns_cdn.fetch_media("https://b23.tv/abc", cache_dir=None)
        self.assertFalse(r["ok"])
        self.assertEqual(r["reason"], "not-cdn")
        self.assertTrue(any("[sns-media]" in line for line in cm.output))

    def test_undecodable_wins_over_later_http_error(self):
        """下载成功但认不出内容时，不能被后续域名的 400 覆盖成「HTTP 400」。"""
        import urllib.error
        calls = {"n": 0, "hosts": []}

        def fake_fetch(url, timeout=15.0, ctx=None):
            calls["n"] += 1
            calls["hosts"].append(url.split("/")[2])
            if calls["n"] == 1:
                return b"\x01\x02\x03" * 8, {"x-enc": "1"}
            raise urllib.error.HTTPError(url, 400, "Bad Request", {}, None)

        url = "https://shmmsns.qpic.cn/mmsns/a/0?token=t"
        old = sns_cdn.fetch
        sns_cdn.fetch = fake_fetch
        try:
            with self.assertLogs("siwx", level="WARNING") as cm:
                r = sns_cdn.fetch_media(url, key="12345", cache_dir=None)
        finally:
            sns_cdn.fetch = old
        self.assertFalse(r["ok"])
        self.assertEqual(r["reason"], "undecodable")
        self.assertEqual(r["hosts_tried"], 3)
        self.assertIn("已下载", r["error"])
        self.assertIsNone(r["status"])
        self.assertIn("[sns-media]", "\n".join(cm.output))

    def test_first_http_error_reported(self):
        """真 404 的图：报 404（而不是最后一个域名的状态码）。"""
        import urllib.error
        codes = []

        def fake_fetch(url, timeout=15.0, ctx=None):
            code = 404 if len(codes) == 0 else 400
            codes.append(code)
            raise urllib.error.HTTPError(url, code, "x", {}, None)

        url = "https://shmmsns.qpic.cn/mmsns/a/0?token=t"
        old = sns_cdn.fetch
        sns_cdn.fetch = fake_fetch
        try:
            r = sns_cdn.fetch_media(url, key="1", cache_dir=None)
        finally:
            sns_cdn.fetch = old
        self.assertFalse(r["ok"])
        self.assertEqual(r["status"], 404)
        self.assertEqual(r["reason"], "http-404")

    def test_encrypted_body_decrypts_with_parsed_key(self):
        """离线闭环：按 <enc key> 加密的体，用 parse_timeline 的 key 能解出 mp4。"""
        import xml.etree.ElementTree as ET
        key = 929615230
        plain = b"\x00\x00\x00 ftypisom" + b"\x00" * 32
        ks = sns_isaac64.keystream(key, len(plain))
        enc = bytes(a ^ b for a, b in zip(plain, ks))
        el = ET.fromstring(
            "<media><type>6</type><url key='0'>http://h/x/snsvideodownload?e=1</url>"
            f"<enc key='{key}'>1</enc></media>")
        parsed = sns._parse_media_el(el)
        self.assertEqual(sns_cdn.detect_mime(sns_cdn.decrypt_isaac(enc, parsed["key"]))[0],
                         "mp4")

    def test_api_media_rejects_external_and_exposes_reason(self):
        """SIWX_ROOT 由 IsolatedRootCase 统一隔离到临时目录。"""
        import urllib.error
        from siwx.server import app
        c = app.test_client()
        r = c.get("/api/sns/media?url=https://b23.tv/abc")
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.get_json()["reason"], "not-cdn")

        # 404 的图：响应体要带原因，前端据此提示
        def fake_fetch(url, timeout=15.0, ctx=None):
            raise urllib.error.HTTPError(url, 404, "x", {}, None)

        old_fetch = sns_cdn.fetch
        sns_cdn.fetch = fake_fetch
        try:
            r2 = c.get("/api/sns/media?url=" +
                       "https%3A%2F%2Fshmmsns.qpic.cn%2Fmmsns%2Fa%2F0%3Ftoken%3Dt"
                       "&key=1&account=x")
        finally:
            sns_cdn.fetch = old_fetch
        self.assertEqual(r2.status_code, 404)
        body = r2.get_json()
        self.assertEqual(body["reason"], "http-404")
        self.assertEqual(body["hosts_tried"], 3)


class TestSnsMcp(IsolatedRootCase):
    """MCP 层：直接调用 mcp_server 的 SNS 工具处理函数（不经 HTTP）。"""

    def setUp(self):
        super().setUp()
        self.acc = "wxid_mcp_test"
        db = self.tmp / "output" / self.acc / "sns" / "sns.db"
        base = (1700000000 * 1000) << 23
        _make_sns_db(db, [(_to_signed64(base + 2), "wxid_a", "hello world"),
                          (_to_signed64(base + 1), "wxid_b", "第二条动态"),
                          (_to_signed64(base + 3), "wxid_a", "第三条 hello again")])
        self.base = base

    def _tool(self, name, args):
        import json as _json
        from siwx import mcp_server as M
        return _json.loads(getattr(M, f"tool_{name}")(args))

    def test_sns_tools_registered(self):
        from siwx import mcp_server as M
        names = {t["name"] for t in M.TOOLS}
        for n in ("list_sns_accounts", "get_sns_timeline", "get_sns_detail",
                  "get_sns_friends", "export_sns"):
            self.assertIn(n, names)
            self.assertIn(n, M._HANDLERS)

    def test_list_sns_accounts(self):
        d = self._tool("list_sns_accounts", {})
        self.assertEqual([a["wxid"] for a in d["accounts"]], [self.acc])
        self.assertEqual(d["accounts"][0]["count"], 3)

    def test_timeline_paging_and_filters(self):
        d = self._tool("get_sns_timeline", {"account": self.acc, "limit": 2})
        self.assertEqual(d["total"], 2)
        self.assertTrue(d["has_more"])
        self.assertIsNotNone(d["next_before_tid"])
        # 游标翻页：before_tid 之后的更早动态
        d2 = self._tool("get_sns_timeline",
                        {"account": self.acc, "limit": 10,
                         "before_tid": d["next_before_tid"]})
        self.assertEqual(d2["total"], 1)

        # 关键词（含多词命中：hello 在两条里）
        d3 = self._tool("get_sns_timeline",
                        {"account": self.acc, "keyword": "hello"})
        self.assertEqual(d3["total"], 2)

        # 发布者过滤
        d4 = self._tool("get_sns_timeline",
                        {"account": self.acc, "username": "wxid_b"})
        self.assertEqual(d4["total"], 1)
        self.assertEqual(d4["posts"][0]["user_name"], "wxid_b")

        # 时间范围（合成库三条动态同秒，范围应全命中）
        newest = self._tool("get_sns_timeline", {"account": self.acc, "limit": 1})
        ts = newest["posts"][0]["ts"]
        d5 = self._tool("get_sns_timeline",
                        {"account": self.acc, "start": ts, "end": ts})
        self.assertEqual(d5["total"], 3)
        # 下一秒起应为空
        d6 = self._tool("get_sns_timeline",
                        {"account": self.acc, "start": ts + 1})
        self.assertEqual(d6["total"], 0)

    def test_timeline_slim_shape_strips_urls(self):
        """精简形状：不得携带 CDN URL（AI 客户端取不了媒体）。"""
        d = self._tool("get_sns_timeline", {"account": self.acc})
        blob = _to_json_text(d)
        self.assertNotIn("http", blob)
        # 精简形状应带互动计数与卡片键
        p = d["posts"][0]
        for k in ("tid", "ts", "user_name", "kind", "content_desc",
                  "media_count", "like_count", "comment_count"):
            self.assertIn(k, p)

    def test_detail_full_comments(self):
        import json as _json
        # 用带评论/点赞的库（复用 TestSnsInteraction 的 _EMOJI_XML）
        from siwx import paths
        db = (self.tmp / "output" / "wxid_mcp_detail" / "sns" / "sns.db")
        base = (1700000000 * 1000) << 23
        _make_sns_db_raw(db, [(_to_signed64(base + 9), "wxid_a", _EMOJI_XML)])
        old_acc = self.acc
        d = self._tool("get_sns_detail",
                       {"account": "wxid_mcp_detail", "tid": _to_signed64(base + 9)})
        self.assertEqual(d["comment_count"], 1)
        self.assertEqual(d["comments"][0]["content"], "哈哈")
        self.assertEqual(d["comments"][0]["nickname"], "小明")
        self.assertTrue(d["comments"][0]["has_emoji"])
        self.assertEqual(d["comments"][0]["image_count"], 1)
        self.assertEqual(d["like_count"], 1)
        self.assertIn("小红", d["likes"])
        # URL 不得出现在详情里
        self.assertNotIn("http", _to_json_text(d))

    def test_friends_aggregation(self):
        d = self._tool("get_sns_friends", {"account": self.acc})
        self.assertEqual(d["total"], 2)
        top = d["friends"][0]        # 按动态数降序：wxid_a 有 2 条
        self.assertEqual(top["username"], "wxid_a")
        self.assertEqual(top["count"], 2)
        self.assertIn("display", top)

    def test_export_sns(self):
        d = self._tool("export_sns", {"account": self.acc, "format": "json"})
        self.assertTrue(d["ok"], d.get("error"))
        self.assertEqual(d["count"], 3)
        self.assertTrue(Path(d["file"]).is_file())

        # 关键词过滤
        d2 = self._tool("export_sns",
                        {"account": self.acc, "format": "json", "keyword": "world"})
        self.assertEqual(d2["count"], 1)

        # 非法格式
        with self.assertRaises(ValueError):
            self._tool("export_sns", {"account": self.acc, "format": "yaml"})

    def test_account_validation(self):
        from siwx import mcp_server as M
        for bad in ("", "../..", "a/b", "a\\b", "nope"):
            with self.subTest(account=bad):
                with self.assertRaises(ValueError):
                    M.tool_get_sns_timeline({"account": bad})

    def test_mcp_jsonrpc_tools_list(self):
        """tools/list 应返回全部内置工具（含 5 个 SNS 工具），且 schema 为 object。"""
        import json as _json
        from siwx import mcp_server as M
        names = {t["name"] for t in M._all_tools()}
        self.assertTrue({"list_sns_accounts", "get_sns_timeline",
                         "get_sns_detail", "get_sns_friends", "export_sns"} <= names)
        for t in M.TOOLS:
            self.assertEqual(t["inputSchema"]["type"], "object")
            self.assertTrue(t["description"])


def _to_json_text(obj) -> str:
    import json as _json
    return _json.dumps(obj, ensure_ascii=False)


if __name__ == "__main__":
    unittest.main()
