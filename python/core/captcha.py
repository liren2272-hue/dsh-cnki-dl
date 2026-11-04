# -*- coding: utf-8 -*-
"""知网「拼图校验」验证码自动通过（腾讯 TCaptcha dy 版 · 滑块拼图）。

几何原理
--------
* 图区 `.tencent-captcha-dy__image-area`（CSS 330x191.5），背景图 672x390
  （scale = 672/330 = 2.036）。
* 拼图块元素 `.tencent-captcha-dy__fg-item` 固定 58.93 CSS，`style.left` 是它的
  CSS 偏移；但元素里的 **可见块** 只是 fg 背景雪碧图的一部分，所以要按
  `background-size/position` 把雪碧图里的块位置换算成元素内内缩 inset。
* 拖拽与拼图块 1:1 移动（已用 MutationObserver 实测 +131.0 对 dx 131.1）。
* 于是 `dx = 缺口左缘(bg px)/scale - (fg.style.left + inset.x)`。

用法
----
    import captcha_solver as C
    C.pass_captcha(sid)                 # 当前页是验证码页就自动过，返回 True/False
    python -u src/captcha_solver.py --test <SID>   # 直接在当前页实测一遍
"""
import json, os, re, subprocess, sys, time, urllib.request

import cv2
import numpy as np

from .bsk import B  # bsk 路径统一解析（BSK_BIN 环境变量优先）
from . import bsk as _bsk  # 控制策略/context 配对统一走 bsk.sync_pair，避免两处漂移

TMP = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "data", "tmp", "captcha")

JS_GEO = r"""(function(){
  var q=function(s){return document.querySelector(s)};
  var R=function(e){var r=e.getBoundingClientRect();return [r.left,r.top,r.width,r.height]};
  var ia=q('.tencent-captcha-dy__image-area'), fg=q('.tencent-captcha-dy__fg-item');
  var sb=q('.tencent-captcha-dy__slider-block'), vb=q('.tencent-captcha-dy__verify-bg-img');
  if(!ia||!fg||!sb) return JSON.stringify({no:true,t:document.title,u:location.href.slice(0,120)});
  var cs=getComputedStyle(fg), cb=vb?getComputedStyle(vb):null;
  var bgUrl=cb?(cb.backgroundImage||''):'';
  var fgBg=cs.backgroundImage||'';
  var url=function(s){var m=/url\("?([^")]+)"?\)/.exec(s||'');return m?m[1]:''};
  return JSON.stringify({no:false,t:document.title,u:location.href.slice(0,160),
    ia:R(ia), fg:R(fg), sb:R(sb), fgLeft:parseFloat(fg.style.left)||0,
    bgSize:cs.backgroundSize, bgPos:cs.backgroundPosition,
    vbSize:cb?(cb.backgroundSize||''):'', vbPos:cb?(cb.backgroundPosition||''):'',
    vbr:vb?R(vb):null,
    fgUrl:url(fgBg), vbUrl:url(bgUrl),
    fl:(q('.tencent-captcha-dy__footer-title')||{}).textContent||''});
})()"""

JS_URL = "(function(){return JSON.stringify({u:location.href.slice(0,200),t:document.title,fl:(document.querySelector('.tencent-captcha-dy__footer-title')||{}).textContent||''});})()"

TRANS = _bsk.TRANS


def sh(a, t=120):
    try:
        r = subprocess.run([B] + a, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=t)
        return ((r.stdout or "") + (r.stderr or "")).strip()
    except Exception as ex:
        return "EXC %s" % ex


def ctx_of(sid):
    """兼容旧签名：只回 context。新代码用 _ctx_pair。"""
    return _bsk.sync_pair(sid)[0]


def _ctx_pair(sid):
    """(context_id, observation_id|None) —— strict 会话上 evaluate 必需 obs。"""
    return _bsk.sync_pair(sid)


def _acts(sid):
    return _bsk.act_args(sid)


def ev(sid, js, tries=4):
    for _ in range(tries):
        a = _acts(sid)
        if not a:
            time.sleep(1.0)
            continue
        out = sh(["evaluate", "--session", sid] + a + [js], 90)
        if out and not any(k in out for k in TRANS):
            return out
        time.sleep(1.0)
    return ""


def js_obj(sid, js):
    raw = ev(sid, js)
    m = re.search(r"\{.*\}", raw or "", re.S)
    try:
        return json.loads(m.group(0)) if m else None
    except Exception:
        return None


def drop_dialogs(sid):
    out = sh(["dialog", "status", "--session", sid, "--json"], 30)
    ids = re.findall(r'"id":\s*"([^"]+)"', out or "")
    for i in ids:
        sh(["dialog", "dismiss", "--session", sid, i], 30)
    return ids


# 直连：验证码图片在腾讯 CDN 上，走系统代理会慢 4.6s/张（实测），
# 而验证码有时效限制，必须走直连。
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _dl(url, path, tries=3):
    """腾讯 CDN 偶发 10065（无法连接主机），必须重试；三次仍失败才抛出。"""
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0", "Referer": "https://bar.cnki.net/"})
    last = None
    for i in range(tries):
        try:
            data = _OPENER.open(req, timeout=20).read()
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "wb") as f:
                f.write(data)
            return path
        except Exception as ex:
            last = ex
            time.sleep(1 + i)
    raise last


# ---------------- 缺口检测（形状模板 + 零和核卷积） ----------------

def piece_shape(sprite_png):
    """从 fg 的雪碧图里抠出拼图块剪影：返回 (mask, (x,y,w,h))。"""
    im = cv2.imread(sprite_png, cv2.IMREAD_UNCHANGED)
    if im is None or im.ndim != 3 or im.shape[2] != 4:
        return None, None
    a = im[:, :, 3]
    m = (a > 200).astype(np.uint8) * 255
    n, lab, stats, _ = cv2.connectedComponentsWithStats(m, 8)
    best = None
    for i in range(1, n):
        x, y, w, h, ar = stats[i]
        if w < 40 or h < 40:
            continue
        ratio = w / float(h)
        fill = ar / float(w * h)
        # 雪碧图底部只有四个集成块：轨道灰条、滑块按钮、拼图块；
        # 拼图块形状随题变化（宽高比 0.55~1.9，fill 0.55~0.97），不能写死为 1:1。
        if not (0.5 < ratio < 2.0 and 0.55 < fill < 0.97):
            continue
        if ar < 2500 or ar > 20000:
            continue
        if y < 470 or x < 40:   # 排除轨道灰条(y~422) 与滑块按钮(x=0)
            continue
        if best is None or ar > best[5]:
            best = (lab, x, y, w, h, ar, i)
    if not best:
        return None, None
    lab, x, y, w, h, ar, i = best
    return ((lab[y:y + h, x:x + w] == i).astype(np.uint8) * 255), (x, y, w, h)


def signed_kernel(tpl, margin=14, ring=-0.55):
    ph, pw = tpl.shape[:2]
    body = (tpl > 0).astype(np.float32)
    k = np.zeros((ph + 2 * margin, pw + 2 * margin), np.float32)
    k[margin:margin + ph, margin:margin + pw] = body
    dil = cv2.dilate((body > 0).astype(np.uint8), np.ones((2 * margin + 1, 2 * margin + 1), np.uint8))
    ringm = ((dil > 0) & (body == 0)).astype(np.float32)
    kr = np.zeros_like(k)
    kr[margin:margin + ph, margin:margin + pw] = ringm
    k += ring * kr
    k -= k.mean()
    return k / np.abs(k).sum(), margin


def find_gap(bg_path, tpl, piece_w_px=None, y_hint=None, y_tol=16, ring_margin=14):
    """在背景图里找缺口。

    打分只用「形状 + 暗度」：模板体内的暗度均值减去外环暗度均值，
    与内容无关（实测拼图块不是从背景图裁下来的，内容匹配全是噪声）。
    y_hint（缺口顶边的图像 y）来自拼图块的可见 y —— 缺口与块同高，
    这条约束能干掉绝大多数误检；y_tol 给 16px 容差。
    """
    bg = cv2.imread(bg_path)
    g = cv2.cvtColor(bg, cv2.COLOR_BGR2GRAY).astype(np.float32)
    big = cv2.GaussianBlur(g, (0, 0), 31)
    ratio = np.where(big > 1, g / np.maximum(big, 1), 1.0)
    d = np.clip((1.0 - ratio) / 0.40, 0, 1).astype(np.float32)
    h, w = bg.shape[:2]
    pw = piece_w_px or (58.927 * 2.036) * (w / 672.0)
    ph = tpl.shape[0] * pw / tpl.shape[1]
    pw_i = max(3, int(round(pw)))
    ph_i = max(3, int(round(ph)))
    tm = cv2.resize(tpl, (pw_i, ph_i), interpolation=cv2.INTER_NEAREST)
    body = (tm > 0).astype(np.float32)
    if body.sum() < 20:
        return dict(err="模板为空")
    kk, _mar = signed_kernel(tm, margin=ring_margin)
    sc = cv2.filter2D(d, -1, kk, borderType=cv2.BORDER_REPLICATE)
    sc[:, :pw_i] = -9.0                       # 拼图块自己占着左边
    sc[:, max(0, w - pw_i):] = -9.0
    if y_hint is not None:
        keep = np.zeros(sc.shape, np.float32)  # sc 的行号 = 模板「中心」行
        lo = max(0, int(round(y_hint)) - y_tol + ph_i // 2)
        hi = min(sc.shape[0], int(round(y_hint)) + y_tol + ph_i // 2 + 1)
        keep[lo:hi, :] = 1.0
        sc = np.where(keep > 0, sc, -9.0)
    idx = int(np.argmax(sc))
    cy, cx = divmod(idx, sc.shape[1])
    x0 = max(0, min(cx - pw_i // 2, max(0, w - pw_i)))
    y0 = max(0, min(cy - ph_i // 2, max(0, h - ph_i)))
    M = np.zeros((h, w), np.float32)
    M[y0:y0 + body.shape[0], x0:x0 + body.shape[1]] = body[:min(body.shape[0], h - y0),
                                                           :min(body.shape[1], w - x0)]
    Rimg = (M > 0).astype(np.uint8)
    Rd = cv2.dilate(Rimg, np.ones((2 * ring_margin + 1, 2 * ring_margin + 1), np.uint8)).astype(np.float32)
    ring = np.clip(Rd - M, 0, 1)
    inside = float((d * M).sum() / max(1.0, float(M.sum())))
    ringm = float((d * ring).sum() / max(1.0, float(ring.sum())))
    quality = inside - ringm
    # 二峰歧义：把命中区抹掉再取一次
    sc2 = sc.copy()
    sc2[max(0, cy - ph_i):cy + ph_i, max(0, cx - pw_i):cx + pw_i] = -9.0
    second = float(sc2.max())
    return dict(x=int(x0), y=int(y0), w=int(pw_i), h=int(ph_i),
                score=float(sc.max()), second=second,
                amb=float(second / sc.max()) if sc.max() > 1e-6 else 9.0,
                inside=inside, ring=ringm, quality=quality,
                y_hint=y_hint)


# ---------------- 主流程 ----------------

def _disp_w(vs, vb_w):
    """把 background-size 的第一维换算成「图像显示宽度(CSS)」。"""
    vs = (vs or "").strip().lower()
    m = re.match(r"^([0-9.]+)px", vs)
    if m:
        return float(m.group(1))
    m = re.match(r"^([0-9.]+)%", vs)
    if m:
        return vb_w * float(m.group(1)) / 100.0
    if vs.startswith("cover") or vs.startswith("contain"):
        return vb_w
    return vb_w


def bg_crop(bgp, geo, ia):
    """按 vb 元素的 background-size/position 裁出「图区可见区」。

    返回 (crop_path, k, crop)，其中 **k = CSS px / 背景图 px**，
    crop 左边缘对齐图区左边缘，于是 gap.x * k 即缺口在图区内的 CSS 偏移。
    """
    bg = cv2.imread(bgp)
    if bg is None:
        return None, 1.0, None
    sh_, sw_ = bg.shape[:2]
    vb = geo.get("vbr") or [ia[0], ia[1], ia[2], ia[3]]
    vb_w = float(vb[2]) or float(ia[2])
    disp_w = _disp_w(geo.get("vbSize"), vb_w)
    if disp_w <= 0:
        disp_w = vb_w
    k = disp_w / float(sw_)                     # CSS px per 背景图 px
    try:
        vp = [float(v) for v in re.findall(r"[-0-9.]+", geo.get("vbPos") or "")]
    except Exception:
        vp = []
    px = vp[0] if len(vp) > 0 else 0.0
    py = vp[1] if len(vp) > 1 else 0.0
    # 图区左上角对应的背景图像素
    x0 = int(round((ia[0] - vb[0] - px) / k))
    y0 = int(round((ia[1] - vb[1] - py) / k))
    cw = int(round(ia[2] / k))
    ch = int(round(ia[3] / k))
    if x0 == 0 and y0 == 0 and cw >= sw_ - 2 and ch >= sh_ - 2:
        return bgp, k, bg                   # 整图即图区，直接用
    x0 = max(0, min(x0, max(0, sw_ - 1)))
    y0 = max(0, min(y0, max(0, sh_ - 1)))
    crop = bg[y0:y0 + ch, x0:x0 + cw]
    if crop.size == 0:
        return bgp, k, bg
    cp = os.path.join(TMP, "_bgcrop.png")
    cv2.imwrite(cp, crop)
    return cp, k, crop


def plan(geo, tag="c"):
    """只读：返回 dx 与诊断；不改页面。"""
    os.makedirs(TMP, exist_ok=True)
    if not geo or geo.get("no"):
        return None
    try:
        bgp = _dl(geo["vbUrl"], os.path.join(TMP, tag + "_bg.jpg"))
        sp = _dl(geo["fgUrl"], os.path.join(TMP, tag + "_sprite.png"))
    except Exception as ex:
        # 验证码图床偶发不可达（WinError 10065 等）：这里不能把整批下载带崩，
        # 交回上层按「本次求解失败」处理，它会刷新题库重试。
        return dict(err="图片下载失败: %s" % ex)
    tpl, box = piece_shape(sp)
    if tpl is None:
        return dict(err="sprite 里没找到拼图块")
    box = tuple(int(v) for v in box)
    bg = cv2.imread(bgp)
    ia = geo["ia"]
    try:
        bs = [float(v) for v in re.findall(r"[-0-9.]+", geo["bgSize"])][:2]
        bp = [float(v) for v in re.findall(r"[-0-9.]+", geo["bgPos"])][:2]
    except Exception:
        return dict(err="bgSize/bgPos 解析失败")
    sprite = cv2.imread(sp, cv2.IMREAD_UNCHANGED)
    k = bs[0] / float(sprite.shape[1])          # fg 雪碧图：CSS px / 图像 px
    inset_x = bp[0] + box[0] * k                # 可见块在 fg 元素内的内缩
    piece_left = geo["fgLeft"] + inset_x
    vis_w = box[2] * k                          # 可见拼图块宽（CSS）

    # 背景图同样可能是被裁切的雪碧图：按 vb 的 background-size/position
    # 取出「图区可见区」，这样缺口坐标天然落在图区坐标系里。
    crop_path, k_bg, crop = bg_crop(bgp, geo, ia)
    if crop is None:
        return dict(err="bg 裁切失败", vbSize=geo.get("vbSize"), vbPos=geo.get("vbPos"))
    cv2.imwrite(crop_path, crop)
    # 缺口与拼图块同高：把「块的可见 y」换算成背景图坐标系当 y_hint
    fgt = geo.get("fg") or [0, 0, 0, 0]
    piece_top = fgt[1] + bp[1] + box[1] * k
    y_hint = (piece_top - ia[1]) / k_bg if k_bg > 1e-6 else None
    gap = find_gap(crop_path, tpl, piece_w_px=max(8.0, vis_w / k_bg), y_hint=y_hint)
    if gap.get("err"):
        return dict(err=gap["err"])
    gap_css = gap["x"] * k_bg
    info = dict(dx=gap_css - piece_left, gap_css=gap_css, piece_left=piece_left,
                inset_x=inset_x, k=k, k_bg=k_bg, box=box, score=gap["score"],
                gap=gap, fgLeft=geo["fgLeft"], visible_w=vis_w, piece_top=piece_top,
                y_hint=y_hint,
                vbSize=geo.get("vbSize"), vbPos=geo.get("vbPos"))
    if gap["inside"] < 0.30 or gap["quality"] < 0.10:
        info["err"] = "缺口置信度低 inside=%.2f quality=%.2f" % (gap["inside"], gap["quality"])
    return info


def drag(sid, dx, duration="900ms", timeout="3s"):
    t0 = time.time()
    msg = ""
    for _ in range(3):
        a = _acts(sid)
        if not a:
            time.sleep(1.0)
            continue
        out = sh(["drag", "--session", sid] + a +
                 ["--depends-on", ".tencent-captcha-dy__slider-block",
                  "--timeout", timeout, "--duration", duration,
                  "--dx", "%.2f" % dx, "--dy", "0",
                  ".tencent-captcha-dy__slider-block"], 90)
        msg = (out or "").splitlines()[0][:90] if out else ""
        if out and not any(k in out for k in _bsk.TRANS):
            break
        time.sleep(1.0)
    return time.time() - t0, msg


def wait_outcome(sid, maxs=9.0):
    """拖完盯页面：verifySuccess.html 或离开 /verify/ 即视为通过。"""
    t0 = time.time()
    last = {}
    while time.time() - t0 < maxs:
        st = js_obj(sid, JS_URL) or {}
        last = st
        u = st.get("u", "")
        if "verifySuccess" in u:
            return True, st
        if u and "/verify/" not in u:
            return True, st
        time.sleep(0.7)
    return False, last


def pass_captcha(sid, tries=6, log=print, tag="c"):
    """在当前标签页上解验证码。返回 (ok, 说明)。"""
    last_url = None
    for i in range(1, tries + 1):
        drop_dialogs(sid)
        geo = js_obj(sid, JS_GEO)
        if not geo:
            log("  [captcha %d] 取不到页面状态" % i)
            time.sleep(1.2)
            continue
        if geo.get("no"):
            log("  [captcha %d] 当前没有拼图验证码（%s）" % (i, geo.get("u", "")[:70]))
            return (True, "no-captcha") if "/verify/" not in geo.get("u", "") else (False, "no-fg")
        p = plan(geo, tag="c%d" % i)
        if not p or p.get("err"):
            log("  [captcha %d] 求解失败: %s" % (i, (p or {}).get("err")))
            # 同一张图连续两次解不出来就刷新题库（拖错时腾讯会自己换图，
            # 但「没敢拖」的情况不会，会一直卡在同一张上）
            if geo.get("vbUrl") and geo.get("vbUrl") == last_url:
                js_obj(sid, "(function(){location.reload();return JSON.stringify({ok:1});})()")
                time.sleep(2.0)
                last_url = None
            else:
                last_url = geo.get("vbUrl")
                time.sleep(1.0)
            continue
        last_url = geo.get("vbUrl")
        g = p["gap"]
        log("  [captcha %d] gap=%.1fcss piece=%.1fcss dx=%.1f inside=%.2f q=%.2f amb=%.2f visW=%.1f"
            % (i, p["gap_css"], p["piece_left"], p["dx"], g["inside"], g["quality"],
               g["amb"], p["visible_w"]))
        dt, msg = drag(sid, p["dx"])
        ok, st = wait_outcome(sid)
        log("  [captcha %d] drag %.1fs -> %s | %s" % (i, dt, "PASS" if ok else "FAIL", msg))
        if ok:
            return True, st.get("u", "")
        time.sleep(1.0)
    return False, "tries-exhausted"


# ---------------- 统一入口：不分类型，自己判断 ----------------

JS_KIND = ("(function(){function vis(s){var e=document.querySelector(s);if(!e)return false;"
           "var r=e.getBoundingClientRect();return r.width>10&&r.height>10&&r.top>-50&&r.left>-50;}"
           "return JSON.stringify({u:location.href.slice(0,200),t:document.title,"
           "cw:vis('.verify-img-panel img'),pz:vis('.tencent-captcha-dy__image-area')"
           "||document.title.indexOf('拼图校验')>=0});})()")


def page_kind(sid):
    """返回 ('clickword'|'puzzle'|'clean', info)。"""
    for _ in range(3):
        try:
            o = js_obj(sid, JS_KIND)
        except Exception:
            o = None
        if o:
            if o.get("cw"):
                return "clickword", o
            if o.get("pz"):
                return "puzzle", o
            return "clean", o
        time.sleep(1.0)
    return None, {}


def auto_pass(sid, rounds=6, log=print):
    """自动识别并解决当前页上的验证码；不在验证码页直接返回 True。"""
    import sys as _sys
    here = os.path.dirname(os.path.abspath(__file__))
    if here not in _sys.path:
        _sys.path.insert(0, here)
    for i in range(1, rounds + 1):
        kind, info = page_kind(sid)
        if kind is None:
            log("  [auto %d] 连不上页面" % i)
            time.sleep(1.5)
            continue
        log("  [auto %d] kind=%s title=%s" % (i, kind, str(info.get("t"))[:40]))
        if kind == "clean":
            return True
        if kind == "clickword":
            try:
                from . import clickword as CW
            except Exception:
                try:
                    import clickword as CW          # 直接按文件路径 import 时
                except Exception as ex:
                    log("  clickword 模块不可用: %s" % ex)
                    return False
            ok, why = CW.solve(sid, tries=4, log=log)
            log("  [auto %d] 文字点选 -> %s %s" % (i, ok, why))
            if ok:
                return True
        else:
            ok = pass_captcha(sid, tries=3, log=log, tag="p%d" % i)
            log("  [auto %d] 拼图 -> %s" % (i, ok))
            if ok:
                return True
        time.sleep(1.5)
    return False


def _main():
    sid = None
    for a in sys.argv[1:]:
        if re.fullmatch(r"[a-z0-9]{4}", a):
            sid = a
    if not sid:
        lst = sh(["session", "list"], 30)
        m = re.search(r"^(\w{4})\s+\S+", lst, re.M)
        sid = m.group(1) if m else None
    if not sid:
        print("找不到可用会话，请先 bsk session start")
        return 1
    print("session", sid)
    t0 = time.time()
    ok, why = pass_captcha(sid)
    print("结果:", "通过" if ok else "未通过", "|", str(why)[:100], "| %.1fs" % (time.time() - t0))
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(_main())
