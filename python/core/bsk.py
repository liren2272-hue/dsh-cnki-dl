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
         "could not be attributed", "observation changed",
         "needs an explicit current observation",   # strict/target 配错或 obs 未传
         "observed content changed", "still settling",
         "did not complete before timeout")         # download 抖动，可重发


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


# ---- 控制策略：bsk 在会话的**第一次** cooperate sync 上绑定策略 ----
# 实测（bsk 0.4.1）：不带 --policy 的首次 sync 会绑成 target，而 target 会话上
# evaluate 一律被拒「this action needs an explicit current observation」，即使
# 把 sync 返回的 observation_id 用 --observation 传回去也一样被拒。evaluate 是
# 脚本动作，只有 strict 会话才放行 → 插件原先所有取数、验证码识别全部失效。
# strict 会话的规律：首次 kind=baseline 的 sync 带 observation_id，之后
# kind=unchanged 的 sync **不再返回 observation_id**，此时只带 --context 即可。
# 于是规则：每个会话的首次 sync 固定 --policy strict，obs 有就带、没有就不带。
POLICY = os.environ.get("CNKI_BSK_POLICY", "strict")
_BOUND = {}   # sid -> bool：该会话的控制策略是否已经绑过


def _grab(pat, text):
    m = re.search(pat, text or "")
    return m.group(1) if m else None


def sync_pair(sid, wait_ms=5000, tries=2, log=None):
    """一次 cooperate sync，返回 (context_id, observation_id 或 None)。

    会话第一次 sync 时带上 --policy（默认 strict）绑定控制策略；若 bsk 说策略
    已固定（会话不是我们起的），去掉 --policy 重试并把该会话记为已绑定。
    """
    last = ""
    for _ in range(tries):
        run(["cooperate", "wait", "--session", sid,
             "--timeout-ms", str(wait_ms)], 25)
        args = ["cooperate", "sync", "--session", sid, "--json"]
        if POLICY and not _BOUND.get(sid):
            args += ["--policy", POLICY]
        out = run(args, 40)
        last = out or ""
        c = _grab(r'"context_id":\s*"([^"]+)"', last)
        if c:
            _BOUND[sid] = True
            return c, _grab(r'"observation_id":\s*"([^"]+)"', last)
        if "fixed for this session" in last or "invalid_params" in last:
            _BOUND[sid] = True
            if log:
                log("  [bsk] 会话 %s 策略已固定，改用其既有策略" % sid)
        if log:
            log("  [bsk] sync 未取得 context: %s" % last[:120].replace("\n", " "))
        time.sleep(1.0)
    return None, None


def act_args(sid, wait_ms=5000):
    """给动作命令拼上 --context（以及存在时的 --observation）。"""
    c, o = sync_pair(sid, wait_ms)
    if not c:
        return None
    a = ["--context", c]
    if o:
        a += ["--observation", o]
    return a


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
        """兼容旧调用点：只回 context（新代码请用 ctx_pair）。"""
        return sync_pair(self.sid, log=self.log)[0]

    def ctx_pair(self):
        """(context_id, observation_id|None) —— evaluate/drag 需要后者才能过权限。"""
        return sync_pair(self.sid, log=self.log)

    def ev(self, js, tries=5, timeout=90):
        for i in range(tries):
            a = act_args(self.sid)
            if not a:
                time.sleep(1.2)
                continue
            out = run(["evaluate", "--session", self.sid] + a + [js], timeout)
            if ok(out):
                return out
            if "USER_CANCELED" in out:      # 下载被劫持，evaluate 也可能报
                return out
            time.sleep(1.5)
        return ""

    def nav(self, url, tries=4, wait="domcontentloaded", timeout="45s"):
        out = ""
        for _ in range(tries):
            a = act_args(self.sid) or []
            args = ["navigate", "--session", self.sid] + a + \
                   ["--wait-until", wait, "--timeout", timeout, url]
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
            a = act_args(self.sid)
            if not a:
                time.sleep(1.5)
                continue
            args = ["download", "--session", self.sid] + a + [
                    "--depends-on", depends_on or selector, "--selector", selector,
                    "--out", out_path, "--timeout", timeout, "--overwrite"]
            out = run(args, timeout_secs(timeout) + 60)
            if not any(k in out for k in TRANS):
                return out
            if os.path.exists(out_path):      # 报了抖动但文件已落盘
                return out
            time.sleep(2.5)
        return out

    def drag(self, selector, dx, duration="900ms", timeout="3s"):
        """滑块拖拽。evaluate 在 strict 会话需要 observation，drag 同理。"""
        for _ in range(3):
            a = act_args(self.sid)
            if not a:
                time.sleep(1.0)
                continue
            out = run(["drag", "--session", self.sid] + a + [
                       "--depends-on", selector, "--timeout", timeout,
                       "--duration", duration, "--dx", "%.2f" % dx, "--dy", "0",
                       selector], 90)
            if not any(k in out for k in TRANS):
                return out
            time.sleep(1.0)
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
