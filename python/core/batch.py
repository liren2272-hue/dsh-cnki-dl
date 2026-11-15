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
from . import guard as guard_mod
from . import cite

DELAY = (1.5, 3.0)   # 基础抖动；实际间隔由守卫.min_interval_sec 兜底（取更大者）


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

    g = guard_mod.CnkiGuard()
    ok = caj = noent = fail = skip = 0
    retry_tail = []   # 验证码退避后重试的尾巴
    try:
        for n, item in enumerate(items, 1):
            if limit and n > limit:
                break
            key = norm_title(item.get("title"))
            if key in done and done[key].get("status") == "ok":
                log("[%d/%d] 跳过（已完成）%s" % (n, len(items), item.get("title", "")[:40]))
                skip += 1
                continue
            # 守卫节奏：距上次下载 ≥ min_interval；验证码冷却时在这里睡
            wait = g.wait_needed()
            if wait > 0:
                cool = "（风控冷却）" if g.captcha_streak else ""
                log("    · 守卫等待 %.0fs%s" % (wait, cool))
                time.sleep(wait)
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
            g.note_download()
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
                g.note_ok()
                log("    ✓ PDF %d KB %s (%.1fs)" % (res["bytes"] // 1024,
                                                   os.path.basename(res["file"]),
                                                   res.get("secs", 0)))
            elif res["status"] == "caj-only":
                caj += 1
                g.note_ok()
                log("    ~ 只有 %s：%s" % (res.get("format") or "CAJ",
                                          os.path.basename(res.get("file") or "-")))
            elif res["status"] == "no-entry":
                noent += 1
                log("    - 无下载入口，跳过")
            elif res["status"] == "captcha-fail":
                # 本机已能自动破验证码（grab attempts=3 内部重试）；走到这里说明
                # 自动过码也被挡 → 指数退避冷却，收进尾巴稍后重试，不再整批硬停。
                fail += 1
                cd = g.note_captcha()
                retry_tail.append(item)
                log("    ! 验证码未通过：%s → 退避 %.0fs 后重试（尾巴共 %d 条）"
                    % (item.get("title", "")[:40], cd, len(retry_tail)))
            else:
                fail += 1
                log("    ! 失败：%s" % (res.get("note") or res["status"]))
            time.sleep(random.uniform(*DELAY))

        # —— 尾巴重试：只重跑验证码失败的条目，走同一守卫（此时已冷却）——
        if retry_tail:
            log("\n== 尾巴重试 %d 条 ==" % len(retry_tail))
            for item in retry_tail:
                key = norm_title(item.get("title"))
                wait = g.wait_needed()
                if wait > 0:
                    time.sleep(wait)
                log("[重试] %s" % (item.get("title") or "")[:60])
                sess = bsk.Session(name="cnki-dl", log=log)
                try:
                    res = fetch.grab(sess, item, out_dir, log=log, want=want)
                finally:
                    try:
                        sess.stop()
                    except Exception:
                        pass
                g.note_download()
                res["idx"] = item.get("idx") or 0
                done[key] = res
                if res["status"] == "ok":
                    ok += 1
                    fail -= 1
                    g.note_ok()
                    log("    ✓ 重试成功 PDF %d KB" % (res["bytes"] // 1024))
                elif res["status"] == "captcha-fail":
                    g.note_captcha()
                    log("    ! 重试仍验证码未通过（可稍后重跑续传）")
                else:
                    log("    · 重试结果：%s" % (res.get("note") or res["status"]))
                time.sleep(random.uniform(*DELAY))
    finally:
        write_manifest(out_dir, st)
        # 同步产出题录：references.txt(GB/T 7714 国标) + .bib + .ris
        try:
            recs = cite.to_records(list(st.get("done", {}).values()))
            cite.write_all(out_dir, recs)
        except Exception as e:  # 题录失败不影响下载结果
            log("    · 题录导出失败：%s" % e)

    log("\n汇总：PDF %d / 仅私有格式 %d / 无入口 %d / 失败 %d / 跳过 %d"
        % (ok, caj, noent, fail, skip))
    return 0 if fail == 0 else 1
