# -*- coding: utf-8 -*-
"""DOI 批量导入 → Unpaywall OA 补源 →（兜底）直链 → 落盘 PDF。

移植 wenxian-helper `sidepanel/index.js` 的 `parseDois` / `fetchPdfByDoi`：
* DOI 提取一条正则全文扫，按小写去重保首现大小写。
* 两步兜底：① Unpaywall 取元数据 + `best_oa_location.url_for_pdf`（只此一路）；
  ② 无 OA 再试 sci.bban.top 直链，用 `Range: bytes=0-1023` 只取头部验 `%PDF-`。
* 直链 urllib 落盘，**完全绕开**知网浏览器/验证码那一套。

文件名净化复用本机 `fetch.safe_name`，保持与知网下载一致。
"""
import json
import os
import re
import urllib.request

from . import fetch as _fetch

DOI_RE = re.compile(r"10\.[0-9]{4,9}/[-._;()/:a-zA-Z0-9]+")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
BROWSER_HEADERS = {
    "User-Agent": UA,
    "Accept": "application/pdf,*/*",
    "Accept-Language": "zh-CN,zh;q=0.9",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
}
DEFAULT_EMAIL = "cnkihelper@heykee.com"  # Unpaywall 要求带 email 参数


def parse_dois(text):
    """自由文本里抽 DOI，小写去重保首现。"""
    seen, out = set(), []
    for d in DOI_RE.findall(text or ""):
        k = d.lower()
        if k not in seen:
            seen.add(k)
            out.append(d)
    return out


def _get(url, timeout, rng=False):
    req = urllib.request.Request(url, headers=dict(BROWSER_HEADERS))
    if rng:
        req.add_header("Range", "bytes=0-1023")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, dict(r.headers), r.read()


def _is_pdf_head(head_bytes, ctype):
    return head_bytes.lstrip()[:6].startswith(b"%PDF-") and \
        not re.search(r"html|json|xml", ctype or "", re.I)


def fetch_by_doi(doi, email=DEFAULT_EMAIL, log=print):
    """返回 dict(title, authors, source, year, pdf_link, pdf_source) 或 None。"""
    meta = {"title": "", "authors": "", "source": "", "year": ""}
    link, src = "", ""
    # ① Unpaywall：元数据 + best_oa_location.url_for_pdf
    try:
        u = "https://api.unpaywall.org/v2/%s?email=%s" % (doi, email)
        _st, _hd, body = _get(u, 5)
        data = json.loads(body)
        meta["title"] = data.get("title") or ""
        meta["source"] = data.get("journal_name") or ""
        meta["year"] = str(data.get("year") or "")
        meta["authors"] = "; ".join(
            " ".join(x for x in [a.get("given"), a.get("family")] if x) or a.get("name") or ""
            for a in (data.get("z_authors") or []))
        oa = ((data.get("best_oa_location") or {}).get("url_for_pdf")) or ""
        if oa.startswith("http"):
            link, src = oa, "Unpaywall"
    except Exception as ex:
        log("    · Unpaywall 失败 %s：%s" % (doi, ex))
    # ② 兜底直链：只取头部验魔数
    if not link:
        bban = "https://sci.bban.top/pdf/%s.pdf?download=true" % (
            doi.replace("?", "%3F").replace("#", "%23"))
        try:
            _st, hd, head = _get(bban, 10, rng=True)
            if _is_pdf_head(head, hd.get("Content-Type", "")):
                link, src = bban, "Sci-Hub"
        except Exception as ex:
            log("    · 直链兜底失败 %s：%s" % (doi, ex))
    if link:
        return dict(meta, pdf_link=link, pdf_source=src)
    if meta["title"]:
        return dict(meta, pdf_link="", pdf_source="")
    return None


def download(link, dst, log=print):
    """直链落盘 PDF，带魔数校验。返回 batch 兼容的 result dict。"""
    try:
        _st, _hd, data = _get(link, 60)
    except Exception as ex:
        return {"status": "fail", "note": str(ex)}
    if not data[:6].startswith(b"%PDF-"):
        return {"status": "fail", "note": "非 PDF 内容（可能撞到 HTML/校验页）"}
    os.makedirs(os.path.dirname(dst) or ".", exist_ok=True)
    with open(dst, "wb") as f:
        f.write(data)
    return {"status": "ok", "file": dst, "bytes": len(data), "format": "PDF"}


def safe_name(s):
    return _fetch.safe_name(s)
