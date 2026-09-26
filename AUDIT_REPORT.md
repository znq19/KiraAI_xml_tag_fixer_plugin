# XML 标签修复器插件 — 审计报告与可执行对齐方案

**仓库**：`znq19/KiraAI_xml_tag_fixer_plugin`
**基线**：v1.4.0（`8ac2f37`）
**目标**：v1.5.0
**框架对照**：`xxynet/KiraAI` v2.34.7（本地 `/var/minis/shared/kira_fw`）
**补丁分支**：`fix/parseability-gate` @ `14861ea`（本地 fork 已提交，**未推 PR**）

---

## 一、先回答用户的问题：截图里那是什么情况？

截图日志行：

```
2026-09-26 12:00:17 INFO [message] LLM -> qq:gm:815946270: <msg message_id="309949259"><text>好嘞，狐去搜搜百度今天的热搜榜！ &lt;\/msg&gt;</text></msg>
```

**根因 = 模型用反斜杠"转义"了标签定界符**，写出了 `<\/msg>`。

模型从 JSON / 正则字面量的习惯串味过来（`"\/"` 在 JSON 里是合法的转义），
于是把闭合标签打成了 `热搜榜！<\/msg>`。

**而 v1.4.0 对 `<\/msg>` 完全没有处理**：

| 步骤 | 发生了什么 |
|---|---|
| `_fix_backslash_tags` | **不存在** —— 旧版没有这一步 |
| `_escape_specials` 的 `_RAW_LT_RE = <(?![a-zA-Z/])` | `<\/msg>` 里 `<` 后面是反斜杠 ⇒ **不匹配**，逃过转义 |
| `_escape_specials` 的 `_RAW_AMP_RE` | 与它无关 |
| `_split_msg_segments`（旧版 `find("</msg>")`） | 找不到 `</msg>`（只有 `<\/msg>`）⇒ 该 msg 块被当作未闭合，走兜底 |
| `_fallback_wrap` | 反转义 → 剥标签 → 非空 ⇒ 包成 `<msg><text>{xml_escape(inner)}</text></msg>` |
| `xml_escape` | 把 `<` → `&lt;`、`>` → `&gt;` ⇒ **`&lt;\/msg&gt;`** |
| 框架 `_add_message_ids` | 再 `fromstring` → `tostring` ⇒ 输出 `&lt;\/msg&gt;` |
| `logger.info(f"LLM -> {sid}: {raw_output}")` | **就是你看到的那一行** |

**结论**：这不是"插件坏了"，而是**插件把模型的错误当成了正文，并且原样泄露给用户**——
用户在 QQ 里也会看到末尾多出一串 `<\/msg>`。

### 复现证据（实测）

```python
IN : <msg><text>好嘞，狐去搜搜百度今天的热搜榜！ <\/msg></text></msg>
v1.4.0 OUT: <msg><text>好嘞…热搜榜！ &lt;\/msg&gt;</text></msg>      # ← 乱码泄露
v1.5.0 OUT: <msg><text>好嘞…热搜榜！</text></msg>                  # ← 正确闭合
```

---

## 二、更稳固的防法：把「可解析性」立成硬闸门（不是打补丁）

### 2.0 先明确目标：让框架的「慢速 LLM 修复」永远不被触发

你提到的"框架自带的那个 xml 修复（很慢且效果不好）"精确定位在
**内置插件** `core/plugin/builtin_plugins/kira-ai/main.py`：

```python
@on.llm_response()                      # ← MEDIUM(0)
async def on_llm_resp(self, _, resp):
    xml_data = resp.text_response
    try:
        root = ET.fromstring(f"<root>{xml_data}</root>")
        ...
    except ET.ParseError as e:
        logger.error(f"Error parsing message: {str(e)}")
        llm_req = LLMRequest(
            system_prompt=[Prompt(XML_FIX_PROMPT.format(exc=str(e)))],
            user_prompt=[Prompt(xml_data)])
        client = self.ctx.get_default_fast_llm_client()   # ← 额外一次 LLM 调用
        llm_resp = await client.chat(llm_req)
        resp.text_response = llm_resp.text_response
```

**它为什么能被本插件拦住**（钩子顺序，`core/plugin/plugin_handlers.py`）：

```python
def register(self, eh):
    self._handlers[eh.event_type].append(eh)
    self._handlers[eh.event_type].sort(reverse=True)   # ← 优先级降序 = 高优先级先执行
```

| 钩子 | 优先级 | 执行次序 |
|---|---|---|
| `xml_tag_fixer` 的 `@on.llm_response(priority=Priority.HIGH)` | **HIGH = 50** | **先** |
| 内置 `kira-ai` 的 `@on.llm_response()` | MEDIUM = 0 | 后 |

⇒ 本插件先跑并把 XML 修成良构；内置插件随后的 `ET.fromstring` **成功**，
`except` 分支不进入，**慢速 LLM 修复被跳过**。
（若本插件也修不好，内置的慢速修复仍会兜底 —— 两层是互补而非冲突。）

**实测（17 个真实场景）**：

| 版本 | 触发慢速修复的场景数 |
|---|---|
| 不装插件 | **10 / 17** |
| v1.4.0 | 6 / 17 |
| **v1.5.0** | **0 / 17** |

v1.5.0 比 v1.4.0 **多拦截 6 次**本会走慢速 LLM 的情况 ——
每次拦截省掉一轮 LLM 往返（时间 + token + 被"修坏"的风险）。

### 2.1 关键认识

框架侧只有**一次**解析机会：

```python
# core/message_manager.py:887 send_xml_messages
try:
    actions = await self._parse_xml_msg(xml_data, tag_set)   # ET.fromstring("<root>"+x+"</root>")
except Exception as e:
    logger.error(f"Error parsing message: {str(e)}")
    return []                                                # ← 本轮一条消息都发不出去
```

⇒ 所以插件的**最高不变量不是"修得漂亮"，而是"输出必须能被框架解析"**。

v1.4.0 完全没有这条不变量：它逐块修复，但**从不回头验证自己修出来的东西**。
于是出现两个方向的失守：

- **把好的弄坏**（8 类）—— 本来能正常发的消息，被修成整轮发不出
- **坏的没救回**（7 类）—— 输入已经不可解析，兜底也没兜住

### 防法：逐块闸门 + 总闸门

```
fix_xml()
  ├─ 前置修复（反斜杠还原 / 未闭合尾巴 / 杂散保护 / 双尖括号）
  ├─ 深度配对切块
  ├─ 逐块 _fix_single_msg / _fix_stray_segment
  ├─ 合并标记对
  ├─ ★ 逐块闸门：_is_parseable(b) ?
  │       否 → _salvage_block(b)：剥壳清洗 → 仍不行则丢弃
  └─ ★ 总闸门：整段 _is_parseable ?
          否 → _last_resort()：整段清理为一条纯文本消息
```

**核心权衡**：宁可**少发一条**，也不让**一轮全灭**。
`_last_resort` 在彻底清洗后若已无内容，返回空串——
空串让框架解析出 0 条消息并静默结束，比发一条坏格式导致整轮丢弃损失更小。

---

## 三、量化对比（实测）

### 3.1 模糊测试（随机 token 拼装畸形 XML）

| 指标 | v1.4.0 | v1.5.0 |
|---|---|---|
| **输出不可解析**（默认配置 · 20000 用例） | **6449（32.2%）** | **0** |
| 抛异常（默认配置） | 0 | 0 |

10 种配置组合各自 3000 用例：

| 配置 | v1.4.0 不可解析 | v1.5.0 |
|---|---|---|
| `wrap_mode=whitelist` | 875 | **0** |
| `handle_stray_content=False` | 875 | **0** |
| `strip_reasoning_block=False` | 984 | **0** |
| `fallback_wrap_text=False` | 2162 | **0** |
| `escape_special_chars=False` | 875 | **0** |
| `fix_double_brackets=False` | 860 | **0** |
| `escape_code_fences=False` | 884 | **0** |
| `convert_text_at_to_tag=True` | 875 | **0** |
| 其余 2 组 | 875 | **0** |

> `fallback_wrap_text=False` 这一档尤其说明问题：**关掉兜底就没有任何东西在防整轮丢失**。

### 3.2 自检套件（115 条断言）

| | v1.4.0 | v1.5.0 |
|---|---|---|
| 通过 | 83 | **115** |
| 失败 | **32** | **0** |

### 3.3 端到端（复刻框架 `_parse_xml_msg` + `_add_message_ids`）

16 个真实场景：**v1.5.0 全部可发送，整轮丢失 0 个**。

### 3.4 差分测试：v1.5.0 是 v1.4.0 的严格超集

语料 46 条（正常输出 / 边界 / 畸形）× 4 种配置 = 184 组，逐组对比两版：

| 分类 | 组数 |
|---|---|
| 老版好 & 新版好（**保持**） | **153** |
| 老版好 & 新版坏（**★回归**） | **0** |
| 老版坏 & 新版好（**改进**） | **31** |
| 两版都坏 | 0 |

**零回归**。且其中 20 组"用户可见文本发生变化"的用例，
**全部**是修复本身（4 类）：

1. `a<<b` / `1<<2`：老版把 `<<` 吃成 `<`，新版保留运算符
2. `热搜榜！<\/msg>` 等：老版漏 `<\/msg>` 乱码，新版正确闭合
3. `你好<\/msg>`：同上
4. 其余为 `at` 提升等既有功能

**没有任何一组是"内容被无故改动"。**

### 3.5 良构输出零改动（"感受不变"的最硬证据）

20 条**已经完全正确**的 `<msg><text>…</text></msg>`（含 emoji / markdown 列表 /
代码围栏 / URL / 路径 / `[3p]` 标记 / 带属性 msg / 空消息 `<msg/>`）：

- **15 条逐字节完全不动**（插件对正确输出是 no-op）
- 5 条有"改动"，全部是无害的：
  | 输入 | 输出 | 性质 |
  |---|---|---|
  | `<msg/>` | `<msg />` | ET 往返空格，框架语义相同（静默标记仍生效） |
  | `<msg><text></text></msg>` | `<msg><text /></msg>` | 同上 |
  | `<msg><record url="x"/>` | `<msg><record url="x" />` | 同上 |
  | 多 msg 之间 | 加 `\n` | 分隔符，框架解析结果相同 |
  | `<text>…<at>123</at>…</text>` | 提升 at 为 msg 直接子元素 | **1.4.0 的新功能**，正是为了真 @ 生效 |

---

## 四、完整缺陷清单

### 4.1 ★★★ P0：`<\/msg>` 反斜杠转义标签（用户报的那条）

**`fix_backslash_tags` 是什么？原来有没有？**

**原来没有** —— 这是 v1.5.0 **全新增加**的一步。证据：v1.4.0 全文
`grep 'backslash\|\\/'` **零匹配**；它所有 22 处正则里没有任何一条涉及反斜杠。

它做的事，就是把模型**用反斜杠"转义"掉的标签定界符还原回真标签**。

**为什么模型会这么写？** 模型从 JSON / 正则字面量的习惯串味过来
（JSON 里 `"\/"` 是合法转义，正则里 `/` 常写作 `\/`），
于是把闭合标签打成了 `<\/msg>`。

**具体怎么发挥作用（逐步，实测追踪）**：

```
输入（模型原始输出）
  <msg><text>好嘞，狐去搜搜百度今天的热搜榜！<\/msg></text></msg>
                          ↑↑↑↑↑↑↑↑
                          模型写的"反斜杠转义闭合标签"

① _fix_backslash_tags（v1.5.0 新增，跑在裸字符转义之前）
   匹配 <\ + 可选/ + 合法标签名 + >
   名字 = "msg"，msg 在已知标签名单里 ⇒ 判定为真标签
   还原 →  <msg><text>好嘞…热搜榜！</msg></text></msg>      ← 此刻仍不可解析！
                                                          （</msg> 在 </text> 之前 = mismatched tag）

② 切块器 _split_msg_segments（深度配对）
   扫描：在「第一个 <text> 开口」之后、深度回到 0 之前，
        只认 <msg> 开口 —— 而 </msg> 是**闭合**、不增加深度，
        所以它不会被当成外层闭合。
   结果切出：  [msg] <msg><text>好嘞…热搜榜！</msg>
               [散] </text></msg>          ← 残片，交给散段处理

③ 逐块修复
   msg 块：补全未闭合的 <text> → <msg><text>好嘞…热搜榜！</text></msg>  ✓ 可解析
   散段 </text></msg>：_is_parseable 为假 ⇒ 抢救清洗后为空 ⇒ 丢弃

④ 总闸门复核：整段可解析 ✓

最终
  <msg><text>好嘞，狐去搜搜百度今天的热搜榜！</text></msg>
```

> **更正**：我此前在报告里把 ② 写成"切块器找到真正的 `</msg>`，正常解析"，
> **不准确**。真实机制是**深度配对切块不把 `</msg>` 当外层闭合**（所以 msg 块
> 被正确切到 `<\/msg>` 处），再由**未闭合补全**把 `<text>` 补上；
> 顺序颠倒产生的 `</text></msg>` 残片由**逐块闸门**丢弃。
> 三步配合才得到正确结果，缺一不可 —— 这也正是"闸门"设计的价值。

对比：

| 版本 | 输出 | 用户在 QQ 里看到 |
|---|---|---|
| v1.4.0 | `<msg><text>…热搜榜！ &lt;\/msg&gt;</text></msg>` | `…热搜榜！ <\/msg>` ← **乱码** |
| v1.5.0 | `<msg><text>…热搜榜！</text></msg>` | `…热搜榜！` ← **干净** |

**判据防误伤**（`_is_tag_like`）：只有当名字
①在已知标签名单内（框架内置 / 本次请求已注册 / 用户豁免名单），**或**
②文本别处存在同名的**闭合**标签 `</name>`
才还原。所以这些**都不会被误伤**：

| 输入 | 结果 |
|---|---|
| `C:\dir\file.txt` | 不动（`dir` 不是标签名，也无 `</dir>`） |
| 正则 `\d+`、`\n`、`\t` | 不动 |
| 代码围栏内的 `<\/msg>` | 不动（围栏已先整体转义成 `&lt;`，匹配不到） |
| `<<msg>…</msg>` 里那个 `<<` | 由 `_fix_double_brackets_safe` 单独处理 |

#### 歧义消解：`<\/msg>` 也可能是"正文里讨论的 token"

同一个字符串有两种合理解释，无法单看形态分辨：

| 解释 | 例子 | 正确处理 |
|---|---|---|
| 模型打错的**真闭合标签** | `好嘞，热搜榜！<\/msg>` | 还原成 `</msg>`（否则末尾冒乱码） |
| bot 正文里**讨论这个 token** | `你说的 <\/msg> 这个写法不对` | 保留 token（否则丢内容、还切成两条） |

`fix_xml` 现在会**两条路径都跑**，按判据择优：

1. 「当作真标签」不可解析 而 「保留 token」可解析 ⇒ 保留 token（它其实是正文）
2. 两者都可解析，但「当作真标签」把消息**切碎成更多条**（msg 数变多）
   ⇒ 说明它不是在闭合当前结构，保留 token
3. 否则 ⇒ 按真标签处理

**量化效果**（分类统计 15 个用例）：

| 类别 | 判据前 | 判据后 |
|---|---|---|
| A 真修复（坏标签必须修） | 6/6 | **6/6**（未削弱） |
| B 误伤窗口（正文讲 token） | 4/6 | **1/6** |
| C 无关文本（路径/正则） | 0/3 | **0/3** |

剩下那 1 例是真歧义：`讲一下 <\/text> 是什么` —— 它确实**闭合了** `text`
（无外层 `</text>`），两种读法都自洽，选哪种都不算错。

**顺带说明**：`<\/msg>` 出现在**正文里讨论写法**时（如"什么是 `<\/msg>` 这个写法？"）
会被当成闭合标签、消息切成两条。这是已知取舍（见 §4.6），
因为该写法本身不合法，切成两条优于末尾冒乱码。

- **现象**：消息末尾冒出 `<\/msg>` 乱码；闭合标签失效
- **影响**：用户可见内容损坏 + 该 msg 块被当作未闭合而走兜底
- **修复**：新增 `_fix_backslash_tags` + 配置项 `fix_backslash_tags`（默认开）
  - 匹配 `<` + 反斜杠 + 可选 `/` + 合法标签名 + `>`
  - 仅当名字"确实像标签"才还原（在已知名单内，或别处存在 `</name>`）
  - 排在 `_escape_code_fences` **之后** ⇒ 代码围栏内的 `<\/msg>` 作为原文保留

### 4.2 ★★★ 8 类「把合法输入弄坏」

| # | 输入（本来是合法的） | v1.4.0 输出 | 后果 |
|---|---|---|---|
| A1 | `<msg><text>hi <msg><text>草稿</text></msg></text></msg>` | `…</msg>\n</text></msg>` | **整轮不可解析** |
| A2 | `<msg><text>hi</text><msg><text>草稿</text></msg></msg>` | `…\n</msg>` | 同上 |
| A3 | `<msg>hi<msg><text>x</text></msg></msg>` | `…\n</msg>` | 同上 |
| A4 | `开场白<msg><text>hi <msg>草稿</msg></text></msg>` | `…\n</text></msg>` | 同上 |
| A6 | `<msg><text>a</text></msg><msg><text>b<msg>…</msg></text></msg>` | 残片 | 同上 |
| A10 | `<foo>a<foo>b</foo>c</foo>` | `<foo>a&lt;foo&gt;b</foo>c</foo>` | 缺闭合 |
| A11 | `<reasoning>a<reasoning>b</reasoning>c</reasoning>` | 同上形态 | 缺闭合 |
| A20 | `<msg><text>hi</text><msg/></msg>` | 残片 | 整轮不可解析 |

**两个根因**：
1. **切块器**用 `xml_str.find("</msg>")` 找闭合 ⇒ 嵌套时内层 `</msg>` 被误当外层闭合。
   → 改为 `_split_msg_segments`，**按嵌套深度配对**。
2. **`_STRAY_PAIR_RE`** 的 `(.*?)</\1>` ⇒ 同名嵌套时内层闭合被当外层闭合。
   → 改为 `((?:(?!</?\1(?:\s[^>]*)?>).)*?)`，逼匹配走最外层。

### 4.3 ★★ `_fallback_wrap` 把坏块原样吐回（最隐蔽的放大器）

```python
if not inner:
    return [original_block]      # ← 把不可解析的残片直接塞回结果
```

残片（如 `</text></msg>`）就这样进入最终输出 ⇒ **拖垮整轮**。
→ 改为 `return []` 丢弃；若整轮都被丢弃，外层**总闸门**再兜一次。

### 4.4 ★★ 7 类「输入坏但救不回」

| 类别 | 例子 | v1.4.0 |
|---|---|---|
| 裸闭合标签 | `</text>`、`</msg>`、`<msg>…</msg></text>` | 原样输出 ⇒ 整轮丢 |
| text 里错写闭合名 | `<msg><text>榜！</msg></text></msg>` | 切出残片 ⇒ 整轮丢 |
| XML 非法控制字符 | `\x00`、`\x01` | XML 1.0 不允许 ⇒ 整轮丢 |
| **多个未闭合 root 标签** | `<record url="a"><record url="b">` | **`_handle_unclosed_tail` 处理完第一个就 `return`** |

**`_handle_unclosed_tail` 还有第二个缺陷**：用 `f"</{tag}>" in rest` 判断"是否已闭合"，
**同名嵌套时第一个 `</record>` 关的是内层**，会被误判成已闭合。
→ 改为 `_find_matching_close()` 做**深度配对**，并循环补到平衡（上限 64 轮防死循环）。

### 4.5 ★★ 内容静默损坏（不是不可解析，但用户内容被改坏）

| # | 问题 | 实测 | 修复 |
|---|---|---|---|
| 1 | **`<<` 无差别折叠** | `a<<b`→`a<b`、`1<<2`→`1<2`、`vector<<T>`→`vector<T>` | 加 `_is_tag_like` 判据 |
| 2 | **tail 丢失** | `<text>a<at>1</at>b</text>尾巴` → "尾巴"消失 | `_extract_at_from_text` 搬 outer_tail |
| 3 | **多 tail 顺序错乱** | `head<foo>x</foo>t1<bar>y</bar>t2` → `t2` 跑到 `<bar>` 前面 | `_wrap_text_in_element` 重建子列表 |
| 4 | **标记对合并丢 tail** | `[3p]…尾巴…` 合并后 tail 消失 | `_try_merge_text_blocks` 收集 tail |
| 5 | **`@数字` 转换丢上下文** | `@1 和 abc@1` 两个都转；`x@12345.com` 邮箱误转 | 改 `finditer` 扫原文 |
| 6 | **`domains=None` 崩** | `TypeError: 'NoneType' is not iterable` | `cfg.get(...) or [...]` |
| 7 | **`_fix_at_tags` 死分支** | `elif` 条件与 `if` **完全相同**（永不执行） | 清理，行为明确化 |

> **第 1 项有个陷阱值得单独记**：我第一版判据写成
> `name in known or f"<{name}" in xml_str` —— `a<<b` 里折叠出的 `<b`
> **自己**就满足 `"<b" in xml_str`，等于没判据。
> 正确判据是「在已知名单内 **或** 别处存在同名的**闭合**标签 `</name>`」——
> 闭合标签的形态唯一，不会被开口修复操作本身制造出来。

### 4.6 ★★ 新增修复：`<text>` 内嵌套元素的内容发不出去

**这是本轮追问时新发现的**（我最初的 `itertext()` 测试掩盖了它 —— 见 §8 方法论）。

**现象**：bot 在正文里讲解标签用法时会写出嵌套，例如

```
<msg><text>要写成 <msg><text>你好</text></msg> 这样</text></msg>
```

框架 `_parse_xml_msg` 只读 msg **直接子元素**的**直接文本**：
`value = child.text.strip() if child.text else ""`。所以：

- `<text>` 的**直接文本** = `"要写成 "` ⇒ 只有这段能发出
- 嵌套的 `<msg>`/`<text>` 及其 **tail**（`" 这样"`）在更深层级 ⇒ **用户完全看不到**

**实测（旧版）**：用户只看到 `要写成`，`你好` 和 ` 这样` 都丢了。

**修复**：新增 `_lift_nested_text_content`（跑在 `_extract_at_from_text` **之前**，
因为后者会调整子元素顺序，届时无法重建正确的文本顺序）：

1. 把每个 `<text>` 的嵌套子元素按**文档顺序**提为 msg 直接子元素；
2. 文字片段（`text` 自身 / 子元素的 tail）在它们之间重建为新的 `<text>`；
3. 原 `<text>` 自身的 tail 也接住；
4. **嵌套 `<msg>` 必须"拆壳"** —— msg 不是注册标签，框架遍历 msg 子元素时
   遇到 `<msg>` 会整块跳过，里面的文字照样丢；所以要把内容摊平进外层；
5. 提升后合并相邻碎片 `<text>`（仅限都没有子元素的），避免消息碎裂。

**只对 `<text>` 生效** —— 其他标签的子元素可能有语义（如 `<img path=...>`），不能动。

**修复前后**（忠实框架语义下用户实际看到的内容）：

| 输入 | 旧版看到 | 新版看到 |
|---|---|---|
| `要写成 <msg><text>你好</text></msg> 这样` | `要写成` ← 丢两句 | `要写成 你好 这样` ✓ |
| `想静默就输出 <msg/> 这个` | `想静默就输出` ← 丢 | `想静默就输出  这个` ✓ |
| `` 写成 `<msg><text>hi</text></msg>` 即可 `` | `写成 `` ← 丢"hi 即可" | `` 写成 `hi` 即可 `` ✓ |
| `嵌套示例 <text>内层</text> 结尾` | `嵌套示例` ← 丢 | `嵌套示例 内层 结尾` ✓ |
| `发表情要用 <emoji>21</emoji> 这个写法` | `发表情要用` ← 丢 | `发表情要用 21 这个写法` ✓ |
| `语音用 <record url="x.silk"/> 发送` | `语音用` ← 丢 | `语音用 发送` ✓ |

### 4.7 bot 讲标签时的显示：与原版逐条对照

忠实框架语义实测 13 个"讲解标签"场景：

| # | 场景 | 旧版看到 | 新版看到 | 判定 |
|---|---|---|---|---|
| 1 | 规范转义写法 `&lt;msg&gt;` | 正常 | 正常 | **一致** |
| 2 | 代码围栏里讲标签 | 正常 | 正常 | **一致** |
| 3 | 未转义嵌套 | 丢「你好」「这样」 | **全部保留** | **改进** |
| 4 | 只讲标签名 `<text>` | 正常 | 正常 | **一致** |
| 5 | 讲 at | 正常 | 正常 | **一致** |
| 6 | 讲 emoji | 丢「21 这个写法」 | **全部保留** | **改进** |
| 7 | `<\/msg>` 当术语讨论 | 原样显示 | 切成两条 | 语义差异（见 §4.8） |
| 8 | 行内代码 `` `<msg>` `` | 碎三段且丢标签原文 | **完整保留** | **改进** |
| 9 | 讲 record | 丢「发送」 | **保留** | **改进** |
| 10 | 讲多个标签 | 正常 | 正常 | **一致** |
| 11 | 行内代码包整个标签对 | 丢「hi 即可」 | **保留** | **改进** |
| 12 | 只讲闭合标签名 | 正常 | 正常 | **一致** |
| 13 | 讲自闭合 `<msg/>` | 丢「这个」 | **保留** | **改进** |

⇒ **规范写法（转义 / 围栏）完全不变**；模型写错（未转义嵌套）时
**从"丢内容"变成"保留内容"**。方向一致：更好，不是更差。

### 4.8 ★ 刻意取舍（**不要"修"**，我只加日志）

- **只有语音时丢弃 @/回复**：这是既有设计（README 明写"保证语音消息绝对干净"，
  QQ 侧语音与 @/回复混在同条会显示异常）。我只补一条 debug 日志让它可观测。
- **`<\/msg>` 当正文讲术语**（如"什么是 `<\/msg>` 这个写法？"）：会被当闭合标签，
  消息被切成两条。可接受——该写法本身不合法，切成两条优于末尾冒乱码。

---

## 五、可执行方案

### 5.1 一键应用（本地 fork 已就绪）

```sh
# 补丁分支已在本地 fork：/var/minis/shared/work/xmlfix_patched
cd /var/minis/shared/work/xmlfix_patched
git log --oneline -1          # 14861ea fix(xml_tag_fixer): 可解析性硬闸门 + ...
git show --stat HEAD          # 6 files / +931 −116

# 查看改动
git diff 8ac2f37 HEAD -- main.py
```

### 5.2 验证（5 条命令）

```sh
# 1) 自检套件：160 条断言（插件目录下运行）
python3 tests/test_fixer.py

# 2) 配置迁移：23 条断言（只迁一次/原子写/不丢配置/失败回退）
python3 tests/test_migration.py

# 3) 设计理念保真：30 条断言（默认值/黑名单/豁免/优先级/取舍/接管）
python3 tests/verify_design_intent.py

# 4) 慢速路径验证：确认框架的慢速 LLM 修复一次都不会被触发
python3 tests/verify_no_slow_path.py

# 5) 反向验证：同一套件打向 v1.4.0，应报 43 条红
cp /var/minis/shared/work/xmlfix/main.py /tmp/orig/main.py
cd /tmp/orig && python3 tests/test_fixer.py        # 期望：失败 38

# 6) 模糊测试：20000 + 10×3000 用例，期望 0 崩溃 / 0 不可解析
python3 /tmp/h/fuzz.py
```

### 5.3 部署

```sh
# 解压为 data/plugins/xml_tag_fixer/ ，或直接覆盖 main.py / schema.json / manifest.json
# 新增配置项 fix_backslash_tags 默认 true —— 老用户升级后配置里没有该键，
# 框架会用 schema 默认值补齐（与 wrap_mode 同机制），无需手动迁移
```

### 5.4 如果只想最小改动（不打全量补丁）

**按优先级排**，前三项就能覆盖用户报的现象 + 最严重的整轮丢失：

1. **`_fallback_wrap` 的 `if not inner: return [original_block]` → `return []`**
   （1 行；直接消掉最多的整轮丢失）
2. **加反斜杠还原**（约 20 行；修用户报的乱码）
3. **加总闸门**（约 15 行；兜住所有漏网）
4. 切块改深度配对（约 40 行）
5. `_STRAY_PAIR_RE` 加约束（1 行正则）
6. 其余按 §4.5 逐条

---

## 六、交付物

| 文件 | 说明 |
|---|---|
| [main.py](minis://shared/work/xmlfix_patched/main.py) | v1.5.0 实现 |
| [tests/test_fixer.py](minis://shared/work/xmlfix_patched/tests/test_fixer.py) | 160 条断言自检套件 |
| [tests/test_migration.py](minis://shared/work/xmlfix_patched/tests/test_migration.py) | 23 条配置迁移断言 |
| [tests/test_unregistered_tags.py](minis://shared/work/xmlfix_patched/tests/test_unregistered_tags.py) | 59 条未注册标签断言 |
| [tests/verify_design_intent.py](minis://shared/work/xmlfix_patched/tests/verify_design_intent.py) | 30 条设计理念保真断言 |
| [tests/verify_no_slow_path.py](minis://shared/work/xmlfix_patched/tests/verify_no_slow_path.py) | 慢速路径拦截验证（17 场景） |
| [schema.json](minis://shared/work/xmlfix_patched/schema.json) | 新增 `fix_backslash_tags` |
| [manifest.json](minis://shared/work/xmlfix_patched/manifest.json) | 1.4.0 → 1.5.0 |
| [README.md](minis://shared/work/xmlfix_patched/README.md) | 新增 1.5.0 章节 + 配置表 + 已知取舍 |
| [AUDIT_REPORT.md](minis://shared/work/xmlfix_patched/AUDIT_REPORT.md) | 本报告 |

---

## 六·五、设计理念保真（30 条断言逐条实测）

**"是优化而不是破坏原有设计理念"** 这点单独做了检查（`tests/verify_design_intent.py`）：

| # | 设计承诺 | 实测 |
|---|---|---|
| D1 | 默认黑名单模式，不误伤自定义标签 | ✓ `<mytag>内容</mytag>` 原样保留 |
| D2 | `no_wrap_tags` 豁免（mimo_tts 内置 + 用户追加，带尖括号可识别） | ✓ |
| D3 | `force_wrap_tags` 强制包裹（仅黑名单模式生效） | ✓ |
| D4 | 优先级 `IGNORE_TAGS > no_wrap_tags > force_wrap_tags` | ✓ |
| D5 | `<msg/>` 静默标记原样透传（下游/记忆依赖） | ✓ 仍是 1 个 msg 元素 |
| D6 | 代码围栏内容逐字保留 | ✓ `<foo>bar</foo>` 与 `x < y` 原文 |
| D7 | 语音单条 + @/回复归文字消息 | ✓ |
| D8 | **只有语音时丢弃 @/回复**（刻意取舍） | ✓ **未改变** |
| D9 | `fallback_strip_tags` 开=剥标签 / 关=逐字保真 | ✓ |
| D10 | `strip_reasoning_block` 关=旧行为 | ✓ 开场白被丢（与旧版一致） |
| D11 | `convert_text_at_to_tag` 默认关 | ✓ |
| D12 | `only_final_message` 默认关 | ✓ |
| D13 | `split_blank_line_messages` 默认关 | ✓ |
| D14 | 空行分段遇 `[xxx]` 标记 / 代码围栏整条不拆 | ✓ |
| D15 | 合并跨消息 `[xxx]` 标记对 | ✓ |
| D16 | MiMo 接管：只改运行时属性、不改配置文件；本插件能力关闭时不接管 | ✓ |

另：`schema.json` 21 个键与 `main.py` 的 `cfg.get` **双向完全一致**（无多余、无缺失）。

**验证脚本**（临时，未提交）：`/tmp/h/fuzz.py`、`/tmp/h/e2e.py`、
`/tmp/h/{invariant,regress,audit2,audit3,focus,focus2-5,chain,guard,rootcause}.py`

---

## 六·六、1.5.0 隐私默认调整 + 一次性迁移

### 背景：总开关打开时会泄漏模型心声（实测）

| 模型输出 | 关（新默认） | 开（旧默认） |
|---|---|---|
| `让我想想该查一下\n<msg><text>今天晴</text></msg>` | 只有 `今天晴` | ⚠ 泄漏 `让我想想该查一下` |
| `<msg><text>A</text></msg>等等先确认城市\n<msg><text>B</text></msg>` | 只有 `A` `B` | ⚠ 泄漏 `等等先确认城市` |
| `我需要先确认意图<msg><text>明白了</text></msg>` | 只有 `明白了` | ⚠ 泄漏 `我需要先确认意图` |

汇总（11 例）：**开 = 泄漏 8 处 / 丢失 0 处；关 = 泄漏 3 处 / 丢失 4 处**。

### 只翻默认值不够：旧行为会丢功能内容

纯关闭（旧行为）会把 msg 间的散段整体丢弃，于是**忘包 `<msg>` 的功能标签**也丢：

| 输入 | 纯关闭 | 细粒度（本 PR） |
|---|---|---|
| `<record url="a.silk"/><msg><text>文字</text></msg>` | 丢语音 | **保住 record** |
| `<at>123456</at><msg><text>文字</text></msg>` | 丢 @ | **保住 at** |
| `<emoji>21</emoji><msg><text>文字</text></msg>` | 丢表情 | **保住 emoji** |
| `[3p]<msg>A</msg><msg>B[/3p]</msg>` | 丢标记对 | **保住并合并** |

### 细粒度分流（本 PR 的做法）

关闭总开关时，散段按内容分类：

- **裸文本**（旁白/规划/心声）→ 丢弃 ← 这正是总开关要挡的
- **已注册功能标签 / `no_wrap` 标签** → 补包 `<msg>`，内容零转义
- **含 `[xxx]` 标记的文本** → 保留（折扇留穗等插件的跨消息协议）

⇒ 与纯关闭相比：**同样挡住心声，但不多丢任何功能内容**；
对核心修复能力**零影响**（11 个核心场景逐一与纯关闭完全一致）。

### 一次性迁移（原子写 + 失败不覆盖）

只改 schema 默认值会产生歧义：框架 `_ensure_plugin_config` 只要发现配置
缺键就会写入该键默认值 ⇒ "从没配过"与"主动配成 false"在文件里无法区分。
所以用**显式标记键** `_migrated_strip_reasoning_default`：

| 情况 | 行为 |
|---|---|
| 无标记 + 无该键 | 迁移为 `false` + 打标记 |
| 无标记 + 已显式配置 | **尊重用户选择**，只打标记 |
| 已有标记 | 什么都不做（之后用户怎么改都不干预） |
| 配置文件不存在 | 不创建、不迁移（schema 默认即 `false`） |
| 文件损坏 / 写入失败 / 校验失败 | **中止，绝不覆盖** |

**安全约定**（宁可不动，也不丢配置）：

1. 读不到/解析不了现有配置 → 直接中止
2. 只增改 `strip_reasoning_block` 与 `_migrated_*` 两个键，其余逐键比对确认未动
3. **原子写**：临时文件 + `fsync` + `os.replace`（框架自身的 `save_config`
   是 `open(w)` 直写，写一半崩会丢整个配置 —— 插件绝不走那条路）
4. 写回后重新解析校验，键数不得少于迁移前
5. 任何一步失败 → 放弃迁移，保持原文件不变

`tests/test_migration.py` 用 **23 条断言**覆盖上述全部分支（含失败注入）。

## 六·七、未注册标签（reasoning/thinking）行为修正 → 1.5.1

用户追问后做的专项审计，**又抓出 2 个真问题**：

### 问题① OFF 时 reasoning 里的 msg 草稿被当真消息发出

`_protect_stray_blocks`（负责把未注册标签内部的 `<msg>` 转义掉）被写在
总开关的 `if` 里 ⇒ **关掉开关就不执行保护** ⇒ 块里的草稿被切块器当成真消息边界：

| 输入 | 修复前 OFF | 修复后 OFF |
|---|---|---|
| `<reasoning>草稿<msg><text>示例</text></msg></reasoning><msg><text>正式</text></msg>` | `示例` + `正式` ← **草稿泄漏** | 只有 `正式` ✓ |

**修法**：`_handle_unclosed_tail` 与 `_protect_stray_blocks` 都改为**无条件执行** ——
它们的职责是"防止坏结构破坏解析 / 防止草稿被误发"，属于结构保护，
不决定内容对外可见性（可见性由散段分流决定）。

### 问题② ON 时未闭合 reasoning 把后面的真消息吞掉

`_handle_unclosed_tail` 对未注册标签原本是"**剥到末尾**"：

| 输入 | 修复前 ON | 修复后 ON |
|---|---|---|
| `<reasoning>我在想……<msg><text>真消息</text></msg>` | **(无)** ← **真消息丢了** | `真消息` ✓ |

**修法**：未注册标签不再"剥到末尾"，而是**在下一个真消息（`<msg`）之前补上闭合**，
让它仍是一个完整的未注册标签块。

> 中间试过"只删开标签保留内容"——结果思考文字变成裸文本，
> 总开关打开时照样泄漏。**必须补闭合封块**，而不是拆掉标签。

### 附带恢复：未注册标签块在 OFF 时也保留

原 v1.2.1 特意把 reasoning 从"剥离"改成"保留"，理由很明确：
剥离后模型在历史里看不到自己包规划的范例，**会逐渐忘记这个约定**。

我把它恢复成 OFF 时也**原样透传**（框架对 root 级未注册标签本来就是
"静默跳过"：用户看不到、原文进记忆）。安全性由 `_protect_stray_blocks` 保证。

### 最终行为矩阵（21 个场景 × ON/OFF）

| 输入 | ON 发出 | OFF 发出 |
|---|---|---|
| `<reasoning>我该先搜索</reasoning><msg>好嘞</msg>` | 好嘞 | 好嘞 |
| `<reasoning>想想</reasoning>让我想想该查一下<msg>今天晴</msg>` | 让我想想该查一下 + 今天晴 | 今天晴 |
| `<reasoning>草稿<msg>示例</msg></reasoning><msg>正式</msg>` | 正式 | **正式** |
| `<reasoning>我在想……<msg>真消息</msg>` | 真消息 | 真消息 |
| `<msg>A</msg><reasoning>中间思考</reasoning><msg>B</msg>` | A + B | A + B |
| `<msg><reasoning>内部思考</reasoning><text>正文</text></msg>` | 正文 | 正文 |

**心声泄漏：ON 1 处 / OFF 0 处**；**真消息丢失：两边都是 0**。

`tests/test_unregistered_tags.py` 用 **59 条断言**覆盖上述全部场景。

## 六·八、配置迁移失效修复（1.5.1，用户追问后发现）

### 问题①：迁移判断不出「用户从没配过」—— 对老用户完全没生效

我原本的设计假设是「配置里没有该键 = 用户从没配过」。**这个假设在框架里不成立**：

```python
# core/plugin/plugin_registry.py:_ensure_plugin_config
for field in schema_fields:
    elif isinstance(field, BaseConfigField) and field.key not in cfg:
        cfg[field.key] = field.default        # 缺失就写默认值
with config_path.open("w", ...) as f:
    json.dump(cfg, f, ...)                    # 而且立刻落盘
```

它**每次加载插件都会把缺失的键用默认值补齐并写回文件**。而 v1.4.0 的 schema
默认值是 **`True`** ⇒ **任何跑过 v1.4.0 的用户，文件里必然已有
`strip_reasoning_block: true`**。

于是迁移看到「键已存在」→ 判定为「用户显式配置过」→ **保留 `True`** → 迁移等于没做。

实测：

| 场景 | 1.5.0 迁移后 | 说明 |
|---|---|---|
| 键不存在（几乎不可能） | `False` ✓ | 只在凭空构造时出现 |
| **值为 True（老用户真实情况）** | **`True` ✗** | **心声照旧泄漏** |
| 值为 False | `False` ✓ | |

**为什么之前的测试没抓到**：我在测试里构造的「老用户配置」**故意不含该键** ——
那是我凭空设想的样子，不是真实用户的样子。**测试构造失真掩盖了这个 bug。**

### 修法：改用「值」判断

文件里无法区分「框架补的 `True`」与「用户主动配的 `True`」，
但**我们知道 v1.4.0 的默认值就是 `True`**：

| 升级前文件里的值 | 动作 |
|---|---|
| `true`（= 旧默认，大概率框架补的） | **改为 `false`** |
| `false` | 保持 |
| 键不存在 | 设为 `false` |

**代价**：极少数**真的主动打开过**的用户会被改回一次，日志与 README 都会提示怎么恢复。

### 问题②：迁移对当前会话不生效

```python
async def initialize(self):
    logger.info(f"... stray={self.handle_stray_content}")   # ① 已读定配置
    self._run_config_migration()                            # ② 才改文件
```

`self.handle_stray_content` 在 `__init__` 就定下来了，迁移改的是文件
⇒ **当前会话仍用旧值**，要等下次重载。实测确认。

**修法**：迁移后**重读文件并回填实例**（不是直接写 `False`，
因为迁移也可能是"保持原值"）。

> 顺带发现并修掉一个**死 import**：`_run_config_migration` 里有一句
> `from core.utils.path_utils import get_config_path  # noqa: F401`
> 在 harness 下会抛 `ModuleNotFoundError`，被 `except` 吞掉 ⇒ **整个回填逻辑被跳过**。
> 这正是问题②在测试里仍失败的原因。

### 「只做一次」的不变量（用户特别要求）

用户明确要求：**迁移后他手动打开开关，重载不得再被改回**。

`tests/test_migration.py` 用 **M4/M5** 锁定该不变量：
迁移 → 手动改 `true` → **连续重载 5 次**（每次都会跑迁移）→ 值仍为 `true`。

### 测试扩写（23 → 28 条断言）

| 编号 | 覆盖 |
|---|---|
| M1 | ★ 老用户（框架补的 True）→ 迁移为 False |
| M2 / M3 | 已关 / 缺键 的处理 |
| **M4 / M5** | ★ **只做一次**：手动开启后重载 5 次不被改回 |
| **M6** | ★ 迁移对当前实例**立即生效** |
| M7~M13 | 文件不存在 / 损坏 / 写失败 / 其它键保留 / 写回可读 / 幂等 / 无临时文件残留 |

## 七、还没做的（需要你确认）

1. **推 PR** —— 按 GLOBAL.md 约定，推 PR 前要**重新 fork** 且**需你确认**才能推。
   当前只提交到本地 `xmlfix_patched`，**没有推任何远端**。
2. ~~`<\/msg>` 当作正文术语的处理策略~~ —— **已解决**（见下方"歧义消解"）。
   现在会自动判别：还原会弄坏结果 / 会切碎消息 ⇒ 按正文处理，保留 token。
3. **`fix_backslash_tags` 的默认值** —— 我设成 `true`。
   若你担心误伤（例如 bot 专门讨论 XML 写法），可改 `false` 让用户按需开。

---

## 八、方法论：为什么我第一轮漏掉了 §4.6

**我最初用 `itertext()` 统计"用户看到什么"——这是错的。**
`itertext()` 把整棵子树的文本都算进去，而框架**只取 msg 直接子元素的直接文本**。
于是"嵌套在 `<text>` 里的内容"在 itertext 下看起来"还在"，
实际用户根本看不到 ⇒ **掩盖了一整类内容丢失**。

第二轮改成**忠实复刻 `_parse_xml_msg`**（只遍历 msg 直接子元素、只取 `child.text`）
才发现：旧版在"讲解标签"这类场景里**一直丢内容**，而我的补丁也没修它。

**教训（可复用）**：
- 判据必须**对准被测对象的真实取值口径**（`itertext()` ≠ 框架的 `child.text`）
- 差分测试要拿这个口径去比，而不是"看起来像就行"
- 一旦发现"报告里的机制描述与实际追踪不符"（§4.1 的更正），
  说明理解的抽象层级错了 —— 这时要**回到代码逐步打点**，而不是修补文字


