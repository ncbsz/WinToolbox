# -*- coding: utf-8 -*-
"""计划任务：schtasks → PowerShell 替代的定向验证。

⚠️ 只做**只读**与「改完立刻改回」的操作，绝不新增/删除任务。
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import server as S                                          # noqa: E402

OK, FAIL = 0, []


def chk(cond, label):
    global OK
    if cond:
        OK += 1
        print("  ok  %s" % label)
    else:
        FAIL.append(label)
        print("  FAIL %s" % label)


EXIST = "\\Microsoft\\Windows\\Defrag\\ScheduledDefrag"   # 本机存在且已禁用
ABSENT = "\\ZZ_NoSuchTask_9F3A\\nope"

print("[1] 源码里已无可执行的 schtasks 调用")
import ast                                                       # noqa: E402
import io                                                       # noqa: E402
_p = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                  "server.py")
src = io.open(_p, encoding="utf-8").read()
# 用 AST 找`run([...])` 里第一个字符串字面量是 schtasks 的调用
# —— 纯文本 grep 会把 docstring/注释里的历史说明也算进来（那不是调用）。
hits = []
for _node in ast.walk(ast.parse(src)):
    if (isinstance(_node, ast.Call) and isinstance(_node.func, ast.Name)
            and _node.func.id == "run" and _node.args
            and isinstance(_node.args[0], ast.List) and _node.args[0].elts
            and isinstance(_node.args[0].elts[0], ast.Constant)
            and _node.args[0].elts[0].value == "schtasks"):
        hits.append(_node.lineno)
chk(not hits, "没有任何 run(['schtasks', ...]) 调用（AST 查，行号 %s）"
    % (hits or "无"))

print()
print("[2] _task_list_raw 能读到任务")
t0 = time.time()
rows = S._task_list_raw()
print("      %d 条，用时 %.2fs，样本 %s" % (len(rows), time.time() - t0, rows[:2]))
chk(len(rows) > 100, "读到 %d 条任务（>100）" % len(rows))
chk(all(isinstance(r, tuple) and len(r) == 2 for r in rows),
    "每项是 (路径, 名称) 二元组")

print()
print("[3] _task_exists：修好了假阴性")
e1 = S._task_exists(EXIST)
e2 = S._task_exists(ABSENT)
e3 = S._task_exists("")
print("      存在的任务 -> %s（应为 True）" % e1)
print("      不存在的   -> %s（应为 False）" % e2)
print("      空串       -> %s（应为 False）" % e3)
chk(e1 is True, "**存在的任务现在能正确判定为 True**（修复前恒 False）")
chk(e2 is False, "不存在的任务仍是 False")
chk(e3 is False, "空串安全返回 False")

print()
print("[4] _task_split 拆路径")
for raw, want in ((EXIST, ("\\Microsoft\\Windows\\Defrag\\", "ScheduledDefrag")),
                  ("\\ASUS\\ArmourySocketServer",
                   ("\\ASUS\\", "ArmourySocketServer")),
                  ("NoPathTask", ("\\", "NoPathTask"))):
    got = S._task_split(raw)
    chk(got == want, "拆 %r -> %s（应为 %s）" % (raw, got, want))

print()
print("[5] _scan_tasks 能扫出非系统任务（改走 PowerShell 后不再返回 0 条）")
# ⚠️ 匹配语义（实测自 `_res_score`）：命中靠 **`full`（规范化后的全名精确/
#    近似匹配）**，`words` 只是「独立词相同」的弱命中兜底。所以要用真实的
#    程序名去构造 token，不能随便传几个关键词 —— 传 `{"asus"}` 这种 4 字符
#    词根本不会进 words（阈值 ≥5），自然一条都扫不到。
full_a, words_a = S._res_tokens("AcPowerNotification", "ASUS")
print("      _res_tokens('AcPowerNotification','ASUS') -> full=%s words=%s"
      % (full_a, words_a))
found_a = S._scan_tasks(full_a, words_a, 10)
print("      扫 ASUS -> %d 条" % len(found_a))
chk(len(found_a) > 0,
    "按程序名能扫到对应任务（%d 条）—— 修复前恒为 0 条" % len(found_a))
chk(all(not f["path"].lower().startswith("\\microsoft\\") for f in found_a),
    "系统任务 \\Microsoft\\ 被正确跳过")
chk(all(f["kind"] == "task" and f["hive"] == "TASK" for f in found_a),
    "结构与原来一致（hive=TASK / kind=task）")
chk(all("\\" in f["path"] for f in found_a),
    "path 是完整全名（带路径），可直接喂给 set_task / 删除")
if found_a:
    chk(S._task_exists(found_a[0]["path"]),
        "扫出的第一条能被 _task_exists 证实存在（%s）" % found_a[0]["path"][:50])
# 不相关的词不该误报
found_none = S._scan_tasks(*S._res_tokens("ZZ不存在的软件名XYZ", "ZZPublisher"),
                           10)
print("      扫不存在的软件 -> %d 条（应为 0）" % len(found_none))
chk(len(found_none) == 0, "无关程序名不误报")

print()
print("[6] set_task：真能用吗（只对已禁用的任务做「禁用」，状态应不变）")
t0 = time.time()
ok, msg = S.set_task(EXIST, False)      # 本来就是 Disabled，禁用是幂等的
print("      set_task(disable) -> %s，%r（%.2fs）" % (ok, msg, time.time() - t0))
chk(isinstance(ok, bool) and msg, "返回 (bool, 说明)，说明非空")
chk("找不到" not in msg and "不是内部" not in msg, "不是「命令不存在」类报错")

print()
print("[7] 真的启用一下再改回（验证写路径，然后复原）")
ok1, m1 = S.set_task(EXIST, True)
st = ""
rc, out = S.ps("Get-ScheduledTask -TaskName 'ScheduledDefrag' "
               "-TaskPath '\\Microsoft\\Windows\\Defrag\\' "
               "-ErrorAction SilentlyContinue | ForEach-Object { $_.State }",
               timeout=90)
st = (out or "").strip().splitlines()
st = st[0].strip() if st else "?"
print("      启用 -> %s，%r ；回读状态 = %s" % (ok1, m1, st))
ok2, m2 = S.set_task(EXIST, False)      # 改回原来的 Disabled
rc, out = S.ps("Get-ScheduledTask -TaskName 'ScheduledDefrag' "
               "-TaskPath '\\Microsoft\\Windows\\Defrag\\' "
               "-ErrorAction SilentlyContinue | ForEach-Object { $_.State }",
               timeout=90)
st2 = (out or "").strip().splitlines()
st2 = st2[0].strip() if st2 else "?"
print("      改回禁用 -> %s，%r ；回读状态 = %s" % (ok2, m2, st2))
chk(st2.lower() == "disabled", "**已复原成原来的 Disabled 状态**（没留下副作用）")

print()
print("=" * 58)
print("结果：%s（ok %d / fail %d）" % ("全通过" if not FAIL else "有失败",
                                      OK, len(FAIL)))
for f in FAIL:
    print("   -", f)
sys.stdout.flush()
os._exit(0 if not FAIL else 1)