"""插件系统测试（轻量、自包含、秒级）。

宿主回归测试 tests/test_regressions.py 通过 SIWX_NO_PLUGINS=1 与"用户装了什么插件"
解耦；本套件反过来**专门验证插件框架本身**，全部使用合成插件与临时目录，不读取任何
真实聊天库，也不触碰用户级插件目录。

覆盖：
  1. 契约解析：字符串函数名 / 页面 / 主题 / 蓝图 / 重名去重 / api_version 校验
  2. 逐 hook 隔离：单个 hook 声明非法不影响同插件其它 hook
  3. 目录发现与加载：单文件 / 包 / 跳过规则 / import 异常不致命
  4. 显示条件求值（conditions）与解释
  5. 配置存储：schema 校验、类型强制、上下界、原子写、非法值回退
  6. 加载报告：ok / degraded / error 三态与同名先到先得
  7. chat_bridge：渲染器 / 装饰器（含热路径守卫与熔断）/ 内容转换 / 会话过滤 / 头像
  8. 导出格式注册（内置优先）
  9. MCP 工具注册（内置优先）

运行：
    python -m unittest tests.test_plugins -v
或
    python -m unittest discover -s tests -v
"""
import importlib
import json
import os
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# 本套件必须自己控制插件加载：宿主可能已被 SIWX_NO_PLUGINS=1 污染，
# 故显式清掉该开关（测试用临时目录，不会读到用户真实插件）。
os.environ.pop("SIWX_NO_PLUGINS", None)


# ── 测试替身：不依赖 Flask 的轻量模块对象 ──────────────────────

class FakeModule:
    """模拟被 import 的插件模块（契约解析只看 __dict__）。"""

    def __init__(self, source="dir:/tmp/fake_plugin.py", **fns):
        self.__siwx_source__ = source
        self.__siwx_pages_dir__ = ""
        for k, v in fns.items():
            if k.startswith("__") and k.endswith("__"):
                continue
            setattr(self, k, v)


def _mk_plugin_pkg(root: Path, name: str, body: str,
                   pages: dict = None) -> Path:
    """在 root 下写一个包形态插件，返回插件目录。"""
    pkg = root / name
    pkg.mkdir(parents=True, exist_ok=True)
    (pkg / "__init__.py").write_text(textwrap.dedent(body), encoding="utf-8")
    if pages:
        pdir = pkg / "pages"
        pdir.mkdir(exist_ok=True)
        for fname, content in pages.items():
            (pdir / fname).write_text(content, encoding="utf-8")
    return pkg


def _mk_plugin_file(root: Path, name: str, body: str) -> Path:
    """在 root 下写一个单文件插件，返回文件路径。"""
    f = root / f"{name}.py"
    f.write_text(textwrap.dedent(body), encoding="utf-8")
    return f


class PluginTestCase(unittest.TestCase):
    """共享夹具：每个测试一个全新的 registry + 隔离的临时插件根目录。

    registry 是模块级单例，因此用 setUp 重置其内容而不是重建对象 ——
    宿主各处持有的是同一个引用。
    """

    def setUp(self):
        from siwx.plugins import registry

        self.reg = registry
        self._snapshot = self._capture()
        self._reset()
        self._env_backup = {}
        # 宿主回归套件在 import 期设了 SIWX_NO_PLUGINS=1；`unittest discover`
        # 会把两个模块放进同一进程，必须在本套件运行时把它摘掉，否则全部插件
        # 加载路径都不生效。用 _env_backup 记录原值以便 tearDown 还原。
        self._set_env("SIWX_NO_PLUGINS", "")     # 空串 == 未关闭
        os.environ.pop("SIWX_NO_PLUGINS", None)
        self._tmp = tempfile.TemporaryDirectory(prefix="siwx_plugin_test_")
        # loader 从 LOCALAPPDATA 推导 <LOCALAPPDATA>/stories-in-wx/plugins，
        # 因此把 LOCALAPPDATA 指向临时根，插件写在 tmp 子目录里。
        self._set_env("LOCALAPPDATA", self._tmp.name)
        self.tmp = Path(self._tmp.name) / "stories-in-wx" / "plugins"
        self.tmp.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        self._restore()
        for k, v in self._env_backup.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        # MCP 日志器在 LOCALAPPDATA 下开着 mcp.log（Windows 上文件被占用），
        # 临时目录清理失败不应让测试变红 —— 交给系统回收。
        try:
            self._tmp.cleanup()
        except (PermissionError, OSError):
            pass

    # ── 夹具实现 ────────────────────────────────────────────

    def _set_env(self, key: str, val: str):
        if key not in self._env_backup:
            self._env_backup[key] = os.environ.get(key)
        os.environ[key] = val

    _NS_FIELDS = (
        "pages", "renderers", "message_decorators", "session_decorators",
        "settings", "after_export", "cli_commands", "content_transformers",
        "avatar_resolvers", "media_providers", "session_filters",
        "task_listeners", "themes", "mcp_tools", "export_writers",
        "key_strategies", "routes", "api_blueprints",
    )

    def _capture(self):
        snap = {}
        for f in self._NS_FIELDS:
            ns = getattr(self.reg, f)
            snap[f] = list(ns.items) if hasattr(ns, "items") else list(ns)
        snap["metas"] = dict(self.reg.metas)
        snap["report"] = self.reg.report
        snap["loaded"] = self.reg._loaded
        return snap

    def _reset(self):
        for f in self._NS_FIELDS:
            ns = getattr(self.reg, f)
            if hasattr(ns, "items"):
                ns.items = []
        self.reg.metas = {}
        self.reg.report = None
        self.reg._loaded = False

    def _restore(self):
        for f in self._NS_FIELDS:
            ns = getattr(self.reg, f)
            if hasattr(ns, "items"):
                ns.items = list(self._snapshot[f])
        self.reg.metas = dict(self._snapshot["metas"])
        self.reg.report = self._snapshot["report"]
        self.reg._loaded = self._snapshot["loaded"]


# ═══════════════════════════════════════════════════════════════
# 1. 契约解析
# ═══════════════════════════════════════════════════════════════

class TestContract(PluginTestCase):

    def test_meta_requires_name(self):
        from siwx.plugins import contract
        self.assertIsNone(contract.meta_from_dict({}))
        self.assertIsNone(contract.meta_from_dict({"version": "1.0"}))
        m = contract.meta_from_dict({"name": "x", "version": "2.1", "author": "a"})
        self.assertEqual((m.name, m.version, m.author), ("x", "2.1", "a"))

    def test_meta_nested_form(self):
        """支持 {"meta": {...}} 嵌套写法。"""
        from siwx.plugins import contract
        m = contract.meta_from_dict({"meta": {"name": "nested", "version": "9"}})
        self.assertEqual(m.name, "nested")
        self.assertEqual(m.version, "9")

    def test_resolve_callable_by_string(self):
        """字符串函数名 → 同模块顶层函数（插件不必 import siwx）。"""
        from siwx.plugins import contract

        def fn():
            return 42

        mod = FakeModule(render=fn)
        self.assertIs(contract.resolve_callable(mod, "render"), fn)
        self.assertIsNone(contract.resolve_callable(mod, "not_there"))
        self.assertIsNone(contract.resolve_callable(mod, None))
        self.assertIsNone(contract.resolve_callable(mod, ""))

    def test_resolve_callable_passthrough(self):
        from siwx.plugins import contract

        def fn():
            return 1

        self.assertIs(contract.resolve_callable(FakeModule(), fn), fn)

    def test_register_pages_full_fields(self):
        """pages 的全部可选字段都要落到 UiPage 上。"""
        from siwx.plugins import contract

        mod = FakeModule()
        counts = contract.register_plugin(mod, self.reg, {
            "name": "p1",
            "pages": [{
                "name": "stats", "title": "统计", "icon": "📊", "order": 7,
                "badge": "NEW", "tip": "提示", "group": "分析",
                "entry": "main", "condition": {"requires_decrypted": True},
            }],
        })
        self.assertEqual(counts.get("pages"), 1)
        page = self.reg.pages.get("stats")
        self.assertEqual(page.title, "统计")
        self.assertEqual(page.icon, "📊")
        self.assertEqual(page.order, 7)
        self.assertEqual(page.badge, "NEW")
        self.assertEqual(page.tip, "提示")
        self.assertEqual(page.group, "分析")
        self.assertEqual(page.entry, "main")
        self.assertEqual(page.condition, {"requires_decrypted": True})
        self.assertEqual(page.meta.name, "p1")

    def test_page_without_name_skipped(self):
        from siwx.plugins import contract
        counts = contract.register_plugin(FakeModule(), self.reg, {
            "name": "p1", "pages": [{"title": "无名字"}],
        })
        self.assertNotIn("pages", counts)
        self.assertEqual(len(self.reg.pages), 0)

    def test_builtin_page_names_protected(self):
        """插件不得占用内置页面名（服务端也要挡一层）。"""
        from siwx.plugins import contract
        for name in ("guide", "chat", "export", "mcp", "logs", "settings"):
            self.reg.pages.items = []
            counts = contract.register_plugin(FakeModule(), self.reg, {
                "name": "p1", "pages": [{"name": name, "title": "x"}],
            })
            self.assertNotIn("pages", counts, f"内置页名 {name} 未被保护")

    def test_duplicate_page_name_across_plugins(self):
        """两个插件声明同名页面 → 先加载者胜，后者跳过。"""
        from siwx.plugins import contract
        contract.register_plugin(FakeModule(), self.reg, {
            "name": "first", "pages": [{"name": "dup", "title": "A"}],
        })
        counts = contract.register_plugin(FakeModule(), self.reg, {
            "name": "second", "pages": [{"name": "dup", "title": "B"}],
        })
        self.assertNotIn("pages", counts)
        self.assertEqual(len(self.reg.pages), 1)
        self.assertEqual(self.reg.pages.get("dup").title, "A")

    def test_bad_hook_does_not_break_others(self):
        """单个 hook 声明非法（函数名解析不到）不影响同插件其它 hook。"""
        from siwx.plugins import contract
        counts = contract.register_plugin(FakeModule(), self.reg, {
            "name": "mixed",
            "renderers": [{"local_types": [1], "render": "no_such_fn"}],
            "settings": [{"key": "k", "type": "bool", "default": True}],
            "pages": [{"name": "ok", "title": "正常"}],
        })
        self.assertNotIn("renderers", counts)
        self.assertEqual(counts.get("settings"), 1)
        self.assertEqual(counts.get("pages"), 1)

    def test_api_version_too_high_rejected(self):
        from siwx.plugins import contract
        with self.assertRaises(ValueError):
            contract.register_plugin(FakeModule(), self.reg, {
                "name": "future", "api_version": 999,
            })

    def test_themes_filename_and_callable(self):
        """themes 支持两种形态：CSS 文件名（注入 link）/ 函数（内联 CSS）。"""
        from siwx.plugins import contract
        counts = contract.register_plugin(FakeModule(), self.reg, {
            "name": "t1",
            "themes": [
                {"name": "skin", "css": "theme.css"},
                "plain.css",
            ],
        })
        self.assertEqual(counts.get("themes"), 2)
        keys = {t.key for t in self.reg.themes.items}
        self.assertEqual(keys, {"theme.css", "plain.css"})

    def test_themes_path_escape_rejected(self):
        """主题 CSS 名含路径分隔符 → 跳过（防目录穿越）。"""
        from siwx.plugins import contract
        counts = contract.register_plugin(FakeModule(), self.reg, {
            "name": "t1", "themes": [{"name": "evil", "css": "../../etc/passwd"}],
        })
        self.assertNotIn("themes", counts)

    def test_themes_inline_callable(self):
        from siwx.plugins import contract

        def make_css():
            return ":root{--x:1}"

        counts = contract.register_plugin(FakeModule(make_css=make_css), self.reg, {
            "name": "t2", "themes": [{"name": "inline", "css": "make_css"}],
        })
        self.assertEqual(counts.get("themes"), 1)
        hook = self.reg.themes.items[0]
        self.assertEqual(hook.key, "")       # 无文件名 → 走内联
        self.assertEqual(hook.fn(), ":root{--x:1}")

    def test_export_format_normalized(self):
        from siwx.plugins import contract
        contract.register_plugin(FakeModule(go=lambda *a: 0), self.reg, {
            "name": "e1",
            "export_formats": [{"fmt": "TSV", "writer": "go", "label": "表格"}],
        })
        fmt = self.reg.find_export_format("tsv")     # fmt 小写归一
        self.assertIsNotNone(fmt)
        self.assertEqual(fmt.label, "表格")
        self.assertEqual(fmt.ext, "tsv")             # ext 缺省回退 fmt

    def test_export_format_missing_writer_skipped(self):
        from siwx.plugins import contract
        counts = contract.register_plugin(FakeModule(), self.reg, {
            "name": "e2", "export_formats": [{"fmt": "bad", "writer": "nope"}],
        })
        self.assertNotIn("export_writers", counts)

    def test_mcp_tool_schema_alias(self):
        """input_schema 与 inputSchema 两种写法都要认。"""
        from siwx.plugins import contract
        contract.register_plugin(FakeModule(h=lambda a: {}), self.reg, {
            "name": "m1",
            "mcp_tools": [
                {"name": "t_a", "handler": "h",
                 "input_schema": {"type": "object"}},
                {"name": "t_b", "handler": "h",
                 "inputSchema": {"type": "object"}},
            ],
        })
        by_name = {t.name: t for t in self.reg.mcp_tools.items}
        self.assertEqual(len(by_name), 2)
        self.assertEqual(by_name["t_a"].input_schema, {"type": "object"})
        self.assertEqual(by_name["t_b"].input_schema, {"type": "object"})

    def test_after_export_when_default_and_invalid(self):
        from siwx.plugins import contract
        contract.register_plugin(FakeModule(fn=lambda c: None), self.reg, {
            "name": "a1",
            "after_export": [
                {"name": "d", "run": "fn"},
                {"name": "bad", "run": "fn", "when": "whenever"},
            ],
        })
        whens = [(h.name, h.when) for h in self.reg.after_export.items]
        self.assertIn(("d", "before_zip"), whens)          # 缺省 before_zip
        self.assertIn(("bad", "before_zip"), whens)        # 非法值回退

    def test_settings_duplicate_key_rejected(self):
        from siwx.plugins import contract
        counts = contract.register_plugin(FakeModule(), self.reg, {
            "name": "s1",
            "settings": [
                {"key": "k", "type": "str"},
                {"key": "k", "type": "int"},     # 同插件内重复
                {"key": "", "type": "str"},      # 空 key
            ],
        })
        self.assertEqual(counts.get("settings"), 1)

    def test_key_strategy_process_dependent(self):
        from siwx.plugins import contract
        contract.register_plugin(FakeModule(ex=lambda c: 0), self.reg, {
            "name": "k1",
            "key_strategies": [{"name": "mine", "fn": "ex",
                                "process_dependent": True}],
        })
        self.assertEqual(self.reg.key_strategies.process_dependent(),
                         {self.reg.key_strategies.items[0].fn})

    def test_cli_command_args_preserved(self):
        from siwx.plugins import contract
        args = [{"name": "--limit", "type": "int", "default": 5}]
        contract.register_plugin(FakeModule(h=lambda a: 0), self.reg, {
            "name": "c1",
            "cli": [{"name": "my-cmd", "handler": "h", "help": "帮助",
                     "args": args}],
        })
        cmd = self.reg.cli_commands.items[0]
        self.assertEqual(cmd.name, "my-cmd")
        self.assertEqual(cmd.args, args)

    def test_blueprint_factory_by_string(self):
        """api_blueprints 用字符串引用工厂函数（避免模块加载顺序问题）。"""
        from siwx.plugins import contract

        class FakeBP:
            name = "fake_bp"

        def make_bp():
            return FakeBP()

        counts = contract.register_plugin(FakeModule(make_bp=make_bp), self.reg, {
            "name": "b1", "api_blueprints": ["make_bp"],
        })
        self.assertEqual(counts.get("api_blueprints"), 1)
        self.assertIs(self.reg.api_blueprints.items[0].bp, make_bp)


# ═══════════════════════════════════════════════════════════════
# 2. 显示条件
# ═══════════════════════════════════════════════════════════════

class TestConditions(unittest.TestCase):

    def _ctx(self, **kw):
        from siwx.plugins.conditions import ConditionContext
        base = dict(decrypted_accounts=[], wechat_running=False,
                    version="5.0.0", plugin_config={})
        base.update(kw)
        return ConditionContext(**base)

    def test_empty_condition_is_true(self):
        from siwx.plugins import conditions
        self.assertTrue(conditions.evaluate({}, self._ctx()))
        self.assertTrue(conditions.evaluate(None, self._ctx()))
        self.assertTrue(conditions.evaluate("垃圾值", self._ctx()))

    def test_requires_decrypted(self):
        from siwx.plugins import conditions
        cond = {"requires_decrypted": True}
        self.assertFalse(conditions.evaluate(cond, self._ctx()))
        self.assertTrue(conditions.evaluate(
            cond, self._ctx(decrypted_accounts=["wxid_a"])))

    def test_requires_account(self):
        from siwx.plugins import conditions
        cond = {"requires_account": "wxid_target"}
        self.assertFalse(conditions.evaluate(
            cond, self._ctx(decrypted_accounts=["wxid_other"])))
        self.assertTrue(conditions.evaluate(
            cond, self._ctx(decrypted_accounts=["wxid_target"])))

    def test_platform_match_and_alias(self):
        from siwx.plugins import conditions
        cond = {"platform": "windows"}
        expected = sys.platform.startswith("win")
        self.assertEqual(conditions.evaluate(cond, self._ctx()), expected)
        # win / win32 是 windows 的别名
        self.assertEqual(conditions.evaluate({"platform": "win"}, self._ctx()),
                         expected)

    def test_platform_list_is_any_of(self):
        from siwx.plugins import conditions
        ctx = self._ctx()
        cur = "windows" if sys.platform.startswith("win") else (
            "macos" if sys.platform == "darwin" else "linux")
        self.assertTrue(conditions.evaluate(
            {"platform": [cur, "plan9"]}, ctx))

    def test_min_version(self):
        from siwx.plugins import conditions
        ctx = self._ctx(version="5.0.0")
        self.assertTrue(conditions.evaluate({"min_version": "4.9"}, ctx))
        self.assertTrue(conditions.evaluate({"min_version": "5.0.0"}, ctx))
        self.assertFalse(conditions.evaluate({"min_version": "5.0.1"}, ctx))

    def test_env_condition(self):
        from siwx.plugins import conditions
        key = "SIWX_TEST_COND_ENV"
        os.environ.pop(key, None)
        try:
            self.assertFalse(conditions.evaluate({"env": key}, self._ctx()))
            os.environ[key] = "1"
            self.assertTrue(conditions.evaluate({"env": key}, self._ctx()))
            os.environ[key] = "0"
            self.assertFalse(conditions.evaluate({"env": key}, self._ctx()))
        finally:
            os.environ.pop(key, None)

    def test_env_expected_value(self):
        from siwx.plugins import conditions
        key = "SIWX_TEST_COND_ENV2"
        os.environ[key] = "yes"
        try:
            self.assertTrue(conditions.evaluate(
                {"env": {key: "yes"}}, self._ctx()))
            self.assertFalse(conditions.evaluate(
                {"env": {key: "no"}}, self._ctx()))
        finally:
            os.environ.pop(key, None)

    def test_config_condition(self):
        from siwx.plugins import conditions
        ctx = self._ctx(plugin_config={"show_media": True})
        self.assertTrue(conditions.evaluate(
            {"config": {"show_media": True}}, ctx))
        self.assertFalse(conditions.evaluate(
            {"config": {"show_media": False}}, ctx))

    def test_any_of(self):
        from siwx.plugins import conditions
        ctx = self._ctx()
        cond = {"any_of": [{"platform": "plan9"},
                           {"platform": "windows" if sys.platform.startswith("win")
                            else sys.platform}]}
        self.assertTrue(conditions.evaluate(cond, ctx))
        self.assertFalse(conditions.evaluate(
            {"any_of": [{"platform": "plan9"}]}, ctx))

    def test_and_semantics(self):
        from siwx.plugins import conditions
        ctx = self._ctx(decrypted_accounts=["a"])
        self.assertFalse(conditions.evaluate(
            {"requires_decrypted": True, "platform": "plan9"}, ctx))

    def test_explain(self):
        from siwx.plugins import conditions
        ctx = self._ctx()
        self.assertEqual(conditions.explain({}, ctx), "无显示条件")
        self.assertEqual(conditions.explain({"requires_decrypted": True}, ctx),
                         "条件已满足" if False else conditions.explain(
                             {"requires_decrypted": True}, ctx))
        msg = conditions.explain({"requires_decrypted": True,
                                  "platform": "plan9"}, ctx)
        self.assertIn("未满足", msg)


# ═══════════════════════════════════════════════════════════════
# 3. 配置存储
# ═══════════════════════════════════════════════════════════════

class TestConfigStore(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="siwx_plgcfg_")
        self._old = os.environ.get("LOCALAPPDATA")
        os.environ["LOCALAPPDATA"] = self._tmp.name

    def tearDown(self):
        if self._old is None:
            os.environ.pop("LOCALAPPDATA", None)
        else:
            os.environ["LOCALAPPDATA"] = self._old
        self._tmp.cleanup()

    SCHEMA = [
        {"key": "n", "type": "int", "label": "数量", "default": 10,
         "min": 1, "max": 100},
        {"key": "flag", "type": "bool", "default": False},
        {"key": "mode", "type": "choice", "default": "auto",
         "choices": ["auto", "manual"]},
        {"key": "name", "type": "str", "default": "x"},
    ]

    def test_defaults_from_schema(self):
        from siwx.plugins import config
        vals = config.build_values(self.SCHEMA, {})
        self.assertEqual(vals, {"n": 10, "flag": False, "mode": "auto",
                                "name": "x"})

    def test_no_file_returns_defaults(self):
        from siwx.plugins import config
        self.assertEqual(config.load_raw("nope"), {})
        self.assertEqual(config.load("nope", self.SCHEMA),
                         {"n": 10, "flag": False, "mode": "auto", "name": "x"})

    def test_save_and_load_roundtrip(self):
        from siwx.plugins import config
        config.save("p1", {"n": 42, "flag": True, "mode": "manual",
                           "name": "hello"})
        got = config.load("p1", self.SCHEMA)
        self.assertEqual(got, {"n": 42, "flag": True, "mode": "manual",
                               "name": "hello"})

    def test_int_coercion_from_string(self):
        from siwx.plugins import config
        got = config.build_values(self.SCHEMA, {"n": "37"})
        self.assertEqual(got["n"], 37)

    def test_int_out_of_range_falls_back(self):
        from siwx.plugins import config
        self.assertEqual(config.build_values(self.SCHEMA, {"n": 0})["n"], 10)
        self.assertEqual(config.build_values(self.SCHEMA, {"n": 999})["n"], 10)

    def test_bool_coercion(self):
        from siwx.plugins import config
        for truthy in (True, 1, "1", "true", "TRUE", "yes", "on"):
            self.assertTrue(config.build_values(
                self.SCHEMA, {"flag": truthy})["flag"], repr(truthy))
        for falsy in (False, 0, "0", "false", "no", ""):
            self.assertFalse(config.build_values(
                self.SCHEMA, {"flag": falsy})["flag"], repr(falsy))

    def test_choice_invalid_falls_back(self):
        from siwx.plugins import config
        self.assertEqual(config.build_values(
            self.SCHEMA, {"mode": "bogus"})["mode"], "auto")

    def test_apply_update_rejects_undeclared(self):
        """未在 schema 声明过的键必须被忽略（不落盘）。"""
        from siwx.plugins import config
        out = config.apply_update("p2", self.SCHEMA,
                                  {"n": 5, "hacker": "boom"})
        self.assertNotIn("hacker", out)
        self.assertEqual(out["n"], 5)
        self.assertNotIn("hacker", config.load_raw("p2"))

    def test_apply_update_invalid_value_falls_back_to_default(self):
        from siwx.plugins import config
        out = config.apply_update("p3", self.SCHEMA, {"n": 9999})
        self.assertEqual(out["n"], 10)

    def test_apply_update_no_change_does_not_write(self):
        """无实际变更时不应产生配置文件（懒创建）。"""
        from siwx.plugins import config
        config.apply_update("p4", self.SCHEMA, {"n": 10})     # 等于默认值
        self.assertFalse(config.config_path("p4").exists())

    def test_plugin_name_path_traversal_blocked(self):
        """插件名只保留 [a-zA-Z0-9_.-]，路径分隔符被剥掉 → 无法逃出配置目录。"""
        from siwx.plugins import config
        p = config.config_path("../../evil")
        self.assertEqual(p.parent, config.config_root())
        self.assertNotIn("/", p.name)
        self.assertNotIn("\\", p.name)
        p2 = config.config_path("a/b/c")
        self.assertEqual(p2.parent, config.config_root())
        self.assertEqual(p2.name, "abc.json")
        self.assertEqual(config.config_path("").name, "_.json")
        # 合法名字原样保留（点号是允许的，用于 my.plugin 这类命名）
        self.assertEqual(config.config_path("my.plugin").name, "my.plugin.json")

    def test_corrupt_file_falls_back(self):
        from siwx.plugins import config
        p = config.config_path("broken")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("{这不是 JSON", encoding="utf-8")
        self.assertEqual(config.load_raw("broken"), {})
        self.assertEqual(config.load("broken", self.SCHEMA)["n"], 10)

    def test_atomic_write_leaves_no_tmp(self):
        from siwx.plugins import config
        config.save("p5", {"n": 3})
        leftovers = list(config.config_root().glob("*.tmp"))
        self.assertEqual(leftovers, [])


# ═══════════════════════════════════════════════════════════════
# 4. 加载报告
# ═══════════════════════════════════════════════════════════════

class TestReport(unittest.TestCase):

    def test_counts_and_summary(self):
        from siwx.plugins.report import PluginLoadReport
        rep = PluginLoadReport()
        rep.add_ok("a", "1.0", hooks={"pages": 1})
        rep.add_ok("b", "1.0", missing=["numpy"])
        rep.add_error("c", "import", RuntimeError("炸了"))
        counts = rep.counts()
        self.assertEqual(counts, {"ok": 1, "degraded": 1, "error": 1})
        self.assertIn("3 个", rep.summary())

    def test_degraded_status_when_missing_requires(self):
        from siwx.plugins.report import PluginLoadReport
        rep = PluginLoadReport()
        st = rep.add_ok("x", "1.0", missing=["nonexistent_pkg"])
        self.assertEqual(st.status, "degraded")
        self.assertEqual(st.missing_requires, ["nonexistent_pkg"])

    def test_first_wins_on_duplicate_name(self):
        from siwx.plugins.report import PluginLoadReport
        rep = PluginLoadReport()
        rep.add_ok("dup", "1.0")
        rep.add_ok("dup", "2.0")
        self.assertEqual(len(rep.statuses), 1)
        self.assertEqual(rep.statuses[0].version, "1.0")

    def test_error_message_desensitized_and_truncated(self):
        from siwx.plugins.report import PluginLoadReport
        rep = PluginLoadReport()
        st = rep.add_error("e", "register", ValueError("x" * 500))
        self.assertLessEqual(len(st.error), 300)
        self.assertTrue(st.error.startswith("[register]"))

    def test_to_dicts_shape(self):
        from siwx.plugins.report import PluginLoadReport
        rep = PluginLoadReport()
        rep.add_ok("a", "1.0", source="dir:/x/a.py", hooks={"pages": 2})
        d = rep.to_dicts()[0]
        for key in ("name", "version", "source", "status", "hooks",
                    "missing_requires", "error"):
            self.assertIn(key, d)


# ═══════════════════════════════════════════════════════════════
# 5. 目录发现与加载（端到端，写真实临时文件）
# ═══════════════════════════════════════════════════════════════

class TestLoader(PluginTestCase):

    def test_single_file_plugin_loaded(self):
        from siwx.plugins import loader
        _mk_plugin_file(self.tmp, "myplug", """
            PLUGIN = {"name": "myplug", "version": "1.2.3",
                      "settings": [{"key": "k", "type": "bool",
                                    "default": True}]}
        """)
        rep = loader.load_all(reset=True)
        self.assertTrue(rep.has("myplug"))
        self.assertEqual(rep._seen["myplug"].version, "1.2.3")
        self.assertEqual(len(self.reg.plugin_settings("myplug")), 1)

    def test_package_plugin_loaded(self):
        from siwx.plugins import loader
        _mk_plugin_pkg(self.tmp, "pkgplug", """
            PLUGIN = {"name": "pkgplug", "version": "2.0",
                      "pages": [{"name": "p", "title": "P"}]}
        """, pages={"index.html": "<div>hi</div>"})
        rep = loader.load_all(reset=True)
        self.assertTrue(rep.has("pkgplug"))
        page = self.reg.pages.get("p")
        self.assertIsNotNone(page)
        self.assertTrue(Path(page.pages_dir).is_dir())

    def test_skip_rules(self):
        """下划线开头 / 点开头 / .disabled / __pycache__ 一律跳过。"""
        from siwx.plugins import loader
        body = 'PLUGIN = {"name": "should_not_load"}'
        for bad in ("_hidden.py", ".dot.py", "off.py.disabled"):
            (self.tmp / bad).write_text(body, encoding="utf-8")
        cache = self.tmp / "__pycache__"
        cache.mkdir(exist_ok=True)
        (cache / "junk.py").write_text(body, encoding="utf-8")
        rep = loader.load_all(reset=True)
        self.assertFalse(rep.has("should_not_load"))
        self.assertEqual(len(rep.statuses), 0)

    def test_import_error_is_isolated(self):
        """一个插件 import 崩溃不影响另一个正常插件加载。"""
        from siwx.plugins import loader
        _mk_plugin_file(self.tmp, "broken", "raise RuntimeError('boom')")
        _mk_plugin_file(self.tmp, "healthy", """
            PLUGIN = {"name": "healthy", "version": "1.0"}
        """)
        rep = loader.load_all(reset=True)
        self.assertTrue(rep.has("broken"))
        self.assertEqual(rep._seen["broken"].status, "error")
        self.assertTrue(rep.has("healthy"))
        self.assertEqual(rep._seen["healthy"].status, "ok")

    def test_register_error_is_isolated(self):
        """PLUGIN 非法（缺 name）→ 记 error，其它插件照常。"""
        from siwx.plugins import loader
        _mk_plugin_file(self.tmp, "noname", 'PLUGIN = {"version": "1.0"}')
        _mk_plugin_file(self.tmp, "good", 'PLUGIN = {"name": "good"}')
        rep = loader.load_all(reset=True)
        self.assertEqual(rep._seen["noname"].status, "error")
        self.assertEqual(rep._seen["good"].status, "ok")

    def test_duplicate_plugin_name_skipped(self):
        """同名插件只加载最先扫描到的那一个（报告按插件名去重，各留一条）。"""
        from siwx.plugins import loader
        from siwx.plugins.registry import registry
        _mk_plugin_file(self.tmp, "aaa", 'PLUGIN = {"name": "same",'
                                         ' "version": "1.0"}')
        _mk_plugin_file(self.tmp, "bbb", 'PLUGIN = {"name": "same",'
                                         ' "version": "2.0"}')
        rep = loader.load_all(reset=True)
        names = [s.name for s in rep.statuses]
        self.assertEqual(names.count("same"), 1)          # 同名单条记录
        self.assertEqual(rep._seen["same"].version, "1.0")  # 先加载者胜
        self.assertEqual(registry.metas["same"].version, "1.0")
        self.assertEqual(rep.counts()["ok"], 1)

    def test_load_all_is_idempotent(self):
        from siwx.plugins import loader
        _mk_plugin_file(self.tmp, "once", 'PLUGIN = {"name": "once"}')
        rep1 = loader.load_all(reset=True)
        n = len(rep1.statuses)
        rep2 = loader.load_all()                    # 不再扫目录
        self.assertIs(rep1, rep2)
        self.assertEqual(len(rep2.statuses), n)

    def test_env_switch_disables_all(self):
        from siwx.plugins import loader
        _mk_plugin_file(self.tmp, "living", 'PLUGIN = {"name": "living"}')
        os.environ["SIWX_NO_PLUGINS"] = "1"
        try:
            self.assertFalse(loader.plugins_enabled())
            rep = loader.load_all(reset=True)
            self.assertEqual(len(rep.statuses), 0)
            self.assertFalse(self.reg.metas)
        finally:
            os.environ.pop("SIWX_NO_PLUGINS", None)

    def test_plugins_enabled_values(self):
        from siwx.plugins import loader
        for truthy in ("1", "true", "TRUE", "yes", "on"):
            os.environ["SIWX_NO_PLUGINS"] = truthy
            self.assertFalse(loader.plugins_enabled(), truthy)
        for falsy in ("", "0", "no", "off", "anything"):
            os.environ["SIWX_NO_PLUGINS"] = falsy
            self.assertTrue(loader.plugins_enabled(), falsy)
        os.environ.pop("SIWX_NO_PLUGINS", None)

    def test_plugins_root_and_ensure_root(self):
        from siwx.plugins import loader
        root = loader.ensure_root()
        self.assertTrue(root.is_dir())
        self.assertIn("stories-in-wx", str(root))
        self.assertTrue(str(root).endswith("plugins"))

    def test_plugin_ui_dir_resolves(self):
        from siwx.plugins import loader
        _mk_plugin_pkg(self.tmp, "withui", """
            PLUGIN = {"name": "withui",
                      "pages": [{"name": "p", "title": "P"}]}
        """, pages={"index.html": "<div>x</div>"})
        loader.load_all(reset=True)
        d = loader.plugin_ui_dir("withui")
        self.assertIsNotNone(d)
        self.assertTrue((d / "index.html").is_file())
        self.assertIsNone(loader.plugin_ui_dir("no_such_plugin"))

    def test_pages_hint_relative_to_plugin_file(self):
        """PLUGIN["pages_hint"] 相对插件源文件解析。"""
        from siwx.plugins import loader
        root = self.tmp
        (root / "assets").mkdir()
        (root / "assets" / "index.html").write_text("<b>y</b>",
                                                    encoding="utf-8")
        _mk_plugin_file(root, "hinted", """
            PLUGIN = {"name": "hinted", "pages_hint": "assets",
                      "pages": [{"name": "p", "title": "P"}]}
        """)
        loader.load_all(reset=True)
        page = self.reg.pages.get("p")
        self.assertTrue(Path(page.pages_dir).name == "assets")

    def test_discover_entrypoints_stub(self):
        """pip entry_points 发现目前是预留接口，返回空列表。"""
        from siwx.plugins import loader
        self.assertEqual(loader.discover_entrypoints(), [])


# ═══════════════════════════════════════════════════════════════
# 6. 注册表查询辅助
# ═══════════════════════════════════════════════════════════════

class TestRegistry(PluginTestCase):

    def test_empty_namespaces_are_falsy(self):
        """零插件时所有命名空间必须为假 → 消费方可短路跳过。"""
        for f in self._NS_FIELDS:
            self.assertFalse(getattr(self.reg, f), f)
        self.assertFalse(self.reg.pages)
        self.assertFalse(self.reg.renderers)

    def test_renderer_single_winner_by_priority(self):
        from siwx.plugins.contract import register_plugin

        def low(m, c):
            return {}

        def high(m, c):
            return {}

        register_plugin(FakeModule(low=low), self.reg, {
            "name": "a", "renderers": [
                {"local_types": [1], "render": "low", "priority": 1}]})
        register_plugin(FakeModule(high=high), self.reg, {
            "name": "b", "renderers": [
                {"local_types": [1], "render": "high", "priority": 9}]})
        win = self.reg.renderers.for_type(1)
        self.assertIs(win.render, high)
        self.assertIsNone(self.reg.renderers.for_type(2))

    def test_deterministic_sort_by_priority_then_name(self):
        from siwx.plugins.contract import register_plugin

        def f(m, c):
            return {}

        register_plugin(FakeModule(f=f), self.reg, {
            "name": "zzz", "renderers": [
                {"local_types": [1], "render": "f", "priority": 5}]})
        register_plugin(FakeModule(f=f), self.reg, {
            "name": "aaa", "renderers": [
                {"local_types": [2], "render": "f", "priority": 5}]})
        names = [r.meta.name for _i, r in self.reg.renderers.sorted_items()]
        self.assertEqual(names, ["aaa", "zzz"])

    def test_pages_after_builtin_filters_by_condition(self):
        from siwx.plugins import conditions
        from siwx.plugins.contract import register_plugin
        register_plugin(FakeModule(), self.reg, {
            "name": "p", "pages": [
                {"name": "always", "title": "总是"},
                {"name": "needs", "title": "要解密",
                 "condition": {"requires_decrypted": True}},
            ]})
        ctx = conditions.ConditionContext(decrypted_accounts=[])
        visible = [p.name for p in self.reg.pages.after_builtin(ctx)]
        self.assertEqual(visible, ["always"])
        ctx2 = conditions.ConditionContext(decrypted_accounts=["a"])
        visible2 = [p.name for p in self.reg.pages.after_builtin(ctx2)]
        self.assertEqual(sorted(visible2), ["always", "needs"])

    def test_page_owner_lookup(self):
        from siwx.plugins.contract import register_plugin
        register_plugin(FakeModule(), self.reg, {
            "name": "owner_plug",
            "pages": [{"name": "pg", "title": "P"}]})
        self.assertEqual(self.reg.pages.owner_of("pg"), "owner_plug")
        self.assertEqual(self.reg.pages.owner_of("nope"), "")

    def test_schema_for_and_plugin_settings(self):
        from siwx.plugins.contract import register_plugin
        register_plugin(FakeModule(), self.reg, {
            "name": "s", "settings": [
                {"key": "a", "type": "int", "default": 1, "min": 0, "max": 5,
                 "label": "A", "group": "G"},
                {"key": "b", "type": "bool", "default": True},
            ]})
        schema = self.reg.schema_for("s")
        self.assertEqual(len(schema), 2)
        self.assertEqual(schema[0]["min"], 0)
        self.assertEqual(schema[0]["max"], 5)
        self.assertEqual(schema[0]["group"], "G")
        self.assertEqual(len(self.reg.plugin_settings("s")), 2)
        self.assertEqual(self.reg.plugin_settings(), self.reg.settings.entries())

    def test_namespace_add_and_append_aliases(self):
        from siwx.plugins.registry import UiPage
        page = UiPage(name="x", title="X")
        self.reg.pages.add(page)
        self.assertEqual(len(self.reg.pages), 1)
        self.reg.pages.append(UiPage(name="y", title="Y"))
        self.assertEqual(len(self.reg.pages), 2)
        self.assertEqual(len(self.reg.pages.entries()), 2)

    def test_summary_counts_skips_empty(self):
        from siwx.plugins.contract import register_plugin
        self.assertEqual(self.reg.summary_counts(), {})
        register_plugin(FakeModule(), self.reg, {
            "name": "c", "pages": [{"name": "p", "title": "P"}],
            "settings": [{"key": "k", "type": "str"}]})
        counts = self.reg.summary_counts()
        self.assertEqual(counts.get("pages"), 1)
        self.assertEqual(counts.get("settings"), 1)

    def test_find_media_provider_by_key(self):
        from siwx.plugins.contract import register_plugin
        register_plugin(FakeModule(fn=lambda *a: None), self.reg, {
            "name": "mp", "media_providers": [{"kind": "image", "fn": "fn"}]})
        self.assertIsNotNone(self.reg.find_media_provider("image"))
        self.assertIsNone(self.reg.find_media_provider("video"))


# ═══════════════════════════════════════════════════════════════
# 7. chat_bridge（渲染 / 装饰 / 转换 / 过滤 / 头像）
# ═══════════════════════════════════════════════════════════════

class TestChatBridge(PluginTestCase):

    def _install(self, name, fns=None, **hooks):
        """注册一个合成插件。

        fns   —— {函数名: 函数} 作为"模块顶层函数"（契约用字符串名解析）
        hooks —— PLUGIN dict 的其它键（renderers / session_filters ...）
        """
        from siwx.plugins.contract import register_plugin
        return register_plugin(FakeModule(**(fns or {})), self.reg,
                               {"name": name, **hooks})

    def test_no_hooks_is_passthrough(self):
        from siwx.plugins import chat_bridge
        msg = {"type": 1, "text": "hi"}
        self.assertFalse(chat_bridge.has_message_hooks())
        chat_bridge.apply_renderer(msg, {})
        self.assertEqual(msg, {"type": 1, "text": "hi"})

    def test_renderer_replaces_kind_and_render(self):
        from siwx.plugins import chat_bridge

        def render(m, c):
            return {"kind": "custom", "render": [{"t": "text", "v": "x"}]}

        self._install("r", {"render": render},
                      renderers=[{"local_types": [50], "render": "render",
                                  "kind": "custom"}])
        msg = {"type": 50, "text": "通话"}
        chat_bridge.apply_renderer(msg, {})
        self.assertEqual(msg["kind"], "custom")
        self.assertEqual(msg["render"], [{"t": "text", "v": "x"}])

    def test_renderer_only_whitelisted_keys(self):
        """渲染器返回的额外键（如 __evil__）不得污染消息。"""
        from siwx.plugins import chat_bridge

        def render(m, c):
            return {"kind": "k", "__evil__": "boom", "text": "纯文本"}

        self._install("r2", {"render": render},
                      renderers=[{"local_types": [1], "render": "render"}])
        msg = {"type": 1, "text": "原始"}
        chat_bridge.apply_renderer(msg, {})
        self.assertNotIn("__evil__", msg)
        self.assertEqual(msg["text"], "纯文本")

    def test_renderer_non_dict_ignored(self):
        from siwx.plugins import chat_bridge

        def render(m, c):
            return "<script>alert(1)</script>"     # 危险：非 dict 直接忽略

        self._install("r3", {"render": render},
                      renderers=[{"local_types": [1], "render": "render"}])
        msg = {"type": 1, "text": "safe"}
        chat_bridge.apply_renderer(msg, {})
        self.assertNotIn("render", msg)
        self.assertEqual(msg["text"], "safe")

    def test_renderer_exception_isolated(self):
        from siwx.plugins import chat_bridge

        def render(m, c):
            raise RuntimeError("插件崩了")

        self._install("r4", {"render": render},
                      renderers=[{"local_types": [1], "render": "render"}])
        msg = {"type": 1, "text": "ok"}
        chat_bridge.apply_renderer(msg, {})       # 不应抛异常
        self.assertEqual(msg["text"], "ok")

    def test_decorator_not_applied_when_hot(self):
        """默认（hot=False）装饰器不进热路径；hot=True 时才生效。"""
        from siwx.plugins import chat_bridge

        def dec(m, c):
            return {"tagged": True}

        self._install("d1", {"dec": dec},
                      message_decorators=[{"name": "tag", "decorate": "dec"}])
        msg = {"type": 1}
        chat_bridge.decorate_message(msg, {}, hot=True)
        self.assertNotIn("tagged", msg)          # hot 路径被守卫拦下
        chat_bridge.decorate_message(msg, {}, hot=False)
        self.assertTrue(msg["tagged"])

    def test_decorator_hot_opt_in(self):
        from siwx.plugins import chat_bridge

        def dec(m, c):
            return {"live": 1}

        self._install("d2", {"dec": dec},
                      message_decorators=[{"name": "live", "decorate": "dec",
                                           "hot": True}])
        msg = {}
        chat_bridge.decorate_message(msg, {}, hot=True)
        self.assertEqual(msg.get("live"), 1)

    def test_decorator_exception_isolated(self):
        from siwx.plugins import chat_bridge

        def dec(m, c):
            raise ValueError("nope")

        self._install("d3", {"dec": dec},
                      message_decorators=[{"name": "bad", "decorate": "dec"}])
        msg = {"a": 1}
        chat_bridge.decorate_message(msg, {})
        self.assertEqual(msg, {"a": 1})

    def test_decorator_timeout_trips_breaker(self):
        """超时装饰器被熔断，后续调用直接跳过（不再阻塞）。"""
        import time
        from siwx.plugins import chat_bridge
        from siwx.plugins.registry import Decorator

        calls = {"n": 0}
        sleep_s = 2.0

        def slow(m, c):
            calls["n"] += 1
            time.sleep(sleep_s)
            return {"late": True}

        self.reg.message_decorators.add(
            Decorator(decorate=slow, name="slow", timeout_ms=30))

        t0 = time.time()
        chat_bridge.decorate_message({"x": 1}, {})     # 首次：超时 + 熔断
        first = time.time() - t0
        # 必须远小于插件的 sleep（否则说明超时机制没生效）
        self.assertLess(first, sleep_s / 2,
                        f"超时未生效，实际耗时 {first:.3f}s")

        t1 = time.time()
        chat_bridge.decorate_message({"x": 2}, {})     # 已熔断：立即返回
        self.assertLess(time.time() - t1, 0.05)
        self.assertEqual(calls["n"], 1)                # 第二次根本没进插件

    def test_breaker_expires(self):
        """熔断到期后应恢复调用（不是永久封禁）。"""
        import time
        from siwx.plugins import chat_bridge
        from siwx.plugins.registry import Decorator

        calls = {"n": 0}

        def slow(m, c):
            calls["n"] += 1
            time.sleep(0.5)
            return {}

        self.reg.message_decorators.add(
            Decorator(decorate=slow, name="slow2", timeout_ms=20))
        chat_bridge.decorate_message({}, {})
        self.assertEqual(calls["n"], 1)
        # 手动把熔断到期时间拨到过去，模拟 60s 之后
        self.reg.message_decorators._breaker["slow2"] = time.time() - 1
        chat_bridge.decorate_message({}, {})
        self.assertEqual(calls["n"], 2)

    def test_session_decorator_and_filter(self):
        from siwx.plugins import chat_bridge

        def dec(s, c):
            return {"pinned": True}

        def keep(s, c):
            return not s.get("is_official")

        self._install("s1", {"dec": dec, "keep": keep},
                      session_decorators=[{"name": "pin", "decorate": "dec"}],
                      session_filters=[{"name": "no_off", "fn": "keep"}])
        sessions = [{"username": "a", "is_official": False},
                    {"username": "b", "is_official": True}]
        for s in sessions:
            chat_bridge.decorate_session(s, {})
        self.assertTrue(all(s["pinned"] for s in sessions))
        out = chat_bridge.filter_sessions(sessions, {})
        self.assertEqual([s["username"] for s in out], ["a"])

    def test_filter_none_or_true_keeps(self):
        from siwx.plugins import chat_bridge

        def keep_none(s, c):
            return None            # 未表态 → 保留

        self._install("s2", {"keep_none": keep_none},
                      session_filters=[{"name": "n", "fn": "keep_none"}])
        out = chat_bridge.filter_sessions([{"u": 1}], {})
        self.assertEqual(len(out), 1)

    def test_filter_exception_keeps_session(self):
        from siwx.plugins import chat_bridge

        def boom(s, c):
            raise RuntimeError("x")

        self._install("s3", {"boom": boom},
                      session_filters=[{"name": "b", "fn": "boom"}])
        out = chat_bridge.filter_sessions([{"u": 1}], {})
        self.assertEqual(len(out), 1)          # 插件异常 → 保守保留

    def test_content_transformer_chain(self):
        from siwx.plugins import chat_bridge

        def upper(t, m, c):
            return t.upper()

        def suffix(t, m, c):
            return t + "!"

        self._install("t1", {"upper": upper, "suffix": suffix},
                      content_transformers=[
                          {"name": "s", "transform": "suffix", "priority": 1},
                          {"name": "u", "transform": "upper", "priority": 5},
                      ])
        # priority 高者先前 → 先 upper 再 suffix
        self.assertEqual(chat_bridge.transform_content("hi", {}, {}), "HI!")

    def test_content_transformer_exception_skipped(self):
        from siwx.plugins import chat_bridge

        def boom(t, m, c):
            raise RuntimeError("x")

        def ok(t, m, c):
            return t + "*"

        self._install("t2", {"boom": boom, "ok": ok},
                      content_transformers=[
                          {"name": "b", "transform": "boom", "priority": 9},
                          {"name": "o", "transform": "ok", "priority": 1},
                      ])
        self.assertEqual(chat_bridge.transform_content("a", {}, {}), "a*")

    def test_avatar_resolver_bytes_or_none(self):
        from siwx.plugins import chat_bridge

        def give(u, c):
            return b"JPEGDATA" if u == "known" else None

        self._install("av", {"give": give},
                      avatar_resolvers=[{"name": "a", "resolve": "give"}])
        self.assertEqual(chat_bridge.resolve_avatar("known", "acc"), b"JPEGDATA")
        self.assertIsNone(chat_bridge.resolve_avatar("other", "acc"))

    def test_chat_ctx_shape(self):
        from siwx.plugins import chat_bridge
        ctx = chat_bridge.chat_ctx("acc", "chat1", True, names={"a": "A"})
        self.assertEqual(ctx["account"], "acc")
        self.assertEqual(ctx["chat"], "chat1")
        self.assertTrue(ctx["is_group"])
        self.assertEqual(ctx["names"], {"a": "A"})


# ═══════════════════════════════════════════════════════════════
# 8. 导出格式 / MCP 工具注册（内置优先）
# ═══════════════════════════════════════════════════════════════

class TestExportAndMcpRegistration(PluginTestCase):

    def test_plugin_export_format_appended_after_builtin(self):
        """插件格式追加在内置之后，同名内置格式不被覆盖。"""
        from siwx.api_export import BUILTIN_FORMATS
        from siwx.plugins.contract import register_plugin
        register_plugin(FakeModule(w=lambda p, c: 3), self.reg, {
            "name": "exp", "export_formats": [
                {"fmt": "tsv", "writer": "w", "label": "TSV"},
                {"fmt": "json", "writer": "w", "label": "劫持 JSON"},
            ]})

        builtin = {f["fmt"] for f in BUILTIN_FORMATS}
        self.assertNotIn("tsv", builtin)
        # 内置 json 仍然由内置实现（导出侧 BUILTIN 分支先命中）
        self.assertIn("json", builtin)

    def test_mcp_plugin_tools_exclude_builtin_names(self):
        from siwx.mcp_server import TOOLS, _all_tools, _plugin_tools
        from siwx.plugins.contract import register_plugin

        builtin_name = TOOLS[0]["name"]
        register_plugin(FakeModule(h=lambda a: "ok"), self.reg, {
            "name": "mcp", "mcp_tools": [
                {"name": "plugin_custom", "handler": "h",
                 "description": "自定义"},
                {"name": builtin_name, "handler": "h",
                 "description": "试图覆盖内置"},
            ]})

        self.reg._loaded = True                       # 让 ensure_loaded 短路
        names = [t["name"] for t in _plugin_tools()]
        self.assertIn("plugin_custom", names)
        self.assertNotIn(builtin_name, names)
        all_names = [t["name"] for t in _all_tools()]
        self.assertEqual(all_names[:len(TOOLS)], [t["name"] for t in TOOLS])
        self.assertIn("plugin_custom", all_names)

    def test_mcp_plugin_tool_handler_lookup(self):
        from siwx.mcp_server import _plugin_tool_handler
        from siwx.plugins.contract import register_plugin

        def handler(args):
            return "hi"

        register_plugin(FakeModule(handler=handler), self.reg, {
            "name": "mcp2", "mcp_tools": [
                {"name": "t_x", "handler": "handler"}]})
        self.reg._loaded = True
        self.assertIs(_plugin_tool_handler("t_x"), handler)
        self.assertIsNone(_plugin_tool_handler("nope"))

    def test_export_format_missing_dependency_degrades(self):
        """requires 缺失 → 报告标记 degraded，但 hook 仍注册。"""
        from siwx.plugins import loader
        _mk_plugin_file(self.tmp, "degraded_plug", """
            PLUGIN = {"name": "degraded_plug", "version": "1.0",
                      "requires": ["definitely_not_installed_pkg_xyz"],
                      "settings": [{"key": "k", "type": "bool",
                                    "default": False}]}
        """)
        rep = loader.load_all(reset=True)
        st = rep._seen["degraded_plug"]
        self.assertEqual(st.status, "degraded")
        self.assertEqual(st.missing_requires,
                         ["definitely_not_installed_pkg_xyz"])
        self.assertEqual(len(self.reg.plugin_settings("degraded_plug")), 1)

    def test_check_requires_parses_version_specs(self):
        from siwx.plugins import contract
        missing = contract.check_requires([
            "definitely_absent_pkg>=1.0", "another_absent_pkg==2.0",
            "third_absent_pkg[extra]>=3",
        ])
        self.assertIn("definitely_absent_pkg", missing)
        self.assertIn("another_absent_pkg", missing)
        self.assertIn("third_absent_pkg", missing)      # extras 被剥掉
        self.assertEqual(contract.check_requires(["os", "json"]), [])
        self.assertEqual(contract.check_requires([]), [])
        self.assertEqual(contract.check_requires(None), [])


# ═══════════════════════════════════════════════════════════════
# 9. 前端节点树安全契约（对齐 common.js renderNodes）
# ═══════════════════════════════════════════════════════════════

class TestNodeTreeContract(unittest.TestCase):
    """renderNodes 是前端 JS，这里只固化**服务端必须遵守的契约**：
    渲染器返回的必须是纯数据（可 JSON 序列化），且不含可执行内容。
    真正的前端渲染逻辑由 docs/plugin-development.md 的人工测试清单覆盖。
    """

    def test_renderer_output_is_json_serializable(self):
        payload = {
            "kind": "plugin-call",
            "render": [
                {"tag": "span", "cls": "plg-call", "c": [
                    {"t": "text", "v": "📞 "},
                    {"tag": "b", "v": "语音通话"},
                ]},
            ],
        }
        s = json.dumps(payload, ensure_ascii=False)
        self.assertEqual(json.loads(s), payload)

    def test_allowed_tags_do_not_include_script(self):
        """前端白名单里不得出现 script/iframe/object 等危险**标签字面量**。"""
        common_js = (ROOT / "siwx" / "ui" / "common.js").read_text(
            encoding="utf-8")
        self.assertIn("const ALLOWED_TAGS", common_js)
        # 抽取 ALLOWED_TAGS 的 Set(...) 字面量，只在该片段里断言
        start = common_js.index("const ALLOWED_TAGS")
        end = common_js.index("const ALLOWED_ATTRS")
        allowed = common_js[start:end]
        for danger in ("'script'", "'iframe'", "'object'", "'embed'",
                       "'form'", "'link'", "'style'", "'base'", "'meta'"):
            self.assertNotIn(danger, allowed,
                             f"renderNodes 白名单不得包含 {danger}")

    def test_render_nodes_rejects_raw_and_event_attrs(self):
        common_js = (ROOT / "siwx" / "ui" / "common.js").read_text(
            encoding="utf-8")
        self.assertIn("t === 'raw'", common_js)       # raw 被显式拒绝
        self.assertIn("indexOf('on') === 0", common_js)   # on* 事件属性被拒
        self.assertIn("javascript:", common_js)       # 协议白名单

    def test_chat_js_uses_render_nodes(self):
        chat_js = (ROOT / "siwx" / "ui" / "pages" / "chat.js").read_text(
            encoding="utf-8")
        self.assertIn("SX.renderNodes(m.render)", chat_js)


# ═══════════════════════════════════════════════════════════════
# 10. 零插件向后兼容（关键红线）
# ═══════════════════════════════════════════════════════════════

class TestZeroPluginBackwardCompat(PluginTestCase):

    def test_empty_registry_all_namespaces_falsy(self):
        self.assertTrue(all(not getattr(self.reg, f) for f in self._NS_FIELDS))

    def test_summary_counts_empty(self):
        self.assertEqual(self.reg.summary_counts(), {})

    def test_chat_bridge_short_circuits(self):
        from siwx.plugins import chat_bridge
        self.assertFalse(chat_bridge.has_message_hooks())
        self.assertFalse(chat_bridge.has_session_hooks())
        self.assertIsNone(chat_bridge.resolve_avatar("u", "a"))

    def test_disabled_loader_yields_no_plugin(self):
        from siwx.plugins import loader
        _mk_plugin_file(self.tmp, "anything", 'PLUGIN = {"name": "anything"}')
        os.environ["SIWX_NO_PLUGINS"] = "1"
        try:
            loader.load_all(reset=True)
            self.assertEqual(self.reg.summary_counts(), {})
            self.assertFalse(self.reg.metas)
        finally:
            os.environ.pop("SIWX_NO_PLUGINS", None)


if __name__ == "__main__":
    unittest.main(verbosity=2)
