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
        self.IGNORE_TAGS = {
            "file", "record", "video", "image", "sticker", "forward", "reply", "reasoning",
            "at", "face", "json", "lightapp", "animation", "poke", "node", "location", "share",
            "voice", "shortvideo", "gif", "cardimage", "tts", "pe", "redbag", "emoji", "img", "selfie"
        }

    async def initialize(self):
        logger.info(f"XmlTagFixerPlugin initialized (only_final={self.only_final}, fix_msg={self.fix_missing_msg}, "
                    f"double_brackets={self.fix_double_brackets})")

    async def terminate(self):
        logger.info("XmlTagFixerPlugin terminated")

    def _preprocess(self, xml_str: str) -> str:
        """修复双尖括号错误"""
        if not self.fix_double_brackets:
            return xml_str
        # 使用更宽泛的匹配：<<后面跟任意非空字符（但为了安全，只替换标签名）
        new_str = re.sub(r'<<(\w+)', r'<\1', xml_str)
        if new_str != xml_str:
            logger.debug(f"双尖括号修复: {xml_str[:80]} -> {new_str[:80]}")
        return new_str

    def _wrap_text_in_element(self, elem: ET.Element) -> bool:
        """递归修复元素内部及尾随的裸文本，跳过忽略标签"""
        if elem.tag in self.IGNORE_TAGS:
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
            if child.tag in self.IGNORE_TAGS:
                continue
            if self._wrap_text_in_element(child):
                modified = True
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
