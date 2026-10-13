# -*- coding: utf-8 -*-
"""渲染界面预览图 → `WinToolbox/_preview/<页面>_<浅色|深色>.png`。

为什么需要它：以前预览图是手工截的，改完代码就过期了，很容易「图与代码脱节」。
现在 `build_exe.py` 打包完会自动调一次（`--no-preview` 可跳过）。

要点：
  * 用真实平台 + `WA_DontShowOnScreen`（**不要用 offscreen** —— 那种模式下
    Qt 加载不到系统字体，中文会全变方框，看不出真实排版）；
  * **不写用户配置**：打桩 `settings_save`、不 close 窗口，结尾 `os._exit()`；
  * `_preview/` 是本脚本的专属输出目录，每次运行前会清掉里面的旧 png。

用法：
    python tools/make_previews.py            # 全部页面
    python tools/make_previews.py overview   # 只出指定页面
"""
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
os.environ["WTB_NO_BROWSER"] = "1"

from PySide6.QtCore import Qt                                    # noqa: E402
from PySide6.QtWidgets import QApplication                       # noqa: E402

import app as A                                                  # noqa: E402

# (页面 key, 文件名里用的中文名, 页签序号)。页签序号 > 0 表示切到该页签再截。
PAGES = (("overview", "概览", 0),
         ("optimize", "系统优化", 0),
         ("cputune", "CPU调试", 0),
         ("power", "电源管理", 0),
         ("startup", "启动与服务", 0),
         ("software", "软件管理", 0),
         ("software", "预装应用", 1),
         ("software", "安装监视", 2),
         ("tools", "磁盘工具", 0),
         ("tools", "重复文件查找", 1),
         ("tools", "磁盘占用扫描", 3),
         ("hub", "清理与诊断", 0),
         ("regtool", "注册表清理", 0),
         ("regtool", "备份与还原", 2),
         ("systool", "系统组件", 0),
         ("settings", "设置", 0))

OUT_DIR = os.path.join(ROOT, "_preview")
# 每个画面渲染前的等待时间（秒）：要够异步读取数据回来，否则截到的是"—"
WAIT = {"概览": 7.0, "系统优化": 6.0, "CPU调试": 6.0, "电源管理": 5.0,
        "启动与服务": 6.0, "软件管理": 6.0, "预装应用": 10.0,
        "设置": 3.0, "备份与还原": 3.0,
        # 新页没有 WAIT 会截到「读取中…」；hub 是静态页，1s 够
        "清理与诊断": 1.0}


def pump(qapp, sec):
    t0 = time.time()
    while time.time() - t0 < sec:
        qapp.processEvents()
        time.sleep(0.03)


def _expected_names():
    """本次会重新生成的文件名集合（PAGES × 深浅 + 服务详情弹窗）。"""
    names = set()
    for _key, name, _tab in PAGES:
        names.add("%s_浅色.png" % name)
        names.add("%s_深色.png" % name)
    names.add("服务详情弹窗_深色.png")
    return names


# 手工 / 专项截图（由 tools/shot_*.py 生成）：**不参与自动重画**，清理时保留。
# 前缀匹配；以后新增这类图，把前缀加进来即可（否则下次刷新会被当陈旧图删掉）。
KEEP_PREFIXES = ("显卡页_重启显卡按钮", "电源页_", "设置页_", "注册表清理_不限",
                 "磁盘占用", "高级启动", "读取失败_")


def clean_old():
    """删掉**陈旧**的预览图，返回删除数。

    ⚠️ 以前是「先把 _preview 下所有 .png 全删掉，再重画」—— 一次删 50 张会撞上
    沙箱的批量删除保护，脚本**直接中断**，结果一张都没重画（README 里写的
    「预览图随打包自动刷新」就是这么失效的）。现在只删两类之外的：
      · 本次会重新生成的（expected）；
      · 手工/专项截图（KEEP_PREFIXES）。
    正常情况下删 0～2 张，远低于批量阈值，脚本不会再被打断。
    """
    if not os.path.isdir(OUT_DIR):
        return 0
    keep = _expected_names()
    n = 0
    for f in os.listdir(OUT_DIR):
        if not f.lower().endswith(".png"):
            continue
        if f in keep or any(f.startswith(p) for p in KEEP_PREFIXES):
            continue
        try:
            os.remove(os.path.join(OUT_DIR, f))
            n += 1
        except OSError:
            pass
    return n


def main():
    keys = [k for k in sys.argv[1:]] if len(sys.argv) > 1 else None
    qapp = QApplication.instance() or QApplication([])
    A.settings_save = lambda *a, **k: None       # 绝不写 settings.json
    os.makedirs(OUT_DIR, exist_ok=True)
    if keys is None:
        print("清理旧预览图 %d 张" % clean_old())

    shots = [p for p in PAGES if keys is None or p[0] in keys]
    sysinfo = A.probe_system_info(qapp)
    made = 0
    dialog_done = False
    for dark in (False, True):
        w = A.MainWindow(settings={"close_action": "ask", "check_update": False,
                                   "accent": "#2B8CFF"},
                         admin_mode=True, dark=dark, sysinfo=sysinfo)
        w.resize(1180, 780)
        w.setAttribute(Qt.WA_DontShowOnScreen, True)
        w.show()
        pump(qapp, 2.5)
        for key, name, tab in shots:
            w.goto(key)
            pump(qapp, WAIT.get(name, 5.0))
            if tab:
                page = w.pages.get(key)
                t = getattr(page, "tabs", None)
                if t is not None and tab < t.count():
                    t.setCurrentIndex(tab)
                    pump(qapp, WAIT.get(name, 5.0))
            path = os.path.join(OUT_DIR, "%s_%s.png" % (name, "深色" if dark else "浅色"))
            w.grab().save(path)
            made += 1
            print("  %s" % os.path.basename(path))
        # 「服务详情」弹窗只在深色那轮拍一张（它不是页面，是模态弹窗）
        if dark and not dialog_done:
            dlg = A.ServiceDialog(w, "Spooler")
            dlg.setAttribute(Qt.WA_DontShowOnScreen, True)
            dlg.show()
            pump(qapp, 0.8)
            p = os.path.join(OUT_DIR, "服务详情弹窗_深色.png")
            dlg.grab().save(p)
            made += 1
            print("  %s" % os.path.basename(p))
            dlg.reject()
            dialog_done = True
        w.hide()
        w.deleteLater()
        pump(qapp, 0.4)
    print("预览图完成：%d 张 → %s" % (made, OUT_DIR))
    sys.stdout.flush()
    os._exit(0)


if __name__ == "__main__":
    main()
