# -*- coding: utf-8 -*-
"""批量连续下载：队列 → 单篇抓取 → 状态落盘 → 清单产出。

* 状态文件 `<out>/_state.json`：按归一化标题去重、断点续跑。
* 清单 `<out>/文献清单.csv`（utf-8-sig，Excel 直接打开）。
* 验证码过不去时打印 `RESUME_AT=<n>` 并以退出码 2 结束（人工兜底接口）。
"""
import csv
import json
import os
import random
import re
import time

from . import bsk
from . import pages
from . import fetch
from . import captcha

DELAY = (1.5, 3.0)


def norm_title(t):
    t = (t or "").lower()
    for w in ("免费", "增强出版", "网络首发", "!", "！", " ", "　"):
        t = t.replace(w, "")
    return re.sub(r"[\s\-—–_·、,，.。:：;；()（）\[\]【】《》<>\"'“”‘’?？]", "", t)


def state_path(out):
    return os.path.join(out, "_state.json")


def load_state(out):
    p = state_path(out)
    if os.path.exists(p):
        try:
            return json.load(open(p, encoding="utf-8"))
        except Exception:
            pass
    return {"done": {}, "items": []}


def save_state(out, st):
    tmp = state_path(out) + ".tmp"
    json.dump(st, open(tmp, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    os.replace(tmp, state_path(out))


def collect(sess, kw, kind="journal", top=20, pages_n=1, log=print):
    """检索并按下载量取前 top 条。kind=thesis 会自动切到学位论文库。"""
    got = []
    for pg in range(pages_n):
        url = pages.search_url(kw, kind)
        if pg:
            url += "&page=%d" % (pg + 1)
        sess.nav(url)
        time.sleep(5)
        # 检索页也可能先弹验证码（换了会话/切库后尤其常见）
        for _ in range(3):
            isv, _info = pages.is_verify(sess)
            if not isv:
                break
            log("  · 检索页命中验证码，自动通过")
            if not captcha.auto_pass(sess.sid, rounds=4):
                log("  ! 检索页验证码未通过")
                break
            time.sleep(3.0)
        switched = False
        if kind != "all":
            switched = pages.switch(sess, kind)
            if switched:
                log("  · 已切到%s库" % ("学位论文" if kind == "thesis" else "期刊"))
            else:
                log("  ! 切库未确认，按当前结果继续")
        rows = pages.norm_rows(sess)
        if switched:
            # 切库后的列表没有「类型」列，直接按目标库认类型
            for r in rows:
                if r["kind"] in ("other", "unknown"):
                    r["kind"] = kind
        if kind != "all":
            rows = [r for r in rows if r["kind"] == kind] or rows
        got.extend(rows)
        log("  · 第 %d 页取到 %d 条" % (pg + 1, len(rows)))
        if len(got) >= top:
            break
    # 下载量降序（取不到下载量时保留原顺序）
    got.sort(key=lambda r: (r["dl"] is None, -(r["dl"] or 0)))
    out, seen = [], set()
    for r in got:
        k = norm_title(r["title"])
        if not k or k in seen:
            continue
        seen.add(k)
        r["idx"] = len(out) + 1
        out.append(r)
        if len(out) >= top:
            break
    return out


def write_manifest(out, st):
    rows = st.get("items") or []
    if not rows:
        return ""
    p = os.path.join(out, "文献清单.csv")
    with open(p, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["序号", "标题", "类型", "下载量", "状态", "格式", "字节", "文件名", "备注"])
        for r in rows:
            w.writerow([r.get("idx", ""), r.get("title", ""), r.get("kind", ""),
                        r.get("dl") or "", r.get("status", ""), r.get("format", ""),
                        r.get("bytes", 0), os.path.basename(r.get("file") or ""),
                        r.get("note", "")])
    return p


def run(items, out_dir, sid=None, log=print, want="pdf", limit=None):
    os.makedirs(out_dir, exist_ok=True)
    st = load_state(out_dir)
    done = st.setdefault("done", {})
    log("队列 %d 篇，已完成 %d 篇（断点续跑）" % (len(items), len(done)))

    ok = caj = noent = fail = skip = 0
    try:
        for n, item in enumerate(items, 1):
            if limit and n > limit:
                break
            key = norm_title(item.get("title"))
            if key in done and done[key].get("status") == "ok":
                log("[%d/%d] 跳过（已完成）%s" % (n, len(items), item.get("title", "")[:40]))
                skip += 1
                continue
            log("[%d/%d] %s" % (n, len(items), (item.get("title") or "")[:60]))
            # 每篇一个全新会话 + 新标签：bar 的验证/下载页会把旧标签拽在
            # bar 域上（后续 navigate 被拉回），实测跨篇复用会话必卡死。
            sess = bsk.Session(name="cnki-dl", log=log)
            log("    · 新会话 sid=%s" % sess.sid)
            t0 = time.time()
            try:
                res = fetch.grab(sess, item, out_dir, log=log, want=want)
            finally:
                try:
                    sess.stop()
                except Exception:
                    pass
            res["idx"] = item.get("idx") or n
            res["secs"] = round(time.time() - t0, 1)
            done[key] = res
            # 清单直接从 done 重建：按「真实标题」去重，避免详情页标题与队列
            # 标题不一致时（如「网络首发」后缀）留下同一篇的两条记录。
            rec = {k: res.get(k) for k in ("idx", "title", "kind", "dl", "status",
                                           "file", "bytes", "format", "note")}
            done[norm_title(rec.get("title") or item.get("title"))] = dict(rec)
            st["items"] = sorted(
                [{k: v.get(k) for k in ("idx", "title", "kind", "dl", "status",
                                        "file", "bytes", "format", "note")}
                 for v in done.values()],
                key=lambda r: (r.get("idx") or 0))
            save_state(out_dir, st)

            if res["status"] == "ok":
                ok += 1
                log("    ✓ PDF %d KB %s (%.1fs)" % (res["bytes"] // 1024,
                                                   os.path.basename(res["file"]),
                                                   res.get("secs", 0)))
            elif res["status"] == "caj-only":
                caj += 1
                log("    ~ 只有 %s：%s" % (res.get("format") or "CAJ",
                                          os.path.basename(res.get("file") or "-")))
            elif res["status"] == "no-entry":
                noent += 1
                log("    - 无下载入口，跳过")
            elif res["status"] == "captcha-fail":
                fail += 1
                log("    ! 验证码未通过，停在第 %d 条：%s" % (n, item.get("title", "")[:40]))
                write_manifest(out_dir, st)
                print("\nRESUME_AT=%d" % n)
                return 2
            else:
                fail += 1
                log("    ! 失败：%s" % (res.get("note") or res["status"]))
            time.sleep(random.uniform(*DELAY))
    finally:
        write_manifest(out_dir, st)

    log("\n汇总：PDF %d / 仅私有格式 %d / 无入口 %d / 失败 %d / 跳过 %d"
        % (ok, caj, noent, fail, skip))
    return 0 if fail == 0 else 1
