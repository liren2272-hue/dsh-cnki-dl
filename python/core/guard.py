# -*- coding: utf-8 -*-
"""知网访问守卫 —— 本机是「能自动破验证码」的批量下载器，守卫只做**节奏 + 冷却**，
不做硬性请求配额（配额会跟我们自己的 auto_pass 批量设计打架）。

与社区 cnki-assist 的差异（关键）：
* 社区无验证码破解、走人工导出兜底 → 靠 `12 次/10min` 配额硬拦下载端点。
* 本机 `fetch.grab(attempts=3)` 会自动过验证码并重试整条链路 → 真正要防的是
  「在风控点上连续撞」，不是「下得多」。所以：
  - 保留 **最小间隔** `min_interval_sec`（每篇下载之间匀速，别密集触发）。
  - 命中验证码走 `note_captcha()` → **退避冷却** `cooldown_sec`，且随连续失败
    指数退避（60s → 120s → 240s 封顶），`note_ok()` 成功后复位。
  - **去掉请求配额**：不限制单位时间下载篇数，只保证节奏与冷却。

两条正则（host/download 门槛、最多 3 轮百分号解码防绕过、`[\\/]` 双斜杠、`i`
旗标）仍照搬社区，供 `check_download()` 在工具面/独立脚本里判「这是不是知网
下载端点」。纯内存状态，进程重启清零。
"""
import re
import time
import urllib.parse

MIN_INTERVAL_SEC = 4.0      # 每篇下载之间最小间隔（匀速，别密集触发风控）
COOLDOWN_BASE_SEC = 60.0    # 首次验证码退避
COOLDOWN_MAX_SEC = 240.0    # 连续失败退避封顶

CNKI_HOST_RE = re.compile(r"cnki\.(?:net|com\.cn)|\.cnki\.", re.I)
CNKI_DOWNLOAD_RE = re.compile(
    r"kcms2?[/\\]article[/\\]download|kcms[/\\]download|bar[/\\]download"
    r"|download\.aspx|kns8s?[/\\]download|[/\\]download\?|getDocumentPage"
    r"|filename=[^&\s\"']*\.(?:caj|pdf|nh)(?:[&\s\"']|$)", re.I)


def normalize(s):
    """最多 3 轮百分号解码，容忍非法序列（照搬社区 normalize）。"""
    out = s
    for _ in range(3):
        try:
            nxt = urllib.parse.unquote(out)
        except Exception:
            break
        if nxt == out:
            break
        out = nxt
    return out


class CnkiGuard:
    def __init__(self, min_interval_sec=MIN_INTERVAL_SEC,
                 cooldown_base_sec=COOLDOWN_BASE_SEC,
                 cooldown_max_sec=COOLDOWN_MAX_SEC, now=None):
        self.min_interval_sec = min_interval_sec
        self.cooldown_base_sec = cooldown_base_sec
        self.cooldown_max_sec = cooldown_max_sec
        self._now = now or time.time
        self.last = float("-inf")
        self.cooldown_until = 0.0
        self.captcha_streak = 0   # 连续验证码失败次数（成功复位）

    # —— 状态推进 ——
    def note_captcha(self):
        """验证码未通过 → 指数退避冷却，别在风控点上继续撞。"""
        self.captcha_streak += 1
        cd = min(self.cooldown_base_sec * (2 ** (self.captcha_streak - 1)),
                 self.cooldown_max_sec)
        self.cooldown_until = self._now() + cd
        return cd

    def note_ok(self):
        """一次成功下载 → 复位退避。"""
        self.captcha_streak = 0

    def note_download(self):
        """记一笔「刚发起过一次下载」，推进最小间隔基准。"""
        self.last = self._now()

    # —— 查询 ——
    def _hit_host(self, url):
        s = url or ""
        d = normalize(s)
        return bool(CNKI_HOST_RE.search(s) or CNKI_HOST_RE.search(d))

    def is_download(self, url):
        s = url or ""
        d = normalize(s)
        return bool(CNKI_DOWNLOAD_RE.search(s) or CNKI_DOWNLOAD_RE.search(d))

    def wait_needed(self):
        """返回距「可以发起下一次下载」还需等待的秒数（<=0 表示现在可以）。"""
        t = self._now()
        gap = self.last + self.min_interval_sec - t
        cd = self.cooldown_until - t
        return max(gap, cd, 0.0)

    def reason(self):
        t = self._now()
        if t < self.cooldown_until:
            return "知网风控冷却中（验证码后还剩 %.0f 秒）" % (self.cooldown_until - t)
        if t - self.last < self.min_interval_sec:
            return "距上次下载不足 %.0f 秒间隔" % self.min_interval_sec
        return ""

    # —— 工具面闸门（供独立脚本/工具调用判下载端点）——
    def check_download(self, url=""):
        """下载类知网 URL 一律 deny 且不占任何额度（照搬社区语义）。"""
        if not self._hit_host(url):
            return True, ""
        if self.is_download(url):
            return False, "download-block"
        if self.wait_needed() > 0:
            return False, self.reason()
        self.last = self._now()
        return True, ""
