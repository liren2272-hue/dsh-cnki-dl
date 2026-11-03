# -*- coding: utf-8 -*-
"""知网页面层：检索 URL、结果行解析、库切换（期刊/学位论文）、详情页下载入口。"""

CROSS = ("YSTT4HG0%2CLSTPFY1C%2CEMRPGLPA%2CJUP3MUPD%2CMPMFIG1A%2CWQ0UVIAA%2CBLZOG7CK"
         "%2CPWFIRAGL%2CNLBO1Z6R%2CNN3FJMUV")
BASE = "https://kns.cnki.net/kns8s/defaultresult/index?crossids=" + CROSS + "&korder=DL&kw="

# 检索页上的库切换按钮：<a name="classify" classid=... resource=...>
CLASSIFY = {"journal": "JOURNAL", "thesis": "DISSERTATION", "all": ""}

# 详情页下载入口：
#   期刊页  li.btn-dlpdf > a#pdfDown
#   学位页  li.btn-dlpdf > a（id 竟然也叫 cajDown，和 CAJ 那个重名 → 只能用父 li 限定）
SEL_PDF = "li.btn-dlpdf a"
SEL_CAJ = "li.btn-dlcaj a:not(.chapterDown)"

JS_ROWS = r"""(function(){
  var rs=[...document.querySelectorAll('table tbody tr')];
  return JSON.stringify({u:location.href, t:document.title, n:rs.length,
    rows: rs.map(function(tr){
      var td=[...tr.querySelectorAll('td')];
      var a=tr.querySelector('a');
      var txt=(tr.innerText||'').replace(/\s+/g,' ').trim();
      /* 各库的列数不一样（总库 9 列、学术期刊 8 列），下载量取「最后一个纯数字单元格」 */
      var k=-1;
      for(var i=td.length-1;i>=0;i--){var v=(td[i].innerText||'').replace(/\s+/g,'').trim();
        if(/^\d+$/.test(v)){k=i;break;}}
      var kindTd='';
      for(var j=td.length-1;j>=0;j--){var w=(td[j].innerText||'').replace(/\s+/g,' ').trim();
        if(/期刊|报纸|硕士|博士|会议|图书|年鉴/.test(w)){kindTd=w;break;}}
      return {txt:txt, kt:kindTd, title:(td[1]?td[1].innerText.trim():'').replace(/\s+/g,' '),
              href:a?a.href:'', dl:(k>=0?(td[k].innerText||'').replace(/\s+/g,'').trim():'')};})});})()"""

JS_CLASSIFY = r"""(function(){
  var res="__RES__";
  var a=document.querySelector('a[name=classify][resource="'+res+'"]');
  if(!a){var ls=document.querySelectorAll('a[name=classify]');
    for(var i=0;i<ls.length;i++){if((ls[i].getAttribute('resource')||'').toUpperCase()===res.toUpperCase()){a=ls[i];break;}}}
  if(!a)return JSON.stringify({ok:false});
  var r=a.getBoundingClientRect();
  ['mousedown','mouseup','click'].forEach(function(ty){
    a.dispatchEvent(new MouseEvent(ty,{bubbles:true,cancelable:true,view:window,
      clientX:r.left+r.width/2,clientY:r.top+r.height/2}));});
  return JSON.stringify({ok:true,resource:res,cls:(a.className||''),txt:(a.textContent||'').replace(/\s+/g,' ').trim().slice(0,14)});})()"""


JS_DETAIL = r"""(function(){
  var q=function(s){return document.querySelector(s)};
  var pick=function(sel){var e=q(sel);if(!e)return null;
    return {txt:(e.innerText||'').replace(/\s+/g,' ').trim().slice(0,20),
            href:e.getAttribute('href')||''};};
  var pdf=pick('li.btn-dlpdf a'), caj=pick('li.btn-dlcaj a:not(.chapterDown)');
  var pdfDown=pick('a#pdfDown');
  /* 标题：详情页上有 <h1>自动登录</h1> 这种干扰项，取 document.title 去掉站名最稳 */
  var t=(document.title||'').replace(/\s*-\s*中国知网\s*$/,'').trim();
  var h1s=[...document.querySelectorAll('h1')].map(function(e){return (e.innerText||'').replace(/\s+/g,' ').trim()})
    .filter(function(x){return x && x!=='自动登录' && x.length>3});
  if(!t || t.length<3) t=h1s[0]||'';
  return JSON.stringify({u:location.href.slice(0,220), t:document.title, title:t,
    pdf:pdf, caj:caj, pdfDown:pdfDown,
    has_pdf:!!(pdf||pdfDown), has_caj:!!caj});})()"""


def search_url(kw, kind="journal"):
    import urllib.parse as up
    return BASE + up.quote(kw)


JS_COLS = "(function(){var tr=document.querySelector('table tbody tr');return JSON.stringify({c:tr?tr.querySelectorAll('td').length:0,n:document.querySelectorAll('table tbody tr').length});})()"


def row_cols(sess):
    """当前列表每行几列 + 行数。切库是前端路由，URL 不变，但列数会变
    （总库 9 列、学术期刊 8 列），用它当「切成功」的旁证。"""
    from .bsk import js_call
    d = js_call(sess, JS_COLS) or {}
    return d.get("c") or 0, d.get("n") or 0


def rows_of(sess):
    """当前结果页的表格行。"""
    from .bsk import js_call
    d = js_call(sess, JS_ROWS) or {}
    return d.get("rows") or []


def row_kind(txt, kt=""):
    """从行文本判断文献类型。"学位论文" 行会带 硕士/博士。"""
    if kt:
        for k in ("硕士", "博士"):
            if k in kt:
                return "thesis"
        for k, v in (("期刊", "journal"), ("报纸", "newspaper"),
                     ("会议", "conference"), ("图书", "book"), ("年鉴", "almanac")):
            if k in kt:
                return v
    for k in ("硕士", "博士"):
        if k in txt:
            return "thesis"
    if "期刊" in txt:
        return "journal"
    if "报纸" in txt:
        return "newspaper"
    if "会议" in txt:
        return "conference"
    if "图书" in txt:
        return "book"
    return "other"


def norm_rows(sess):
    """规整成 [{title, href, dl, kind, txt}]，丢掉没有链接的行。"""
    out = []
    for i, r in enumerate(rows_of(sess), 1):
        href = (r.get("href") or "").strip()
        if not href:
            continue
        title = (r.get("title") or "").strip()
        if not title:
            # td[1] 偶发取不到时退化为去掉序号后的行文本
            txt = (r.get("txt") or "")
            title = txt.split(" ")[1] if len(txt.split(" ")) > 1 else txt
        dl = (r.get("dl") or "").strip()
        out.append({"idx": i, "title": title, "href": href,
                    "dl": int(dl) if dl.isdigit() else None,
                    "kind": row_kind(r.get("txt") or "", r.get("kt") or ""),
                    "txt": r.get("txt") or ""})
    return out


def switch(sess, kind, wait=3.0, tries=6):
    """把结果列表切到指定库（journal / thesis）。返回是否切换成功。

    ⚠ 只能点 `<a name=classify resource=...>` 并派发 mousedown/mouseup/click；
    点外层 `li.haschild` 无效。切换是前端路由：URL 不变、只换列表内容，
    而且切完的行没有「类型」列，所以判定靠「行内类型文字」或「列数变化」。
    """
    import time
    from .bsk import js_call
    res = CLASSIFY.get(kind, "")
    if not res:
        return True
    js = JS_CLASSIFY.replace("__RES__", res)
    c0, n0 = row_cols(sess)
    for _ in range(tries):
        d = js_call(sess, js) or {}
        if not d.get("ok"):
            time.sleep(2)
            continue
        for _w in range(5):
            time.sleep(wait)
            rows = norm_rows(sess)
            if rows:
                hit = sum(1 for r in rows if r["kind"] == kind)
                if hit >= max(3, len(rows) * 0.8):
                    return True
            c1, n1 = row_cols(sess)
            if n1 and (c1, n1) != (c0, n0):
                return True
        wait = 2.0
    return False


def detail(sess):
    from .bsk import js_call
    return js_call(sess, JS_DETAIL) or {}


JS_VERIFY = r"""(function(){
  var q=function(s){return document.querySelector(s)};
  return JSON.stringify({u:location.href.slice(0,220), t:document.title,
    cw:!!q('.verify-img-panel img'),
    slider:!!q('.tencent-captcha-dy__slider-block'),
    body:(document.body?document.body.innerText:'').replace(/\s+/g,' ').slice(0,160)});})()"""


def is_verify(sess):
    """当前页是不是验证码页。

    ⚠ 详情页正文里**内嵌**了一个 188x114 的 `.tencent-captcha-dy__image-area` 容器
    （下载时才启用），所以只看元素存在会 100% 误判。判定只认：
    URL 里有 /verify/、标题含「安全验证」、文字点选面板、或滑块按钮出现。
    """
    from .bsk import js_call
    d = js_call(sess, JS_VERIFY) or {}
    u, t = d.get("u", "") or "", d.get("t", "") or ""
    hit = ("/verify/" in u or "安全验证" in t or "安全验证" in (d.get("body") or "")
           or d.get("cw") or d.get("slider"))
    return bool(hit), d
