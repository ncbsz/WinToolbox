# -*- coding: utf-8 -*-
"""还原点删除/创建的定向验证：哨兵互斥性 + 错误如实上报 + 脚本无语法错。

不写任何系统状态：只在**没有管理员权限**的环境下验证失败路径能拿到真原因；
真正的删除需要 admin + 系统保护已开启，那属于用户手动操作。
"""
import io
import os
import sys

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


print("[1] 旧的错误命令名已经彻底不用了")
src = io.open(os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "server.py"), encoding="utf-8").read()
seg = src[src.find("def remove_restore_point"):]
seg = seg[:seg.find("\ndef ")]
chk("Remove-ComputerRestorePoint" not in seg,
    "remove_restore_point 里不再出现那个不存在的命令名")
chk(".Delete()" in seg, "改用对象上的 .Delete()")
chk("try {" in seg and "catch {" in seg, "有 try/catch 包住")
chk("SRP_OK" in seg, "用互斥哨兵而不是裸 'OK'")
chk("not $rp" in seg, "区分「序列号不存在」和「删除失败」")
chk("最早的那个还原点" in seg, "回读复核时把「最早的不让删」说清楚")

print()
print("[2] 哨兵不会误报成功（关键：不能用 `'OK' in out` 判定）")
st, why = S._rp_run(
    "$ErrorActionPreference='Stop';"
    "try { Checkpoint-Computer -Description 'ZZ_probe' "
    "  -RestorePointType 'APPLICATION_INSTALL' -ErrorAction Stop; 'SRP_OK' }"
    "catch { 'SRP_ERR:' + $_.Exception.Message }", timeout=120)
print("      状态=%s  原因=%r" % (st, why))
chk(st == "err", "无 admin 时状态是 err（不是假 ok）")
chk(bool(why), "拿到了真实原因，不是空串")
chk("权限" in why or "拒绝访问" in why or "denied" in why.lower(),
    "原因是权限/拒绝访问这类可读信息（%s）" % why[:60])
chk(not why.strip().isascii(), "报错已本地化成中文（%s）" % why[:40])

print()
print("[3] 错误解析：能从多行 PowerShell 报错里挑出有用的那一句")
msg = S._rp_first_error(
    "Get-ComputerRestorePoint : Access denied \r\n"
    "所在位置 行:1 字符: 101\r\n"
    "+ ...UTF8;Get-ComputerRestorePoint | ForEach-...\r\n"
    "    ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~\r\n"
    "    + CategoryInfo          : InvalidOperation: (:) [Get],X\r\n"
    "    + FullyQualifiedErrorId : GetWMIManagementException,Y\r\n")
print("      ->", repr(msg))
chk("权限" in msg, "取到了第一句真原因并翻成中文（%s）" % msg[:30])
chk("所在位置" not in msg and "CategoryInfo" not in msg, "滤掉了位置行与分类行")
chk(not msg.strip().isascii(), "输出是中文不是英文")
chk(S._rp_zh("some unknown english error") == "some unknown english error",
    "翻译表没命中就原样返回，不乱编")

print()
print("[4] 输入边界")
chk(S.remove_restore_point("abc") == {"ok": False, "msg": "序列号无效"},
    "非法序列号直接拒")
chk(S.remove_restore_point(None)["ok"] is False, "None 也不崩")
r = S.create_restore_point("带'单引号'的描述")
print("      带单引号描述 ->", r)
chk(r["ok"] is False and "脚本" not in str(r.get("msg")),
    "描述里的单引号被转义，没把脚本截断")

print()
print("[5] 服务端接口齐全")
for f in ("list_restore_points", "create_restore_point", "remove_restore_point",
          "_rp_run", "_rp_first_error"):
    chk(hasattr(S, f), "S.%s 存在" % f)
chk(isinstance(S.list_restore_points(), list), "list_restore_points 仍返回列表")

print()
print("=" * 56)
print("结果：%s（ok %d / fail %d）" % ("全通过" if not FAIL else "有失败",
                                      OK, len(FAIL)))
for f in FAIL:
    print("   -", f)
sys.stdout.flush()
os._exit(0 if not FAIL else 1)
