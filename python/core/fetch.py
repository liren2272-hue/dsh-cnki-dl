# -*- coding: utf-8 -*-
"""单篇抓取：详情页 → PDF 入口 → trusted 点击 → bar 拼图 → 落盘魔数验收。

判定顺序（顺序很重要）
1. 打开详情页；若落在 /verify/ 或标题含「安全验证」→ `captcha.auto_pass`。
2. 读下载入口。**学位论文没有 a#pdfDown**，PDF 入口是 `li.btn-dlpdf a`
   （它的 id 也叫 cajDown，和 CAJ 重名，所以只能用父 li 限定）。
3. **必须 trusted 点击下载按钮**：把锚点 target 改成 _self 后用 bsk click，
   本标签页会跳到 bar.cnki.net/bar/verify 拼图页。
   直接读 href 再 nav 过去是不行的 —— bar 那边没有登记订单，会弹
   「您操作已超时，请重新打开文献！」/「来源应用不正确(01)」。
4. 拼图用 captcha.auto_pass 解；过了会跳 verifySuccess → 订单 URL →
   Chrome 走自己的下载器落到默认下载目录（带 Content-Disposition 的中文名）。
5. 落盘文件一律按魔数判定：`%PDF` 才算 PDF，`CAJ/KDH/HN/KDF` 分别标注，
   绝不把私有格式改名成 .pdf 充数。
"""
import json
import os
import re
import time

from . import bsk
from . import pages
from . import captcha

PDF_MIN = 20000          # 生产阈值：真 PDF 一般远大于 20KB
MAGIC = {b"%PDF": "pdf", b"CAJ": "caj", b"KDH": "kdh", b"HN": "hn",
         b"NH": "hn", b"KDF": "kdf"}
DL_EXT = (".pdf", ".caj", ".nh", ".kdh")


def probe(path):
    """返回 (kind, size, head)。kind ∈ pdf/caj/kdh/hn/empty/other。"""
    if not os.path.exists(path):
        return "missing", 0, b""
    size = os.path.getsize(path)
    if size == 0:
        return "empty", 0, b""
    with open(path, "rb") as f:
        head = f.read(8)
        if size > 9000:
            f.seek(max(0, size - 8192))
            tail = f.read()
        else:
            tail = b""
    if head[:5] == b"%PDF-" or head[:4] == b"%PDF":
        eof = b"%%EOF" in tail
        return ("pdf" if eof else "pdf_noeof"), size, head
    for m, k in MAGIC.items():
        if head[:len(m)] == m:
            return k, size, head
    return "other", size, head


def safe_name(s):
    s = re.sub(r'[\\/:*?"<>|\r\n\t]', "_", (s or "").strip())
    s = re.sub(r"\s+", " ", s).strip(" .")
    return s[:120] or "untitled"


# ---------------- Chrome 下载目录 ----------------

def chrome_dl_dir():
    """Chrome 默认下载目录：优先读用户配置，读不到再猜。"""
    la = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    ud = os.path.join(la, "Google", "Chrome", "User Data")
    cands = [os.path.join(ud, "Default", "Preferences")]
    try:
        for n in os.listdir(ud):
            if n.startswith("Profile "):
                cands.append(os.path.join(ud, n, "Preferences"))
    except OSError:
        pass
    for p in cands:
        try:
            with open(p, "r", encoding="utf-8", errors="replace") as f:
                j = json.load(f)
            d = ((j.get("download") or {}).get("default_directory") or "").strip()
            if d and os.path.isdir(d):
                return d
        except Exception:
            continue
    for d in (os.path.join(os.path.expanduser("~"), "Downloads"),):
        if os.path.isdir(d):
            return d
    return os.path.expanduser("~")


def _snap(d):
    try:
        return set(os.listdir(d))
    except OSError:
        return set()


def _wait_new_file(d, before, maxwait=150, log=print):
    """等 Chrome 把新下载写完：目录里出现新文件且没有 .crdownload 残留、大小稳定。"""
    t0 = time.time()
    lastseen = None
    stable = 0
    while time.time() - t0 < maxwait:
        time.sleep(1.2)
        now = _snap(d)
        new = [n for n in (now - before)
               if n.lower().endswith(DL_EXT) or n.endswith(".crdownload")]
        busy = [n for n in now if n.endswith(".crdownload")]
        if busy:
            stable = 0
            lastseen = busy[0]
            continue
        done = [n for n in (now - before) if n.lower().endswith(DL_EXT)]
        if not done:
            continue
        f = os.path.join(d, sorted(done, key=lambda n: os.path.getmtime(os.path.join(d, n)))[-1])
        sig = (f, os.path.getsize(f))
        if sig == lastseen:
            stable += 1
            if stable >= 1:
                return f
        else:
            lastseen = sig
            stable = 0
    if lastseen and isinstance(lastseen, tuple):
        log("    ! 下载超时，先收下正在写的文件 %s" % os.path.basename(lastseen[0]))
        return lastseen[0]
    return None


# ---------------- 详情页 ----------------

def _same_page(cur, want):
    """两个详情页 URL 是否是同一篇（比对 v= 参数）。"""
    import urllib.parse as _up
    try:
        a = (_up.parse_qs(_up.urlparse(cur).query).get("v") or [""])[0][:40]
        b = (_up.parse_qs(_up.urlparse(want).query).get("v") or [""])[0][:40]
    except Exception:
        return False
    return bool(a and b and a == b)


def _wait_nav(sess, url, maxwait=15, log=print):
    """等到 location.href 指向目标详情页（比对 v= 参数）才算导航完成。"""
    import urllib.parse as _up
    try:
        q = _up.parse_qs(_up.urlparse(url).query)
    except Exception:
        return True
    want = (q.get("v") or [""])[0][:40]
    for _ in range(int(maxwait / 1.5)):
        st = bsk.js_call(sess, "(function(){return JSON.stringify({u:location.href});})()") or {}
        u = st.get("u") or ""
        if want and want in u:
            return True
        if not want and "abstract" in u:
            return True
        time.sleep(1.5)
    log("    ! 导航未确认到目标页（继续按当前页面处理）")
    return False


def open_detail(sess, url, log=print, tries=2):
    """导航到详情页，必要时过验证码。返回 (ok, detail_dict)。

    注意：文字点选/拼图在 kns 的 /verify/home 页上解完后，**页面自己会跳到
    原本要去的详情页**；这时再 nav(url) 会重新触发一次验证码，形成死循环。
    只有解完还停在验证页时才继续处理。
    """
    for i in range(tries):
        # 已经在新标签里打开到目标页就不用再导航（nav 会重新触发一次验证码）
        cur = bsk.js_call(sess, "(function(){return JSON.stringify({u:location.href});})()") or {}
        if _same_page(cur.get("u") or "", url):
            pass
        else:
            sess.nav(url)
        time.sleep(2.5)
        # 导航可能还没落地（尤其刚从 bar 下载页回来），等地址栏真的变过来再读 DOM，
        # 否则 detail() 会把上一篇的页面当成这一篇（表现为「无下载入口」）。
        _wait_nav(sess, url, log=log)
        st0 = bsk.js_call(sess, "(function(){return JSON.stringify({u:location.href.slice(0,80),t:document.title});})()") or {}
        if "bar.cnki.net" in (st0.get("u") or "") or "拼图" in (st0.get("t") or ""):
            # 上一篇下载完把标签页留在了 bar 验证页，bar 会把后续导航拽回来；
            # 先在当前页把（可能存在的）验证码过掉，再重新导航一次。
            log("    · 标签页停在 bar 验证页，先清一次")
            captcha.auto_pass(sess.sid, rounds=3, log=log)
            time.sleep(1.5)
            sess.nav(url)
            time.sleep(2.5)
            _wait_nav(sess, url, maxwait=10, log=log)
        for _ in range(3):
            isv, info = pages.is_verify(sess)
            if not isv:
                break
            log("    · 命中验证码，尝试自动通过（第 %d 次）" % (i + 1))
            if not captcha.auto_pass(sess.sid, rounds=4):
                log("    · 验证码未通过")
                return False, {"verify": True}
            log("    · 验证码已通过")
            time.sleep(1.2)
            st = bsk.js_call(sess, "(function(){return JSON.stringify({u:location.href.slice(0,120)});})()") or {}
            if "verify" not in (st.get("u") or ""):
                break
        d = pages.detail(sess)
        if d.get("has_pdf") or d.get("has_caj") or d.get("title"):
            return True, d
        time.sleep(2)
    return False, pages.detail(sess)


# ---------------- trusted 点击 → bar 验证 → 下载 ----------------

JS_MARK = ("(function(){var a=document.querySelector(%s);"
           "if(!a)return JSON.stringify({ok:0});"
           "a.setAttribute('data-bsk','dl');"
           "a.setAttribute('target','_self');"      # 别开新标签，否则拿不到控制权
           "return JSON.stringify({ok:1,h:(a.getAttribute('href')||''),"
           "txt:(a.textContent||'').trim().slice(0,10)});})()")


def click_pdf(sess, sel, log=print, tries=4):
    """标记按钮并 trusted 点击。成功时本标签页已在 bar 拼图页。

    返回 (ok, 说明)。bsk 的「shared page control or observation changed」
    是常态（页面有轮播/广告），重试即可。
    """
    js = JS_MARK % json.dumps(sel)
    m = {}
    for attempt in range(6):          # 验证页跳详情页时有二次重定向，DOM 会瞬变；
        m = bsk.js_call(sess, js) or {}   # 按钮有时是滚动到可视区才渲染
        if m.get("ok"):
            break
        if attempt in (1, 3):
            bsk.js_call(sess, "(function(){window.scrollTo(0,document.body.scrollHeight/2);"
                              "window.dispatchEvent(new Event('scroll'));return '{}';})()")
        time.sleep(1.5)
    if not m.get("ok"):
        st = bsk.js_call(sess, "(function(){return JSON.stringify({u:location.href.slice(0,90),"
                               "t:document.title,n:document.querySelectorAll(%s).length});})()"
                         % json.dumps(sel)) or {}
        return False, "没找到下载按钮 %s @%s n=%s" % (sel, (st.get("u") or "")[:60], st.get("n")), ""
    for i in range(1, tries + 1):
        c = sess.ctx()
        out = bsk.run(["click", "--session", sess.sid, "--context", c or "",
                       "a[data-bsk='dl']"], 90)
        if not any(t in (out or "") for t in bsk.TRANS):
            return True, (out or "").splitlines()[0][:80] if out else "", m.get("h") or ""
        time.sleep(0.7)
    return False, "点击一直被页面变化打断", ""


# download-first-v2 / 2026-11-06: skip the second (stale-order) bar verification
def fetch_via_click(sess, sel, out_dir, dst, log=print, maxwait=150):
    """trusted 点击 → 收 Chrome 下载的文件 → 移到 dst。

    返回 (status, path, note)。status ∈ ok / verify-fail / no-jump / no-file

    CNKI 期刊页的下载按钮即便被设了 target=_self，站点 JS 仍常把 bar 验证页
    开成「新标签」；那个新标签会自己登记订单，并让 Chrome 把 PDF 原生下载到
    ddir。受控的这一页若再 navigate 回同一个 order URL，bar 会认为订单已被
    消费，弹「您操作已超时，请重新打开文献！」并出一道在受控页解不了的拼图，
    每篇白白烧掉几十秒。所以改成「文件优先」：先盯下载目录，新标签下好就直接
    收、完全跳过二次验证；只有确认没有原生下载时才退回老路径（学位论文页、
    或受控标签真的停在 bar 验证页需要过拼图）。
    """
    ddir = chrome_dl_dir()
    log("    · 下载目录 %s" % ddir)
    before = _snap(ddir)
    ok, why, order = click_pdf(sess, sel, log=log)
    if not ok:
        return "no-jump", "", why

    def _peek():
        now = _snap(ddir)
        new = now - before
        if any(n.endswith(".crdownload") for n in new):
            return None                      # 还在写，等下一轮
        done = [n for n in new if n.lower().endswith(DL_EXT)]
        if not done:
            return None
        p = os.path.join(ddir, sorted(
            done, key=lambda n: os.path.getmtime(os.path.join(ddir, n)))[-1])
        try:
            sz = os.path.getsize(p)
        except OSError:
            return None
        return (p, sz) if sz >= PDF_MIN else None

    f = None
    jumped = False
    seen = None
    # 观察窗：~30s。每轮「先查文件、再看跳转」，文件优先——新标签一旦落盘就
    # 收下并跳过二次验证（也就不会再有「操作已超时」弹窗与无解拼图）。
    # 不用固定 10s 是因为大文件写 .crdownload 可能超过 10s，提前退出会误判。
    for _ in range(30):
        st = bsk.js_call(sess, "(function(){return JSON.stringify({u:location.href.slice(0,120)});})()") or {}
        u = st.get("u") or ""
        if "bar.cnki.net" in u:
            jumped = True
        cand = _peek()
        if cand and cand == seen:            # 大小稳定 ≥1s，认为下载完成
            f = cand[0]
            break
        seen = cand
        time.sleep(1.0)

    if f:
        log("    · 新标签已原生下载，跳过二次验证（不再触发「操作已超时」弹窗）")
        bsk.drop_dialogs(sess)
    elif jumped:
        # 受控标签停在 bar 验证页：正常过拼图再等文件。
        time.sleep(0.8)
        bsk.drop_dialogs(sess)
        passed = captcha.auto_pass(sess.sid, rounds=6, log=log)
        f = _wait_new_file(ddir, before, maxwait=(maxwait if passed else 8), log=log)
        if not passed and not f:
            return "verify-fail", "", "拼图验证码未通过"
    else:
        # 既没跳 bar、也没等到文件：学位论文页的按钮由 JS 处理，订单已登记，
        # 但新标签没触发下载。退回老路径——用同一订单 URL 在本标签打开验证页。
        if not order:
            return "no-jump", "", "点击后没跳到 bar，且取不到订单 URL"
        import urllib.parse as _up
        vurl = ("https://bar.cnki.net/bar/verify/index.html?platform=nxgp&returnUrl="
                + _up.quote(order, safe=""))
        log("    · 按钮开在新标签且未下载，改用订单 URL 在本标签验证")
        sess.nav(vurl)
        time.sleep(3.0)
        st = bsk.js_call(sess, "(function(){return JSON.stringify({u:location.href.slice(0,120)});})()") or {}
        if "bar.cnki.net" not in (st.get("u") or ""):
            return "no-jump", "", "订单 URL 也没打开验证页"
        time.sleep(0.8)
        bsk.drop_dialogs(sess)
        captcha.auto_pass(sess.sid, rounds=6, log=log)
        f = _wait_new_file(ddir, before, maxwait=maxwait, log=log)

    if not f:
        f = _wait_new_file(ddir, before, maxwait=maxwait, log=log)
    if not f:
        return "no-file", "", "验证通过但没等到下载文件"

    kind, size, head = probe(f)
    target = dst
    if kind in ("caj", "kdh", "hn", "kdf"):
        target = dst[:-4] + "." + kind if dst.lower().endswith(".pdf") else dst + "." + kind
    try:
        os.makedirs(os.path.dirname(target), exist_ok=True)
        os.replace(f, target)
    except OSError as ex:
        log("    ! 移动文件失败(%s)，留在下载目录" % ex)
        target = f
    kind, size, head = probe(target)
    note = ""
    if kind == "pdf_noeof":
        note = "缺 %%EOF，可能被截断"
    log("    · 落盘 %s  %.2fMB  %s" % (os.path.basename(target), size / 1048576.0, kind))
    try:                       # 离开 bar 页，避免影响下一篇
        sess.nav("https://kns.cnki.net/kns8s/index")
        time.sleep(1.2)
    except Exception:
        pass
    return ("ok" if kind in ("pdf", "pdf_noeof") else "ok-other"), target, note


# ---------------- 对外入口 ----------------

def grab(sess, item, out_dir, log=print, want="pdf", attempts=3):
    """下载一条检索结果。item 需要 title / href。

    返回 dict：status ∈ ok / caj-only / no-entry / captcha-fail / no-jump / fail
    """
    title = item.get("title") or ""
    href = item.get("href") or ""
    fname = safe_name(("%02d_%s" % (item.get("idx", 0), title)) if item.get("idx") else title)
    dst = os.path.join(out_dir, fname + ".pdf")
    res = {"title": title, "href": href, "kind": item.get("kind", ""),
           "dl": item.get("dl"), "status": "fail", "file": "", "bytes": 0,
           "format": "", "note": ""}

    for a in range(1, attempts + 1):
        oknav, d = open_detail(sess, href, log=log)
        if d.get("verify"):
            return dict(res, status="captcha-fail", note="详情页验证码未通过")
        if not oknav:
            res["note"] = "详情页打不开"
            continue

        real_title = d.get("title") or title
        if not d.get("has_pdf") and not d.get("has_caj"):
            time.sleep(2)
            d = pages.detail(sess)
        if d.get("has_pdf"):
            sel = pages.SEL_PDF
        elif d.get("has_caj"):
            if want == "pdf":
                return dict(res, status="caj-only", title=real_title,
                            note="仅提供 CAJ，无 PDF 入口")
            sel = pages.SEL_CAJ
        else:
            if a == attempts:
                return dict(res, status="no-entry", title=real_title,
                            note="详情页无下载入口")
            continue

        st, path, note = fetch_via_click(sess, sel, out_dir, dst, log=log)
        if st in ("ok", "ok-other"):
            kind, size, _ = probe(path)
            res.update(status="ok", file=path, bytes=size,
                       format="PDF" if kind.startswith("pdf") else kind.upper(),
                       title=real_title, note=note)
            return res
        res["note"] = note
        if st == "verify-fail":
            return dict(res, status="captcha-fail", title=real_title, note=note)
        log("    · 第 %d 次失败：%s" % (a, note))
        time.sleep(1.5)

    return res
