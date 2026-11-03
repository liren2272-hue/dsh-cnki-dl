# -*- coding: utf-8 -*-
"""知网 PDF 批量下载器（自包含）。

    python run.py doctor                        # 环境体检
    python run.py captcha                       # 只对付当前页的验证码
    python run.py search "农业文化遗产" --type thesis --top 20
    python run.py batch "农业文化遗产" --type thesis --top 10 --out D:/out/thesis
    python run.py batch --list urls.txt --out D:/out/misc
    python run.py download "<详情页URL>" --out D:/out/one
    python run.py verify D:/out/thesis           # 按魔数复核已下载文件
    python run.py selftest                       # 离线自测（不需要浏览器）

关键实现说明见 README.md；尤其「学位论文 PDF 入口」与「验证码自动通过」两节。
"""
import argparse
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from core import bsk, pages, fetch, batch      # noqa: E402
from core import captcha                        # noqa: E402

try:                                            # 中文输出永远走 UTF-8
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def log(*a):
    print(*a, flush=True)


# --------------------------------------------------------------------------- doctor
def cmd_doctor(args):
    import importlib.util
    bad = 0
    log("== 环境体检 ==")

    log("[1] bsk 可执行文件：%s" % bsk.B)
    if not os.path.exists(bsk.B) and not bsk.B.endswith("bsk"):
        log("    ✗ 找不到 bsk，设置 BSK_BIN 环境变量指向 bsk.exe")
        bad += 1
    else:
        st = bsk.status()
        log("    status: %s" % (st.replace("\n", " ")[:160] or "无响应"))
        if '"browser_name"' in st and '"unresponsive": false' in st:
            log("    ✓ 浏览器已连接")
        else:
            log("    ✗ 浏览器未连接：确认 Chrome + BrowserSkill 扩展已开，"
                "并先跑 `bsk daemon start --foreground`")
            bad += 1

    log("[2] Python 依赖")
    for m in ("cv2", "numpy", "PIL", "rapidocr_onnxruntime"):
        if importlib.util.find_spec(m):
            log("    ✓ %s" % m)
        else:
            log("    ! 缺 %s（验证码识别会退化，装：pip install %s）"
                % (m, "opencv-python" if m == "cv2" else
                   "pillow" if m == "PIL" else "rapidocr_onnxruntime"))
            if m == "rapidocr_onnxruntime":
                bad += 1

    log("[3] 输出目录可写")
    out = args.out or os.path.join(HERE, "data", "downloads")
    try:
        os.makedirs(out, exist_ok=True)
        f = os.path.join(out, ".w")
        open(f, "w").write("ok")
        os.remove(f)
        log("    ✓ %s" % out)
    except Exception as ex:
        log("    ✗ %s (%s)" % (out, ex))
        bad += 1

    log("[4] 下载器扩展（IDM 等会劫持 PDF）")
    log("    · 如遇 USER_CANCELED，请手动到 chrome://extensions 关掉下载器扩展")
    log("\n结论：%s" % ("全部通过" if bad == 0 else "%d 项需要处理" % bad))
    return 0 if bad == 0 else 1


# --------------------------------------------------------------------------- captcha
def cmd_captcha(args):
    sess = bsk.Session(sid=args.sid, name="cnki-cap", log=log)
    log("会话 sid=%s" % sess.sid)
    kind, info = captcha.page_kind(sess.sid)
    log("页面 kind=%s url=%s" % (kind, str(info.get("u"))[:120]))
    ok = captcha.auto_pass(sess.sid, rounds=args.rounds, log=log)
    log("结果：%s" % ("已通过" if ok else "未通过"))
    if not args.sid:
        sess.stop()
    return 0 if ok else 1


# --------------------------------------------------------------------------- search
def cmd_search(args):
    sess = bsk.Session(sid=args.sid, name="cnki-search", log=log)
    log("会话 sid=%s" % sess.sid)
    try:
        items = batch.collect(sess, args.kw, kind=args.type, top=args.top,
                             pages_n=args.pages, log=log)
    finally:
        if not args.sid:
            sess.stop()
    log("\n%-4s %-8s %-8s %s" % ("#", "下载量", "类型", "标题"))
    for r in items:
        log("%-4d %-8s %-8s %s" % (r["idx"], r["dl"] if r["dl"] else "-",
                                   r["kind"], r["title"][:70]))
    if args.json:
        import json
        json.dump(items, open(args.json, "w", encoding="utf-8"),
                  ensure_ascii=False, indent=1)
        log("\n已写出 %s" % args.json)
    return 0


# --------------------------------------------------------------------------- download
def cmd_download(args):
    os.makedirs(args.out, exist_ok=True)
    sess = bsk.Session(sid=args.sid, name="cnki-one", log=log)
    log("会话 sid=%s" % sess.sid)
    item = {"idx": 1, "title": args.name or "cnki", "href": args.url, "kind": ""}
    try:
        res = fetch.grab(sess, item, args.out, log=log, want=args.want)
    finally:
        if not args.sid:
            sess.stop()
    log("\n状态：%s  格式：%s  字节：%s  文件：%s"
        % (res["status"], res.get("format") or "-", res.get("bytes"),
           res.get("file") or "-"))
    if res.get("note"):
        log("备注：%s" % res["note"])
    return 0 if res["status"] == "ok" else 1


# --------------------------------------------------------------------------- batch
def cmd_batch(args):
    out = os.path.abspath(args.out)
    os.makedirs(out, exist_ok=True)
    items = []
    if args.list:
        for line in open(args.list, encoding="utf-8"):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "\t" in line:
                title, url = line.split("\t", 1)
            elif " " in line and line.startswith("http"):
                url, title = line, os.path.basename(line)[:40]
            else:
                title, url = os.path.basename(line)[:40], line
            items.append({"idx": len(items) + 1, "title": title.strip(),
                          "href": url.strip(), "kind": "", "dl": None})
    else:
        if not args.kw:
            log("需要关键词或 --list")
            return 2
        sess = bsk.Session(sid=args.sid, name="cnki-q", log=log)
        log("会话 sid=%s（用于检索）" % sess.sid)
        try:
            items = batch.collect(sess, args.kw, kind=args.type, top=args.top,
                                 pages_n=args.pages, log=log)
        finally:
            if not args.sid:
                sess.stop()
        log("取到 %d 篇，开始下载" % len(items))
    return batch.run(items, out, sid=args.sid, log=log, want=args.want,
                     limit=args.limit)


# --------------------------------------------------------------------------- verify
def cmd_verify(args):
    root = os.path.abspath(args.dir)
    n = {"pdf": 0, "caj": 0, "other": 0}
    rows = []
    for dirpath, _dn, fn in os.walk(root):
        if os.path.basename(dirpath) == "_excluded":
            continue
        for f in sorted(fn):
            if not (f.lower().endswith(".pdf") or f.lower().endswith(".caj")):
                continue
            p = os.path.join(dirpath, f)
            kind, size, _head = fetch.probe(p)
            tag = "PDF" if kind.startswith("pdf") else kind.upper()
            n["pdf" if tag == "PDF" else "caj" if tag in ("CAJ", "KDH", "HN") else "other"] += 1
            rows.append((tag, size, os.path.relpath(p, root), kind))
    for tag, size, rel, kind in rows:
        flag = "" if tag == "PDF" else "   <== 非真 PDF"
        log("%-6s %9d  %s%s" % (tag, size, rel, flag))
    log("\n合计：真 PDF %d / 私有格式 %d / 其他 %d" % (n["pdf"], n["caj"], n["other"]))
    return 0 if n["caj"] == 0 and n["other"] == 0 else 1


# --------------------------------------------------------------------------- selftest
def cmd_selftest(args):
    import tempfile
    ok = 0
    fail = []

    def ck(name, cond, why=""):
        nonlocal ok
        if cond:
            ok += 1
            log("  ✓ %s" % name)
        else:
            fail.append(name)
            log("  ✗ %s  %s" % (name, why))

    log("== 离线自测 ==")
    # 1 检索 URL 必须带 korder=DL（下载量降序）
    u = pages.search_url("农业 文化遗产", "thesis")
    ck("search_url 带 korder=DL", "korder=DL" in u, u)
    ck("search_url 关键词已编码", "%E5%86%9C" in u, u)
    ck("search_url 带 crossids", "crossids=" in u)

    # 2 入口选择器：学位论文 PDF 只能靠父 li 限定（id 与 CAJ 重名）
    ck("PDF 选择器用 li.btn-dlpdf a", pages.SEL_PDF == "li.btn-dlpdf a", pages.SEL_PDF)
    ck("CAJ 选择器排除 chapterDown", ".chapterDown" in pages.SEL_CAJ or "not(" in pages.SEL_CAJ)

    # 3 行类型判定
    ck("硕士行判为 thesis", pages.row_kind("1 某论文 郑某 中南林业科技大学 硕士 2026 103 原版阅读") == "thesis")
    ck("期刊行判为 journal", pages.row_kind("7 某文 李某 某大学学报 2026-10-23 期刊 337 下载 原版阅读") == "journal")
    ck("报纸行判为 newspaper", pages.row_kind("1 某新闻 王某 山西日报 2026-10-30 报纸 下载") == "newspaper")

    # 4 魔数判定（正向 + 反向对照）
    d = tempfile.mkdtemp()
    real = os.path.join(d, "a.pdf")
    open(real, "wb").write(b"%PDF-1.7\n" + b"x" * 30000 + b"\n%%EOF\n")
    caj = os.path.join(d, "b.pdf")
    open(caj, "wb").write(b"CAJ\x00" + b"x" * 30000)
    empty = os.path.join(d, "c.pdf")
    open(empty, "wb").write(b"")
    ck("真 PDF 判为 pdf", fetch.probe(real)[0] == "pdf", fetch.probe(real))
    ck("伪 .pdf(CAJ) 不判为 pdf", fetch.probe(caj)[0] == "caj", fetch.probe(caj))
    ck("空文件判为 empty", fetch.probe(empty)[0] == "empty")

    # 5 文件名净化 / 去重键
    ck("文件名净化", fetch.safe_name('a/b:c*?"<>|d') == "a_b_c______d", fetch.safe_name('a/b:c*?"<>|d'))
    ck("去重键忽略空白与标点", batch.norm_title("农业 文化遗产—保护!") ==
       batch.norm_title("农业文化遗产保护"), batch.norm_title("农业 文化遗产—保护!"))
    ck("去重键能区分不同标题", batch.norm_title("甲") != batch.norm_title("乙"))

    # 6 状态与清单往返
    st = {"done": {}, "items": [{"idx": 1, "title": "测试", "kind": "thesis",
                                 "dl": 12, "status": "ok", "file": os.path.join(d, "a.pdf"),
                                 "bytes": 30008, "format": "PDF", "note": ""}]}
    batch.save_state(d, st)
    ck("状态可写回", batch.load_state(d)["items"][0]["title"] == "测试")
    p = batch.write_manifest(d, st)
    ck("清单已生成", os.path.exists(p) and "测试" in open(p, encoding="utf-8-sig").read())
    return 0 if not fail else 1


# --------------------------------------------------------------------------- main
def main(argv=None):
    ap = argparse.ArgumentParser(prog="cnki-dl", description="知网 PDF 批量下载器")
    sub = ap.add_subparsers(dest="cmd")

    p = sub.add_parser("doctor", help="环境体检")
    p.add_argument("--out")
    p.set_defaults(fn=cmd_doctor)

    p = sub.add_parser("captcha", help="只通过当前页验证码")
    p.add_argument("--sid")
    p.add_argument("--rounds", type=int, default=6)
    p.set_defaults(fn=cmd_captcha)

    p = sub.add_parser("search", help="检索并列出按下载量排序的结果")
    p.add_argument("kw")
    p.add_argument("--type", default="journal", choices=["journal", "thesis", "all"])
    p.add_argument("--top", type=int, default=20)
    p.add_argument("--pages", type=int, default=1)
    p.add_argument("--json")
    p.add_argument("--sid")
    p.set_defaults(fn=cmd_search)

    p = sub.add_parser("download", help="下载单篇")
    p.add_argument("url")
    p.add_argument("--out", default=os.path.join(HERE, "data", "downloads"))
    p.add_argument("--name")
    p.add_argument("--want", default="pdf", choices=["pdf", "caj"])
    p.add_argument("--sid")
    p.set_defaults(fn=cmd_download)

    p = sub.add_parser("batch", help="批量连续下载")
    p.add_argument("kw", nargs="?")
    p.add_argument("--list", help="URL 清单文件（每行 URL 或 标题<TAB>URL）")
    p.add_argument("--type", default="journal", choices=["journal", "thesis", "all"])
    p.add_argument("--top", type=int, default=20)
    p.add_argument("--pages", type=int, default=1)
    p.add_argument("--limit", type=int)
    p.add_argument("--out", default=os.path.join(HERE, "data", "downloads"))
    p.add_argument("--want", default="pdf", choices=["pdf", "caj"])
    p.add_argument("--sid")
    p.set_defaults(fn=cmd_batch)

    p = sub.add_parser("verify", help="按魔数复核目录里的 PDF")
    p.add_argument("dir")
    p.set_defaults(fn=cmd_verify)

    p = sub.add_parser("selftest", help="离线自测")
    p.set_defaults(fn=cmd_selftest)

    a = ap.parse_args(argv)
    if not getattr(a, "fn", None):
        ap.print_help()
        return 2
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
