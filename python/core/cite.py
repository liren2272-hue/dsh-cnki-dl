# -*- coding: utf-8 -*-
"""文献题录导出：统一内部记录模型 → GB/T 7714（国标，默认）/ BibTeX / RIS。

用户硬要求：**之后所有论文信息输出都跟国标（GB/T 7714）**。所以 `gb7714()` 是
首选路径；BibTeX / RIS 供 Zotero / EndNote / LaTeX 用户导入。

内部记录 CiteRec 字段（缺什么就留空，导出时自动跳过空字段）：
    title, authors(list[str]), kind("journal"/"thesis"/...), year, source,
    volume, issue, pages, publisher, address, doi, url, language("zh"/"en"),
    thesis_level("硕士"/"博士"/"master"/"doctor")

参考文献：
* GB/T 7714-2015《信息与文献 参考文献著录规则》
* 文献类型标识：M(图书) J(期刊) D(学位论文) C(会议录) N(报纸) R(报告) S(标准) P(专利) EB/OL(电子资源)
"""
import re

# 文献类型标识（GB/T 7714-2015 表 1）
TYPE_CODE = {"journal": "J", "thesis": "D", "book": "M", "proceedings": "C",
             "newspaper": "N", "report": "R", "standard": "S", "patent": "P",
             "web": "EB/OL"}
# BibTeX entry type
BIB_TYPE = {"journal": "article", "thesis": "phdthesis", "book": "book",
            "proceedings": "inproceedings", "newspaper": "article",
            "report": "techreport", "web": "misc"}
# RIS TY
RIS_TY = {"journal": "JOUR", "thesis": "THES", "book": "BOOK",
          "proceedings": "CONF", "newspaper": "NEWS", "report": "RPRT",
          "web": "ELEC"}
# 学位论文 BibTeX school/level
_THESIS_LEVEL_EN = {"硕士": "master", "博士": "doctor", "master": "master",
                    "doctor": "doctor", "phd": "doctor"}


class CiteRec(object):
    """统一题录记录。所有字段可选。"""
    def __init__(self, title="", authors=None, kind="journal", year="", source="",
                 volume="", issue="", pages="", publisher="", address="",
                 doi="", url="", language="", thesis_level=""):
        self.title = title or ""
        self.authors = list(authors or [])
        self.kind = kind or "journal"
        self.year = str(year or "")
        self.source = source or ""
        self.volume = str(volume or "")
        self.issue = str(issue or "")
        self.pages = str(pages or "")
        self.publisher = publisher or ""
        self.address = address or ""
        self.doi = doi or ""
        self.url = url or ""
        self.language = language or _guess_lang(self.title)
        self.thesis_level = thesis_level or ""


def _guess_lang(title):
    return "zh" if re.search(r"[\u4e00-\u9fff]", title or "") else "en"


def _is_zh(rec):
    return rec.language == "zh" or _guess_lang(rec.title) == "zh"


# --------------------------------------------------------------------------- GB/T 7714
def _gb_authors(rec):
    """作者串：3 位以内全列、超 3 位取前 3 加「等/et al」；姓前名后逗号分隔。"""
    a = [x.strip() for x in rec.authors if x and x.strip()]
    if not a:
        return ""
    if len(a) > 3:
        head = a[:3]
        tail = "等" if _is_zh(rec) else "et al"
    else:
        head, tail = a, ""
    s = ", ".join(head)
    return (s + ", " + tail) if tail else s


def gb7714(rec):
    """按 GB/T 7714-2015 生成一条参考文献字符串（含文献类型标识 [J]/[D]…）。"""
    code = TYPE_CODE.get(rec.kind, "J")
    au = _gb_authors(rec)
    au_part = (au + ". ") if au else ""
    is_zh = _is_zh(rec)
    if rec.kind == "thesis":
        # 作者. 题名[D]. 保存地: 保存单位, 年.
        loc = ("%s: %s" % (rec.address, rec.source)) if rec.address else rec.source
        return "%s%s[D]. %s, %s." % (au_part, rec.title, loc, rec.year)
    if rec.kind == "book":
        loc = ("%s: %s" % (rec.address, rec.publisher)) if rec.address else rec.publisher
        return "%s%s[M]. %s, %s." % (au_part, rec.title, loc, rec.year)
    # 期刊（默认）：作者. 题名[J]. 刊名, 年, 卷(期): 起止页.
    tail = rec.source
    if rec.volume:
        tail += ", " + rec.volume
        if rec.issue:
            tail += "(%s)" % rec.issue
    elif rec.issue:
        tail += ", (%s)" % rec.issue
    if rec.year:
        tail = (tail + ", " + rec.year) if tail else rec.year
    if rec.pages:
        tail += ": " + rec.pages
    return "%s%s[%s]. %s." % (au_part, rec.title, code, tail)


# --------------------------------------------------------------------------- BibTeX
def _bib_key(rec, idx=0):
    """生成 cite key：第一作者姓+年+题名首词。"""
    a0 = (rec.authors[0] if rec.authors else "anon")
    a0 = re.split(r"[\s,]+", a0.strip())[0] or "anon"
    a0 = re.sub(r"[^A-Za-z0-9\u4e00-\u9fff]", "", a0) or "anon"
    t0 = re.sub(r"[^A-Za-z0-9\u4e00-\u9fff]", "", (rec.title or "ref")[:12])
    return "%s%s%s" % (a0, rec.year or "", t0)


def _bib_esc(s):
    return (s or "").replace("{", "\\{").replace("}", "\\}").replace("&", "\\&")


def _bib_authors(rec):
    # BibTeX 用 " and " 连接；英文姓前名后用 "Last, First"
    return " and ".join(a.strip() for a in rec.authors if a and a.strip())


def bibtex(rec, key=None):
    t = BIB_TYPE.get(rec.kind, "article")
    k = key or _bib_key(rec)
    fields = []
    if rec.authors:
        fields.append("  author       = {%s}" % _bib_authors(rec))
    fields.append("  title        = {%s}" % _bib_esc(rec.title))
    if rec.kind == "thesis":
        if rec.source:
            fields.append("  school       = {%s}" % _bib_esc(rec.source))
        if rec.address:
            fields.append("  address      = {%s}" % _bib_esc(rec.address))
        lvl = _THESIS_LEVEL_EN.get((rec.thesis_level or "").lower())
        if lvl:
            fields.append("  type         = {%s thesis}" % lvl)
    elif rec.kind == "book":
        if rec.publisher:
            fields.append("  publisher    = {%s}" % _bib_esc(rec.publisher))
        if rec.address:
            fields.append("  address      = {%s}" % _bib_esc(rec.address))
    else:
        if rec.source:
            fields.append("  journal      = {%s}" % _bib_esc(rec.source))
        if rec.volume:
            fields.append("  volume       = {%s}" % rec.volume)
        if rec.issue:
            fields.append("  number       = {%s}" % rec.issue)
        if rec.pages:
            fields.append("  pages        = {%s}" % rec.pages)
    if rec.year:
        fields.append("  year         = {%s}" % rec.year)
    if rec.doi:
        fields.append("  doi          = {%s}" % rec.doi)
    if rec.url:
        fields.append("  url          = {%s}" % rec.url)
    fields.append("  language     = {%s}" % ("chinese" if _is_zh(rec) else "english"))
    return "@%s{%s,\n%s\n}" % (t, k, ",\n".join(fields))


# --------------------------------------------------------------------------- RIS
def _ris_first_last(name):
    """英文「Last, First」→ RIS 的 last/first 两段；中文整串进 last。"""
    if _guess_lang(name) == "zh":
        return name, ""
    if "," in name:
        last, first = name.split(",", 1)
        return last.strip(), first.strip()
    parts = name.split()
    if len(parts) >= 2:
        return parts[-1], " ".join(parts[:-1])
    return name, ""


def ris(rec):
    L = []
    ty = RIS_TY.get(rec.kind, "JOUR")
    L.append("TY  - %s" % ty)
    for a in rec.authors:
        if not (a and a.strip()):
            continue
        L.append("AU  - %s" % a.strip())
    L.append("TI  - %s" % rec.title)
    if rec.source:
        if rec.kind == "thesis":
            L.append("PB  - %s" % rec.source)
        elif rec.kind == "book":
            L.append("PB  - %s" % (rec.publisher or rec.source))
        else:
            L.append("JO  - %s" % rec.source)
    if rec.volume:
        L.append("VL  - %s" % rec.volume)
    if rec.issue:
        L.append("IS  - %s" % rec.issue)
    if rec.pages:
        if "-" in rec.pages:
            sp = rec.pages.split("-", 1)
            L.append("SP  - %s" % sp[0].strip())
            L.append("EP  - %s" % sp[1].strip())
        else:
            L.append("SP  - %s" % rec.pages)
    if rec.publisher:
        L.append("PB  - %s" % rec.publisher)
    if rec.year:
        L.append("PY  - %s" % rec.year)
    if rec.doi:
        L.append("DO  - %s" % rec.doi)
    if rec.url:
        L.append("UR  - %s" % rec.url)
    L.append("LA  - %s" % ("zh" if _is_zh(rec) else "en"))
    L.append("ER  - ")
    return "\n".join(L)


# --------------------------------------------------------------------------- 批量导出
def _as_author_list(v):
    """authors 可能是 list，也可能是 ';'/','/',' 连接的字符串（DOI/题录导出常给字符串）。"""
    if not v:
        return []
    if isinstance(v, str):
        v = re.split(r"[;,，;]", v)
    out = []
    for a in v:
        a = (a or "").strip()
        if a:
            out.append(a)
    return out


def to_records(items, default_kind="journal"):
    """把 batch/done 里的记录 dict 列表转成 CiteRec 列表。容忍缺字段。"""
    recs = []
    for it in items:
        if not isinstance(it, dict):
            continue
        title = it.get("title") or ""
        if not title:
            continue
        recs.append(CiteRec(
            title=title,
            authors=_as_author_list(it.get("authors")),
            kind=it.get("kind") or default_kind,
            year=it.get("year") or "",
            source=it.get("source") or it.get("journal") or "",
            volume=it.get("volume") or "",
            issue=it.get("issue") or "",
            pages=it.get("pages") or "",
            publisher=it.get("publisher") or "",
            address=it.get("address") or "",
            doi=it.get("doi") or "",
            url=it.get("href") or it.get("url") or "",
            language=it.get("language") or "",
            thesis_level=it.get("thesis_level") or ""))
    return recs


def write_all(out_dir, recs, stem="references"):
    """一次写出三份：references.txt(GB/T 7714)、references.bib、references.ris。
    返回 {fmt: path}。"""
    import os
    paths = {}
    if not recs:
        return paths
    # txt：GB/T 7714，带 [1] [2] 序号
    p = os.path.join(out_dir, stem + ".txt")
    with open(p, "w", encoding="utf-8") as f:
        for i, r in enumerate(recs, 1):
            f.write("[%d] %s\n" % (i, gb7714(r)))
    paths["gb7714"] = p
    # bib
    p = os.path.join(out_dir, stem + ".bib")
    with open(p, "w", encoding="utf-8") as f:
        f.write("\n\n".join(bibtex(r, key=_bib_key(r, i)) for i, r in enumerate(recs)))
        f.write("\n")
    paths["bibtex"] = p
    # ris
    p = os.path.join(out_dir, stem + ".ris")
    with open(p, "w", encoding="utf-8") as f:
        f.write("\n".join(ris(r) for r in recs))
        f.write("\n")
    paths["ris"] = p
    return paths
