import json
import re
import xml.etree.ElementTree as ET
from typing import Optional
from xml.sax.saxutils import escape as xml_escape, unescape as xml_unescape

from core.plugin import BasePlugin, logger, on, Priority
from core.provider import LLMResponse
from core.chat import MessageChain
from core.chat.message_elements import At, Record, Reply
from core.chat.message_utils import KiraMessageBatchEvent

# 本插件的 plugin_id（= manifest.json 的 plugin_id = 配置文件名）
PLUGIN_ID = "xml_tag_fixer"

# MiMo TTS 插件的 plugin_id（用于接管其格式修复功能）
MIMO_PLUGIN_ID = "kira-ai-plugin-mimo-tts"

# 切块器用的 <msg> 扫描正则（模块级，避免每次调用重复编译）
# 属性部分懒惰匹配，且不吞掉自闭合的 /（对齐原实现的开口识别口径）
_MSG_OPEN_SEARCH_RE = re.compile(r"<msg(?:\s[^>]*?)?(/?)>")
_MSG_ANY_SEARCH_RE = re.compile(r"<msg(?:\s[^>]*?)?(/?)>|</msg\s*>")

# 标签定界符修复：`<<msg`（首尖括号打重）与 `<\/msg>`（反斜杠转义）
_DBL_ANGLE_RE = re.compile(r"<<(\w+)")
_BACKSLASH_TAG_RE = re.compile(r"<\\(/?)([a-zA-Z][\w-]*)>")

# ========== 一次性配置迁移 ==========
#
# 1.5.0 把「处理 msg 外杂散内容」(strip_reasoning_block) 的默认值由 true 改为
# false：总开关打开时，模型写在 <msg> 外的**规划/心声**会被当成消息发出去。
# 对"把规划写到输出里"的模型，这是很尴尬的泄漏。
#
# 但只改 schema 默认值会产生歧义：框架 _ensure_plugin_config 只要发现配置
# 文件里缺少某个键，就会写入该键的默认值 —— 于是"从没配过"和"主动配成 false"
# 在文件里长得一模一样，无法区分该不该迁移。
#
# 所以这里用一个显式标记键来保证"只迁移一次"：
#   - 没有标记 + 没有显式配置 → 迁移为 false，并打标记
#   - 没有标记 + 已显式配置  → 尊重用户选择，只打标记
#   - 已有标记               → 什么都不做（用户之后怎么改都不再干预）
_MIGRATION_KEY = "_migrated_strip_reasoning_default"
_MIGRATION_VERSION = "1.5.1"
_STRIP_KEY = "strip_reasoning_block"
# v1.4.0 schema 的默认值（上游实测）。只有"当前值 == 这个"才迁移。
_LEGACY_DEFAULT = True


def _atomic_write_json(path, data) -> bool:
    """原子写 JSON：临时文件 + fsync + os.replace。

    框架自身的 save_config 是 `open(w)` 直写，写一半崩溃会丢整个配置。
    插件绝不走那条路 —— 迁移必须保证"要么完整写成，要么原文件不动"。
    """
    import os
    import tempfile
    d = os.path.dirname(str(path))
    try:
        os.makedirs(d, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=d, prefix=".xmlfix_", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=4, ensure_ascii=False)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, str(path))
            return True
        except Exception:
            try:
                os.unlink(tmp)
            except Exception:
                pass
            raise
    except Exception as e:
        logger.error(f"[xml_tag_fixer] 原子写配置失败，保持原文件不动: {e}")
        return False


def _read_json(path) -> Optional[dict]:
    """读 JSON。任何异常都返回 None（调用方据此中止迁移，绝不覆盖）。"""
    import os
    if not os.path.isfile(str(path)):
        return None
    try:
        with open(str(path), encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except Exception as e:
        logger.error(f"[xml_tag_fixer] 读取配置失败，跳过迁移: {e}")
        return None


def migrate_config_once(config_path) -> Optional[str]:
    """把「处理 msg 外杂散内容」的默认值一次性迁移为 false。

    返回描述迁移结果的字符串（供日志），无需迁移时返回 None。

    安全约定（宁可不动，也不丢配置）：
      1. 读不到 / 解析不了现有配置 → 直接中止，不写任何东西
      2. 只增改 _STRIP_KEY 与 _MIGRATION_KEY 两个键，其余键原样保留
      3. 原子写（临时文件 + fsync + os.replace）
      4. 写回后重新解析校验，确认配置仍完整可读
      5. 任何一步失败 → 放弃迁移，保持原文件不变
    """
    import os

    cfg = _read_json(config_path)
    if cfg is None:
        # 文件不存在 = 全新安装，schema 默认值即 false，无需迁移
        # 存在但读不了 → 中止（绝不覆盖）
        return None

    if _MIGRATION_KEY in cfg:
        # ★ 已经迁移过 ⇒ 什么都不做。
        # 这是"只做一次"的关键：用户之后手动把开关打开，重载/重启都不会被改回。
        return None

    before = dict(cfg)
    cur = cfg.get(_STRIP_KEY, _LEGACY_DEFAULT)

    # 为什么不能靠"键是否存在"区分"用户配过"：
    #   框架 _ensure_plugin_config 只要发现配置缺某个键，就会写入该键的
    #   schema 默认值并立刻落盘 ⇒ 跑过 v1.4.0 的用户文件里**必然**已有
    #   该键，值为 v1.4.0 的默认值 True。两者在文件里完全无法区分。
    # 所以改用"值 == 旧版本默认值"当判据：
    #   - 值为旧默认 True  → 极大概率是框架补的 ⇒ 迁移为 False
    #   - 值已是 False     → 保持
    #   - 键不存在         → 设为 False
    # 代价：极少数**真的主动打开过**的用户会被改回 False（下面日志会提示怎么恢复）。
    if cur is not _LEGACY_DEFAULT:
        action = f"已是 {cur!r}，无需改动"
    else:
        cfg[_STRIP_KEY] = False
        action = ("由旧默认 true 迁移为 false（关闭 msg 外杂散内容；"
                  "需要原来的行为请到 WebUI 重新打开该开关）")

    cfg[_MIGRATION_KEY] = _MIGRATION_VERSION

    # 二次确认：除这两个键外不得有任何改动
    for k, v in before.items():
        if k in (_STRIP_KEY,):
            continue
        if cfg.get(k) != v:
            logger.error("[xml_tag_fixer] 迁移会改动其它键，已放弃")
            return None

    if not _atomic_write_json(config_path, cfg):
        return None

    # 写回后校验：配置必须仍然完整可读，且键数不少于迁移前
    check = _read_json(config_path)
    if check is None or len(check) < len(before):
        logger.error("[xml_tag_fixer] 迁移后校验失败（配置可能损坏），请手动检查")
        return None

    logger.info(f"[xml_tag_fixer] 配置迁移 1.5.0：{action}")
    return action


class XmlTagFixerPlugin(BasePlugin):
    def __init__(self, ctx, cfg: dict):
        super().__init__(ctx, cfg)
        self.enabled = cfg.get("enabled", True)
        self.only_final = cfg.get("only_final_message", False)
        # text 包裹模式：blacklist（默认，只包落单文字+黑名单标签）/ whitelist（旧行为，除豁免外全包）
        self.wrap_mode = cfg.get("wrap_mode", "blacklist")
        if self.wrap_mode not in ("blacklist", "whitelist"):
            logger.warning(f"无法识别的 wrap_mode: {self.wrap_mode!r}，回退为 blacklist")
            self.wrap_mode = "blacklist"
        self.fix_missing_msg = cfg.get("fix_missing_msg", True)
        self.fix_double_brackets = cfg.get("fix_double_brackets", True)
        # 还原 `<\/msg>` 这类反斜杠转义的标签定界符（模型从 JSON/正则习惯串味）
        self.fix_backslash_tags = cfg.get("fix_backslash_tags", True)
        self.fix_at_tag_format = cfg.get("fix_at_tag_format", True)
        self.convert_text_at_to_tag = cfg.get("convert_text_at_to_tag", False)
        # 提取 text 内嵌套的 at 标签为 msg 直接子元素（模型常把 at 写进 text 里导致失效）
        self.extract_at_from_text = cfg.get("extract_at_from_text", True)
        self.escape_special_chars = cfg.get("escape_special_chars", True)
        self.escape_code_fences = cfg.get("escape_code_fences", True)
        self.fallback_wrap_text = cfg.get("fallback_wrap_text", True)
        self.fallback_strip_tags = cfg.get("fallback_strip_tags", True)
        self.flatten_no_wrap_tags = cfg.get("flatten_no_wrap_tags", True)
        self.fix_record_split = cfg.get("fix_record_split", True)
        self.split_blank_line_messages = cfg.get("split_blank_line_messages", False)
        self.merge_marker_span_msgs = cfg.get("merge_marker_span_msgs", True)
        # 「处理 msg 外杂散内容」总开关（键名保留 strip_reasoning_block 兼容旧配置）
        # 1.5.0 起默认关闭：打开时模型写在 <msg> 外的**规划/心声**会被当成消息发出。
        # 关掉后仍会补包「忘带 msg 的功能标签」（语音/图片/@/表情），不影响修复能力。
        self.handle_stray_content = cfg.get("strip_reasoning_block", False)
        # @on.llm_request 缓存的已注册标签名（区分 msg 级 / root 级），
        # 用于杂散内容按身份分流：已注册标签内容绝不转义，未注册标签内部整体转义。
        # 初始值填入框架内置标签：即使缓存尚未刷新（异常/首次），内置功能标签也受保护
        self._registered_msg_tags: set = {
            "text", "image", "at", "reply", "forward", "emoji",
            "record", "file", "video", "poke", "json",
        }
        self._registered_root_tags: set = set()
        # 排除的邮箱域名后缀（额外保护）；显式配成 null 时回退默认，避免迭代 None 崩
        self.text_at_exclude_domains = cfg.get("text_at_exclude_domains") or [
            "com", "cn", "net", "org", "edu", "gov", "io", "co", "uk", "jp", "de", "fr", "ru"
        ]
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

        # 强制包裹黑名单（仅 blacklist 模式生效）：名单内标签里的文字强制包进 <text>
        # 优先级 IGNORE_TAGS > no_wrap_tags > force_wrap_tags：重复时赦免优先
        self.force_wrap_tags = set()
        for item in (cfg.get("force_wrap_tags") or []):
            name = self._normalize_tag_name(item)
            if name:
                self.force_wrap_tags.add(name)
            elif item and str(item).strip():
                logger.debug(f"忽略无法识别的 force_wrap_tags 配置项: {item!r}")
        self.force_wrap_tags -= self.IGNORE_TAGS
        self.force_wrap_tags -= self.no_wrap_tags

        self._mimo_checked = False

    # ========== 标签定界符修复（双尖括号 / 反斜杠转义）==========

    def _known_tag_names(self) -> set:
        """当前环境里"确实算标签"的名字集合。"""
        return (
            self.IGNORE_TAGS
            | self._registered_msg_tags
            | self._registered_root_tags
            | self.no_wrap_tags
            | self.force_wrap_tags
            | {"msg", "text"}
        )

    def _is_tag_like(self, name: str, xml_str: str) -> bool:
        """判断某个名字"确实像标签"，用于给 `<<name` / `<\\name>` 的还原把关。

        判据（任一成立即可）：
        1. 在已知标签名单里（框架内置 / 本次请求已注册 / 用户配置的豁免名单）；
        2. 文本别处存在**同名的闭合标签** `</name>` ——
           模型写错开口却写对闭合是最常见形态（`<<msg>...</msg>`）。

        ⚠ 不能用"别处存在 `<name`"当判据：`a<<b` 里那个被折叠出来的
        `<b` 自身就会让判据自匹配，等于没判据。闭合标签的形态唯一，
        不会被开口修复操作本身制造出来。
        """
        return name in self._known_tag_names() or f"</{name}>" in xml_str

    def _apply_backslash_tags(self, xml_str: str) -> str:
        r"""把 `<\/msg>` / `<\\msg>` 这类"反斜杠转义定界符"还原成真标签。

        模型从 JSON/正则习惯串味过来时，会把闭合标签写成 `<\/msg>`。
        旧版对此毫无处理 —— 它被 _escape_specials 当成普通文本，
        转义成 `&lt;\/msg&gt;` 后原样发出去，用户就在消息末尾看到一串
        `<\/msg>` 乱码（即 issue 截图里的现象）。

        只匹配 `<` + 反斜杠 + 可选 `/` + 合法标签名 + `>` 的形态；
        且仅当该名字"确实像标签"（在已知名单里，或别处以正常形态出现过）
        才还原，避免误伤 `C:\\dir` 之类的普通文本。

        ⚠ 此步必须排在 _escape_code_fences 之后：围栏内此刻已是 `&lt;`，
        因此代码里的 `<\/msg>` 作为原文保留，不会被改写。
        """
        def _repl(m):
            slash, name = m.group(1), m.group(2)
            if not self._is_tag_like(name, xml_str):
                return m.group(0)
            logger.debug(f"已还原反斜杠转义的标签 <\\{slash}{name}>")
            return f"<{slash}{name}>"

        return _BACKSLASH_TAG_RE.sub(_repl, xml_str)

    def _fix_double_brackets_safe(self, xml_str: str) -> str:
        """`<<msg` → `<msg`，但**不再无差别吞掉 `<<`**。

        旧实现 `re.sub(r'<<(\\w+)', r'<\\1')` 不判断右侧是不是真标签，
        会把普通文本里的比较/移位运算符一起吃掉：
        `a<<b` → `a<b`（丢一个 `<`）、`1<<2` → `1<2`、`vector<<T>` → `vector<T>`。
        这是内容损坏，不是修复。

        新判据：仅当 `<<name` 的 name 确实是标签（在已知名单内，
        或本文本别处存在同名闭合标签 `</name>`）时才折叠 ——
        不能只看"别处有 `<name`"，那会被自身折叠结果带来自匹配。
        """
        if not self.fix_double_brackets:
            return xml_str

        def _repl(m):
            name = m.group(1)
            if self._is_tag_like(name, xml_str):
                return f"<{name}"
            return m.group(0)

        return _DBL_ANGLE_RE.sub(_repl, xml_str)

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
        logger.info(f"XmlTagFixerPlugin initialized (mode={self.wrap_mode}, force_wrap={sorted(self.force_wrap_tags)}, "
                    f"only_final={self.only_final}, fix_msg={self.fix_missing_msg}, "
                    f"double_brackets={self.fix_double_brackets}, fix_at={self.fix_at_tag_format}, "
                    f"convert_at={self.convert_text_at_to_tag}, escape={self.escape_special_chars}, "
                    f"fallback={self.fallback_wrap_text}, no_wrap={sorted(self.no_wrap_tags)}, "
                    f"flatten={self.flatten_no_wrap_tags}, record_split={self.fix_record_split}, "
                    f"split_blank={self.split_blank_line_messages}, stray={self.handle_stray_content})")
        self._run_config_migration()
        self._try_takeover_mimo()

    def _run_config_migration(self) -> None:
        """执行一次性配置迁移（幂等），并让当前会话立即采用迁移后的值。

        只在本插件首次以 1.5.1 运行时生效一次；之后无论用户怎么改都不再干预。
        迁移失败一律静默降级（保持原配置与原行为），绝不影响插件可用性。
        """
        try:
            path = self._resolve_config_path()
            if path is None:
                logger.debug("[xml_tag_fixer] 无法定位插件配置文件，跳过迁移")
                return
            action = migrate_config_once(path)
            if action is None:
                return
            # ★ 迁移改的是文件，但本实例的 handle_stray_content 在 __init__
            #    时就读定了 ⇒ 必须回填，否则要等下次重载才生效（实测确认）。
            #    重读文件而不是直接用 False：迁移也可能是"保持原值"。
            cfg = _read_json(path)
            if isinstance(cfg, dict) and _STRIP_KEY in cfg:
                self.handle_stray_content = cfg[_STRIP_KEY]
                logger.info(f"[xml_tag_fixer] 迁移后当前会话立即采用 {_STRIP_KEY}="
                            f"{self.handle_stray_content}")
        except Exception as e:
            logger.error(f"[xml_tag_fixer] 配置迁移异常（已忽略，保持原配置）: {e}")

    @staticmethod
    def _resolve_config_path():
        """定位 data/config/plugins/xml_tag_fixer.json。

        框架布局：`get_config_path()/plugins/<plugin_id>.json`
        （见 core/plugin/plugin_registry.py 的 PLUGIN_CONFIG_DIR）。
        只做定位，不做任何猜测性写入。
        """
        try:
            from core.utils.path_utils import get_config_path
            return get_config_path() / "plugins" / f"{PLUGIN_ID}.json"
        except Exception:
            return None

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

    # ========== 代码围栏感知转义 ==========

    def _escape_code_fences(self, xml_str: str) -> str:
        """代码围栏 ``` 内的内容整体转义为纯文本。

        围栏内的 <foo>、<msg> 等只是代码文本，不应被当作标签：不转义会被
        ET 解析成元素、遭框架静默跳过（用户看到的代码缺行），<msg> 字样还会
        误导切块器把围栏切开。围栏内 & 一律转义（包括已有实体 &amp;），
        保证代码经 ET 往返逐字保真。围栏未闭合时剩余内容全部当代码，
        与兜底逻辑 _strip_outside_fences 的约定一致。
        """
        if not self.escape_code_fences or "```" not in xml_str:
            return xml_str
        parts = xml_str.split("```")
        for i in range(1, len(parts), 2):
            parts[i] = xml_escape(parts[i])
        return "```".join(parts)

    # ========== msg 外杂散内容：统一保护 / 分流 ==========

    # 成对的非 msg 标签块 <foo ...>...</foo>
    # ⚠ 不能用 `(.*?)</\1>`：同名嵌套（<foo>a<foo>b</foo>c</foo>）时会把
    #   内层闭合当成外层闭合，导致"合法输入被改成缺闭合标签"的硬回归
    #   （整轮不可解析）。这里用"内部不得再出现同名开口"约束，逼匹配走最外层。
    _STRAY_PAIR_RE = re.compile(
        r"<([a-zA-Z][\w-]*)((?:\s[^>]*)?)>"
        r"((?:(?!</?\1(?:\s[^>]*)?>).)*?)"
        r"</\1>",
        re.DOTALL,
    )
    # 散段是「单个标签块」（成对或自闭合）的识别
    _STRAY_SINGLE_BLOCK_RE = re.compile(
        r"^<([a-zA-Z][\w-]*)(?:\s[^>]*)?>.*?</\1>$|^<([a-zA-Z][\w-]*)(?:\s[^>]*)?/>$", re.DOTALL)
    # 标签开口（判断未闭合尾巴用；属性懒惰匹配，避免吞掉自闭合的 /）
    _TAG_OPEN_RE = re.compile(r"<([a-zA-Z][\w-]*)(?:\s[^>]*?)?(/?)>")

    @on.llm_request()
    async def cache_registered_tags(self, event, req, tag_set, *_):
        """缓存本次请求 tag_set 的已注册标签名，供杂散内容按身份分流。

        已注册标签（内置 text/record/file 及插件注册的 mimo_tts 等）内容绝不转义；
        未注册标签（reasoning/think/foo 等）内部整体转义后原样保留。
        """
        try:
            self._registered_msg_tags = {t.name for t in tag_set.get_all()}
            self._registered_root_tags = {t.name for t in tag_set.get_all_root()}
        except Exception:
            pass

    @staticmethod
    def _inside_msg(prefix: str) -> bool:
        """粗判某位置是否处于未闭合的 <msg> 内部（msg 不会合法嵌套，计数即可）。"""
        opens = len(re.findall(r"<msg(?:\s[^>]*)?>", prefix))
        self_closing = len(re.findall(r"<msg(?:\s[^>]*)?/>", prefix))
        return opens - self_closing - prefix.count("</msg>") > 0

    # ========== 可解析性硬闸门 ==========
    #
    # 框架侧（core/message_manager.py:send_xml_messages）对整段文本只有一次
    # ET.fromstring 机会，解析失败就走 `logger.error("Error parsing message")`
    # 直接 return []，**本轮一条消息都发不出去**。所以"输出必须可解析"是
    # 本插件的最高不变量，任何修复分支都必须过这道闸。

    # XML 1.0 不允许的控制字符（\t \n \r 除外）；留着会让框架解析整体失败
    _ILLEGAL_XML_CHARS_RE = re.compile(
        "[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x84\x86-\x9f\ufdd0-\ufdef\ufffe\uffff]"
    )

    @staticmethod
    def _is_parseable(xml_str: str) -> bool:
        """整段文本能否被框架的 <root> 包裹方式解析成功。"""
        try:
            ET.fromstring(f"<root>{xml_str}</root>")
            return True
        except Exception:
            return False

    def _sanitize_illegal_chars(self, s: str) -> str:
        """剔除 XML 1.0 非法控制字符。仅在兜底/闸门阶段调用，正常路径不动用户内容。"""
        return self._ILLEGAL_XML_CHARS_RE.sub("", s)

    def _salvage_block(self, block: str) -> list:
        """单块不可解析时的抢救：先剥壳清洗，仍不行就丢弃。

        丢弃比原样透传安全得多 —— 原样透传会拖垮整轮解析（见 _is_parseable 注释）。
        """
        cleaned = self._fallback_wrap(block, allow_original=False)
        for b in cleaned:
            if self._is_parseable(b):
                return [b]
        logger.debug(f"闸门丢弃无法抢救的块: {block[:80]}")
        return []

    def _last_resort(self, xml_str: str) -> str:
        """总闸门兜底的兜底：把整段当纯文本救成一条消息。

        走到这里说明逐块修复全线失守。此时唯一还能保证的收益是"别把整轮弄没"，
        因此剥掉全部结构性标签、剔除非法字符、整段转义成 <msg><text>。
        若清洗后已无内容，返回空串 —— 空串让框架解析出 0 条消息并静默结束，
        比发一条坏格式导致整轮丢弃损失更小。
        """
        text = xml_unescape(self._sanitize_illegal_chars(xml_str),
                            {"&quot;": '"', "&apos;": "'"})
        if "```" in text:
            text = self._strip_outside_fences(text)
        else:
            text = self._strip_structural_tags(text)
        # 残余的成对标签一律剥掉（此时不再区分注册与否）
        text = re.sub(r"</?[a-zA-Z][\w-]*(?:\s[^<>]*)?/?>", "", text)
        text = self._sanitize_illegal_chars(text).strip()
        if not text:
            logger.debug("总闸门清洗后无内容，本轮静默")
            return ""
        logger.warning("触发总闸门兜底：整段清洗为纯文本消息")
        return f"<msg><text>{xml_escape(text)}</text></msg>"

    def _split_msg_segments(self, xml_str: str) -> list:
        """位置感知 + **嵌套深度感知** 的切块。

        旧实现用 `find("</msg>")` 找闭合，遇到嵌套 <msg>（模型写草稿、示例、
        或把 </text> 打字成 </msg>）会把内层 </msg> 误当成外层闭合，
        切出的残片（如 `</text></msg>`）留在结果里 ⇒ 整轮不可解析。

        这里按深度配对：只有深度回到 0 的那个 </msg> 才是真正的闭合。
        返回 [(is_msg, text), ...]，msg 块与散段都保留，顺序不乱。
        """
        segments = []
        pos = 0
        length = len(xml_str)
        while pos < length:
            m = _MSG_OPEN_SEARCH_RE.search(xml_str, pos)
            if not m:
                tail = xml_str[pos:]
                if tail.strip():
                    segments.append((False, tail))
                break
            if m.start() > pos:
                gap = xml_str[pos:m.start()]
                if gap.strip():
                    segments.append((False, gap))
            if m.group(1) == "/":
                # 自闭合 <msg/> 或 <msg .../>：独立成块。
                # 空消息 <msg/> 是合法的「静默」操作，原样透传：
                # 框架会自行优雅处理，下游插件和记忆持久化都依赖这个标记
                segments.append((True, m.group(0)))
                pos = m.end()
                continue
            depth = 1
            scan = m.end()
            closed = False
            while depth > 0:
                nxt = _MSG_ANY_SEARCH_RE.search(xml_str, scan)
                if not nxt:
                    break
                if nxt.group(0).startswith("</"):
                    depth -= 1
                    if depth == 0:
                        segments.append((True, xml_str[m.start():nxt.end()]))
                        pos = nxt.end()
                        closed = True
                        break
                elif nxt.group(1) == "/":
                    pass  # 嵌套的自闭合 msg 不改变深度
                else:
                    depth += 1
                scan = nxt.end()
            if not closed:
                # 未闭合：截断为独立块，交给修复/兜底
                segments.append((True, xml_str[m.start():]))
                pos = length
        return segments

    @staticmethod
    def _find_matching_close(tag: str, s: str, start: int) -> Optional[int]:
        """从 `start` 起为标签 `tag` 找"配对的那一个" `</tag>`，返回其起始下标。

        必须做深度配对：同名嵌套时（`<record a><record b>`）第一个 `</record>`
        关闭的是**内层**，用 `f"</{tag}>" in rest` 这种全局包含判断会误判成已关闭。
        """
        opens = re.compile(rf"<{re.escape(tag)}(?:\s[^>]*?)?(/?)>")
        depth = 1
        pos = start
        while pos < len(s):
            nxt = opens.search(s, pos)
            close_idx = s.find(f"</{tag}>", pos)
            if close_idx == -1:
                return None
            if nxt and nxt.start() < close_idx:
                if nxt.group(1) != "/":
                    depth += 1
                pos = nxt.end()
                continue
            depth -= 1
            if depth == 0:
                return close_idx
            pos = close_idx + len(tag) + 3
        return None

    def _handle_unclosed_tail(self, xml_str: str) -> str:
        """处理 root 级未闭合的非 msg 标签尾巴。

        - **已注册/no_wrap 标签**：补上闭合标签救回内容（如模型没写完的 mimo_tts）
        - **未注册标签**（reasoning/think 等框架本来就静默跳过的）：
          在**该标签最后一个闭合标签处**补上闭合，把它封成一个完整块。
          该块随后由 _protect_stray_blocks 内部转义 ⇒ 用户看不到、不会污染
          结果，且**不吞掉其后紧跟的真消息**。
          （旧行为是"剥到末尾"，会把后面的真消息一起删掉 —— 那是内容丢失。）
        - msg 内部的未闭合标签不动（留给解析失败 → 兜底管线）

        无论「处理 msg 外杂散内容」开关如何，这一步都必须执行：
        它的首要职责是**防止坏结构破坏解析**，而不是决定内容去留。
        """
        for _ in range(64):
            target = None
            for m in self._TAG_OPEN_RE.finditer(xml_str):
                tag = m.group(1)
                if tag == "msg" or m.group(2) == "/":
                    continue
                if self._find_matching_close(tag, xml_str, m.end()) is not None:
                    continue
                if self._inside_msg(xml_str[:m.start()]):
                    continue
                target = (m, tag)
                break
            if target is None:
                break
            m, tag = target
            if tag in self._registered_msg_tags or tag in self._registered_root_tags or tag in self.no_wrap_tags:
                # 每轮只补一个闭合，循环会重新做深度配对检查 ——
                # 同名嵌套时这样能自然逐层补齐，计数法反而会多补。
                logger.debug(f"已补全 root 级未闭合标签 <{tag}>")
                xml_str = xml_str + f"</{tag}>"
            else:
                # 未注册标签：优先在它自己的闭合标签处封口，保全其后内容
                last_close = xml_str.rfind(f"</{tag}>")
                if last_close != -1:
                    insert_at = last_close + len(tag) + 3
                    logger.debug(f"已在末尾闭合处补全未闭合的未注册标签 <{tag}>")
                    xml_str = xml_str[:insert_at] + f"</{tag}>" + xml_str[insert_at:]
                else:
                    # 完全没有闭合标签：**在下一个真消息（`<msg`）之前补上闭合**，
                    # 让它仍是一个完整的未注册标签块。
                    # 为什么不是简单删掉开标签：删掉后里面的思考文字会变成裸文本，
                    # 总开关打开时会被当消息发出（泄漏）；也不是"剥到末尾"，
                    # 那会把后面紧跟的真消息一起删掉。
                    nxt_msg = xml_str.find("<msg", m.end())
                    insert_at = nxt_msg if nxt_msg != -1 else len(xml_str)
                    logger.debug(f"已在下一个消息前补全未闭合的未注册标签 <{tag}>")
                    xml_str = xml_str[:insert_at] + f"</{tag}>" + xml_str[insert_at:]
        return xml_str

    def _protect_stray_blocks(self, xml_str: str) -> str:
        """前置保护：处理文本中成对的非 msg 标签块，防止其内部干扰切块器与框架解析。

        - msg 块：不动；
        - 已注册/no_wrap 标签（file/record/mimo_tts 等 handler 会真正消费内容的）：
          只中和 <msg、</msg> 边界字面量，内容字节级保留，绝不转义；
        - 未注册标签（reasoning/think/foo 等框架本来就静默跳过的）：
          内部先反转义再整体转义——<msg> 草稿失效化、裸特殊字符无害化，
          框架 ET 解析不会被炸掉，且转义经 ET 往返无损。
        """
        def _repl(m):
            tag, attrs, inner = m.group(1), m.group(2), m.group(3)
            if tag == "msg":
                return m.group(0)
            if tag in self._registered_msg_tags or tag in self._registered_root_tags or tag in self.no_wrap_tags:
                inner = inner.replace("</msg>", "&lt;/msg&gt;").replace("<msg", "&lt;msg")
                return f"<{tag}{attrs}>{inner}</{tag}>"
            inner = xml_unescape(inner, {"&quot;": '"', "&apos;": "'"})
            return f"<{tag}{attrs}>{xml_escape(inner)}</{tag}>"
        return self._STRAY_PAIR_RE.sub(_repl, xml_str)

    @staticmethod
    def _stray_block_tag(seg: str) -> Optional[str]:
        """若散段是单个标签块，返回其标签名；否则 None。"""
        m = XmlTagFixerPlugin._STRAY_SINGLE_BLOCK_RE.match(seg)
        return (m.group(1) or m.group(2)) if m else None

    def _passes_through_when_off(self, seg: str) -> bool:
        """关闭总开关时，该散段是否应当保留（而不是丢弃）。

        该散段是「不应被丢弃的功能性内容」吗。

        关闭「处理 msg 外杂散内容」时，旧行为会把 msg 之间的散段整体丢弃。
        但其中两类散段是**真实的修复目标**，丢掉等于功能静默失效：

        1. **忘带 msg 的已注册功能标签** —— 模型忘了给语音/图片/@/表情包
           `<msg>`（如 `<record url="a.silk"/><msg>…</msg>`）。丢掉它用户
           发了语音却没声音。
        2. **含 `[xxx]` 式标记的文本** —— 折扇留穗等插件靠 `[3p]…[/3p]`
           这类**跨消息**标记对工作（README 已列为支持的互操作）。
           标记被丢弃会让下游插件无法识别。

        这两类与"模型心声"的区别很明确：前者是框架会真正消费的结构化内容，
        后者是其他插件约定的协议标记；而裸文本（旁白/规划）正是总开关要挡的。
        所以只放行前两类，裸文本仍按旧行为丢弃。
        """
        tag = self._stray_block_tag(seg)
        if tag:
            if tag in self._registered_root_tags:
                return True
            if tag in self._registered_msg_tags or tag in self.no_wrap_tags:
                return True
            # 未注册标签块（reasoning/think 等）：**也保留**，但处理方式不同 ——
            # 由调用方原样透传（框架对其"静默跳过"：用户看不到，原文进记忆），
            # 让模型在历史里仍能看到自己包规划的范例。
            # ⚠ 安全性由 _protect_stray_blocks 保证：它已把块内的 <msg> 草稿
            #    转义成 &lt;msg&gt;，所以透传不会被误当真消息发送。
            return True
        # 含 [xxx] / [/xxx] 协议标记的文本（折扇留穗等插件依赖）
        if self.merge_marker_span_msgs and self._BBCODE_MARKER_RE.search(seg):
            return True
        return False

    def _fix_stray_segment(self, seg: str) -> list:
        """处理 msg 之外的散段，按内容身份分流（对齐框架原生语义）：

        - 已注册 root 标签：原样透传（框架按 RootTagAction 执行）；
        - 已注册 msg 级/no_wrap 标签：包进 <msg> 走正常修复（模型忘包 msg 的
          语音/图片/表情等得以生效，内容零转义）；
        - 未注册标签块（reasoning/think/foo 等）：内部已在前置保护转义，
          原样留在 root 级——框架静默跳过（用户不可见），原文形态进记忆；
        - 松散文字：包 <msg><text> 发出（修掉消息）。
        """
        m = self._STRAY_SINGLE_BLOCK_RE.match(seg)
        if m:
            tag = m.group(1) or m.group(2)
            if tag in self._registered_root_tags:
                # 原样透传但过一道裸字符转义：属性/内容里的裸 & 会把框架 ET 解析炸掉
                return [self._escape_specials(seg)]
            if tag in self._registered_msg_tags or tag in self.no_wrap_tags:
                return self._fix_single_msg(f"<msg>{seg}</msg>")
            # 未注册：成对块内部已在前置保护转义；这里补上 attrs 与自闭合块的裸 &
            return [self._escape_specials(seg)]
        return self._fix_single_msg(seg)

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
        """`<at user_id="123"/>` → `<at>123</at>`。

        ⚠ 旧版的分支写法有死代码（`elif` 条件与 `if` 完全相同，永远进不去），
        且当 `<at>` 同时带 user_id 和文本时**静默丢弃原文本**。
        现在两者都带上时优先保 user_id（id 才是 @ 的目标），
        但把原文本记录到 debug 日志便于排查。
        """
        if not self.fix_at_tag_format:
            return
        for child in elem.iter():
            if child.tag != "at":
                continue
            qq = child.attrib.pop("user_id", None)
            if qq is None:
                continue
            if child.text and child.text.strip() and child.text.strip() != qq:
                # 形如 <at user_id="123">456</at>：模型给了两个目标，以 user_id 为准
                logger.debug(f"at 标签同时含 user_id={qq} 与文本 {child.text!r}，取 user_id")
            child.text = qq

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

    def _wrap_text_in_element(self, elem: ET.Element, wrap_here: bool = True) -> bool:
        """把松散文字包进 <text>。

        wrap_here: 当前元素是否是包裹上下文（是则处理其直接文本与子元素的 tail）。
        - whitelist（旧行为）：所有非 IGNORE/豁免标签都是包裹上下文，递归全包；
        - blacklist（默认）：仅 msg 根与 force_wrap_tags 内标签是包裹上下文，
          其余标签的内容保持原样、信任模型/插件的输出。
        """
        if elem.tag in self.IGNORE_TAGS or elem.tag in self.no_wrap_tags:
            return False

        modified = False

        if wrap_here and elem.text and elem.text.strip() and elem.tag != "text":
            text_elem = ET.Element("text")
            text_elem.text = elem.text
            elem.text = None
            if len(elem):
                elem.insert(0, text_elem)
            else:
                elem.append(text_elem)
            modified = True

        children = list(elem)
        # ⚠ 旧版用 `elem.insert(i + 1, ...)` 就地插入，但 i 来自插入前拍下的
        # children 快照 —— 每插一个 tail，后面所有子元素的真实下标就 +1，
        # 于是后续 tail 被插到错误位置，**消息内容顺序被打乱**
        # （实测 <msg><foo>x</foo>t1<bar>y</bar>t2</msg> → [foo, text(t1), text(t2), bar]，
        # t2 跑到了 <bar> 前面）。改为重建子列表，顺序严格保持。
        rebuilt = []
        for child in children:
            if not self.fix_at_tag_format and child.tag in self.IGNORE_TAGS:
                rebuilt.append(child)
                continue

            if child.tag not in self.IGNORE_TAGS and child.tag not in self.no_wrap_tags:
                if self.wrap_mode == "whitelist":
                    child_wrap = True
                else:
                    child_wrap = child.tag in self.force_wrap_tags
                if self._wrap_text_in_element(child, child_wrap):
                    modified = True

            rebuilt.append(child)

            if wrap_here and child.tail and child.tail.strip():
                tail_text = ET.Element("text")
                tail_text.text = child.tail
                child.tail = None
                rebuilt.append(tail_text)
                modified = True

        if rebuilt != children:
            for c in children:
                elem.remove(c)
            for c in rebuilt:
                elem.append(c)

        return modified

    def _lift_nested_text_content(self, root: ET.Element) -> bool:
        """把 <text> 内部嵌着的子元素"提出来"，让它们的内容能真正发出去。

        框架 _parse_xml_msg 只读 msg **直接子元素**的**直接文本**
        （`value = child.text.strip() if child.text else ""`），
        所以嵌在 <text> 里的子元素及其 tail 用户完全看不到。实测：

            <msg><text>要写成 <msg><text>你好</text></msg> 这样</text></msg>
            → 用户只看到「要写成」（"你好" 和 " 这样" 都丢了）

        模型解释标签用法时会写出这种嵌套，属于真实场景。

        两个关键点：
        1. 嵌套的 **<msg> 必须"拆壳"**：msg 不是注册标签，框架遍历 msg 的
           子元素时遇到 <msg> 会整块跳过 ⇒ 里面的文字照样丢。所以要把嵌套 msg
           的**内容**（text / 子元素 / tail）原样摊平进外层，而不是保留这层壳。
        2. 顺序与 tail 都要保：按文档顺序重建，原 <text> 自身的 tail 也接住。

        仅对 <text> 生效 —— 其他标签的子元素可能有语义（如 img 的 path），不能动。
        """
        modified = False
        for child in list(root):
            if child.tag != "text" or not len(child):
                continue
            outer_tail = child.tail
            pieces = []          # ("text", str) 或 ("elem", Element)

            def emit_elem(el):
                """把子元素加入片段流；可"拆壳"的标签递归摊平。"""
                # msg：非注册标签，框架遍历 msg 子元素时遇到它会整块跳过
                # text：框架只读它的直接文本，嵌套内容同样看不到
                # 两者都必须拆壳，把内容按序摊平进外层
                if el.tag in ("msg", "text"):
                    if el.text and el.text.strip():
                        pieces.append(("text", el.text))
                    for sub in list(el):
                        # ⚠ 必须先取出 sub.tail：emit_elem 对非拆壳元素会清空它，
                        # 之后再也读不到（曾漏掉 "A<msg><text>B</text>C</msg>D" 里的 C）
                        sub_tail = sub.tail
                        sub.tail = None
                        emit_elem(sub)
                        if sub_tail:
                            pieces.append(("text", sub_tail))
                    return
                el.tail = None
                pieces.append(("elem", el))

            buf = child.text or ""

            def flush():
                if buf.strip():
                    pieces.append(("text", buf))

            for sub in list(child):
                flush()
                sub_tail = sub.tail or ""
                emit_elem(sub)
                buf = sub_tail
            flush()

            if not pieces:
                continue

            idx = list(root).index(child)
            root.remove(child)
            node_idx = idx
            for kind, val in pieces:
                if kind == "text":
                    te = ET.Element("text")
                    te.text = val
                    root.insert(node_idx, te)
                else:
                    root.insert(node_idx, val)
                node_idx += 1
            if outer_tail:
                # 原 <text> 的 tail 接到最后一个新节点，内容不丢
                root[node_idx - 1].tail = outer_tail
            modified = True
            logger.debug(f"已提升 <text> 内部嵌套元素（{len(pieces)} 个片段）")

        # 子元素提升后可能出现相邻的 <text>，合并以免碎片化
        if modified:
            self._merge_adjacent_text(root)
        return modified

    @staticmethod
    def _merge_adjacent_text(root: ET.Element) -> None:
        """合并相邻的 <text> 子元素（仅当它们都没有子元素时），避免消息碎裂。"""
        prev = None
        for child in list(root):
            if (prev is not None and prev.tag == "text" and child.tag == "text"
                    and not len(prev) and not len(child)):
                prev.text = (prev.text or "") + (child.text or "")
                prev.tail = child.tail
                root.remove(child)
                continue
            prev = child

    def _extract_at_from_text(self, root: ET.Element) -> bool:
        """把 text 内嵌套的 <at> 提升为 msg 直接子元素。

        模型经常把 at 写进 text 内部（如 <text>那这样 <at>123</at> 能收到不</text>），
        而框架只认 msg 直接子元素的 at，导致 at 失效、消息里出现不了真@。
        这里把 text 按 at 拆分：at 提升为 msg 直接子元素，其余文字按原顺序
        拆成多个 text 包回原位置，顺序不变。
        仅处理 text 直接子级的 at；更深层嵌套（如 text > foo > at）不动。

        ⚠ 旧版把 text 元素自身的 tail（`<text>...</text>后面这段`）丢了 ——
        拆分后只搬 text/at 子节点，没搬外层 tail ⇒ 内容静默丢失。
        这里把 tail 一起搬到最后一个新节点上。
        """
        modified = False
        for child in list(root):
            if child.tag != "text" or not len(child):
                continue
            if not any(sub.tag == "at" for sub in child):
                continue
            outer_tail = child.tail
            new_nodes = []
            text_buf = child.text or ""
            at_count = 0
            for sub in list(child):
                if sub.tag == "at":
                    at_count += 1
                if text_buf.strip():
                    te = ET.Element("text")
                    te.text = text_buf
                    new_nodes.append(te)
                tail = sub.tail or ""
                sub.tail = None  # 清掉 tail，避免序列化时重复
                new_nodes.append(sub)
                text_buf = tail
            if text_buf.strip():
                te = ET.Element("text")
                te.text = text_buf
                new_nodes.append(te)
            if not new_nodes or not at_count:
                continue
            idx = list(root).index(child)
            root.remove(child)
            for j, node in enumerate(new_nodes):
                root.insert(idx + j, node)
            if outer_tail:
                # 把原 text 的 tail 接到最后一个新节点，保持内容不丢
                new_nodes[-1].tail = outer_tail
            modified = True
        return modified

    def _convert_text_at_in_element(self, elem: ET.Element, parent: ET.Element = None) -> None:
        """
        递归处理元素及其子元素，将 text 节点中的 @纯数字 替换为 at 标签。
        规则：
        - @ 前后不能是字母、数字、下划线、点号
        - 数字至少 4 位（避免误转换短数字）
        - 排除邮箱地址（@数字.后缀）

        ⚠ 旧版用 `re.split` 切段后**对每一段单独跑含 lookbehind 的正则**，
        上下文在切分处被切断 ⇒ 边界判据失效。实测：
        `@12345678 和 abc@12345678 都在这` → 两个都转成 at，
        第二个本该被 `abc` 挡住；邮箱 `x@12345678.com` 也会被误转。
        现在改为一次 `finditer` 扫描原文，**在原文位置做全部判据**，
        再按匹配区间切段重建，上下文不再丢失。
        """
        if not self.convert_text_at_to_tag:
            return

        # 先处理子节点（深度优先）
        for child in list(elem):
            self._convert_text_at_in_element(child, elem)

        if elem.tag != "text" or not elem.text:
            return

        txt = elem.text
        domains = [d for d in (self.text_at_exclude_domains or []) if d]
        domains_pattern = '|'.join(re.escape(d) for d in domains) if domains else None

        # 前边界：不能是字母数字下划线点号（避免 abc@ / a.b@）
        # 后边界：不能是字母数字下划线点号（避免 @12345678x）
        # 邮箱排除：@数字 后面若紧跟 .顶级域 则不是 at
        def _is_email_like(end: int) -> bool:
            if not domains_pattern:
                return False
            rest = txt[end:]
            m = re.match(rf'\.(?:{domains_pattern})(?:\b|$)', rest, re.IGNORECASE)
            return bool(m)

        matches = []
        for m in re.finditer(r'@(\d{4,})', txt):
            start, end = m.start(), m.end()
            if start > 0 and re.match(r'[A-Za-z0-9_.]', txt[start - 1]):
                continue
            if _is_email_like(end):
                continue
            matches.append((start, end, m.group(1)))

        if not matches:
            return

        new_nodes = []
        cursor = 0
        for start, end, num in matches:
            if start > cursor:
                seg = ET.Element("text")
                seg.text = txt[cursor:start]
                new_nodes.append(seg)
            at_elem = ET.Element("at")
            at_elem.text = num
            new_nodes.append(at_elem)
            cursor = end
        if cursor < len(txt):
            seg = ET.Element("text")
            seg.text = txt[cursor:]
            new_nodes.append(seg)

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
        """尝试把多个 msg 块合并为一条纯文本 msg；任一块含非 text 子元素或无法解析则放弃。

        ⚠ 旧版只取 `c.text`，丢了每个 text 子元素的 `tail`（块内散落文字）
        ⇒ 合并后内容静默丢失。这里把 text 与 tail 一并收集，并保留 msg 属性。
        """
        texts = []
        attrs = {}
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
            parts = []
            for c in children:
                if c.text:
                    parts.append(c.text)
                if c.tail:
                    # 块内 tail 也是消息内容，不能丢
                    parts.append(c.tail)
            texts.append("".join(parts).strip())
            if not attrs:
                attrs = dict(root.attrib)
        # 合并后 message_id 已无意义（会由框架重新分配），剔除旧值避免误导
        attrs.pop("message_id", None)
        msg = ET.Element("msg")
        for k, v in attrs.items():
            msg.set(k, v)
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

    def _fallback_wrap(self, original_block: str, allow_original: bool = False) -> list:
        """所有修复手段都失败时，清洗为纯文本消息。

        两种模式（fallback_strip_tags 控制）：
        - 开（默认）：反转义后剥离结构性标签、只留干净文本，
          用户不会看到 <msg>/<text> 原文；适合日常聊天。
        - 关：整段转义保留所有内容（包括标签原文），
          适合 payload/注入测试等要求逐字保真的场景。
        保证消息能发出去，且进入记忆的永远是良构 XML。
        注意基于未转义的原始块处理，避免双重转义。

        ⚠ allow_original 默认 False：旧实现在"清洗后为空"时把**原始坏块**
        原样返回，等于把不可解析的残片（如 `</text></msg>`）直接塞回结果
        ⇒ 拖垮整轮解析 ⇒ 本轮一条消息都发不出去。现在改为返回空列表丢弃；
        若整轮都被丢弃，外层总闸门 _last_resort 还会兜一次。
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
        # XML 1.0 非法控制字符会让框架解析整体失败，必须剔除
        inner = self._sanitize_illegal_chars(inner)
        if not inner:
            if allow_original:
                return [original_block]
            logger.debug(f"兜底清洗后无内容，丢弃该块（避免残片拖垮整轮）: {original_block[:80]}")
            return []
        logger.debug(f"触发终极兜底，清洗为纯文本消息: {original_block[:80]}")
        return [f"<msg><text>{xml_escape(inner)}</text></msg>"]

    def _fix_single_msg(self, msg_str: str) -> list:
        if self.fix_double_brackets:
            msg_str = self._fix_double_brackets_safe(msg_str)

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
                # 与主路径保持一致：先提升 <text> 内嵌套内容，避免被拆分后丢失
                self._lift_nested_text_content(root)
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
                    # ★ 必须早于 _extract_at_from_text / _wrap_text_in_element：
                    # 后两者会调整子元素顺序，届时已无法重建正确的文本顺序
                    self._lift_nested_text_content(root)
                    if self.extract_at_from_text:
                        self._extract_at_from_text(root)
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
        """入口：反斜杠形态有歧义时跑两条路径，选更能保住消息的那条。"""
        xml_str = self._escape_code_fences(xml_str)
        if self.fix_backslash_tags and _BACKSLASH_TAG_RE.search(xml_str):
            restored_raw = self._apply_backslash_tags(xml_str)
            if restored_raw != xml_str:
                keep_token = self._fix_xml_core(xml_str)          # 当作正文，保留 token
                as_tag = self._fix_xml_core(restored_raw)         # 当作真标签
                return self._pick_backslash_interpretation(keep_token, as_tag)
        return self._fix_xml_core(xml_str)

    @staticmethod
    def _count_msgs(xml_text: str) -> int:
        try:
            return len(ET.fromstring(f"<root>{xml_text}</root>").findall("msg"))
        except Exception:
            return -1

    def _pick_backslash_interpretation(self, keep_token: str, as_tag: str) -> str:
        r"""`<\/msg>` 有两种解释：模型打错的真闭合标签 / 正文里讨论的术语。

        判据（按优先级）：
        1. 「当作真标签」不可解析而「保留 token」可解析
           ⇒ 还原会弄坏结果，保留 token（它其实是正文）；
        2. 「当作真标签」把消息**切碎成更多条**
           ⇒ 说明它并不是在闭合当前结构，而是正文里的字面量，保留 token；
        3. 否则 ⇒ 按真标签处理（这正是用户遇到的现象：末尾冒 <\/msg> 乱码）。
        """
        keep_ok = self._is_parseable(keep_token)
        tag_ok = self._is_parseable(as_tag)
        if keep_ok and not tag_ok:
            logger.debug("反斜杠标签还原后不可解析，按正文处理（保留 token）")
            return keep_token
        if keep_ok and tag_ok:
            n_keep = self._count_msgs(keep_token)
            n_tag = self._count_msgs(as_tag)
            if n_keep >= 0 and n_tag > n_keep:
                logger.debug("反斜杠标签会切碎消息，按正文处理（保留 token）")
                return keep_token
        return as_tag

    def _fix_xml_core(self, xml_str: str) -> str:
        # ★ 这两步都必须**无条件**执行，不能受总开关影响：
        #   它们的职责是"防止坏结构破坏解析 / 防止草稿被当真消息"，
        #   属于结构保护，不决定内容对外可见性（可见性由散段分流决定）。
        #   实测把 _protect_stray_blocks 放进开关内会让 OFF 泄漏 reasoning 里的草稿。
        xml_str = self._handle_unclosed_tail(xml_str)
        xml_str = self._protect_stray_blocks(xml_str)
        xml_str = self._fix_double_brackets_safe(xml_str)

        if xml_str.strip().startswith("[") and ("Error" in xml_str or "error" in xml_str):
            return xml_str

        # 位置感知 + 嵌套深度感知切块：msg 块与散段（msg 前/之间/之后的内容）
        # 都保留，顺序不乱；嵌套 <msg> 由 _split_msg_segments 按深度配对处理
        segments = self._split_msg_segments(xml_str)

        fixed_blocks = []
        last_idx = len(segments) - 1
        for i, (is_msg, seg) in enumerate(segments):
            seg = seg.strip()
            if not seg:
                continue
            if is_msg:
                fixed_blocks.extend(self._fix_single_msg(seg))
            elif self.handle_stray_content:
                fixed_blocks.extend(self._fix_stray_segment(seg))
            elif self._passes_through_when_off(seg):
                # 关闭总开关时的散段分流：
                #   ① 未注册标签块（reasoning/think）→ **原样透传**。
                #      框架对 root 级未注册标签本来就是「静默跳过」——
                #      用户看不到，但原文进记忆。保留它能让模型在历史里
                #      看到自己用 <reasoning> 包规划的范例，约定不丢
                #      （v1.2.1 正是为这个才从"剥离"改成"保留"）。
                #      ⚠ 前置的 _protect_stray_blocks 已把内部草稿 <msg> 转义，
                #        所以透传不会让草稿被误发。
                #   ② 忘带 msg 的已注册功能标签（语音/图片/@/表情）→ 补包。
                #      它们是真修复目标，丢弃属于功能静默丢失。
                #   ③ 含 [xxx] 标记的文本 → 补包，保折扇留穗等插件的跨消息协议。
                tag = self._stray_block_tag(seg)
                if tag and tag not in self._registered_msg_tags \
                        and tag not in self._registered_root_tags \
                        and tag not in self.no_wrap_tags:
                    logger.debug(f"原样透传未注册标签 <{tag}>（用户不可见、原文进记忆）")
                    fixed_blocks.append(seg)
                else:
                    logger.debug(f"补包散段的功能内容 <{tag}>（散段处理已关闭）")
                    fixed_blocks.extend(self._fix_single_msg(f"<msg>{seg}</msg>"))
            elif i == last_idx:
                # 开关关闭时保持旧行为：只有末尾残余散段会被救回，其余丢弃
                fixed_blocks.extend(self._fix_single_msg(seg))
            else:
                logger.debug(f"已丢弃 msg 外的散段: {seg[:60]}")
        fixed_blocks = self._merge_marker_spanning_blocks(fixed_blocks)

        # ---- 逐块闸门：任何单块不可解析都不能进入最终结果 ----
        # 框架只有一次 ET.fromstring 机会，一块坏 = 整轮零消息。
        # 抢救（剥壳清洗）→ 仍不行则丢弃。
        gated = []
        for b in fixed_blocks:
            if self._is_parseable(b):
                gated.append(b)
            else:
                logger.debug(f"闸门拦截不可解析块，尝试抢救: {b[:80]}")
                gated.extend(self._salvage_block(b))
        fixed_blocks = gated

        result = "\n".join(fixed_blocks)

        # ---- 总闸门：整轮级别的不变量 ----
        if not self._is_parseable(result):
            logger.warning("最终结果仍不可解析，启用总闸门兜底")
            return self._last_resort(result)
        return result

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
        elif stray:
            # 这是**刻意的取舍**（见 README：只有语音时丢弃 @/回复，
            # 保证语音消息绝对干净 —— QQ 侧语音与 @/回复混在同条会显示异常）。
            # 只补一条 debug 日志，让"@ 消失"这件事可观测、可排查，
            # 不改变既有行为。
            logger.debug(f"消息仅含语音，按既有约定丢弃 {len(stray)} 个 @/回复元素")

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
