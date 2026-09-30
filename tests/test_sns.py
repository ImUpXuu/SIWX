import hashlib
import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from siwx import sns, sns_cdn, sns_isaac64


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
                r = E.run_sns_export(self.db, "wxid_test", fmt=fmt,
                                     export_root=self.tmp / "out")
                self.assertTrue(r["ok"], r.get("error"))
                self.assertEqual(r["count"], 2)
                f = Path(r["file"])
                self.assertTrue(f.is_file())
                self.assertEqual(f.suffix, suffix)
                self.assertGreater(f.stat().st_size, 0)

    def test_export_keyword_filter(self):
        from siwx import sns_export as E
        r = E.run_sns_export(self.db, "wxid_test", fmt="json",
                             export_root=self.tmp / "out", keyword="world")
        self.assertEqual(r["count"], 1)

    def test_export_author_filter(self):
        from siwx import sns_export as E
        r = E.run_sns_export(self.db, "wxid_test", fmt="json",
                             export_root=self.tmp / "out", usernames=["wxid_a"])
        self.assertEqual(r["count"], 1)

    def test_export_rejects_unknown_format(self):
        from siwx import sns_export as E
        r = E.run_sns_export(self.db, "wxid_test", fmt="yaml",
                             export_root=self.tmp / "out")
        self.assertFalse(r["ok"])

    def test_export_empty_result(self):
        from siwx import sns_export as E
        r = E.run_sns_export(self.db, "wxid_test", fmt="json",
                             export_root=self.tmp / "out", keyword="不存在的内容")
        self.assertFalse(r["ok"])


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
                         "<title>今晚免费直播：细胞治疗</title>"
                         "<description>7月29日 19:30</description>"
                         "<contentUrl>https://mp.weixin.qq.com/s?__biz=abc</contentUrl>"
                         "<mediaList><media><type>2</type>"
                         "<url token='t' key='k'>http://h/mmsns/cov/0</url>"
                         "</media></mediaList>",
                         extra="<sourceNickName>制药台</sourceNickName>")
        feed = sns.parse_timeline(xml)
        card = feed["card"]
        self.assertEqual(card["kind"], "link")
        self.assertEqual(card["title"], "今晚免费直播：细胞治疗")
        self.assertEqual(card["description"], "7月29日 19:30")
        self.assertIn("mp.weixin.qq.com", card["content_url"])
        self.assertEqual(card["source"], "制药台")
        self.assertEqual(card["cover_url"], "http://h/mmsns/cov/0")

    def test_music_card_kind_from_fields_not_type(self):
        """type 42 在早期文档里叫 finder_live，实际是音乐 —— kind 必须按字段判定。"""
        xml = self._wrap("42",
                         "<title>让风告诉你</title><description>花玲、喵酱油</description>"
                         "<contentUrl>https://t1.kugou.com/abc</contentUrl>"
                         "<musicShareItem><mvSingerName>花玲</mvSingerName>"
                         "<mvAlbumName>让风告诉你</mvAlbumName>"
                         "<musicDuration>226000</musicDuration></musicShareItem>")
        card = sns.parse_timeline(xml)["card"]
        self.assertEqual(card["kind"], "music")
        self.assertEqual(card["music"]["singer"], "花玲")
        self.assertEqual(card["music"]["album"], "让风告诉你")
        self.assertEqual(card["music"]["duration_ms"], 226000)

    def test_finder_feed_card(self):
        xml = self._wrap(
            "28",
            "<description>中考进步学员专访</description>"
            "<finderFeed><objectId>14711924164078868603</objectId>"
            "<feedType>4</feedType><nickname>高途英语小唐老师</nickname>"
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
        self.assertEqual(f["nickname"], "高途英语小唐老师")
        self.assertEqual(f["media_count"], 1)
        self.assertEqual(f["medias"][0]["media_type"], 4)
        self.assertEqual(f["medias"][0]["duration_s"], 255)
        # finderFeed 的媒体**不是**主 mediaList，不应混进 medias
        self.assertEqual(feed["medias"], [])

    def test_finder_live_card(self):
        xml = self._wrap("34",
                         "<finderLive><finderLiveID>2042905931828856520</finderLiveID>"
                         "<nickname>新华网健康</nickname>"
                         "<coverUrl>https://wxapp.tc.qq.com/251/20304/stodownload?encfilekey=y</coverUrl>"
                         "<desc>庆祝大会</desc><liveStatus>1</liveStatus>"
                         "<media><coverUrl>https://wxapp.tc.qq.com/251/20304/stodownload?encfilekey=y</coverUrl>"
                         "<width>1440</width><height>1920</height></media></finderLive>")
        card = sns.parse_timeline(xml)["card"]
        self.assertEqual(card["kind"], "live")
        self.assertEqual(card["live"]["nickname"], "新华网健康")
        self.assertEqual(card["live"]["desc"], "庆祝大会")
        self.assertEqual(card["live"]["status"], 1)
        self.assertEqual(card["cover_width"], 1440)

    def test_note_card(self):
        xml = self._wrap("26",
                         "<title>小时候的端倪</title><description>note description</description>"
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
            "42", "<title>让风告诉你</title>"
                  "<musicShareItem><mvSingerName>花玲</mvSingerName>"
                  "<mvAlbumName>让风告诉你</mvAlbumName></musicShareItem>"))
        self.assertEqual(music["content_desc"], "")
        self.assertIn("让风告诉你", sns.search_text(music))
        self.assertIn("花玲", sns.search_text(music))

        finder = sns.parse_timeline(self._wrap(
            "28", "<finderFeed><nickname>小唐老师</nickname>"
                  "<mediaList><media><mediaType>4</mediaType>"
                  "<coverUrl>http://h/cover</coverUrl></media></mediaList></finderFeed>"))
        self.assertIn("小唐老师", sns.search_text(finder))

    def test_search_text_covers_media_description_and_location(self):
        """实测 type 54 的正文只存在于 mediaList/media/description；位置也可搜。"""
        xml = self._wrap("54", "<mediaList><media><type>2</type>"
                              "<description>我去这个入特别师</description>"
                              "<url>http://h/mmsns/a/0</url></media></mediaList>",
                         extra="<location latitude='31.2' longitude='121.4' poiName='外滩'/>")
        feed = sns.parse_timeline(xml)
        blob = sns.search_text(feed)
        self.assertIn("我去这个入特别师", blob)
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
                  "<nickname>小唐老师</nickname><mediaCount>1</mediaCount>"
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
        r = E.run_sns_export(self.db, "wxid_test", fmt="json",
                             export_root=self.tmp / "out", keyword="一篇好文")
        self.assertTrue(r["ok"], r.get("error"))
        self.assertEqual(r["count"], 1)

    def test_keyword_matches_finder_nickname(self):
        from siwx import sns_export as E
        self._db()
        r = E.run_sns_export(self.db, "wxid_test", fmt="json",
                             export_root=self.tmp / "out", keyword="小唐老师")
        self.assertEqual(r["count"], 1)

    def test_json_card_and_comment_image(self):
        import json as _json
        from siwx import sns_export as E
        self._db()
        r = E.run_sns_export(self.db, "wxid_test", fmt="json",
                             export_root=self.tmp / "out")
        data = _json.loads(Path(r["file"]).read_text(encoding="utf-8"))
        posts = {p["author"]: p for p in data["posts"]}
        link = posts["wxid_a"]
        self.assertEqual(link["card"]["kind"], "link")
        self.assertEqual(link["card"]["title"], "一篇好文")
        self.assertEqual(link["card"]["url"], "https://mp.weixin.qq.com/s/x")
        self.assertEqual(link["location"]["name"], "外滩")
        self.assertEqual(link["comments"][0]["images"][0]["url"], "http://h/mmcomment/a/0")
        self.assertEqual(posts["wxid_c"]["card"]["finder"]["nickname"], "小唐老师")

    def test_markdown_txt_html_card_blocks(self):
        from siwx import sns_export as E
        self._db()
        md = Path(E.run_sns_export(self.db, "wxid_test", fmt="markdown",
                                   export_root=self.tmp / "out")["file"]).read_text(encoding="utf-8")
        self.assertIn("[一篇好文](https://mp.weixin.qq.com/s/x)", md)
        self.assertIn("📹 视频号 @小唐老师", md)
        self.assertIn("📍 外滩", md)
        self.assertIn("![评论图](http://h/mmcomment/a/0)", md)

        txt = Path(E.run_sns_export(self.db, "wxid_test", fmt="txt",
                                    export_root=self.tmp / "out")["file"]).read_text(encoding="utf-8")
        self.assertIn("🔗 一篇好文", txt)
        self.assertIn("📍 外滩 中山东一路", txt)
        self.assertIn("<评论图> http://h/mmcomment/a/0", txt)

        html = Path(E.run_sns_export(self.db, "wxid_test", fmt="html",
                                     export_root=self.tmp / "out")["file"]).read_text(encoding="utf-8")
        self.assertIn('class="card card--link"', html)
        self.assertIn('class="card card--finder"', html)
        self.assertIn('href="https://mp.weixin.qq.com/s/x"', html)
        self.assertIn("http://h/finder/cover", html)
        self.assertIn("📍 外滩", html)
        self.assertIn('class="cm-img"', html)


class TestSnsApi(unittest.TestCase):
    """API 层：账号隔离、分页、边界、异步导出任务。"""

    def setUp(self):
        import tempfile
        from siwx import paths
        self._tmp = tempfile.TemporaryDirectory(prefix="siwx_sns_api_",
                                                ignore_cleanup_errors=True)
        self._old_root = os.environ.get("SIWX_ROOT")
        os.environ["SIWX_ROOT"] = self._tmp.name
        paths._PATH_CACHE.clear()
        self.acc = "wxid_api_test"
        db = Path(self._tmp.name) / "output" / self.acc / "sns" / "sns.db"
        base = (1700000000 * 1000) << 23
        _make_sns_db(db, [(_to_signed64(base + 2), "wxid_a", "hello world"),
                          (_to_signed64(base + 1), "wxid_b", "第二条")])

    def tearDown(self):
        from siwx import paths
        if self._old_root is None:
            os.environ.pop("SIWX_ROOT", None)
        else:
            os.environ["SIWX_ROOT"] = self._old_root
        paths._PATH_CACHE.clear()
        self._tmp.cleanup()

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

    def test_emoji_requires_url(self):
        c = self._client()
        self.assertEqual(c.get("/api/sns/emoji").status_code, 400)
        self.assertEqual(c.get("/api/sns/emoji?emoji=notjson").status_code, 400)
        self.assertEqual(c.get('/api/sns/emoji?emoji={"foo":1}').status_code, 400)

    def test_timeline_keyword_matches_card_fields(self):
        """卡片标题/歌手可搜（此前只搜 contentDesc，卡片动态搜不到）。"""
        acc = "wxid_cards"
        db = Path(self._tmp.name) / "output" / acc / "sns" / "sns.db"
        base = (1700000000 * 1000) << 23
        link = ("<SnsDataItem><TimelineObject><id>9</id><username>wxid_a</username>"
                "<createTime>1700000000</createTime><contentDesc></contentDesc>"
                "<ContentObject><type>3</type><title>一篇好文</title>"
                "<contentUrl>https://mp.weixin.qq.com/s/x</contentUrl>"
                "<mediaList/></ContentObject></TimelineObject></SnsDataItem>")
        music = ("<SnsDataItem><TimelineObject><id>8</id><username>wxid_a</username>"
                 "<createTime>1700000000</createTime><contentDesc></contentDesc>"
                 "<ContentObject><type>42</type>"
                 "<musicShareItem><mvAlbumName>让风告诉你</mvAlbumName>"
                 "<mvSingerName>花玲</mvSingerName></musicShareItem>"
                 "<mediaList/></ContentObject></TimelineObject></SnsDataItem>")
        finder = ("<SnsDataItem><TimelineObject><id>7</id><username>wxid_a</username>"
                  "<createTime>1700000000</createTime><contentDesc></contentDesc>"
                  "<ContentObject><type>28</type><finderFeed><nickname>小唐老师</nickname>"
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

        d2 = c.get(f"/api/sns/timeline?account={acc}&keyword=花玲").get_json()
        self.assertEqual(len(d2["timeline"]), 1)
        self.assertEqual(d2["timeline"][0]["card"]["kind"], "music")
        self.assertEqual(d2["timeline"][0]["card"]["music"]["album"], "让风告诉你")

        d3 = c.get(f"/api/sns/timeline?account={acc}&keyword=小唐老师").get_json()
        self.assertEqual(len(d3["timeline"]), 1)
        fc = d3["timeline"][0]["card"]
        self.assertEqual(fc["kind"], "finder")
        self.assertEqual(fc["finder"]["nickname"], "小唐老师")
        self.assertEqual(fc["finder"]["video_url"], "http://h/finder/v.mp4")
        self.assertEqual(fc["cover"], "http://h/finder/cover")
        self.assertNotIn("cover_url", fc)

        # 详情接口同样是 public 形状
        tid = d3["timeline"][0]["tid"]
        post = c.get(f"/api/sns/detail?account={acc}&tid={tid}").get_json()["post"]
        self.assertEqual(post["card"]["finder"]["nickname"], "小唐老师")

        # 负面用例：搜不到的词仍应为空
        d4 = c.get(f"/api/sns/timeline?account={acc}&keyword=不存在").get_json()
        self.assertEqual(len(d4["timeline"]), 0)


if __name__ == "__main__":
    unittest.main()
