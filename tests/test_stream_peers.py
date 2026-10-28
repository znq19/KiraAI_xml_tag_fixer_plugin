"""acc / QQ 官方预览协同测试（自包含 fake peers，无外部仓库依赖）。

真实仓库版本另行做集成审计；本套件锁定插件自身的挂接、清理、台账、
请求级闸门、only_final、幂等与卸载还原契约。
"""
import asyncio
import importlib.util
import pathlib
import sys
import types

ROOT = pathlib.Path(__file__).resolve().parent.parent

core = types.ModuleType("core")
core_plugin = types.ModuleType("core.plugin")


class _Logger:
    def info(self, *a, **k): pass
    def warning(self, *a, **k): pass
    def error(self, *a, **k): pass
    def debug(self, *a, **k): pass
    def exception(self, *a, **k): pass


class _BasePlugin:
    def __init__(self, ctx, cfg): self.ctx = ctx


class _On:
    def __getattr__(self, name): return lambda *a, **k: (lambda f: f)


core_plugin.BasePlugin = _BasePlugin
core_plugin.logger = _Logger()
core_plugin.on = _On()
core_plugin.Priority = types.SimpleNamespace(HIGH=50)
core_provider = types.ModuleType("core.provider")
core_provider.LLMResponse = types.SimpleNamespace
core_chat = types.ModuleType("core.chat")
core_chat.MessageChain = types.SimpleNamespace
core_elements = types.ModuleType("core.chat.message_elements")
core_elements.At = core_elements.Record = core_elements.Reply = types.SimpleNamespace
core_utils = types.ModuleType("core.chat.message_utils")
core_utils.KiraMessageBatchEvent = types.SimpleNamespace
sys.modules.update({
    "core": core, "core.plugin": core_plugin, "core.provider": core_provider,
    "core.chat": core_chat, "core.chat.message_elements": core_elements,
    "core.chat.message_utils": core_utils,
})

PKG = "_xmlfix_stream_peer_test"
pkg = types.ModuleType(PKG); pkg.__path__ = [str(ROOT)]
sys.modules[PKG] = pkg
spec = importlib.util.spec_from_file_location(PKG + ".main", ROOT / "main.py")
main_mod = importlib.util.module_from_spec(spec)
sys.modules[PKG + ".main"] = main_mod
spec.loader.exec_module(main_mod)
rescue_spec = importlib.util.spec_from_file_location(PKG + ".tool_rescue", ROOT / "tool_rescue.py")
rescue_mod = importlib.util.module_from_spec(rescue_spec)
sys.modules[PKG + ".tool_rescue"] = rescue_mod
rescue_spec.loader.exec_module(rescue_mod)

PASS, FAIL = [], []


def check(name, cond, detail=""):
    if cond:
        PASS.append(name)
    else:
        FAIL.append(name)
        print("  FAIL", name, detail)


class FakeTool:
    name = "exec"
    def get_schema(self):
        return {"parameters": {"type": "object", "properties": {"cmd": {"type": "string"}}, "required": ["cmd"]}}


class FakeToolSet:
    def __init__(self): self.tools = [FakeTool()]
    def __contains__(self, name): return name == "exec"
    def get(self, name): return self.tools[0] if name == "exec" else None


class FakeEngine:
    def __init__(self, owner):
        self.owner = owner
        self.emit = self._emit
        self.on_complete = self._complete
    async def _emit(self, seg):
        self.owner.sent.append(seg)
        return True
    def _complete(self, resp):
        self.owner.completed += 1
        self.owner.responses.append(resp)


class FakeAcc:
    def __init__(self):
        self.sent = []
        self.completed = 0
        self.responses = []
    def _make_engine(self, request=None):
        return FakeEngine(self)


def display_text_of(raw):
    return raw


class FakeQQManager:
    def __init__(self):
        self._sessions = {}
        self.calls = 0
    def observe_raw(self, adapter, client, target, msg_id, raw):
        self.calls += 1
        text = display_text_of(raw)
        if text:
            self._sessions[target] = types.SimpleNamespace(value=text)


class Ctx:
    def __init__(self, acc, qq=None):
        self.acc = acc
        self.qq = types.SimpleNamespace(c2c_stream=qq) if qq is not None else None
        self.tool_mgr = types.SimpleNamespace(tool_set=FakeToolSet())
    def get_plugin_inst(self, pid):
        if pid == main_mod.ACC_PLUGIN_ID: return self.acc
        if pid == main_mod.QQBOT_PLUGIN_ID: return self.qq
        return None


class Resp:
    def __init__(self, text="", calls=None, segs=None):
        self.text_response = text
        self.tool_calls = calls or []
        if segs is not None:
            self._accel_early_sent_segments = segs


def fixer(acc, qq=None, cfg=None):
    c = {"enabled": True, "tool_tag_rescue": True}
    if cfg: c.update(cfg)
    p = main_mod.XmlTagFixerPlugin(Ctx(acc, qq), c)
    p._rescue = rescue_mod.ToolRescue(p)
    return p


print("流式协同：acc 台账 / QQ 预览 / 生命周期")
print("=" * 76)

# 1. acc 正常段和原回调完全保留。
acc = FakeAcc()
p = fixer(acc)
p._try_attach_stream_peers()
p._try_attach_stream_peers()
engine = acc._make_engine(types.SimpleNamespace(tools=None, tool_set=None))
seg = "<msg><text>正常</text></msg>"
assertion = asyncio.run(engine.emit(seg))
check("acc 正常段原样投递", assertion is True and acc.sent == [seg], acc.sent)
resp = Resp(segs=[seg])
engine.on_complete(resp)
check("acc 正常台账不变且原完成回调执行", resp._accel_early_sent_segments == [seg] and acc.completed == 1)

# 2. 纯残片消费但不投递；混合段正文和台账同步，避免重复。
acc.sent.clear()
debris = "<msg><text>&lt;/use&gt;</text></msg>"
check("acc 纯残片按已消费处理", asyncio.run(engine.emit(debris)) is True and not acc.sent, acc.sent)
resp = Resp(segs=[debris])
engine.on_complete(resp)
check("acc 纯残片台账同步为空", resp._accel_early_sent_segments == [""], resp.__dict__)

mixed = "<msg><text>前\n&lt;/use&gt;\n后</text></msg>"
cleaned = "<msg><text>前\n后</text></msg>"
acc.sent.clear()
asyncio.run(engine.emit(mixed))
resp = Resp(segs=[mixed])
engine.on_complete(resp)
check("acc 混合段只清残片", acc.sent == [cleaned], acc.sent)
check("acc 混合台账与最终文本一致", resp._accel_early_sent_segments == [cleaned], resp.__dict__)

# 3. 真实 ToolRescue：带工具请求翻译；无工具直调不翻译。
raw_tool = ('<msg><text>我看看</text></msg>\n<invoke name="exec">\n'
            '<parameter name="cmd">ls</parameter>\n</invoke>')
req_with = types.SimpleNamespace(tools=[{"type": "function"}], tool_set=None)
engine_with = acc._make_engine(req_with)
resp = Resp(raw_tool)
engine_with.on_complete(resp)
check("acc 流式完成时恢复原生工具调用", len(resp.tool_calls) == 1
      and resp.tool_calls[0]["function"]["name"] == "exec", resp.tool_calls)
check("acc 搭救后工具残片剥离", "invoke" not in resp.text_response and "我看看" in resp.text_response)

req_without = types.SimpleNamespace(tools=None, tool_set=None)
engine_without = acc._make_engine(req_without)
resp = Resp(raw_tool)
engine_without.on_complete(resp)
check("acc 无工具直调仍不翻译", not resp.tool_calls and "invoke" in resp.text_response)

# 4. only_final：工具步不做台账清理，与最终跳过修复保持一致。
acc2 = FakeAcc()
p2 = fixer(acc2, cfg={"only_final_message": True})
p2._try_attach_stream_peers()
engine2 = acc2._make_engine(req_with)
resp = Resp(raw_tool, calls=[{"id": "native"}], segs=[mixed])
engine2.on_complete(resp)
check("only_final 工具步保留原台账", resp._accel_early_sent_segments == [mixed], resp.__dict__)

# 5. 卸载恢复；新 acc 实例热重载后可重新挂接。
original = p2._acc_original_make_engine
p2._detach_stream_peers()
check("acc 卸载只还原本层", acc2._make_engine == original)
acc3 = FakeAcc()
p2.ctx.acc = acc3
p2._try_attach_stream_peers()
check("acc 新实例可重新挂接", getattr(acc3._make_engine, "_xtf_acc_owner", None) is p2)

# 6. QQ 预览：只清理展示文本；正常、行内讲解和代码保留。
acc = FakeAcc(); qq = FakeQQManager()
p = fixer(acc, qq)
p._try_attach_stream_peers()
p._try_attach_stream_peers()
for target, raw, wanted in [
    ("normal", "正常正文", "正常正文"),
    ("mixed", "正常\n&lt;/use&gt;", "正常\n"),
    ("inline", "教程 &lt;/use&gt; 保留", "教程 &lt;/use&gt; 保留"),
    ("code", "```\n&lt;/use&gt;", "```\n&lt;/use&gt;"),
]:
    qq.observe_raw(None, object(), target, "m", raw)
    sess = qq._sessions.get(target)
    check(f"QQ 预览保留/清理 {target}", sess is not None and sess.value == wanted,
          getattr(sess, "value", None))
for target, raw in [("use", "&lt;/use&gt;"), ("calls", "calls>"),
                    ("parameter", "parameter>"), ("msgs", "&lt;/msgs&gt;")]:
    qq.observe_raw(None, object(), target, "m", raw)
    check(f"QQ 纯残片预览抑制 {target}", target not in qq._sessions)

original = p._qq_preview_original
p._detach_stream_peers()
check("QQ 卸载只还原本层", qq.observe_raw == original)

# 7. 总开关与显式保真模式优先。
acc = FakeAcc(); qq = FakeQQManager()
p = fixer(acc, qq, cfg={"enabled": False})
p._try_attach_stream_peers()
check("总开关关闭不挂流式协同", not hasattr(acc._make_engine, "_xtf_acc_owner")
      and not hasattr(qq.observe_raw, "_xtf_qq_owner"))

acc = FakeAcc()
p = fixer(acc, cfg={"fallback_strip_tags": False})
p._try_attach_stream_peers()
engine = acc._make_engine(req_without)
seg = "<msg><text>&lt;/use&gt;</text></msg>"
asyncio.run(engine.emit(seg))
check("显式保真模式下 acc 原文保留", acc.sent == [seg], acc.sent)

# 8. 协同自身异常/热停用：必须回原实现，不拖垮模型调用。
acc = FakeAcc()
p = fixer(acc)
p._try_attach_stream_peers()
engine = acc._make_engine(req_without)
def boom(_):
    raise RuntimeError("injected cleanup failure")
p._clean_orphan_debris_blocks = boom
seg = "<msg><text>正常</text></msg>"
check("acc 清理异常按原段放行", asyncio.run(engine.emit(seg)) is True and acc.sent == [seg], acc.sent)
p.enabled = False
seg2 = "<msg><text>&lt;/use&gt;</text></msg>"
check("acc 热停用后回原 emit", asyncio.run(engine.emit(seg2)) is True and acc.sent[-1] == seg2, acc.sent)

print("=" * 76)
print(f"断言：通过 {len(PASS)}，失败 {len(FAIL)}")
for name in FAIL:
    print("  -", name)
sys.exit(1 if FAIL else 0)
