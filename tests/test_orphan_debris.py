"""最终可见文本中的孤儿标签残片清理测试。

跑法：python3 tests/test_orphan_debris.py（自包含，无需安装 KiraAI）。
判据使用 KiraAI 的真实可见口径：只读取 msg 直接子 text 的直接文本；
不能只检查原始 `</xxx>` 子串，因为实体转义后用户仍然看得见。
"""
import importlib.util
import pathlib
import sys
import types
import xml.etree.ElementTree as ET

ROOT = pathlib.Path(__file__).resolve().parent.parent

# 最小 core 桩；包加载方式保留 main.py -> .tool_rescue 的真实相对导入。
core = types.ModuleType("core")
core_plugin = types.ModuleType("core.plugin")


class _Logger:
    def info(self, *a, **k): pass
    def warning(self, *a, **k): pass
    def error(self, *a, **k): pass
    def debug(self, *a, **k): pass
    def exception(self, *a, **k): pass


class _BasePlugin:
    def __init__(self, ctx, cfg):
        self.ctx = ctx
        self.plugin_cfg = cfg


class _On:
    def __getattr__(self, name):
        return lambda *a, **k: (lambda f: f)


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
    "core": core,
    "core.plugin": core_plugin,
    "core.provider": core_provider,
    "core.chat": core_chat,
    "core.chat.message_elements": core_elements,
    "core.chat.message_utils": core_utils,
})

PKG = "_xmlfix_orphan_test"
pkg = types.ModuleType(PKG)
pkg.__path__ = [str(ROOT)]
sys.modules[PKG] = pkg
spec = importlib.util.spec_from_file_location(PKG + ".main", ROOT / "main.py")
main_mod = importlib.util.module_from_spec(spec)
sys.modules[PKG + ".main"] = main_mod
spec.loader.exec_module(main_mod)
XmlTagFixerPlugin = main_mod.XmlTagFixerPlugin

PASS = 0
FAIL = []


def check(name, cond, detail=""):
    global PASS
    if cond:
        PASS += 1
    else:
        FAIL.append(name)
        print(f"  FAIL {name}: {detail}")


def plugin(cfg=None):
    c = {"enabled": True}
    if cfg:
        c.update(cfg)
    return XmlTagFixerPlugin(None, c)


def parseable(xml):
    try:
        ET.fromstring("<root>" + xml + "</root>")
        return True
    except Exception:
        return False


def visible(xml):
    """与 MessageDeliveryService.parse_xml 一致的用户可见文本。"""
    try:
        root = ET.fromstring("<root>" + xml + "</root>")
    except Exception:
        return None
    result = []
    for msg in root:
        if msg.tag != "msg":
            continue
        text = "".join((c.text or "").strip() for c in msg if c.tag == "text")
        if text:
            result.append(text)
    return result


def expect(name, raw, wanted, cfg=None):
    out = plugin(cfg).fix_xml(raw)
    check(name + "｜可见文本", visible(out) == wanted,
          f"want={wanted!r}, got={visible(out)!r}, xml={out!r}")
    check(name + "｜仍可解析", parseable(out), repr(out))
    return out


print("孤儿标签残片：截图、任意闭合符、保护矩阵")
print("=" * 76)

# 1. 四张截图及同类生产形态。
expect("截图 br", "</br>", [])
expect("截图 msgs", "</msgs>", [])
expect("截图 calls", "calls>", [])
expect("截图三行", "parameter>\ninvoke>\ncalls>", [])
expect("正常消息后 br", "<msg><text>电脑开机后连的是同一台</text></msg>\n</br>",
       ["电脑开机后连的是同一台"])
expect("正常消息后 calls", "<msg><text>别只看任务管理器</text></msg>\ncalls>",
       ["别只看任务管理器"])
expect("text 内实体 br", "<msg><text>&lt;/br&gt;</text></msg>", [])
expect("text 内实体 calls", "<msg><text>calls&gt;</text></msg>", [])
expect("坏嵌套 br", "<msg><text></br></text></msg>", [])
expect("msg 外实体 calls", "<msg><text>正文</text></msg>\n&lt;/calls&gt;", ["正文"])

# 2. 任意独立 </xxx>，而非只维护几张截图的白名单。
names = ["invoke", "use", "anything", "my_plugin", "plugin-custom",
         "x.y", "ns:foo", "_private", "MSG", "reasoning"]
for name in names:
    expect(f"任意根级闭合 {name}", f"</{name}>", [])
    expect(f"任意 text 闭合 {name}", f"<msg><text>&lt;/{name}&gt;</text></msg>", [])
expect("多个闭合同一行", "</use> </whatever>\n</third>", [])
expect("闭合符缩进与空白", "  </use>  \n\t</another>\t", [])
expect("闭合符混工具截断", "</use> calls> invoke>", [])
expect("非工具截断不乱删", "use>", ["use>"])

# 3. 正常正文、讲解、比较式必须保留。
expect("行内讲解闭合符", "<msg><text>教程里 &lt;/use&gt; 这样写</text></msg>",
       ["教程里 </use> 这样写"])
expect("行内讲解 calls", "<msg><text>这里的 calls&gt; 是残缺 token</text></msg>",
       ["这里的 calls> 是残缺 token"])
expect("比较式 calls", "<msg><text>若 calls&gt;0 就继续</text></msg>",
       ["若 calls>0 就继续"])
expect("普通比较", "<msg><text>a &lt; b 且 2 &gt; 1</text></msg>",
       ["a < b 且 2 > 1"])
expect("泛型文本", "<msg><text>vector&lt;T&gt; 与 a&lt;&lt;b</text></msg>",
       ["vector<T> 与 a<<b"])
expect("残片后有标点不删", "<msg><text>&lt;/use&gt;，这是示例</text></msg>",
       ["</use>，这是示例"])
expect("正常末尾散段仍救回", "<msg><text>前半句</text></msg>\n好的，我待会看",
       ["前半句", "好的，我待会看"])

# 4. 代码保护：围栏、未闭合围栏、行内代码。
expect("围栏内残片保留", "<msg><text>```\n</use>\ncalls>\n```</text></msg>",
       ["```\n</use>\ncalls>\n```"])
expect("未闭合围栏残片保留", "<msg><text>```xml\n</use>\ncalls>",
       ["```xml\n</use>\ncalls>"])
expect("行内代码闭合符保留", "<msg><text>`&lt;/use&gt;`</text></msg>",
       ["`</use>`"])
expect("双反引号代码保留", "<msg><text>``&lt;/use&gt;``</text></msg>",
       ["``</use>``"])
expect("代码所在行整体保守", "<msg><text>示例 `x` &lt;/use&gt;</text></msg>",
       ["示例 `x` </use>"])

# 5. 真功能标签、root 约定与消息语义不受影响。
root_pair = "<my_plugin>秘密</my_plugin><msg><text>正文</text></msg>"
out = expect("未知完整 root 块仍保留", root_pair, ["正文"])
check("未知 root 原文不删除", "<my_plugin>秘密</my_plugin>" in out, repr(out))

mimo = "<msg><mimo_tts>你好</mimo_tts><text>正文</text></msg>"
out = expect("no_wrap 功能标签保留", mimo, ["正文"])
check("mimo_tts 元素原样存在", "<mimo_tts>你好</mimo_tts>" in out, repr(out))

custom = "<msg><custom>abc</custom><text>正文</text></msg>"
out = expect("msg 自定义元素保留", custom, ["正文"])
check("custom 元素原样存在", "<custom>abc</custom>" in out, repr(out))

media = '<msg message_id="m1"><text>&lt;/use&gt;</text><record>x.silk</record></msg>'
out = expect("媒体与残片同消息", media, [])
check("媒体元素保留", "<record>x.silk</record>" in out, repr(out))
check("消息属性保留", 'message_id="m1"' in out, repr(out))
check("空 text 已删除", "<text" not in out, repr(out))

mixed = "<msg><text>&lt;/use&gt;</text><text>正常</text></msg>"
out = expect("多 text 只删残片", mixed, ["正常"])
check("正常 text 保留", "<text>正常</text>" in out, repr(out))
check("不留下空 text", "&lt;/use&gt;" not in out and "<text />" not in out, repr(out))

out = expect("显式静默消息不变", "<msg/>", [])
check("不伪造也不改写 msg 静默", out in ("<msg/>", "<msg />"), repr(out))
for raw in ("</use>", "calls>", "<msg><text>&lt;/use&gt;</text></msg>"):
    out = expect("纯残片静默", raw, [])
    check("纯残片不变成显式 msg 静默", out == "", repr(out))

# 6. 原有开关语义与两种散段模式。
for flag in (False, True):
    cfg = {"strip_reasoning_block": flag}
    expect(f"stray={flag} 任意残片", "</use>", [], cfg)
    expect(f"stray={flag} 截断残片", "calls>", [], cfg)
    expect(f"stray={flag} 正常尾巴", "<msg><text>A</text></msg>\n正常尾巴",
           (["A", "正常尾巴"] if flag else ["A", "正常尾巴"]), cfg)

expect("工具清理关时 invoke 保留", "</invoke>", ["</invoke>"],
       {"strip_tool_debris": False})
expect("工具清理关时 calls 保留", "calls>", ["calls>"],
       {"strip_tool_debris": False})
expect("工具清理关不影响通用 use", "</use>", [],
       {"strip_tool_debris": False})
expect("显式保真模式保留 use", "</use>", ["</use>"],
       {"fallback_strip_tags": False})
expect("显式保真模式保留 calls", "calls>", ["calls>"],
       {"fallback_strip_tags": False})

# 7. 未命中块逐字节返回；清理幂等。
block = "<msg><text>正常 &gt; 文本</text></msg>"
check("未命中块逐字节不变", plugin()._clean_orphan_debris_blocks([block]) == [block])
code_block = "<msg><text>`&lt;/use&gt;`</text></msg>"
check("代码块逐字节不变", plugin()._clean_orphan_debris_blocks([code_block]) == [code_block])
for raw in ("</use>", "calls>", "<msg><text>正文</text></msg>\n</br>",
            "<msg><text>教程 &lt;/use&gt; 保留</text></msg>"):
    once = plugin().fix_xml(raw)
    twice = plugin().fix_xml(once)
    check(f"幂等 {raw!r}", once == twice, f"once={once!r}, twice={twice!r}")

print("=" * 76)
print(f"断言：通过 {PASS}，失败 {len(FAIL)}")
for name in FAIL:
    print("  -", name)
sys.exit(1 if FAIL else 0)
