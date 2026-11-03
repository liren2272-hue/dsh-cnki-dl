# -*- coding: utf-8 -*-
"""bsk（BrowserSkill CLI）薄封装：会话、context、evaluate、导航、下载、拖拽。

设计要点
--------
* bsk 一次动作经常报 `shared page control or observation changed`——这是**页面侧**
  的协作观测抖动，不是动作失败。所有动作都带重试：重拿 context 再重发。
* 每次动作前重新 `cooperate sync` 取 context，是踩坑最多的一条（skill 里记过）。
* Chrome 下载会被下载器扩展劫持，这里只把 bsk 的输出当**线索**，真正判定以
  落盘文件的魔数为准（见 verify.py）。
"""
import os
import re
import shutil
import subprocess
import time

TRANS = ("shared page control", "requires the context", "did not become ready",
         "could not be attributed", "observation changed")


def _find_bsk():
    cands = [os.environ.get("BSK_BIN", ""),
             r"C:/Users/15940/.local/bin/bsk.exe",
             os.path.expanduser("~/.local/bin/bsk.exe")]
    for c in cands:
        if c and os.path.exists(c):
            return c
    return shutil.which("bsk") or "bsk"


B = _find_bsk()
ENV = dict(os.environ, BSK_AUTO_START="0", PYTHONIOENCODING="utf-8")


def run(args, timeout=120):
    """跑一条 bsk 命令，返回 stdout+stderr 文本。"""
    try:
        r = subprocess.run([B] + list(args), capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout, env=ENV)
        return ((r.stdout or "") + (r.stderr or "")).strip()
    except Exception as ex:  # 超时 / 找不到可执行文件 / 编码异常
        return "EXC %s" % ex


def ok(out):
    """bsk 输出里没有协作抖动错误、且非空，就当作一次有效返回。"""
    return bool(out) and not any(k in out for k in TRANS)


def status():
    return run(["status", "--json"], 30)


def browsers_ready():
    s = status()
    return '"browser_name"' in s and '"unresponsive": false' in s


class Session:
    """一个 bsk 会话。`--context` 每次现取，避免陈旧 context 报错。"""

    def __init__(self, sid=None, name="cnki", log=print, fresh=True):
        self.log = log
        self.sid = sid or (self.start(name) if fresh else None)

    # ---------- 基础 ----------
    def start(self, name="cnki"):
        out = run(["session", "start", "--name", name], 60)
        m = re.search(r'"session_id":\s*"([^"]+)"', out) or re.search(r"\b([a-z0-9]{4})\b\s*$", out)
        if not m:
            tail = out.strip().splitlines()[-1] if out.strip() else ""
            self.sid = tail.split()[-1]
        else:
            self.sid = m.group(1)
        return self.sid

    def stop(self):
        if self.sid:
            run(["session", "stop", self.sid], 30)

    def ctx(self):
        run(["cooperate", "wait", "--session", self.sid, "--timeout-ms", "5000"], 25)
        out = run(["cooperate", "sync", "--session", self.sid, "--json"], 40)
        m = re.search(r'"context_id":\s*"([^"]+)"', out)
        return m.group(1) if m else None

    def ev(self, js, tries=5, timeout=90):
        for i in range(tries):
            c = self.ctx()
            if not c:
                time.sleep(1.2)
                continue
            out = run(["evaluate", "--session", self.sid, "--context", c, js], timeout)
            if ok(out):
                return out
            if "USER_CANCELED" in out:      # 下载被劫持，evaluate 也可能报
                return out
            time.sleep(1.5)
        return ""

    def nav(self, url, tries=4, wait="domcontentloaded", timeout="45s"):
        for _ in range(tries):
            c = self.ctx()
            args = ["navigate", "--session", self.sid]
            if c:
                args += ["--context", c]
            args += ["--wait-until", wait, "--timeout", timeout, url]
            out = run(args, 100)
            if ok(out) or "error" not in (out or "").lower():
                return out
            time.sleep(2)
        return out

    # ---------- 动作 ----------
    def download(self, selector, out_path, depends_on=None, timeout="150s", tries=4):
        """bsk download 常报 shared page control；换 context 重试同一目标即可。"""
        out = ""
        for i in range(tries):
            c = self.ctx()
            if not c:
                time.sleep(1.5)
                continue
            args = ["download", "--session", self.sid, "--context", c,
                    "--depends-on", depends_on or selector, "--selector", selector,
                    "--out", out_path, "--timeout", timeout, "--overwrite"]
            out = run(args, timeout_secs(timeout) + 60)
            if not any(k in out for k in TRANS):
                return out
            time.sleep(2.5)
        return out

    def drag(self, selector, dx, duration="900ms", timeout="3s"):
        c = self.ctx()
        out = run(["drag", "--session", self.sid, "--context", c or "",
                   "--depends-on", selector, "--timeout", timeout,
                   "--duration", duration, "--dx", "%.2f" % dx, "--dy", "0",
                   selector], 90)
        return out

    def tabs(self):
        return run(["tab", "list", "--session", self.sid], 30)

    def fresh_tab(self, url="https://kns.cnki.net/kns8s/index"):
        """在 agent 窗口新开一个标签（并切过去），返回 tab_id。

        bar 的下载/验证页会把标签页「焊」在 bar 域上（之后的 navigate 会被
        拽回来），批量下载时每篇换个干净标签最稳。

        ⚠ 一定要给真实 http(s) URL：about:blank 里没有扩展 content script，
        evaluate/navigate 全部会失败（报 Receiving end does not exist）。
        """
        out = run(["tab", "create", "--session", self.sid, "--url", url, "--json"], 60)
        m = re.search(r'"tab_id":\s*(\d+)', out or "")
        return int(m.group(1)) if m else None

    def close_tab(self, tab_id):
        if not tab_id:
            return False
        out = run(["tab", "close", "--session", self.sid, str(tab_id)], 30)
        return "error" not in (out or "").lower()

    def front(self, tab_id=None):
        tid = tab_id
        if not tid:
            for line in self.tabs().splitlines():
                m = re.match(r"\s*(\d{6,})", line)
                if m:
                    tid = m.group(1)
                    break
        if tid:
            return run(["tab", "select", "--session", self.sid, tid], 20)
        return ""


def timeout_secs(spec):
    m = re.match(r"(\d+)s?$", str(spec).strip())
    return int(m.group(1)) if m else 180


def js_call(sess, js):
    """evaluate 一段 JS，返回其中的 JSON 对象；失败返回 None。"""
    import json
    raw = sess.ev(js)
    if not raw:
        return None
    m = re.search(r"\{.*\}", raw, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except Exception:
        return None


def drop_dialogs(sess):
    out = run(["dialog", "status", "--session", sess.sid, "--json"], 30)
    for i in re.findall(r'"id":\s*"([^"]+)"', out or ""):
        run(["dialog", "dismiss", "--session", sess.sid, i], 30)
