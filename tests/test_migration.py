"""配置迁移测试（v1.5.1 彻底版）。

核心不变量：
  M1 老用户（框架补的 True）        → 迁移为 False  ★本次修复的核心
  M2 已是 False                     → 保持 False
  M3 键不存在                       → 设为 False
  M4 ★ 只做一次：迁移后用户手动开 True，重载/再跑迁移【不得】改回
  M5 重载多次（模拟插件反复重载）    → 用户的手动设置始终保留
  M6 迁移对当前实例立即生效          → 不用等重载
  M7 文件不存在                     → 不创建、不迁移
  M8 文件损坏                       → 中止，不覆盖
  M9 写入失败                       → 中止，不损坏
  M10 其它键原样保留
  M11 写回后配置完整可读
  M12 幂等（连跑 5 次结果不变）
"""
import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, '/tmp/h')   # 本地 harness

from main import (migrate_config_once, _MIGRATION_KEY, _STRIP_KEY,
                  _MIGRATION_VERSION, XmlTagFixerPlugin)

PASS = 0
FAIL = []


def ck(name, cond, detail=""):
    global PASS
    if cond:
        PASS += 1
    else:
        FAIL.append(name)
        print(f"  FAIL {name}  {detail}")


def sandbox(initial=None, raw=None):
    d = tempfile.mkdtemp(prefix="xmlfix_mig_")
    path = os.path.join(d, "xml_tag_fixer.json")
    if raw is not None:
        with open(path, "w", encoding="utf-8") as f:
            f.write(raw)
    elif initial is not None:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(initial, f, ensure_ascii=False, indent=4)
    return d, path


def read(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


print("配置迁移测试（1.5.1 彻底版）")
print("=" * 74)

# ---- 真实老用户：框架把 v1.4.0 默认值 True 写进了文件 ----
OLD_USER = {
    "enabled": True, "only_final_message": False, "fix_missing_msg": True,
    "wrap_mode": "blacklist", "no_wrap_tags": ["mimo_tts"], "force_wrap_tags": [],
    "flatten_no_wrap_tags": True, "fix_record_split": True,
    "merge_marker_span_msgs": True,
    _STRIP_KEY: True,                       # ← 框架补的 v1.4.0 默认值
    "convert_text_at_to_tag": False,
}

# M1 ★核心：老用户必须被迁移
d, p = sandbox(OLD_USER)
before = read(p)
r = migrate_config_once(p)
after = read(p)
ck("M1 老用户被迁移（返回非 None）", r is not None, r)
ck("M1 值改为 False", after.get(_STRIP_KEY) is False, after.get(_STRIP_KEY))
ck("M1 标记已写入", after.get(_MIGRATION_KEY) == _MIGRATION_VERSION)
ck("M1 其它键全部保留",
   all(after.get(k) == v for k, v in before.items() if k != _STRIP_KEY),
   f"{before} -> {after}")
ck("M1 键数只增 1（标记）", len(after) == len(before) + 1, f"{len(before)} -> {len(after)}")
shutil.rmtree(d)

# M2 已是 False → 保持
d, p = sandbox({"enabled": True, _STRIP_KEY: False})
migrate_config_once(p)
ck("M2 已关的保持 False", read(p).get(_STRIP_KEY) is False, read(p))
shutil.rmtree(d)

# M3 键不存在 → 设为 False
d, p = sandbox({"enabled": True, "wrap_mode": "blacklist"})
migrate_config_once(p)
ck("M3 缺键时设为 False", read(p).get(_STRIP_KEY) is False, read(p))
shutil.rmtree(d)

# ---- M4/M5 ★ 只做一次：用户手动开启必须被尊重 ----
d, p = sandbox(OLD_USER)
migrate_config_once(p)                       # 第一次迁移 → False
ck("M4 迁移后为 False", read(p).get(_STRIP_KEY) is False)

# 用户手动到 WebUI 把开关打开
cfg = read(p)
cfg[_STRIP_KEY] = True
with open(p, "w", encoding="utf-8") as f:
    json.dump(cfg, f, ensure_ascii=False, indent=4)

# 模拟插件反复重载（每次重载 initialize 都会调 _run_config_migration）
for i in range(5):
    res = migrate_config_once(p)
    ck(f"M5 第{i+1}次重载迁移不再动手（返回 None）", res is None, res)
ck("M4★ 用户手动开启被保留（只做一次）", read(p).get(_STRIP_KEY) is True,
   f"被改回了！{read(p)}")
shutil.rmtree(d)

# ---- M6 迁移对当前实例立即生效 ----
d, p = sandbox(OLD_USER)
inst = XmlTagFixerPlugin(None, dict(OLD_USER))     # 构造时读到 True
ck("M6 构造时读到 True", inst.handle_stray_content is True, inst.handle_stray_content)
inst._resolve_config_path = staticmethod(lambda: p)   # 指向沙箱文件
inst._run_config_migration()
ck("M6 迁移后文件为 False", read(p).get(_STRIP_KEY) is False)
ck("M6★ 迁移后实例立即变为 False（不用等重载）",
   inst.handle_stray_content is False, inst.handle_stray_content)
shutil.rmtree(d)

# ---- M7 文件不存在 → 不创建 ----
d = tempfile.mkdtemp(prefix="xmlfix_mig_")
p = os.path.join(d, "xml_tag_fixer.json")
ck("M7 全新安装不迁移", migrate_config_once(p) is None)
ck("M7 不创建文件", not os.path.exists(p))
shutil.rmtree(d)

# ---- M8 损坏 JSON → 中止且不覆盖 ----
d, p = sandbox(raw='{"enabled": true, "wrap_mode": BROKEN')
raw_before = open(p, encoding="utf-8").read()
ck("M8 损坏时中止", migrate_config_once(p) is None)
ck("M8 损坏文件未被覆盖", open(p, encoding="utf-8").read() == raw_before)
shutil.rmtree(d)

# ---- M9 写入失败 → 不损坏 ----
d, p = sandbox({"enabled": True})
os.unlink(p)
os.mkdir(p)          # 目标变目录 ⇒ os.replace 必失败
ck("M9 写入失败时安全返回", migrate_config_once(p) is None)
ck("M9 不抛异常、不损坏", os.path.isdir(p))
shutil.rmtree(d)

# ---- M10/M11 复杂配置完整保留 ----
complex_cfg = {
    "enabled": True, "wrap_mode": "blacklist",
    "no_wrap_tags": ["mimo_tts", "foo", "<Bar>"],
    "force_wrap_tags": ["x", "y"],
    "text_at_exclude_domains": ["com", "cn", "zz"],
    "nested_like": {"a": 1, "b": [1, 2, {"c": 3}]},
    "unicode": "中文测试 🐍",
    _STRIP_KEY: True,
}
d, p = sandbox(complex_cfg)
migrate_config_once(p)
after = read(p)
ck("M10 复杂配置逐键保留",
   all(after.get(k) == v for k, v in complex_cfg.items() if k != _STRIP_KEY),
   f"{complex_cfg}\n-> {after}")
ck("M11 新增键恰好 1 个（标记）", len(after) == len(complex_cfg) + 1)
ck("M11 写回后可读且值正确", after.get(_STRIP_KEY) is False)
shutil.rmtree(d)

# ---- M12 幂等 ----
d, p = sandbox(OLD_USER)
migrate_config_once(p)
snap = read(p)
for _ in range(5):
    migrate_config_once(p)
ck("M12 连跑 5 次结果不变", read(p) == snap, f"{snap} -> {read(p)}")
shutil.rmtree(d)

# ---- M13 无残留临时文件 ----
d, p = sandbox(OLD_USER)
migrate_config_once(p)
leftovers = [f for f in os.listdir(d) if f != "xml_tag_fixer.json"]
ck("M13 无临时文件残留", not leftovers, leftovers)
shutil.rmtree(d)

print("=" * 74)
print(f"迁移断言：通过 {PASS}，失败 {len(FAIL)}")
for n in FAIL:
    print("  -", n)
sys.exit(1 if FAIL else 0)
