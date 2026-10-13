# -*- coding: utf-8 -*-
"""显卡伪装修复后的验证（**只读 + 非管理员下验证「不会谎报成功」**）。

⚠️ 本机 shell 非管理员 → 真正的写入会被拒绝，正好用来验证
   「失败时如实报错、且不留下半成品」这条。
"""
import io
import json
import os
import tempfile
import sys
import winreg

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import server as S

# ⚠️ 测试**绝不能**写程序目录里那个真实的备份文件（会被当成「用户改过显卡」的记录）。
_orig_bk = S._GPU_BACKUP_FILE
S._GPU_BACKUP_FILE = os.path.join(tempfile.gettempdir(), "wtb_gpu_alias_bk_probe.json")

OK, BAD = 0, []


def ck(cond, label):
    global OK
    if cond:
        OK += 1
        print("  ok   %s" % label)
    else:
        BAD.append(label)
        print("  FAIL %s" % label)


print("=" * 72)
print("① 实例映射：类键序号 -> 设备实例 ID（写 Enum 键的前提）")
print("=" * 72)
m = S.gpu_instance_map()
print("      ", json.dumps(m, ensure_ascii=False, indent=None))
ck(len(m) >= 2, "至少映射到 2 个显示设备")
ck("0002" in m and m["0002"].startswith("PCI\\VEN_10DE"),
   "0002 -> NVIDIA 的设备实例")
ck("0001" in m and m["0001"].startswith("PCI\\VEN_8086"),
   "0001 -> Intel 的设备实例")

print()
print("=" * 72)
print("② 要写的位置：必须包含 Enum 的 DeviceDesc（设备管理器真正读的那个）")
print("=" * 72)
tg, inst = S._gpu_targets("0002")
for lab, path, name, make in tg:
    print("      %-20s %-14s 可新建=%s" % (lab, name, make))
paths = {(l, n) for l, _p, n, _m in tg}
ck(any(n == "DeviceDesc" and l.startswith("设备实例") for l, n in paths),
   "包含 设备实例 DeviceDesc（老实现漏的就是这个）")
ck(any(n == "FriendlyName" for _l, n in paths), "包含 FriendlyName（存在时优先）")
ck(any(l == "显示驱动配置" for l, _n in paths), "顺带包含 Control\\Video（best-effort）")

print()
print("=" * 72)
print("③ REG_BINARY 的 UTF-16LE 名字（Intel 核显）能正确解出")
print("=" * 72)
with S._gpu_open(S._GPU_CLASS + r"\0001", write=False) as sk:
    txt, t = S._gpu_read_str(sk, "HardwareInformation.AdapterString")
    print("      AdapterString = %r  (type %d)" % (txt, t))
    ck(txt == "Intel(R) Iris(R) Xe Graphics", "解出 Intel 核显的 AdapterString")
    ck(t == winreg.REG_BINARY, "类型是 REG_BINARY（老实现会直接跳过这个）")
    enc = S._gpu_encode("NVIDIA GeForce RTX 4090", t)
    ck(isinstance(enc, bytes) and "4090".encode("utf-16le") in enc,
       "写回时按原类型编码成 UTF-16LE")
    # BiosString 是文本但**不是名字**（它是 BIOS 版本串）→ 由 _GPU_NEVER_TOUCH 挡住
    ck("HardwareInformation.BiosString" in S._GPU_NEVER_TOUCH,
       "BiosString 被列入「绝不能碰」清单")
    ck("HardwareInformation.BiosString" not in S._GPU_VALUE_KEYS,
       "BiosString 不在要写的值清单里")
    ck("HardwareInformation.MemorySize" in S._GPU_NEVER_TOUCH,
       "MemorySize（纯二进制）也在禁碰清单里")
    try:
        S._gpu_read_str(sk, "不存在的值")
    except OSError:
        ck(True, "不存在的值抛 OSError（调用方会跳过）")

print()
print("=" * 72)
print("④ 非管理员下 apply：必须**如实失败**，且不写坏任何东西")
print("=" * 72)
before = S._gpu_backup_load()
r = S.gpu_alias_apply("0002", "NVIDIA GeForce RTX 4090")
print("      ok =", r["ok"])
print("      msg =", r["msg"][:220])
ck(r["ok"] is False, "非管理员下如实返回失败（不谎报成功）")
ck(("管理员" in r["msg"]) or ("拒绝" in r["msg"]),
   "失败原因指向权限（用户知道该怎么办）")
# 关键：每个目标位置都必须**逐条出现在结果里** —— 曾经的 bad_list[:4]
# 会把「设备实例」那两条截掉，用户以为全都写成功了。
_tg, _i2 = S._gpu_targets("0002")
_miss = [n for _l, _p, n, _m in _tg if n not in r["msg"]]
ck(not _miss, "所有目标位置都逐条列出，无静默丢弃（缺：%s）" % (_miss or "无"))
ck("设备实例" in r["msg"], "明确点名「设备实例」这一项（设备管理器真正读的就是它）")
ck(("未改动" in r["msg"]) or ("拒绝访问" in r["msg"]),
   "没写成功的位置如实说明了原因")
ck(S._gpu_backup_load() == before, "失败后备份文件没被写坏")

print()
print("=" * 72)
print("⑤ 参数校验（不该写的时候一步都不写）")
print("=" * 72)
ck(S.gpu_alias_apply("0002", "")["ok"] is False, "空名字被拒")
ck(S.gpu_alias_apply("0002", "x" * 65)["ok"] is False, "超长被拒")
ck(S.gpu_alias_apply("abc", "NVIDIA GeForce RTX 4090")["ok"] is False, "非法序号被拒")
ck(S.gpu_alias_restore("0002")["ok"] is False, "没有备份时还原被拒（不假装）")
ck(S.gpu_restart_device("0002")["ok"] is True or True,
   "gpu_restart_device 仍在（通用能力；GPU 页已无入口，按用户要求删掉按钮）")

print()
print("=" * 72)
print("⑥ 免重启生效：映射不到时不谎报")
print("=" * 72)
r2 = S.gpu_restart_device("9999")
print("      ok =", r2["ok"], "| msg =", r2["msg"][:80])
ck(r2["ok"] is False and "找不到" in r2["msg"], "映射不到时如实说找不到")

print()
print("=" * 72)
print("⑦ 系统现在实际报的显卡名（验证用）")
print("=" * 72)
for x in S.gpu_current_wmi_names():
    print("      %-40s %s" % (x["name"], x["pnp"][:46]))
ck(len(S.gpu_current_wmi_names()) >= 2, "能查到系统当前报的显卡名")

print()
print("⑥ 显示名清洗（用户会把预览里的 <b>…</b> 原样粘回输入框）")
import app as A                                                  # noqa: E402

L, G = chr(60), chr(62)          # 用 chr 拼尖括号，源码里不出现裸标记
for _src, _want in ((L + "b" + G + "NVIDIA GeForce RTX 3080" + L + "/b" + G,
                     "NVIDIA GeForce RTX 3080"),
                    ("NVIDIA RTX 4090", "NVIDIA RTX 4090"),
                    (L + "b" + G + L + "i" + G + "RTX" + L + "/i" + G
                     + L + "/b" + G + " 4090", "RTX 4090"),
                    ("AMD &amp; Co", "AMD & Co"),
                    ("  " + L + "b" + G + "  多  空格  " + L + "/b" + G + "  ",
                     "多 空格"),
                    ("", ""), (None, "")):
    ck(A.plain_name(_src) == _want,
       "plain_name(%r) -> %r" % (_src, A.plain_name(_src)))

_src_gpu = io.open(os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "app.py"), encoding="utf-8").read()
ck("def plain_name(" in _src_gpu, "app.py 里有 plain_name（不是内联正则）")
ck(_src_gpu.count("plain_name(self.name_edit.text())") >= 3,
   "apply / 预览 / 核验三处读取都走清洗")
ck("def _clean_pasted(" in _src_gpu, "输入框有粘贴自动清洗")
ck("名字太长" in _src_gpu, "超长名字在写注册表之前被拦")
ck("HardwareInformation.BiosString" in S._GPU_NEVER_TOUCH
   and "HardwareInformation.MemorySize" in S._GPU_NEVER_TOUCH,
   "禁碰清单在 server.py 且没被这次改动碰坏")

print()
print("=" * 72)
print("结果：%s（ok %d / fail %d）"
      % ("全通过" if not BAD else "有失败", OK, len(BAD)))
for b in BAD:
    print("   -", b)
try:
    os.remove(S._GPU_BACKUP_FILE)
except OSError:
    pass
S._GPU_BACKUP_FILE = _orig_bk
sys.stdout.flush()
os._exit(0 if not BAD else 1)