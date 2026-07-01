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
        self.fix_at_tag_format = cfg.get("fix_at_tag_format", True)
        self.convert_text_at_to_tag = cfg.get("convert_text_at_to_tag", False)
        # 排除的邮箱域名后缀（额外保护）
        self.text_at_exclude_domains = cfg.get("text_at_exclude_domains", [
            "com", "cn", "net", "org", "edu", "gov", "io", "co", "uk", "jp", "de", "fr", "ru"
        ])
        self.IGNORE_TAGS = {
            "file", "record", "video", "image", "sticker", "forward", "reply", "reasoning",
            "at", "face", "json", "lightapp", "animation", "poke", "node", "location", "share",
            "voice", "shortvideo", "gif", "cardimage", "tts", "pe", "redbag", "emoji", "img", "selfie"
        }

    async def initialize(self):
        logger.info(f"XmlTagFixerPlugin initialized (only_final={self.only_final}, fix_msg={self.fix_missing_msg}, "
                    f"double_brackets={self.fix_double_brackets}, fix_at={self.fix_at_tag_format}, "
                    f"convert_at={self.convert_text_at_to_tag})")

    async def terminate(self):
        logger.info("XmlTagFixerPlugin terminated")

    def _preprocess(self, xml_str: str) -> str:
        if not self.fix_double_brackets:
            return xml_str
        new_str = re.sub(r'<<(\w+)', r'<\1', xml_str)
        if new_str != xml_str:
            logger.debug(f"双尖括号修复: {xml_str[:80]} -> {new_str[:80]}")
        return new_str

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

    def _wrap_text_in_element(self, elem: ET.Element) -> bool:
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
            if not self.fix_at_tag_format and child.tag in self.IGNORE_TAGS:
                continue

            if child.tag not in self.IGNORE_TAGS:
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

    def _fix_single_msg(self, msg_str: str) -> list[str]:
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
                    self._fix_at_tags(root)
                    self._wrap_text_in_element(root)
                    self._convert_text_at_in_element(root, None)
                    fixed = ET.tostring(root, encoding="unicode", method="xml")
                    return [fixed]
                else:
                    return [msg_str]
            except ET.ParseError as e:
                logger.debug(f"解析单个 msg 失败，跳过修复: {e}")
                return [msg_str]

    def fix_xml(self, xml_str: str) -> str:
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
