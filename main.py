import re
import xml.etree.ElementTree as ET
from typing import Optional
from xml.sax.saxutils import escape as xml_escape, unescape as xml_unescape

from core.plugin import BasePlugin, logger, on, Priority
from core.provider import LLMResponse
from core.chat import MessageChain
from core.chat.message_elements import At, Record, Reply
from core.chat.message_utils import KiraMessageBatchEvent

# MiMo TTS 插件的 plugin_id（用于接管其格式修复功能）
MIMO_PLUGIN_ID = "kira-ai-plugin-mimo-tts"


class XmlTagFixerPlugin(BasePlugin):
    def __init__(self, ctx, cfg: dict):
        super().__init__(ctx, cfg)
        self.enabled = cfg.get("enabled", True)
        self.only_final = cfg.get("only_final_message", False)
        self.fix_missing_msg = cfg.get("fix_missing_msg", True)
        self.fix_double_brackets = cfg.get("fix_double_brackets", True)
        self.fix_at_tag_format = cfg.get("fix_at_tag_format", True)
        self.convert_text_at_to_tag = cfg.get("convert_text_at_to_tag", False)
        self.escape_special_chars = cfg.get("escape_special_chars", True)
        self.fallback_wrap_text = cfg.get("fallback_wrap_text", True)
        self.fallback_strip_tags = cfg.get("fallback_strip_tags", True)
        self.flatten_no_wrap_tags = cfg.get("flatten_no_wrap_tags", True)
        self.fix_record_split = cfg.get("fix_record_split", True)
        self.split_blank_line_messages = cfg.get("split_blank_line_messages", False)
        self.merge_marker_span_msgs = cfg.get("merge_marker_span_msgs", True)
        # 排除的邮箱域名后缀（额外保护）
        self.text_at_exclude_domains = cfg.get("text_at_exclude_domains", [
            "com", "cn", "net", "org", "edu", "gov", "io", "co", "uk", "jp", "de", "fr", "ru"
        ])
        # 框架内置媒体/控制标签：内容不是普通文本，完全不递归、不包裹
        self.IGNORE_TAGS = {
            "file", "record", "video", "image", "sticker", "forward", "reply", "reasoning",
            "at", "face", "json", "lightapp", "animation", "poke", "node", "location", "share",
            "voice", "shortvideo", "gif", "cardimage", "tts", "pe", "redbag", "emoji", "img", "selfie"
        }
        # 不包裹 <text> 的自定义标签：内置 mimo_tts + 用户配置（宽容归一化）
        self.BUILTIN_NO_WRAP_TAGS = {"mimo_tts"}
        self.no_wrap_tags = set(self.BUILTIN_NO_WRAP_TAGS)
        for item in (cfg.get("no_wrap_tags") or ["mimo_tts"]):
            name = self._normalize_tag_name(item)
            if name:
                self.no_wrap_tags.add(name)
            elif item and str(item).strip():
                logger.debug(f"忽略无法识别的 no_wrap_tags 配置项: {item!r}")

        self._mimo_checked = False

    @staticmethod
    def _normalize_tag_name(item) -> str:
        """宽容地把用户盲填的内容归一化为纯标签名。

        mimo_tts / <mimo_tts> / </mimo_tts> / <mimo_tts voice="x"> / ' Mimo_TTS '
        都能识别为 mimo_tts；提取不出合法名字的返回空串。
        """
        if not item:
            return ""
        s = str(item).strip().lower()
        s = re.sub(r"^</?", "", s)
        s = re.sub(r"/?>$", "", s)
        m = re.match(r"[a-z0-9_]+", s)
        return m.group(0) if m else ""

    async def initialize(self):
        logger.info(f"XmlTagFixerPlugin initialized (only_final={self.only_final}, fix_msg={self.fix_missing_msg}, "
                    f"double_brackets={self.fix_double_brackets}, fix_at={self.fix_at_tag_format}, "
                    f"convert_at={self.convert_text_at_to_tag}, escape={self.escape_special_chars}, "
                    f"fallback={self.fallback_wrap_text}, no_wrap={sorted(self.no_wrap_tags)}, "
                    f"flatten={self.flatten_no_wrap_tags}, record_split={self.fix_record_split}, "
                    f"split_blank={self.split_blank_line_messages})")
        self._try_takeover_mimo()

    async def terminate(self):
        logger.info("XmlTagFixerPlugin terminated")

    @on.loaded()
    async def _on_loaded(self, *_):
        # 所有插件加载完成后再检测一次（覆盖 mimo 比本插件后加载的情况）
        self._try_takeover_mimo()

    def _try_takeover_mimo(self):
        """接管 MiMo TTS 插件的格式修复：将其 auto_format_fix 运行时置 False。

        本插件的 flatten_no_wrap_tags 与 fix_record_split 已完整覆盖 mimo 的
        标签摊平与语音拆分（after_xml_parse 阶段的 Record 不区分来源标签），
        两边同时做会重复处理。仅运行时改实例属性，不写 mimo 的配置文件。
        若本插件对应能力被关闭，则不动 mimo，避免修复能力出现空窗。
        """
        if self._mimo_checked:
            return
        try:
            inst = self.ctx.get_plugin_inst(MIMO_PLUGIN_ID)
        except Exception:
            return
        if inst is None:
            return
        self._mimo_checked = True
        if getattr(inst, "auto_format_fix", False):
            if self.flatten_no_wrap_tags and self.fix_record_split:
                inst.auto_format_fix = False
                logger.info("已接管 MiMo TTS 的格式修复（标签摊平 + 语音拆分由本插件处理），"
                            "mimo 插件的 auto_format_fix 已运行时关闭")

    # ========== 裸特殊字符转义 ==========

    # 非 XML 实体的 & （如 URL 参数里的 &）转义为 &amp;
    _RAW_AMP_RE = re.compile(r"&(?!amp;|lt;|gt;|quot;|apos;|#\d+;|#x[0-9a-fA-F]+;)")
    # 不构成标签开头的 < （后面不是字母或 / ），如 <?= 、a<b 、<3 转义为 &lt;
    _RAW_LT_RE = re.compile(r"<(?![a-zA-Z/])")

    def _escape_specials(self, s: str) -> str:
        if not self.escape_special_chars:
            return s
        return self._RAW_LT_RE.sub("&lt;", self._RAW_AMP_RE.sub("&amp;", s))

    # ========== 原有修复逻辑 ==========

    def _fix_at_tags(self, elem: ET.Element) -> None:
        if not self.fix_at_tag_format:
            return
        for child in elem.iter():
            if child.tag == "at":
                if child.attrib.get("user_id"):
                    qq = child.attrib.pop("user_id")
                    child.text = qq
                elif child.attrib.get("user_id") and child.text:
                    child.attrib.pop("user_id")

    def _flatten_no_wrap(self, elem: ET.Element) -> None:
        """把 no_wrap_tags 名单内标签里被模型错误嵌套的子标签剥成纯文本。

        常见错误输出：<mimo_tts><text>要说的话</text></mimo_tts>
        框架解析器只取标签的直接文本，嵌套会导致内容静默丢失。
        与 mimo 插件自带的摊平逻辑幂等，两边都开不冲突。
        """
        if not self.flatten_no_wrap_tags:
            return
        for child in list(elem):
            if child.tag in self.no_wrap_tags:
                if len(child):
                    text = "".join(child.itertext())
                    for sub in list(child):
                        child.remove(sub)
                    child.text = text
            else:
                self._flatten_no_wrap(child)

    def _wrap_text_in_element(self, elem: ET.Element) -> bool:
        if elem.tag in self.IGNORE_TAGS or elem.tag in self.no_wrap_tags:
            return False

        modified = False

        if elem.text and elem.text.strip() and elem.tag != "text":
            text_elem = ET.Element("text")
            text_elem.text = elem.text
            elem.text = None
            if len(elem):
                elem.insert(0, text_elem)
            else:
                elem.append(text_elem)
            modified = True

        children = list(elem)
        for i, child in enumerate(children):
            if not self.fix_at_tag_format and child.tag in self.IGNORE_TAGS:
                continue

            if child.tag not in self.IGNORE_TAGS and child.tag not in self.no_wrap_tags:
                if self._wrap_text_in_element(child):
                    modified = True

            if child.tail and child.tail.strip():
                tail_text = ET.Element("text")
                tail_text.text = child.tail
                child.tail = None
                elem.insert(i + 1, tail_text)
                modified = True

        return modified

    def _convert_text_at_in_element(self, elem: ET.Element, parent: ET.Element = None) -> None:
        """
        递归处理元素及其子元素，将 text 节点中的 @纯数字 替换为 at 标签。
        规则：
        - @ 前后不能是字母、数字、下划线、点号
        - 数字至少 4 位（避免误转换短数字）
        - 排除邮箱地址（@数字.后缀）通过负向先行断言实现
        """
        if not self.convert_text_at_to_tag:
            return

        # 先处理子节点（深度优先）
        for child in list(elem):
            self._convert_text_at_in_element(child, elem)

        if elem.tag == "text" and elem.text:
            txt = elem.text

            # 构建排除域名后缀的正则
            domains_pattern = '|'.join(re.escape(d) for d in self.text_at_exclude_domains)
            # 核心正则：
            # - 前后边界：前面不能是字母数字下划线点号，后面不能是字母数字下划线点号
            # - 数字至少 4 位
            # - 负向先行断言排除邮箱：@数字 后面不能直接跟 .后缀 (且后缀后跟单词边界或结束)
            pattern = rf'(?<![A-Za-z0-9_.])@(\d{{4,}})(?![A-Za-z0-9_.])(?!\.(?:{domains_pattern})(?:\b|$))'

            if not re.search(pattern, txt):
                return

            # 使用保留分隔符的方式分割
            parts = re.split(rf'(@\d{{4,}})', txt)

            new_nodes = []
            for part in parts:
                if not part:
                    continue
                m = re.match(r'@(\d{4,})', part)
                if m:
                    # 再次验证是否符合完整规则
                    if re.search(pattern, part):
                        at_elem = ET.Element("at")
                        at_elem.text = m.group(1)
                        new_nodes.append(at_elem)
                    else:
                        new_text = ET.Element("text")
                        new_text.text = part
                        new_nodes.append(new_text)
                else:
                    new_text = ET.Element("text")
                    new_text.text = part
                    new_nodes.append(new_text)

            if len(new_nodes) == 1 and new_nodes[0].tag == "text":
                return

            if parent is not None:
                idx = list(parent).index(elem)
                parent.remove(elem)
                for node in reversed(new_nodes):
                    parent.insert(idx, node)

    # ========== 空行分段拆消息 ==========

    _BLANK_LINE_RE = re.compile(r"\n[ \t]*\n+")
    # [xxx] 或 [/xxx] 式文本标记（其他插件可能用其做自定义协议，如折扇留穗 [3p]）
    _BBCODE_MARKER_RE = re.compile(r"\[/?[a-zA-Z0-9_]+\]")

    def _split_blank_lines(self, root: ET.Element) -> Optional[list]:
        """空行分段拆消息（默认关闭）。

        仅当 msg 内全是 text 且无其他功能标签时生效；
        文本含代码围栏 ``` 时整条不拆（避免代码和解释分家）；
        只认空行（两个以上连续换行），单换行不拆。
        """
        if not self.split_blank_line_messages:
            return None
        children = list(root)
        if not children or any(c.tag != "text" for c in children):
            return None
        paragraphs = []
        for c in children:
            txt = c.text or ""
            if "```" in txt:
                return None
            if self._BBCODE_MARKER_RE.search(txt):
                # 含 [xxx] 式文本标记（如折扇留穗的 [3p]...[/3p]），整条不拆，
                # 避免标记对被打散到其他消息导致其他插件无法识别
                return None
            for p in self._BLANK_LINE_RE.split(txt):
                p = p.strip()
                if p:
                    paragraphs.append(p)
        if len(paragraphs) < 2:
            return None
        results = []
        for p in paragraphs:
            msg = ET.Element("msg")
            for k, v in root.attrib.items():
                msg.set(k, v)
            te = ET.SubElement(msg, "text")
            te.text = p
            results.append(ET.tostring(msg, encoding="unicode", method="xml"))
        logger.debug(f"空行分段：单条消息拆为 {len(paragraphs)} 条发送")
        return results

    # ========== 跨消息标记对合并 ==========

    def _try_merge_text_blocks(self, blocks: list) -> Optional[str]:
        """尝试把多个 msg 块合并为一条纯文本 msg；任一块含非 text 子元素或无法解析则放弃。"""
        texts = []
        for b in blocks:
            try:
                root = ET.fromstring(b)
            except ET.ParseError:
                return None
            if root.tag != "msg":
                return None
            children = list(root)
            if not children or any(c.tag != "text" for c in children):
                return None
            part = "".join(c.text or "" for c in children).strip()
            texts.append(part)
        msg = ET.Element("msg")
        te = ET.SubElement(msg, "text")
        te.text = "\n\n".join(t for t in texts if t)
        return ET.tostring(msg, encoding="unicode", method="xml")

    def _merge_marker_spanning_blocks(self, blocks: list) -> list:
        """合并被 [xxx]...[/xxx] 标记对横跨的连续纯文本消息。

        模型有时会把 [3p] 写在一条消息、[/3p] 写在另一条，
        而折扇留穗等插件只在单条消息内匹配标记对，导致无法触发。
        这里在输出前把横跨的消息合并为一条（仅限纯文本消息），
        让标记对落在同一消息中。
        """
        if not self.merge_marker_span_msgs or len(blocks) < 2:
            return blocks
        result = []
        i = 0
        n = len(blocks)
        while i < n:
            block = blocks[i]
            opener = None
            for m in re.finditer(r"\[([a-zA-Z0-9_]+)\]", block):
                tag = m.group(1)
                if f"[/{tag}]" not in block:
                    opener = tag
                    break
            if opener is None:
                result.append(block)
                i += 1
                continue
            closer_idx = None
            for j in range(i + 1, n):
                if f"[/{opener}]" in blocks[j]:
                    closer_idx = j
                    break
            if closer_idx is None:
                result.append(block)
                i += 1
                continue
            merged = self._try_merge_text_blocks(blocks[i:closer_idx + 1])
            if merged is None:
                result.append(block)
                i += 1
                continue
            logger.debug(f"检测到 [{opener}] 标记对横跨 {closer_idx - i + 1} 条消息，已合并为一条")
            result.append(merged)
            i = closer_idx + 1
        return result

    # ========== 终极兜底 ==========

    def _strip_structural_tags(self, s: str) -> str:
        """剥离已知结构性标签（框架标签 + 不包裹名单），只保留文本内容。

        仅用于兜底清洗；用户有意写的未知字面标签（如 <div>）不受影响。
        """
        tags = self.IGNORE_TAGS | {"msg", "text"} | self.no_wrap_tags
        pattern = r"</?(?:" + "|".join(sorted(tags, key=len, reverse=True)) + r")(?:\s[^>]*)?/?>"
        return re.sub(pattern, "", s, flags=re.IGNORECASE)

    def _strip_outside_fences(self, s: str) -> str:
        """只剥离代码围栏 ``` 之外的结构标签，围栏内的代码逐字保留。

        按 ``` 分段，偶数段在围栏外、奇数段是代码；
        围栏未闭合时剩余内容保守地全部当代码保留。
        """
        parts = s.split("```")
        for i in range(0, len(parts), 2):
            parts[i] = self._strip_structural_tags(parts[i])
        return "```".join(parts)

    def _fallback_wrap(self, original_block: str) -> list:
        """所有修复手段都失败时，清洗为纯文本消息。

        两种模式（fallback_strip_tags 控制）：
        - 开（默认）：反转义后剥离结构性标签、只留干净文本，
          用户不会看到 <msg>/<text> 原文；适合日常聊天。
        - 关：整段转义保留所有内容（包括标签原文），
          适合 payload/注入测试等要求逐字保真的场景。
        保证消息能发出去，且进入记忆的永远是良构 XML。
        注意基于未转义的原始块处理，避免双重转义。
        """
        if not self.fallback_wrap_text:
            return [original_block]
        inner = original_block.strip()
        # 先反转义已有实体，后续两种模式都基于还原后的文本处理
        inner = xml_unescape(inner, {"&quot;": '"', "&apos;": "'"})
        if self.fallback_strip_tags:
            # 剥离结构性标签，只留文本；代码围栏内的内容逐字保留，不吃代码里的标签
            if "```" in inner:
                inner = self._strip_outside_fences(inner).strip()
            else:
                inner = self._strip_structural_tags(inner).strip()
        else:
            # 保真模式：只去掉最外层 msg 包裹，其余原样保留
            inner = re.sub(r"^<msg[^>]*>", "", inner)
            inner = re.sub(r"</msg>\s*$", "", inner).strip()
        if not inner:
            return [original_block]
        logger.debug(f"触发终极兜底，清洗为纯文本消息: {original_block[:80]}")
        return [f"<msg><text>{xml_escape(inner)}</text></msg>"]

    def _fix_single_msg(self, msg_str: str) -> list:
        if self.fix_double_brackets:
            msg_str = re.sub(r'<<(\w+)', r'<\1', msg_str)

        original = msg_str
        msg_str = self._escape_specials(msg_str)

        has_poke = "<poke" in msg_str and "</poke>" in msg_str
        has_text = "<text" in msg_str and "</text>" in msg_str
        if has_poke and has_text:
            logger.debug("检测到同时包含 poke 和 text 的 msg，进行拆分")
            try:
                root = ET.fromstring(msg_str)
                if root.tag != "msg":
                    return [msg_str]
                poke_elem = None
                text_elems = []
                for child in root:
                    if child.tag == "poke":
                        poke_elem = child
                    elif child.tag == "text":
                        text_elems.append(child)
                result = []
                if poke_elem is not None:
                    poke_msg = ET.Element("msg")
                    poke_msg.append(poke_elem)
                    for k, v in root.attrib.items():
                        poke_msg.set(k, v)
                    poke_str = ET.tostring(poke_msg, encoding="unicode", method="xml")
                    result.append(poke_str)
                if text_elems:
                    text_msg = ET.Element("msg")
                    for te in text_elems:
                        text_msg.append(te)
                    for k, v in root.attrib.items():
                        text_msg.set(k, v)
                    text_str = ET.tostring(text_msg, encoding="unicode", method="xml")
                    result.append(text_str)
                return result
            except Exception as e:
                logger.debug(f"拆分失败: {e}")
                return self._fallback_wrap(original)
        else:
            if self.fix_missing_msg:
                stripped = msg_str.strip()
                if not stripped.startswith("<msg"):
                    msg_str = f"<msg>{msg_str}</msg>"
            try:
                root = ET.fromstring(msg_str)
                if root.tag == "msg":
                    self._fix_at_tags(root)
                    self._flatten_no_wrap(root)
                    self._wrap_text_in_element(root)
                    self._convert_text_at_in_element(root, None)
                    split_results = self._split_blank_lines(root)
                    if split_results is not None:
                        return split_results
                    fixed = ET.tostring(root, encoding="unicode", method="xml")
                    return [fixed]
                else:
                    return [msg_str]
            except ET.ParseError as e:
                logger.debug(f"解析单个 msg 失败: {e}")
                return self._fallback_wrap(original)

    def fix_xml(self, xml_str: str) -> str:
        if self.fix_double_brackets:
            xml_str = re.sub(r'<<(\w+)', r'<\1', xml_str)

        if xml_str.strip().startswith("[") and ("Error" in xml_str or "error" in xml_str):
            return xml_str

        msg_blocks = []
        start_pos = 0
        while True:
            idx = xml_str.find("<msg", start_pos)
            if idx == -1:
                remainder = xml_str[start_pos:].strip()
                if remainder:
                    msg_blocks.append(remainder)
                break
            open_end = xml_str.find(">", idx)
            if open_end == -1:
                msg_blocks.append(xml_str[idx:])
                break
            if xml_str[open_end - 1] == "/":
                # 自闭合 <msg/> 或 <msg .../>：独立成块，避免吞掉后续消息
                msg_blocks.append(xml_str[idx:open_end + 1])
                start_pos = open_end + 1
                continue
            end_idx = xml_str.find("</msg>", open_end + 1)
            next_open = xml_str.find("<msg", open_end + 1)
            if end_idx == -1 or (next_open != -1 and next_open < end_idx):
                # 未正常闭合（没有 </msg> 或闭合前出现新 <msg）：截断为独立块，走修复/兜底
                cut = next_open if next_open != -1 else len(xml_str)
                msg_blocks.append(xml_str[idx:cut])
                start_pos = cut
                continue
            msg_blocks.append(xml_str[idx:end_idx + 6])
            start_pos = end_idx + 6

        fixed_blocks = []
        for block in msg_blocks:
            block = block.strip()
            if not block:
                continue
            result_list = self._fix_single_msg(block)
            for fixed in result_list:
                if re.fullmatch(r"<msg\s*/>|<msg>\s*</msg>", fixed.strip()):
                    logger.debug("丢弃完全空的消息块")
                    continue
                fixed_blocks.append(fixed)
        fixed_blocks = self._merge_marker_spanning_blocks(fixed_blocks)
        return "\n".join(fixed_blocks)

    @on.llm_response(priority=Priority.HIGH)
    async def on_llm_response(self, event: KiraMessageBatchEvent, resp: LLMResponse):
        if not self.enabled:
            return
        if self.only_final and resp.tool_calls:
            return
        if not resp.text_response:
            return
        original = resp.text_response
        fixed = self.fix_xml(original)
        if fixed != original:
            resp.text_response = fixed
            logger.debug("已修复 XML 结构（转义特殊字符、补全标签、摊平/拆分消息块）")

    # ========== 语音消息格式自动修复（移植自 MiMo TTS 插件）==========

    @on.after_xml_parse()
    async def fix_voice_format(self, event, actions, *_):
        """发送前把混在消息里的语音拆成单条干净消息（不带 @ 和回复）。

        QQ 上语音与 @/回复等内容混在同一条消息里会无法正常显示。
        在 after_xml_parse 阶段操作的是解析后的 MessageChain，
        官方 <record> 与 <mimo_tts> 此时都是 Record 元素，天然一并覆盖。
        """
        if not self.enabled or not self.fix_record_split:
            return
        self._try_takeover_mimo()
        try:
            new_actions = []
            changed = False
            for action in actions:
                if not isinstance(action, MessageChain):
                    new_actions.append(action)
                    continue
                if not any(isinstance(e, Record) for e in action.message_list):
                    new_actions.append(action)
                    continue
                changed = True
                new_actions.extend(self._split_voice_chain(action))
            if changed:
                actions[:] = new_actions
                logger.debug("已自动修复语音消息格式（语音单条发送，不带@/回复）")
        except Exception:
            logger.exception("语音消息格式修复异常")

    @staticmethod
    def _split_voice_chain(chain: MessageChain) -> list:
        """把含 Record 的消息链按顺序拆分：每个 Record 单独成链，其余内容保持原顺序成链。

        仅含 @/回复的碎片链会并入首个有实际内容的链（回复保持最前）；
        若整条消息只有语音，则 @/回复直接丢弃，保证语音消息绝对干净。
        """
        runs = []  # 按原始顺序的元素分组：语音单独一组，其余连续成组
        run = []
        for e in chain.message_list:
            if isinstance(e, Record):
                if run:
                    runs.append(run)
                    run = []
                runs.append([e])
            else:
                run.append(e)
        if run:
            runs.append(run)

        def has_real_content(elems) -> bool:
            return any(not isinstance(x, (At, Reply)) for x in elems)

        real_runs = [r for r in runs if not (len(r) == 1 and isinstance(r[0], Record)) and has_real_content(r)]
        stray = [e for r in runs
                 if not (len(r) == 1 and isinstance(r[0], Record)) and not has_real_content(r)
                 for e in r]

        if real_runs and stray:
            # 回复需保持在消息最前，@ 其次，其余按原相对顺序
            stray.sort(key=lambda x: 0 if isinstance(x, Reply) else 1)
            real_runs[0] = stray + real_runs[0]
        # 没有文字内容时 stray 直接丢弃

        # 按原顺序重组：文字链和语音链的先后关系保持不变
        ordered = []
        for r in runs:
            if len(r) == 1 and isinstance(r[0], Record):
                ordered.append(r)
            elif has_real_content(r):
                # 首个内容链可能已并入 stray，用 real_runs 里对应版本
                ordered.append(real_runs.pop(0) if real_runs else r)
        # real_runs 若有剩余（理论上不会），追加到末尾
        ordered.extend(real_runs)

        return [MessageChain(list(r)) for r in ordered]
