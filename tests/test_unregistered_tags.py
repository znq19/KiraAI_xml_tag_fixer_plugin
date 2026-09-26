"""未注册标签（reasoning/thinking/think）行为测试。

覆盖三件事：
  Q1 ON/OFF 时，未注册标签里的内容（LLM 没包 msg/text）会不会被发出去？
  Q2 真消息会不会被未闭合的思考标签吞掉？
  Q3 LLM 把这类标签写在 <msg> 里时行为是否正常？

用法： python3 tests/test_unregistered_tags.py
"""
import os
import sys
import xml.etree.ElementTree as ET

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, '/tmp/h')   # 本地 harness（提供 core.* 桩模块）

from main import XmlTagFixerPlugin  # noqa: E402

REG = {"text", "at", "emoji", "record", "image", "file", "video",
       "poke", "reply", "forward", "json"}

PASS = 0
FAIL = []


def ck(name, cond, detail=""):
    global PASS
    if cond:
        PASS += 1
    else:
        FAIL.append(name)
        print(f"  FAIL {name}  {detail}")


def P(flag):
    return XmlTagFixerPlugin(None, {"enabled": True, "strip_reasoning_block": flag})


def sent(xml):
    """用户实际收到的消息文本（忠实框架语义：msg 直接子元素的直接文本）。"""
    try:
        r = ET.fromstring("<root>" + xml + "</root>")
    except Exception:
        return None
    out = []
    for e in r:
        if e.tag != "msg":
            continue
        out.append("".join((c.text or "").strip() for c in e if c.tag in REG))
    return out


def parseable(xml):
    try:
        ET.fromstring("<root>" + xml + "</root>")
        return True
    except Exception:
        return False


R, RC = "<reasoning>", "</reasoning>"
T, TC = "<thinking>", "</thinking>"

print("未注册标签行为测试")
print("=" * 72)

# ---- Q1：思考内容不得泄漏成用户可见消息 ----
LEAKY = ["我该先搜索", "想想", "中间的思考", "I should search", "草稿",
         "我在想", "let me think", "我还没想好", "内部思考"]
CASES_LEAK = [
    ("闭合 reasoning + 真消息", f"{R}用户想要热搜，我该先搜索{RC}<msg><text>好嘞我去搜搜</text></msg>"),
    ("闭合 reasoning 在两条 msg 间", f"<msg><text>A</text></msg>{R}中间的思考{RC}<msg><text>B</text></msg>"),
    ("闭合 thinking", f"{T}I should search first{TC}<msg><text>好嘞</text></msg>"),
    ("reasoning 内 msg 草稿", f"{R}草稿<msg><text>示例</text></msg>{RC}<msg><text>正式</text></msg>"),
    ("未闭合 reasoning + 真消息", f"{R}我在想……<msg><text>真消息</text></msg>"),
    ("未闭合 thinking + 真消息", f"{T}let me think<msg><text>真消息</text></msg>"),
    ("只有思考块", f"{R}我还没想好要不要说话{RC}"),
    ("msg 内 reasoning", f"<msg>{R}内部思考{RC}<text>正文</text></msg>"),
]
for flag in (True, False):
    for label, raw in CASES_LEAK:
        out = P(flag).fix_xml(raw)
        seen = " ".join(sent(out) or [])
        hits = [w for w in LEAKY if w in seen]
        ck(f"{'ON' if flag else 'OFF'} 不泄漏思考[{label}]", not hits, f"泄漏 {hits} / {seen!r}")
        ck(f"{'ON' if flag else 'OFF'} 可解析[{label}]", parseable(out), f"{out!r}")

# ---- Q2：真消息不得被未闭合思考标签吞掉 ----
CASES_KEEP = [
    ("未闭合 reasoning + 真消息", f"{R}我在想……<msg><text>真消息</text></msg>", "真消息"),
    ("未闭合 thinking + 真消息", f"{T}let me think<msg><text>真消息</text></msg>", "真消息"),
    ("闭合 reasoning + 真消息", f"{R}我该先搜索{RC}<msg><text>真消息</text></msg>", "真消息"),
    ("reasoning 草稿 + 正式消息",
     f"{R}草稿<msg><text>示例</text></msg>{RC}<msg><text>正式</text></msg>", "正式"),
    ("多条 msg 夹 reasoning",
     f"<msg><text>A</text></msg>{R}想{RC}<msg><text>B</text></msg>", "B"),
]
for flag in (True, False):
    for label, raw, must in CASES_KEEP:
        out = P(flag).fix_xml(raw)
        seen = " ".join(sent(out) or [])
        ck(f"{'ON' if flag else 'OFF'} 真消息保留[{label}]", must in seen, f"丢了 {must!r} / {seen!r}")

# ---- Q3：msg 内讲这类标签时行为正常 ----
CASES_INMSG = [
    ("msg 内 text 提到 reasoning 这个词",
     f"<msg><text>讲一下 {R} 这个标签</text></msg>", "讲一下"),
    ("msg 内 text 嵌真 reasoning 元素",
     f"<msg><text>看这个 <reasoning>草稿</reasoning> 标签</text></msg>", "看这个"),
    ("正常消息", "<msg><text>你好</text></msg>", "你好"),
    ("裸回复", "今天天气不错", "今天天气不错"),
]
for flag in (True, False):
    for label, raw, must in CASES_INMSG:
        out = P(flag).fix_xml(raw)
        seen = " ".join(sent(out) or [])
        ck(f"{'ON' if flag else 'OFF'} 正常[{label}]", must in seen, f"{out!r}")
        ck(f"{'ON' if flag else 'OFF'} 可解析[{label}]", parseable(out), f"{out!r}")

# ---- 记忆形态：未注册标签原文应保留（框架把 resp.text_response 写回记忆）----
out = P(False).fix_xml(f"{R}用户想要热搜{RC}<msg><text>好嘞</text></msg>")
ck("记忆保留 reasoning 原文（约定不失传）", "reasoning" in out and "用户想要热搜" in out, f"{out!r}")

print("=" * 72)
print(f"断言：通过 {PASS}，失败 {len(FAIL)}")
for n in FAIL:
    print("  -", n)
sys.exit(1 if FAIL else 0)
