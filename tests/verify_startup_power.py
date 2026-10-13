# -*- coding: utf-8 -*-
"""启动项状态 / 定位辅助 / 右键菜单分离 / 电源页 / 打包 SSL / 服务详情 / 主题模式 /
预装应用 —— 定向回归。

约定：
  * 服务端部分**真读系统**（只读），写系统的地方一律打桩；
  * 打包配置部分是纯文本断言（防止再把 OpenSSL 库误删）；
  * UI 部分构造真窗口渲染，但绝不写用户配置、不 close 窗口。

用法：python tests/verify_startup_power.py
"""
import io
import os
import re
import sys
import time
import winreg

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
os.environ["WTB_NO_BROWSER"] = "1"

LOG, FAILS = [], []


def chk(name, cond):
    LOG.append(("  [PASS] " if cond else "  [FAIL] ") + name)
    if not cond:
        FAILS.append(name)


print("=" * 62)
print("① 服务端：开机启动项（真读系统，只读）")
print("=" * 62)
import server as S  # noqa: E402

items = S.list_startup()
chk("list_startup 能读到项（%d 条）" % len(items), len(items) > 0)
need = ("hive", "path", "name", "cmd", "exe", "enabled", "publisher",
        "service", "kind", "managed", "folder")
chk("每项都带齐新字段", all(all(k in x for k in need) for x in items))
chk("enabled 全是布尔值", all(isinstance(x["enabled"], bool) for x in items))
chk("至少有一项取到发布者（%d/%d）"
    % (sum(1 for x in items if x["publisher"]), len(items)),
    any(x["publisher"] for x in items))
chk("启动文件夹项标记为 folder=True（%d 条）"
    % sum(1 for x in items if x["folder"]),
    all(x["folder"] for x in items if x["kind"] == "folder"))

# 禁用判定必须与注册表原始数据一致（这就是"以前禁用过的也要显示出来"的依据）
mismatch = []
for x in items:
    if x["kind"] not in ("run", "run32", "folder"):
        continue
    sub = S._APPR_KEYS[x["kind"]]
    raw = None
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        try:
            with winreg.OpenKey(hive, sub, 0, winreg.KEY_READ) as k:
                raw, _t = winreg.QueryValueEx(k, x["name"])
            break
        except OSError:
            continue
    if raw is None:
        expect = True                       # 没有记录 = 按启用算
    else:
        expect = not (int(raw[0]) & 1)
    if expect != x["enabled"]:
        mismatch.append((x["name"], expect, x["enabled"]))
chk("启用/禁用判定与 StartupApproved 原始字节一致（%s）" % (mismatch or "全部一致"),
    not mismatch)

print("=" * 62)
print("② 服务端：定位辅助")
print("=" * 62)
svcs = S.services_registry()
chk("services_registry 读到服务（%d 条）" % len(svcs), len(svcs) > 100)
chk("服务项含 name/display/img", all(set(("name", "display", "img")) <= set(s) for s in svcs))

# 找一个确实存在的服务来验证匹配
probe = next((s for s in svcs if s["name"] == "Spooler"), None)
if probe is None:
    probe = svcs[0]
chk("按服务名能匹到对应服务（%s）" % probe["name"],
    S.find_service_for(probe["name"], "") == probe["name"])
if probe["display"]:
    chk("按显示名也能匹到（%s）" % probe["display"],
        S.find_service_for(probe["display"], "") == probe["name"])
chk("svchost.exe 这种共享宿主不会乱匹配",
    S.find_service_for("不存在的名字xyz", r"C:\Windows\System32\svchost.exe") == "")
chk("毫不相干的启动项不会匹到服务",
    S.find_service_for("RandomThing.exe", r"C:\Windows\explorer.exe") == "")

# regedit 定位：写入后再还原（只动 LastKey 一个值）
RK = r"Software\Microsoft\Windows\CurrentVersion\Applets\Regedit"
orig = None
try:
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RK) as k:
        orig, _t = winreg.QueryValueEx(k, "LastKey")
except OSError:
    pass
ok = S.set_regedit_lastkey("HKCU", r"SOFTWARE\Microsoft\Windows\CurrentVersion\Run")
got = ""
try:
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RK) as k:
        got, _t = winreg.QueryValueEx(k, "LastKey")
except OSError:
    pass
chk("set_regedit_lastkey 成功", ok)
chk("LastKey 指向目标键（%s）" % got[-60:],
    got.endswith(r"SOFTWARE\Microsoft\Windows\CurrentVersion\Run"))
chk("LastKey 前缀沿用系统语言（%s）"
    % (got.split("\\", 1)[0] if "\\" in got else "?"),
    ("\\" in got) and (not orig or got.split("\\", 1)[0] == orig.split("\\", 1)[0]))
# 还原
try:
    k = winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, RK, 0, winreg.KEY_SET_VALUE)
    if orig is not None:
        winreg.SetValueEx(k, "LastKey", 0, winreg.REG_SZ, orig)
    winreg.CloseKey(k)
    print("  （已还原 regedit LastKey）")
except OSError:
    pass

print("=" * 62)
print("③ 打包配置：OpenSSL 库不能再被误删")
print("=" * 62)
spec = io.open(os.path.join(ROOT, "portable.spec"), encoding="utf-8").read()
# 取出 EXCLUDE_WORDS 列表体；先把注释剥掉，否则注释里提到的
# 「裸写 'libssl' 会…」会被误当成真的排除项
body = spec.split("EXCLUDE_WORDS = [", 1)[1].split("]", 1)[0]
body = "\n".join(line.split("#", 1)[0] for line in body.split("\n"))
words = [w.strip().strip("',\"") for w in re.findall(r"'[^']+'|\"[^\"]+\"", body)]
chk("排除列表里没有裸 'libssl'（%s）" % [w for w in words if "ssl" in w.lower()],
    "libssl" not in words)
chk("排除列表里没有裸 'libcrypto'", "libcrypto" not in words)
chk("排除的是 PySide6 下的副本", any(w.startswith("pyside6\\") and "ssl" in w for w in words))
chk("过滤后强制补回 OpenSSL 库", "libcrypto*.dll" in spec and "libssl*.dll" in spec)
chk("OpenSSL 库不参与 UPX 压缩", "libssl-3-x64.dll" in spec.split("upx_exclude")[1][:200])

print("=" * 62)
print("④ UI：导航 / 表格右键菜单分离 / 启动项 / 电源计划")
print("=" * 62)
from PySide6.QtCore import Qt, QPoint                      # noqa: E402
from PySide6.QtWidgets import QApplication, QMenu          # noqa: E402

import app as A                                            # noqa: E402

QAPP = QApplication.instance() or QApplication([])
A.settings_save = lambda *a, **k: None                     # 绝不写用户配置
A.info = lambda *a, **k: None
A.warn = lambda *a, **k: None
A.confirm = lambda *a, **k: True
A.confirm_ex = lambda *a, **k: (True, False)
A.open_power_control = lambda page=True: True
S.power_plan_delete = lambda guid: {"ok": True, "msg": "已删除"}
S.power_plan_activate_template = lambda kind="high": {"ok": True, "msg": "ok"}
S.cpu_set_state = lambda kind, v, guid=None: {"ok": True}
S.cpu_set_plan = lambda guid: {"ok": True}
S.set_startup_enabled = lambda *a, **k: {"ok": True, "verified": True, "msg": "已切换"}
S.set_service = lambda n, a: (True, "")
S.set_task = lambda f, e: (True, "")
S.power_plan_candidates = lambda extra=None: {"items": [], "pow_count": 0, "plan_count": 0}

CAPTURED = []


class FakeMenu(QMenu):
    def exec(self, *a, **k):
        CAPTURED.append([x.text() for x in self.actions()])
        return None


A.QMenu = FakeMenu


def pump(sec):
    t0 = time.time()
    while time.time() - t0 < sec:
        QAPP.processEvents()
        time.sleep(0.03)


win = A.MainWindow(settings={"close_action": "ask", "check_update": False},
                   admin_mode=True, dark=False, sysinfo=A.probe_system_info(QAPP))
win.resize(1180, 780)
win.setAttribute(Qt.WA_DontShowOnScreen, True)
win.show()
pump(2.0)

nav = [k for k, _, _ in win.NAV]
# 页数会随功能增加而变（13→16），写死会每次加页都误报 —— 只断言关键页都在
chk("导航含全部关键页（%d 项）" % len(nav),
    all(k in nav for k in ("overview", "optimize", "cputune", "power", "gpu",
                           "software", "startup", "performance", "network",
                           "dns", "cleanup", "policies", "settings")))
chk("「电源管理」排在「CPU 调试」之后", nav.index("power") == nav.index("cputune") + 1)

# --- 启动与服务 ---
win.goto("startup")
pump(6.0)
sp = win.pages["startup"]
chk("启动项表 5 列", sp.t1.columnCount() == 5)
chk("启动项表头 = 名称/发布者/状态/位置/命令",
    [sp.t1.horizontalHeaderItem(i).text() for i in range(5)]
    == ["名称", "发布者", "状态", "位置", "命令"])
n_off = sum(1 for x in sp.startup if not x["enabled"])
sp.su_filter.setCurrentIndex(2)
pump(0.4)
chk("「仅已禁用」筛选行数正确（%d）" % sp.t1.rowCount(), sp.t1.rowCount() == n_off)
sp.su_filter.setCurrentIndex(0)
pump(0.4)
chk("「全部」显示所有项（%d）" % sp.t1.rowCount(), sp.t1.rowCount() == len(sp.startup))

CAPTURED.clear()
A._column_menu(sp.t1, QPoint(60, 5))
A._row_menu(sp.t1, QPoint(60, 40))
head, body = (CAPTURED + [[], []])[0], (CAPTURED + [[], []])[1]
chk("表头菜单：只有列调整项", "自动调整此列宽度" in head
    and "适应窗口宽度" in head and "定位文件夹" not in head)
chk("内容区菜单：没有列调整项", "自动调整此列宽度" not in body)
chk("内容区菜单：是行操作", any(t in body for t in ("打开文件所在位置", "定位文件夹")))

m = FakeMenu(sp.t1)
sp._startup_menu(m, sp.t1, 0)
lab = [x.text() for x in m.actions()]
chk("启动项菜单首项是 禁用/启用", lab[0] in ("禁用", "启用"))
chk("启动项菜单含服务详情 / 服务管理器 / 注册表定位",
    "服务详情…" in lab and "在服务管理器中打开" in lab and "定位到注册表项" in lab)
chk("启动项菜单含 在线搜索 / 属性 / 复制命令",
    all(t in lab for t in ("在线搜索", "属性", "复制命令")))

# --- 电源管理 ---
win.goto("power")
pump(5.0)
pw = win.pages["power"]
chk("电源管理页读到计划（%d 个）" % len(pw._plans), len(pw._plans) > 0)
mm = FakeMenu(pw.plan_table)
pw._plan_menu(mm, pw.plan_table, 0)
plab = [x.text() for x in mm.actions()]
chk("电源计划菜单第 1 项 = 删除电源计划", plab[0] == "删除电源计划")
chk("电源计划菜单第 2 项 = 修改电源计划", plab[1] == "修改电源计划")
bad = []
for r in range(pw.plan_table.rowCount()):
    guid = pw.plan_table.item(r, 2).text()
    plan = pw._plan_by_guid(guid)
    mx = FakeMenu(pw.plan_table)
    pw._plan_menu(mx, pw.plan_table, r)
    if plan and mx.actions()[0].isEnabled() == bool(plan.get("active")):
        bad.append(r)
chk("当前生效的计划「删除」置灰（异常行 %s）" % (bad or "无"), not bad)
chk("CPU 调试页已不含电源控件",
    not hasattr(win.pages["cputune"], "plan_combo")
    and not hasattr(win.pages["cputune"], "pow_table"))

print("=" * 62)
print("⑤ 服务详情（真读系统，只读）")
print("=" * 62)
d = S.service_detail("Spooler")
chk("service_detail 读到 Spooler", bool(d.get("name")))
for k in ("display_text", "state_text", "start_text", "type_text", "account",
          "img", "depend", "dependents"):
    chk("详情含字段 %s" % k, k in d)
chk("显示名解析成可读文本（%s）" % d.get("display_text"),
    bool(d.get("display_text")) and not str(d["display_text"]).startswith("@"))
chk("状态可读（%s）" % d.get("state_text"),
    d.get("state_text") in ("运行中", "已停止", "正在启动", "正在停止",
                            "正在暂停", "正在继续", "已暂停"))
chk("描述解析成可读文本（%s…）" % str(d.get("desc_text"))[:16],
    bool(d.get("desc_text")) and not str(d["desc_text"]).startswith("@"))
chk("查不存在的服务返回空 dict", S.service_detail("NoSuchService_XYZ") == {})
ck0 = S.services_registry()[0]
chk("services_registry 带上 desc/depend/account/start",
    all(k in ck0 for k in ("desc", "depend", "account", "start", "type")))

print("=" * 62)
print("⑥ 主题模式（跟随系统 / 浅色 / 深色）")
print("=" * 62)
chk("旧设置（无 theme 键）按 dark 推 → light", win.theme_mode == "light")
win.set_theme_mode("system")
pump(0.8)
chk("system 模式解析到系统深浅（系统=%s 程序=%s）" % (S.system_uses_dark(), win.dark),
    win.dark == bool(S.system_uses_dark()))
sp2 = win.pages["settings"]
chk("设置页有主题下拉，当前值 = system",
    hasattr(sp2, "combo_theme") and sp2.combo_theme.currentData() == "system")
chk("下拉共 3 项（跟随系统 / 浅色 / 深色）", sp2.combo_theme.count() == 3)
win.set_theme_mode("dark")
pump(0.8)
chk("切深色生效且下拉同步",
    win.dark is True and sp2.combo_theme.currentData() == "dark")
ck_title = win.btn_theme.text()
chk("标题栏按钮图标跟随主题（%s）" % ck_title,
    ck_title == ("☀" if win.dark else "🌙"))
win.goto("overview")
pump(6.0)
_g = win.pages["overview"].metrics["cpu"]["bar"]
_c1 = _g._color
win.set_theme_mode("light")
pump(0.8)
chk("自绘控件（环形表）颜色随主题重取", _g._color != _c1 and _g._dark is False)
ck_t = win.pages["overview"].radar
chk("雷达图 _dark 同步", ck_t._dark is False)
win._apply_theme_mode()
chk("重复应用同一模式不会出错（幂等）", win.dark is False)

print("=" * 62)
print("⑦ 预装应用（保护名单 + 清单）")
print("=" * 62)
chk("关键包受保护（商店 / 运行时）",
    S._uwp_protected("Microsoft.WindowsStore")
    and S._uwp_protected("Microsoft.UI.Xaml")
    and S._uwp_protected("Microsoft.DesktopAppInstaller"))
chk("普通应用不受保护", not S._uwp_protected("Microsoft.BingWeather"))
chk("保护名单 ≥ 15 条（%d）" % len(S.UWP_PROTECTED), len(S.UWP_PROTECTED) >= 15)
uwp = S.list_uwp_apps()
chk("UWP 清单非空（%d 个）" % len(uwp), len(uwp) > 20)
chk("清单字段齐全",
    all(k in uwp[0] for k in ("name", "full", "family", "version", "publisher",
                              "location", "framework", "protected", "title")))
chk("存在受保护项与可卸载项（%d / %d）"
    % (sum(1 for r in uwp if r["protected"]), sum(1 for r in uwp if not r["protected"])),
    any(r["protected"] for r in uwp) and any(not r["protected"] for r in uwp))
chk("完全受保护的包不会被误判为可卸载",
    all(r["protected"] for r in uwp if r["name"].startswith("Microsoft.UI.Xaml")))
chk("空清单卸载返回失败而不是抛异常", S.remove_uwp_apps([])["ok"] is False)
_wp = getattr(win.pages.get("software"), "uwp", None)
chk("预装应用页签挂在「软件管理」下且已实例化", _wp is not None)
chk("导航里不再单列「预装应用」", "apps" not in [k for k, _, _ in win.NAV])
win.goto("software")
pump(7.0)
_sw = win.pages["software"]
ck_tab = (_sw.tabs.count() == 3 and _sw.tabs.tabText(1) == "预装应用"
          and _sw.tabs.tabText(2) == "安装监视")
chk("软件管理页三个页签（已安装软件 / 预装应用 / 安装监视）", ck_tab)
_sw.tabs.setCurrentIndex(1)
pump(11.0)
chk("切到预装应用页签会自动加载（%d 个）" % len(_wp.apps), len(_wp.apps) > 20)
chk("页面表格行数 = 清单数", _wp.tbl.rowCount() == len(_wp.apps))
_locked = sum(1 for r in range(_wp.tbl.rowCount())
              if _wp.tbl.item(r, 0) is not None
              and not (_wp.tbl.item(r, 0).flags() & Qt.ItemIsUserCheckable))
chk("锁定行数与清单一致（%d）" % _locked,
    _locked == sum(1 for a in _wp.apps if a["protected"] or a["framework"]))

print("\n".join(LOG))
print("\n失败：%s" % (FAILS or "无"))
print("总计 %d 项，通过 %d" % (len(LOG), len(LOG) - len(FAILS)))
win.hide()
win.deleteLater()
sys.stdout.flush()
os._exit(1 if FAILS else 0)
