import re
import xml.etree.ElementTree as ET
from core.plugin import BasePlugin, logger, on, Priority
from core.provider import LLMResponse
from core.chat.message_utils import KiraMessageBatchEvent


class XmlTagFixerPlugin(BasePlugin):
    def __init__(self, ctx, cfg: dict):
        super().__init__(ctx, cfg)
        self.enabled = cfg.get("enabled", True)
        self.only_final = cfg.get("only_final_message", True)
        self.fix_missing_msg = cfg.get("fix_missing_msg", True)
        self.fix_double_brackets = cfg.get("fix_double_brackets", True)
        # ⭐ 新增：修复 at 标签格式
        self.fix_at_tag_format = cfg.get("fix_at_tag_format", True)
        self.IGNORE_TAGS = {
            "file", "record", "video", "image", "sticker", "forward", "reply", "reasoning",
            "at", "face", "json", "lightapp", "animation", "poke", "node", "location", "share",
            "voice", "shortvideo", "gif", "cardimage", "tts", "pe", "redbag", "emoji", "img", "selfie"
        }

    async def initialize(self):
        logger.info(f"XmlTagFixerPlugin initialized (only_final={self.only_final}, fix_msg={self.fix_missing_msg}, "
                    f"double_brackets={self.fix_double_brackets}, fix_at={self.fix_at_tag_format})")

    async def terminate(self):
        logger.info("XmlTagFixerPlugin terminated")

    def _preprocess(self, xml_str: str) -> str:
        """修复双尖括号错误"""
        if not self.fix_double_brackets:
            return xml_str
        new_str = re.sub(r'<<(\w+)', r'<\1', xml_str)
        if new_str != xml_str:
            logger.debug(f"双尖括号修复: {xml_str[:80]} -> {new_str[:80]}")
        return new_str

    def _fix_at_tags(self, elem: ET.Element) -> None:
        """
        修复 at 标签格式：将 <at user_id="123" /> 转换为 <at>123</at>
        同时处理 <at user_id="123">文本</at> 的情况（如果有文本，保留文本，移除属性）
        """
        if not self.fix_at_tag_format:
            return
        for child in elem.iter():
            if child.tag == "at":
                # 情况1: <at user_id="123" /> （自闭合，属性包含 user_id）
                if child.attrib.get("user_id"):
                    qq = child.attrib.pop("user_id")
                    child.text = qq
                # 情况2: <at user_id="123">文本</at> （有文本，属性包含 user_id）
                elif child.attrib.get("user_id") and child.text:
                    # 已有文本，移除属性即可（保留文本）
                    child.attrib.pop("user_id")
                # 情况3: <at>123</at> 已经是正确的，不做任何修改

    def _wrap_text_in_element(self, elem: ET.Element) -> bool:
        """
        递归修复元素内部及尾随的裸文本。
        如果 fix_at_tag_format 开启，会处理忽略标签的尾随文本。
        如果关闭，则回退到 v1.0.3 行为（遇到忽略标签直接跳过）。
        """
        if elem.tag in self.IGNORE_TAGS:
            # 如果开启了 at 修复，忽略标签仍然需要处理其尾随文本（由父层处理）
            # 但我们在这里直接返回 False，让父层处理 tail
            return False

        modified = False

        # 处理元素的直接文本（非忽略标签）
        if elem.text and elem.text.strip() and elem.tag != "text":
            text_elem = ET.Element("text")
            text_elem.text = elem.text
            elem.text = None
            if len(elem):
                elem.insert(0, text_elem)
            else:
                elem.append(text_elem)
            modified = True

        # 遍历子元素
        children = list(elem)
        for i, child in enumerate(children):
            # ⭐ 核心：如果 fix_at_tag_format 关闭，遇到忽略标签直接跳过（v1.0.3 行为）
            if not self.fix_at_tag_format and child.tag in self.IGNORE_TAGS:
                continue

            # 递归处理子元素（如果子元素不是忽略标签）
            if child.tag not in self.IGNORE_TAGS:
                if self._wrap_text_in_element(child):
                    modified = True

            # 处理子元素的尾随文本（无论子元素是否被忽略）
            # ⭐ 当 fix_at_tag_format 开启时，忽略标签的 tail 也会被处理
            if child.tail and child.tail.strip():
                tail_text = ET.Element("text")
                tail_text.text = child.tail
                child.tail = None
                elem.insert(i + 1, tail_text)
                modified = True

        return modified

    def _fix_single_msg(self, msg_str: str) -> list[str]:
        """返回修复后的消息列表（可能拆分多个）"""
        # 先修复双尖括号（兜底）
        if self.fix_double_brackets:
            msg_str = re.sub(r'<<(\w+)', r'<\1', msg_str)

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
                return [msg_str]
        else:
            if self.fix_missing_msg:
                stripped = msg_str.strip()
                if not stripped.startswith("<msg"):
                    msg_str = f"<msg>{msg_str}</msg>"
            try:
                root = ET.fromstring(msg_str)
                if root.tag == "msg":
                    # ⭐ 先修复 at 标签格式（如果开启）
                    self._fix_at_tags(root)
                    # ⭐ 再包裹裸文本
                    self._wrap_text_in_element(root)
                    fixed = ET.tostring(root, encoding="unicode", method="xml")
                    return [fixed]
                else:
                    return [msg_str]
            except ET.ParseError as e:
                logger.debug(f"解析单个 msg 失败，跳过修复: {e}")
                return [msg_str]

    def fix_xml(self, xml_str: str) -> str:
        # 预处理全局双尖括号
        xml_str = self._preprocess(xml_str)

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
            end_idx = xml_str.find("</msg>", idx)
            if end_idx == -1:
                msg_blocks.append(xml_str[idx:])
                break
            msg_blocks.append(xml_str[idx:end_idx + 6])
            start_pos = end_idx + 6

        fixed_blocks = []
        for block in msg_blocks:
            block = block.strip()
            if not block:
                continue
            result_list = self._fix_single_msg(block)
            for fixed in result_list:
                if fixed == "<msg/>" or fixed == "<msg></msg>":
                    logger.debug("丢弃完全空的消息块")
                    continue
                fixed_blocks.append(fixed)
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
            logger.debug("已修复 XML 结构（双括号、裸露文本、拆分混合块）")
