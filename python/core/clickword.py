# -*- coding: utf-8 -*-
"""知网「安全验证 · 文字点选」(captchaType=clickWord) 自动通过。

本文件由会话 2026-10-31_0535_a461b3/src/clickword.py 复制而来，自包含（自己调 bsk）。

页面结构（kns.cnki.net/verify/home?captchaType=clickWord）
    .verifybox/verifybox-bottom
      .verify-img-panel  <img src=data:image/png;base64,...>   310x155 CSS
      .verify-refresh    刷新
      .verify-msg        提示文字：请依次点击【满,友,不】

要点
* 图上汉字是由**纯色**渲染上来的，每种字色在整张图里是同一个 RGB 值、像素数
  一两百个 → 用「精确颜色 + 连通域」就能稳定抠出每个字的 mask 与中心点，
  不需要任何图像分割模型。
* 验证码接受**合成 MouseEvent**（实测：派发 click 后图上出现标记），所以用
  bsk evaluate 在页面内派发 mousedown/mouseup/click 即可点击任意像素点，
  不用真鼠标、也不用 CDP。
* 识别「哪个字是哪个字」用 RapidOCR（中文 OCR）；没装则退化为模板匹配。

用法
    python -u src/clickword.py --probe                 # 只看当前验证码（不点）
    python -u src/clickword.py --click 满,友,不        # 按字面点击（调试用）
    python -u src/clickword.py --solve                 # 读提示 + OCR + 点击
"""
import base64, json, os, re, subprocess, sys, time

import cv2
import numpy as np

from .bsk import B  # bsk 路径统一解析（BSK_BIN 环境变量优先）

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP = os.path.join(HERE, "data", "tmp", "clickword")

JS_STATE = r"""(function(){
  var img=document.querySelector('.verify-img-panel img');
  var box=document.querySelector('.verifybox');
  var msg=(document.querySelector('.verify-msg')||{}).textContent||'';
  if(!img) return JSON.stringify({no:true,msg:msg,t:document.title,u:location.href.slice(0,140)});
  var r=img.getBoundingClientRect();
  return JSON.stringify({no:false, msg:msg, rect:[r.left,r.top,r.width,r.height],
    src:img.src, nw:img.naturalWidth, nh:img.naturalHeight,
    t:document.title, u:location.href.slice(0,140)});
})()"""

JS_CLICK = ("(function(x,y){var p=document.querySelector('.verify-img-panel img');"
            "if(!p)return 'NO_IMG';var r=p.getBoundingClientRect();"
            "var X=r.left+x,Y=r.top+y;['mousedown','mouseup','click'].forEach(function(t){"
            "p.dispatchEvent(new MouseEvent(t,{bubbles:true,cancelable:true,clientX:X,"
            "clientY:Y,view:window,button:0}));});return JSON.stringify([X,Y]);})(%f,%f)")

TRANS = ("shared page control", "requires the context", "did not become ready",
         "could not be attributed", "observation changed")


def sh(a, t=90):
    try:
        r = subprocess.run([B] + a, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=t)
        return ((r.stdout or "") + (r.stderr or "")).strip()
    except Exception as ex:
        return "EXC %s" % ex


def ctx_of(sid):
    sh(["cooperate", "wait", "--session", sid, "--timeout-ms", "4000"], 20)
    m = re.search(r'"context_id": "([^"]+)"', sh(["cooperate", "sync", "--session", sid, "--json"], 40))
    return m.group(1) if m else None


def ev(sid, js, tries=4):
    for _ in range(tries):
        c = ctx_of(sid)
        if not c:
            time.sleep(1)
            continue
        out = sh(["evaluate", "--session", sid, "--context", c, js], 90)
        if out and not any(k in out for k in TRANS):
            return out
        time.sleep(1)
    return ""


def state(sid):
    raw = ev(sid, JS_STATE)
    m = re.search(r"\{.*\}", raw or "", re.S)
    try:
        return json.loads(m.group(0)) if m else None
    except Exception:
        return None


def img_from_state(st, path=None):
    m = re.search(r"base64,([A-Za-z0-9+/=]+)", st.get("src") or "")
    if not m:
        return None
    data = base64.b64decode(m.group(1))
    path = path or os.path.join(TMP, "cw.png")
    open(path, "wb").write(data)
    return path


# ------------------------- 纯色字形分割 -------------------------

def glyphs(img_path, min_px=60, max_px=1600, min_sat=100, merge_gap=9):
    """按「精确同色 + 连通域」抠出图上每个纯色汉字（同色邻近碎片先合并）。"""
    im = cv2.imread(img_path)
    h, w = im.shape[:2]
    hsv = cv2.cvtColor(im, cv2.COLOR_BGR2HSV)
    flat = im.reshape(-1, 3).astype(np.int32)
    key = flat[:, 0] * 65536 + flat[:, 1] * 256 + flat[:, 2]
    uniq, cnt = np.unique(key, return_counts=True)
    out = []
    for k, c in zip(uniq, cnt):
        if c < min_px or c > max_px:
            continue
        k = int(k)
        b, g, r = k // 65536, (k // 256) % 256, k % 256
        hh, ss, vv = cv2.cvtColor(np.uint8([[[b, g, r]]]), cv2.COLOR_BGR2HSV)[0, 0]
        if ss < min_sat:
            continue
        # 该颜色（容忍 ±2）所在像素
        m = ((np.abs(im[:, :, 0].astype(np.int32) - b) <= 2) &
             (np.abs(im[:, :, 1].astype(np.int32) - g) <= 2) &
             (np.abs(im[:, :, 2].astype(np.int32) - r) <= 2)).astype(np.uint8)
        n, lab, stats, cent = cv2.connectedComponentsWithStats(m, 8)
        parts = []
        for i in range(1, n):
            x, y, ww, hh2, ar = stats[i]
            if ar < min_px * 0.4:
                continue
            if not (0.35 < ww / float(hh2 or 1) < 2.8):
                continue
            parts.append(dict(x=int(x), y=int(y), w=int(ww), h=int(hh2), a=int(ar),
                              cx=float(cent[i][0]), cy=float(cent[i][1]), ids=[i]))
        # 同色邻近碎片合并（「满」常被拆成主块 + 一笔）
        merged = True
        while merged:
            merged = False
            for i in range(len(parts)):
                for j in range(i + 1, len(parts)):
                    p, q = parts[i], parts[j]
                    if (p["x"] - merge_gap < q["x"] + q["w"] and q["x"] - merge_gap < p["x"] + p["w"] and
                            p["y"] - merge_gap < q["y"] + q["h"] and q["y"] - merge_gap < p["y"] + p["h"]):
                        x0, y0 = min(p["x"], q["x"]), min(p["y"], q["y"])
                        x1 = max(p["x"] + p["w"], q["x"] + q["w"])
                        y1 = max(p["y"] + p["h"], q["y"] + q["h"])
                        tot = p["a"] + q["a"]
                        cx = (p["cx"] * p["a"] + q["cx"] * q["a"]) / tot
                        cy = (p["cy"] * p["a"] + q["cy"] * q["a"]) / tot
                        parts[i] = dict(x=x0, y=y0, w=x1 - x0, h=y1 - y0, a=tot, cx=cx, cy=cy,
                                        ids=p["ids"] + q["ids"])
                        parts.pop(j)
                        merged = True
                        break
                if merged:
                    break
        for p in parts:
            # 拼图字形尺寸固定：合并后 13~48px、像素数 150~1500；
            # 不加这层过滤会把背景照片里的同色色块当成字。
            if not (13 <= p["w"] <= 48 and 13 <= p["h"] <= 48):
                continue
            if p["a"] < 150 or p["a"] > 1500:
                continue
            mfull = np.isin(lab, p["ids"]).astype(np.uint8)
            out.append(dict(cx=p["cx"], cy=p["cy"], bbox=(p["x"], p["y"], p["w"], p["h"]),
                            area=int(p["a"]), color=(b, g, r),
                            mask=mfull[p["y"]:p["y"] + p["h"], p["x"]:p["x"] + p["w"]],
                            mfull=mfull))
    # 去重：同一位置只留面积最大的
    out.sort(key=lambda d: -d["area"])
    keep = []
    for d in out:
        if all(abs(d["cx"] - e["cx"]) > 10 or abs(d["cy"] - e["cy"]) > 10 for e in keep):
            keep.append(d)
    return keep, im


def save_preview(gs, im, path=None):
    vis = im.copy()
    for i, d in enumerate(gs):
        x, y, w, h = d["bbox"]
        cv2.rectangle(vis, (x, y), (x + w, y + h), (0, 0, 255), 1)
        cv2.putText(vis, str(i), (x, max(10, y - 2)), cv2.FONT_HERSHEY_SIMPLEX, 0.4, (0, 0, 255), 1)
    path = path or os.path.join(TMP, "cw_glyphs.png")
    cv2.imwrite(path, vis)
    return path


def track_marks(sid, glyphs_list, tag="g"):
    """在页面上画小十字标出检测到的字形中心（仅调试用，点完会被页面重绘）。"""
    pts = [[round(d["cx"], 1), round(d["cy"], 1)] for d in glyphs_list]
    js = ("(function(pts){var p=document.querySelector('.verify-img-panel');"
          "if(!p)return 'no';var old=document.getElementById('bsk-marks');"
          "if(old)old.remove();var c=document.createElement('canvas');c.id='bsk-marks';"
          "var r=p.getBoundingClientRect();c.width=r.width;c.height=r.height;"
          "c.style.cssText='position:absolute;left:0;top:0;pointer-events:none;z-index:9';"
          "var g=c.getContext('2d');g.strokeStyle='red';g.lineWidth=1;"
          "pts.forEach(function(q){g.beginPath();g.moveTo(q[0]-6,q[1]);g.lineTo(q[0]+6,q[1]);"
          "g.moveTo(q[0],q[1]-6);g.lineTo(q[0],q[1]+6);g.stroke();});p.appendChild(c);"
          "return JSON.stringify(pts);})(%s)" % json.dumps(pts))
    return ev(sid, js)


# ------------------------- 识别 -------------------------

_OCR = None


def _ocr():
    global _OCR
    if _OCR is None:
        try:
            from rapidocr_onnxruntime import RapidOCR
            _OCR = RapidOCR()
        except Exception as ex:
            print("  OCR 不可用: %s" % ex, flush=True)
            _OCR = False
    return _OCR


def ocr_words(img_path, scale=3):
    """RapidOCR 识别（中文），返回 [(text, (cx,cy), score)]。"""
    if not _ocr():
        return []
    im = cv2.imread(img_path)
    big = cv2.resize(im, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
    p = os.path.join(TMP, "cw_big.png")
    cv2.imwrite(p, big)
    res, _ = _OCR(p)
    out = []
    for box, txt, sc in (res or []):
        xs = [q[0] / scale for q in box]
        ys = [q[1] / scale for q in box]
        out.append((txt, ((min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2), float(sc)))
    return out


def fonts():
    return [r"C:/Windows/Fonts/msyh.ttc", r"C:/Windows/Fonts/msyhbd.ttc",
            r"C:/Windows/Fonts/simhei.ttf", r"C:/Windows/Fonts/simsun.ttc"]


def render_char(ch, size=40):
    """用系统字体把字渲染成 mask，返回若干个候选 mask（多字体）。"""
    from PIL import Image, ImageDraw, ImageFont
    outs = []
    for fp in fonts():
        if not os.path.exists(fp):
            continue
        try:
            f = ImageFont.truetype(fp, size)
        except Exception:
            continue
        im = Image.new("L", (size * 2, size * 2), 0)
        d = ImageDraw.Draw(im)
        d.text((size // 2, size // 2), ch, fill=255, font=f)
        a = np.array(im)
        ys, xs = np.where(a > 100)
        if len(xs) < 20:
            continue
        outs.append(a[ys.min():ys.max() + 1, xs.min():xs.max() + 1])
    return outs


def match_char(mask, charset):
    """把字形 mask 与 charset 里每个字的多字体渲染图做 IoU，返回排序候选。"""
    target = cv2.resize(mask.astype(np.uint8) * 255, (48, 48), interpolation=cv2.INTER_AREA) > 127
    scored = []
    for ch in charset:
        best = 0.0
        for tpl in render_char(ch, 36):
            t = cv2.resize(tpl, (48, 48), interpolation=cv2.INTER_AREA) > 100
            inter = np.logical_and(target, t).sum()
            union = np.logical_or(target, t).sum()
            iou = inter / max(1, union)
            best = max(best, iou)
        scored.append((ch, round(best, 3)))
    scored.sort(key=lambda t: -t[1])
    return scored


# ------------------------- 主流程 -------------------------

def _render_mask_square(mask, ang=0, box=190, fill=150):
    """把字形 mask 渲染成「白底黑字」的正方形小图（供 OCR），按 ang 旋转。"""
    ys, xs = np.where(mask > 0)
    if len(ys) == 0:
        return None
    m = mask[ys.min():ys.max() + 1, xs.min():xs.max() + 1]
    m = np.pad(m, 16, constant_values=0)
    s = min(fill / float(m.shape[0]), fill / float(m.shape[1]))
    r = cv2.resize(m, None, fx=s, fy=s, interpolation=cv2.INTER_CUBIC)
    hh, ww = r.shape
    n = max(hh, ww) + 24
    canvas = np.zeros((n, n), np.uint8)
    canvas[(n - hh) // 2:(n - hh) // 2 + hh, (n - ww) // 2:(n - ww) // 2 + ww] = r
    if ang:
        M = cv2.getRotationMatrix2D((n / 2.0, n / 2.0), ang, 1.0)
        canvas = cv2.warpAffine(canvas, M, (n, n), borderValue=0)
    out = np.full((n, n), 255, np.uint8)
    out[canvas > 0] = 255 - canvas[canvas > 0]
    return out


def read_glyph(im, d, targets=(), angles=(0, 12, -12, 24, -24, 36, -36, 48, -48), tag="g"):
    """单字形 OCR：颜色 mask → 白底黑字 → 多角度旋转 → 取置信度最高的一次。

    验证码里的字带随机旋转/缩放，整图 OCR 会漏读，单字+旋转扫描实测置信度 1.0。
    返回 (text, score, angle)。
    """
    ocr = _ocr()
    if not ocr:
        return None, 0.0, 0
    col = np.array(d["color"], dtype=np.int16)
    if d.get("mfull") is not None:
        core = d["mfull"]
        ann = (np.abs(im.astype(np.int16) - col).sum(axis=2) < 48) & \
              (cv2.dilate(core, np.ones((5, 5), np.uint8)) > 0)
    else:
        x, y, w, h = d["bbox"]
        sub = np.abs(im[y:y + h, x:x + w].astype(np.int16) - col).sum(axis=2) < 48
        ann = np.zeros(im.shape[:2], bool)
        ann[y:y + h, x:x + w] = sub
    best = (None, 0.0, 0)
    for ang in angles:
        canvas = _render_mask_square(ann.astype(np.uint8) * 255, ang)
        if canvas is None:
            return None, 0.0, 0
        fp = os.path.join(TMP, "cw_g_%s.png" % tag)
        cv2.imwrite(fp, canvas)
        res, _ = ocr(fp)
        if not res:
            continue
        txt = re.sub(r"[^\u4e00-\u9fa5]", "", str(res[0][1]) or "")
        sc = float(res[0][2])
        if not txt:
            continue
        # 目标字优先：只要读到目标字并置信度不低，就提前收工
        if best[0] is None or sc > best[1] + 1e-6:
            best = (txt[:1], sc, ang)
        if txt[:1] in targets and sc >= 0.95:
            break
    return best


def identify(gs, im, targets, log=print):
    """给每个字形贴标签：优先单字旋转 OCR，其次模板匹配（弱兜底）。"""
    for i, d in enumerate(gs):
        txt, sc, ang = read_glyph(im, d, targets, tag=str(i))
        d["ocr"] = (txt, sc, ang)
        d["char"], d["score"] = txt, sc
        if not txt:
            ranked = match_char(d["mask"], list(targets) or ["?"])
            d["ranked"] = ranked
            d["char"], d["score"] = ranked[0][0], ranked[0][1] * 0.5
        log("    #%d bbox=%s area=%d 读作 %r conf=%.2f ang=%s"
            % (i, d["bbox"], d["area"], d["char"], d["score"], ang))
    return gs


def pick_sequence(gs, targets, log=print):
    """按提示顺序挑字形下标；目标字缺失返回 None。"""
    used, seq = set(), []
    for ch in targets:
        cand = [i for i, d in enumerate(gs) if d.get("char") == ch and i not in used]
        cand.sort(key=lambda i: -gs[i].get("score", 0))
        if not cand:
            log("  缺字 %r（已识别 %s）" % (ch, [d.get("char") for d in gs]))
            return None
        used.add(cand[0])
        seq.append(cand[0])
    return seq


def parse_targets(msg):
    m = re.search(r"[【\[]([^】\]]+)[】\]]", msg or "")
    if not m:
        return []
    return [c for c in re.split(r"[,，、\s]+", m.group(1)) if c]


def click_glyph(sid, d):
    return ev(sid, JS_CLICK % (d["cx"], d["cy"]))


JS_REFRESH = ("(function(){var b=document.querySelector('.verify-refresh');"
              "if(!b)return 'NO_BTN';b.click();return 'ok';})()")


def passed(st):
    """判断文字点选是否已通过：URL 离开 verify/home 或出现成功文案。"""
    u = (st or {}).get("u") or ""
    msg = (st or {}).get("msg") or ""
    if "verifySuccess" in u:
        return True
    if "/verify/" in u and "captchaType=clickWord" not in u:
        return True
    if "验证成功" in msg or "已通过" in msg:
        return True
    return False


def solve(sid, tries=8, log=print):
    try:
        os.makedirs(TMP, exist_ok=True)      # 临时图目录首次使用要自己建
    except Exception:
        pass
    for att in range(1, tries + 1):
        st = state(sid)
        if not st:
            log("  [%d] 取不到验证码状态" % att)
            time.sleep(1.2)
            continue
        if st.get("no"):
            if passed(st):
                return True, "passed url=%s" % str(st.get("u"))[:100]
            log("  [%d] 当前没有文字点选验证码（%s）" % (att, str(st.get("u"))[:90]))
            return False, "not-clickword"
        if passed(st):
            return True, "passed url=%s" % str(st.get("u"))[:100]
        targets = parse_targets(st.get("msg"))
        p = img_from_state(st)
        gs, im = glyphs(p)
        log("  [%d] 提示 %r 目标 %s 字形 %d 个" % (att, st.get("msg"), targets, len(gs)))
        if not targets or not gs:
            log("  [%d] 提示或字形为空，刷新重试" % att)
            ev(sid, JS_REFRESH)
            time.sleep(1.5)
            continue
        identify(gs, im, targets, log=log)
        seq = pick_sequence(gs, targets, log=log)
        if seq is None:
            ev(sid, JS_REFRESH)
            time.sleep(1.5)
            continue
        log("  [%d] 点击顺序 -> %s" % (att, [gs[i]["char"] for i in seq]))
        for i in seq:
            click_glyph(sid, gs[i])
            time.sleep(0.45)
        # 等结果：成功会跳走，失败会换题
        for _ in range(10):
            time.sleep(0.6)
            st2 = state(sid) or {}
            if passed(st2):
                return True, "passed url=%s" % str(st2.get("u"))[:100]
            if st2.get("no"):
                return True, "passed(gone) url=%s" % str(st2.get("u"))[:100]
            if (st2.get("msg") or "") != (st.get("msg") or "") or _sig(st2) != _sig(st):
                log("  [%d] 答案不对，已换新题" % att)
                break
    return False, "exhausted"


def _sig(st):
    return (st.get("src") or "")[60:160]


def _main():
    sid = None
    args = sys.argv[1:]
    for a in args:
        if re.fullmatch(r"[a-z0-9]{4}", a):
            sid = a
    if not sid:
        m = re.search(r"^(\w{4})\s", sh(["session", "list"], 30), re.M)
        sid = m.group(1) if m else None
    st = state(sid)
    if "--probe" in args:
        p = img_from_state(st)
        gs, im = glyphs(p)
        print("提示:", st.get("msg"), "| 图像", im.shape)
        for i, d in enumerate(gs):
            print("  #%d (%.1f,%.1f) bbox=%s area=%d" % (i, d["cx"], d["cy"], d["bbox"], d["area"]))
        print("preview ->", save_preview(gs, im))
        print("OCR ->", ocr_words(p))
        return 0
    if "--click" in args:
        chars = args[args.index("--click") + 1]
        p = img_from_state(st)
        gs, im = glyphs(p)
        chars = [c for c in re.split(r"[,，]", chars)]
        for ch, d in zip(chars, gs):
            print("click", ch, click_glyph(sid, d))
            time.sleep(0.5)
        time.sleep(1.5)
        print("after:", json.dumps(state(sid), ensure_ascii=False)[:300])
        return 0
    ok, why = solve(sid)
    print("结果:", ok, why)
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(_main())
