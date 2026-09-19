"""macos_lldb 策略回归测试（issue #3）。

用两类手段覆盖修复点：
  1. 静态检查：渲染出的内嵌脚本可编译，且关键 API 用法正确
     （AttachToProcessWithID 首参必须是 listener、用 FindRegister 而非
     不存在的 GetRegisterByName、用 FindSymbols 而非 FindSymbol、
     listener 事件泵存在）；
  2. 动态执行：注入一个模拟 lldb 模块执行整段脚本，验证
     - sqlite3_key_v2 断点命中后从 rdx/x2 读到 pKey、从 rcx/x3 读到
       nKey=32，并成功打印 OK:<hex>；
     - 寄存器缺失时回退到 $argN 表达式求值仍能捕获；
     - 无符号时输出 FAIL:NoSymbol 并 Detach；
  3. 若本机可导入真实 lldb Python 绑定（macOS 开发者环境），再校验
     上述 API 在当前绑定中真实存在，防止 lldb 版本差异导致回归。

运行：
    python -m unittest discover -s tests -v
"""
import io
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from siwx.strategies.macos_lldb import _build_lldb_script

# lldb 常量取值（与真实绑定一致；测试内部自洽即可）
eStateRunning = 5
eStateStopped = 7
eStateExited = 11
eStateCrashed = 9
eStateDetached = 10
eStateUnloaded = 1
eStopReasonBreakpoint = 2
LLDB_INVALID_ADDRESS = (1 << 64) - 1


# ── 模拟 lldb 模块 ───────────────────────────────────────────────

class _FakeSBValue:
    def __init__(self, valid=True, unsigned=0):
        self._valid = valid
        self._unsigned = unsigned

    def IsValid(self):
        return self._valid

    def GetValueAsUnsigned(self):
        return self._unsigned


class _FakeSBFrame:
    def __init__(self, regs=None, exprs=None):
        self._regs = regs or {}
        self._exprs = exprs or {}

    def FindRegister(self, name):
        if name in self._regs:
            return _FakeSBValue(True, self._regs[name])
        return _FakeSBValue(False)

    def EvaluateExpression(self, expr):
        v = self._exprs.get(expr)
        return v  # None 或 _FakeSBValue


class _FakeSBThread:
    def __init__(self, bp_id, frame, stop_reason=eStopReasonBreakpoint):
        self._bp_id = bp_id
        self._frame = frame
        self._stop_reason = stop_reason

    def GetStopReason(self):
        return self._stop_reason

    def GetStopReasonDataAtIndex(self, _i):
        return self._bp_id

    def GetFrameAtIndex(self, _i):
        return self._frame


class _FakeSBError:
    def Fail(self):
        return False


class _FakeSBEvent:
    pass


class _FakeSBListener:
    """每次 WaitForEvent 弹出一个进程事件；事件耗尽后返回 False。

    真实 lldb 中，事件被 listener 处理后 SBProcess 的公共状态会同步更新
    （这正是脚本必须自己泵事件的原因）；这里同样把状态写回 process，
    否则 GetState() 会永远停在 eStateRunning。
    """

    def __init__(self, states, process=None):
        self._states = list(states)
        self._process = process

    def WaitForEvent(self, _secs, _ev):
        if self._states:
            st = self._states.pop(0)
            self._last_state = st
            if self._process is not None:
                self._process._state = st
            return True
        return False


class _FakeSBProcess:
    def __init__(self, threads, read_bytes, state=eStateStopped):
        self._threads = threads
        self._read = read_bytes
        self._state = state
        self.detached = False

    def IsValid(self):
        return True

    def GetState(self):
        return self._state

    def Continue(self):
        self._state = eStateRunning

    def Detach(self):
        self.detached = True

    def ReadMemory(self, _addr, _size, _error):
        return self._read

    def __iter__(self):
        return iter(self._threads)


class _FakeBreakpoint:
    def __init__(self, bid, nloc):
        self._id = bid
        self._nloc = nloc

    def GetID(self):
        return self._id

    def GetNumLocations(self):
        return self._nloc


class _FakeTarget:
    def __init__(self, process, bps, modules=()):
        self._process = process
        self._bps = bps
        self._modules = modules
        self.listener_seen = None

    def AttachToProcessWithID(self, listener, pid, error):
        # 旧代码误传 SBDebugger；首参必须是 listener，这里强类型校验
        if not isinstance(listener, _FakeSBListener):
            raise TypeError("AttachToProcessWithID first arg must be SBListener")
        self.listener_seen = listener
        return self._process

    def BreakpointCreateByName(self, name):
        return self._bps.get(name, _FakeBreakpoint(99, 0))

    def BreakpointCreateByAddress(self, _addr):
        return _FakeBreakpoint(100, 1)

    def module_iter(self):
        return iter(self._modules)


class _FakeDebugger:
    _inst = None

    def __init__(self, target):
        self._target = target
        self._listener = _FakeSBListener([])

    @staticmethod
    def Create():
        if _FakeDebugger._inst is None:
            _FakeDebugger._inst = _FakeDebugger.__new__(_FakeDebugger)
        return _FakeDebugger._inst

    def SetAsync(self, flag):
        self._async = flag

    def GetListener(self):
        return self._listener

    def CreateTarget(self, _exe):
        return self._target


_SAVED_LLDB_MODULE = None


def _install_fake_lldb(listener_states, target):
    """把模拟 lldb 模块挂进 sys.modules 并返回模块对象。

    先保存真实 lldb 模块（若有），供 tearDown 还原，避免污染套件中
    后续依赖 lldb 的测试（如 RealLldbApiTest）。
    """
    import types

    global _SAVED_LLDB_MODULE
    if _SAVED_LLDB_MODULE is None:
        _SAVED_LLDB_MODULE = sys.modules.get("lldb")

    fake = types.ModuleType("lldb")
    fake.SBDebugger = _FakeDebugger
    fake.SBError = _FakeSBError
    fake.SBEvent = _FakeSBEvent
    fake.SBProcess = types.SimpleNamespace(
        EventIsProcessEvent=lambda ev: True,
        GetStateFromEvent=lambda ev: ev._state if hasattr(ev, "_state") else eStateStopped,
    )
    fake.eStateRunning = eStateRunning
    fake.eStateStopped = eStateStopped
    fake.eStateExited = eStateExited
    fake.eStateCrashed = eStateCrashed
    fake.eStateDetached = eStateDetached
    fake.eStateUnloaded = eStateUnloaded
    fake.eStopReasonBreakpoint = eStopReasonBreakpoint
    fake.LLDB_INVALID_ADDRESS = LLDB_INVALID_ADDRESS

    # listener：脚本从 attach 时传入的 listener 上取事件
    listener = _FakeSBListener(listener_states, target._process)
    _FakeDebugger._inst = _FakeDebugger(target)
    _FakeDebugger._inst._listener = listener

    sys.modules["lldb"] = fake
    return fake


def _restore_lldb():
    """还原 sys.modules 里的 lldb 模块（恢复到安装假模块之前）。"""
    global _SAVED_LLDB_MODULE
    if _SAVED_LLDB_MODULE is None:
        sys.modules.pop("lldb", None)
    else:
        sys.modules["lldb"] = _SAVED_LLDB_MODULE
    _SAVED_LLDB_MODULE = None


def _run_script(pid=12345):
    """编译并执行渲染出的脚本，返回 stdout。"""
    src = _build_lldb_script(pid)
    compile(src, "<macos_lldb_script>", "exec")
    out = io.StringIO()
    with redirect_stdout(out):
        exec(compile(src, "<macos_lldb_script>", "exec"), {"__name__": "macos_test"})
    return out.getvalue()


def _make_v2_scenario(read_bytes):
    """构造：bp_v2 命中，寄存器 rdx=地址、rcx=32。"""
    bps = {"sqlite3_key": _FakeBreakpoint(1, 1), "sqlite3_key_v2": _FakeBreakpoint(2, 1)}
    frame = _FakeSBFrame(regs={"rdx": 0x7000, "rcx": 32})
    thread = _FakeSBThread(bp_id=2, frame=frame)
    process = _FakeSBProcess([thread], read_bytes)
    return _FakeTarget(process, bps)


# ── 测试用例 ─────────────────────────────────────────────────────

class MacosLldbScriptTest(unittest.TestCase):

    def tearDown(self):
        _restore_lldb()

    def test_script_renders_and_compiles(self):
        src = _build_lldb_script(4242)
        compile(src, "<macos_lldb_script>", "exec")  # 抛错即失败
        self.assertIn("pid = 4242", src)

    def test_api_invariants(self):
        """关键 API 用法正确：listener 首参、FindRegister、FindSymbols、事件泵。"""
        src = _build_lldb_script(1)
        # issue #3 根因：AttachToProcessWithID 必须传 listener（而非 debugger）
        self.assertIn("AttachToProcessWithID(debugger.GetListener(), pid, error)", src)
        # 寄存器读取：SBValueList 没有 GetRegisterByName
        self.assertIn("frame.FindRegister(n)", src)
        self.assertNotIn("GetRegisterByName", src)
        # 符号回退：SBModule.FindSymbols 返回 SBSymbolContextList
        self.assertIn("mod.FindSymbols(fn)", src)
        # 事件泵：不能只轮询 GetState()
        self.assertIn("listener.WaitForEvent", src)
        self.assertIn("SBProcess.EventIsProcessEvent", src)
        # 顶层异常必须可见，避免再出现无解的 "module importing failed"
        self.assertIn("FAIL:ScriptError", src)

    def test_capture_v2_success(self):
        key = bytes.fromhex("00112233445566778899aabbccddeeff1032547698badcfe0123456789abcdef")
        target = _make_v2_scenario(key)
        _install_fake_lldb([eStateStopped], target)
        out = _run_script()
        self.assertIn("BP:2", out)
        self.assertIn(f"OK:{key.hex()}", out)
        self.assertTrue(target._process.detached, "捕获成功路径应 Detach 而非 Kill")

    def test_capture_v1_expr_fallback(self):
        """v1 分支 + 寄存器缺失时回退 $argN 表达式求值。"""
        key = bytes.fromhex("deadbeef" * 8)
        bps = {"sqlite3_key": _FakeBreakpoint(1, 1), "sqlite3_key_v2": _FakeBreakpoint(2, 0)}
        frame = _FakeSBFrame(
            regs={},  # 寄存器读不到
            exprs={
                "(const void*)$arg2": _FakeSBValue(True, 0x8000),
                "(int)$arg3": _FakeSBValue(True, 32),
            },
        )
        thread = _FakeSBThread(bp_id=1, frame=frame)  # 命中 sqlite3_key（非 v2）
        process = _FakeSBProcess([thread], key)
        target = _FakeTarget(process, bps)
        _install_fake_lldb([eStateStopped], target)
        out = _run_script()
        self.assertIn(f"OK:{key.hex()}", out)

    def test_no_symbol_fail(self):
        """符号都找不到时打印 FAIL:NoSymbol 并 Detach。"""
        bps = {"sqlite3_key": _FakeBreakpoint(1, 0), "sqlite3_key_v2": _FakeBreakpoint(2, 0)}
        process = _FakeSBProcess([], b"")
        target = _FakeTarget(process, bps)
        _install_fake_lldb([], target)
        out = _run_script()
        self.assertIn("BP:0", out)
        self.assertIn("FAIL:NoSymbol", out)
        self.assertTrue(target._process.detached)

    def test_attach_rejects_debugger_as_listener(self):
        """旧代码把 debugger 传给 AttachToProcessWithID 会直接抛 TypeError。"""
        target = _make_v2_scenario(b"x" * 32)
        _install_fake_lldb([eStateStopped], target)
        out = _run_script()
        self.assertNotIn("ScriptError", out)
        self.assertIn("OK:", out)


def _real_lldb():
    """返回真实 lldb Python 绑定；缺失或为残缺桩（无 SBDebugger）时返回 None。"""
    try:
        import lldb
    except ImportError:
        return None
    return lldb if hasattr(lldb, "SBDebugger") else None


@unittest.skipIf(_real_lldb() is None, "本机无真实 lldb Python 绑定，跳过真实 API 校验")
class RealLldbApiTest(unittest.TestCase):
    """真实 lldb 绑定上的 API 存在性校验（macOS 开发者环境有效）。"""

    def setUp(self):
        self.lldb = _real_lldb()

    def test_apis_exist(self):
        l = self.lldb
        self.assertTrue(hasattr(l.SBTarget, "AttachToProcessWithID"))
        self.assertIn("SBListener", l.SBTarget.AttachToProcessWithID.__doc__)
        self.assertTrue(hasattr(l.SBFrame, "FindRegister"))
        self.assertTrue(hasattr(l.SBModule, "FindSymbols"))
        self.assertTrue(hasattr(l.SBListener, "WaitForEvent"))
        self.assertTrue(hasattr(l.SBProcess, "EventIsProcessEvent"))
        self.assertTrue(hasattr(l.SBProcess, "GetStateFromEvent"))
        self.assertFalse(hasattr(l.SBValueList, "GetRegisterByName"),
                         "SBValueList.GetRegisterByName 不存在，旧代码依赖它必然失败")


if __name__ == "__main__":
    unittest.main()
