# cnki-dl —— 知网 PDF 批量下载器（自包含）

一个只用 `bsk`（本地浏览器控制 CLI）+ Python 就能跑的知网批量下载器：
自动过**文字点选**和**拼图**两种验证码，按下载量取热门文献，连续下载**真 PDF**，
**硕士/博士学位论文也走 PDF 通道**（这是本项目一开始就要修的老毛病）。

产物：`<out>/NN_标题.pdf`、`<out>/文献清单.csv`、`<out>/_state.json`（断点续跑）。

## 〇、本机安装状态（2026-11-01 安装）

* 安装位置：`D:\Tools\cnki-dl`（整目录自包含，代码按 `__file__` 相对取路径，可整体搬移）
* 全局命令：`cnki-dl`（`D:\Tools\cnki-dl\cnki-dl.cmd`，已写入**用户 PATH**；
  启动器固定 `BSK_AUTO_START=0`、`PYTHONIOENCODING=utf-8`，调用系统 `C:\Program Files\Python311\python.exe`）
* 默认下载目录：`D:\Tools\cnki-dl\data\downloads`
* 自检：`cnki-dl selftest`（离线，全绿）/ `cnki-dl doctor`（需浏览器在线）
* 新开终端才会识别 PATH；已开着的窗口重启一下即可。

## 一、快速开始


```bash
python run.py doctor                       # 环境体检（bsk / Chrome / 扩展 / 依赖）
python run.py search 安化黑茶 --type thesis --top 10     # 只看队列，不下载
python run.py batch  安化黑茶 --type thesis --top 5 --out D:/out/thesis
python run.py batch  安化黑茶 --type journal --top 5 --out D:/out/journal
python run.py download "<详情页URL>" --out D:/out/one    # 单篇
python run.py verify  D:/out/thesis        # 按文件魔数复核已下载的 PDF
python run.py selftest                     # 离线自测，不需要浏览器
```

前提：`:8188` 之外的 `bsk` 守护进程在跑，Chrome 装了 bsk 扩展（`bsk browsers` 能看到），
并且这台机器上 Python 有 `cv2 / numpy / scipy / rapidocr_onnxruntime`。
调用时建议 `BSK_AUTO_START=0`（别让每条命令都试图拉起 daemon）。

## 二、三个必须知道的实现真相

### 1. 学位论文的 PDF 入口，id 也叫 `cajDown`

以前的脚本找不到硕士论文的 PDF，是因为在找 `a#pdfDown`。实际页面里：

| 入口 | 选择器 | 备注 |
|---|---|---|
| PDF | `li.btn-dlpdf a` | **学位页上它的 id 就是 `cajDown`**，和 CAJ 重名 |
| CAJ | `li.btn-dlcaj a:not(.chapterDown)` | `chapterDown` 是「章节下载」，要排掉 |

而且**每页有 2 组按钮**（页面上下各一组），`document.querySelector` 只取第一个，
所以必须先用 `evaluate` 给第一个打标记（`data-bsk='dl'`，顺手把 `target` 改成 `_self`），
再用 `bsk click "a[data-bsk='dl']"` 做 **trusted 点击** —— 只有真点击才会让 bar 服务端登记订单。

### 2. 直接 `navigate` 到订单 URL 是行不通的

* `https://bar.cnki.net/bar/download/order?id=...` 直接开会报「来源应用不正确(01)」（校验 Referer）。
* `https://bar.cnki.net/bar/verify/index.html?...returnUrl=<order>` 直接开会报「您操作已超时，请重新打开文献！」。

**必须先在详情页 trusted 点击下载按钮**，订单才在服务端登记。点击后有两种结果：

* 期刊页：本标签自己跳到 bar 拼图页 → 就地解；
* 学位页：按钮走 JS 把 bar 开成**新标签**（user 作用域，拿不到控制权）→
  我们用刚点过的那个订单 href 在本标签 `navigate` 到同一个 verify URL，一样能过。

`core/fetch.py:fetch_via_click()` 就是这两条路的合并实现。

### 3. 拼图几何：缺口要按「可见区」换算

腾讯 TCaptcha 的背景图是整幅雪碧图，靠 `background-size/position` 裁出可见区。
正确做法是先按 `background-size` 反算显示宽度、把可见区裁出来，再在裁出来的图上找缺口：

* `k_bg = 可见宽 CSS / 背景图像素宽`（实测 ≈ 0.49107）
* `gap_css = gap.x(裁切坐标) * k_bg`
* `piece_left = fgLeft + (fg.bgPos.x + box.x * k_fg)`
* 缺口高度与拼图块可见高度一致 → 用 `y_hint`（块顶边 y）把暗度扫描限制在 ±16px，
  能把误检率压到几乎为 0（实测 `inside≈0.92 quality≈0.89` 就是真缺口）。

滑块位移与拼图块位移是 **1:1** 的（dx=150 → 块 left 24.5536→174.554），不需要再标定。

## 三、时间与稳定性（用户明确要求：验证码有时效）

* `plan()` 里所有图片下载都走 **不走系统代理** 的 opener（`build_opener(ProxyHandler({}))`）。
  走系统代理时每张验证码图要 4.6s、`plan` 要 9.4s；改直连后 ~0.08s/张、`plan` ~0.2s。
* 一次拼图「取图 → 算缺口 → 拖动 → 判定」实测 1.1~1.5s，远小于 bar 的超时窗口。
* 同一张图连续两次解不出就 `location.reload()` 换题，不做无用重试。
* 文字点选实测连续 5+ 次全部一次过（读字置信度 0.97~1.00）。

### 为什么每篇都要开一个新会话

bar 的验证/下载页会把标签页「焊」在 `bar.cnki.net` 上：之后 `navigate` 会被拽回来，
表现为「导航未确认到目标页 → 又回到拼图页」的死循环（实测单篇能烧掉 8 分钟）。

所以 `batch.run()` **每篇新开一个 `bsk.Session`**（新 agent 标签），下完就 `stop()`。
同理，`fresh_tab()` 里**不能用 `about:blank`**：那个页面没有扩展 content script，
`evaluate` 会报 `Could not establish connection. Receiving end does not exist`。

## 四、代码结构

```
run.py            命令行入口（doctor/captcha/search/download/batch/verify/selftest）
core/bsk.py       bsk 封装：Session（start/ctx/ev/nav/fresh_tab/stop）、TRANS 抖动重试
core/pages.py     检索页：search_url/rows_of/norm_rows/row_kind/switch/detail/is_verify
core/captcha.py   拼图验证码：JS_GEO/bg_crop/find_gap/plan/drag/pass_captcha/auto_pass
core/clickword.py 文字点选验证码：纯色抠字 + 旋转扫描 RapidOCR + 合成 MouseEvent 点击
core/fetch.py     详情页：_wait_nav/open_detail/click_pdf/fetch_via_click/grab/probe
core/batch.py     队列：collect（切库/排序/去重）+ run（断点续跑/清单）+ RESUME_AT
```

### 切库的两个坑

* 只能对 `<a name=classify resource=DISSERTATION|JOURNAL>` 派发 `mousedown/mouseup/click`；
  点外层 `li.haschild` 无效，改 URL 的 `classid` 会 400。
* 切库是**前端路由**：URL 不变、只换列表内容。切完后列表**没有「类型」列**，
  所以判定成功靠「行内类型文字」或「列数变化」（总库 9 列、学术期刊 8 列）。
  下载量也不能写死取 `td[7]`，要取「最后一个纯数字单元格」。

## 五、排错

| 现象 | 原因 / 处理 |
|---|---|
| `shared page control or observation changed` | 页面有轮播/广告，常态，重试 2 次必过 |
| `Could not establish connection` | 当前标签是 `about:blank` 之类没 content script 的页，换真实 URL |
| 一直「导航未确认到目标页」 | 标签被 bar 焊住了，换新会话（`batch` 已自动这么做） |
| 详情页「无下载入口」 | 先确认导航真的落地了（`_wait_nav`），再看 `li.btn-dlpdf a` 数量 |
| 下载目录里是 `.crdownload` | 还没下完，`_wait_new_file` 会等到大小稳定 |
| 拿到的是 CAJ | 该篇只有 CAJ，`want=pdf` 时记为 `caj-only` 并跳过 |

下载目录不是猜的：`chrome_dl_dir()` 读 Chrome `Preferences` 里的 `download.default_directory`
（本机是 `D:\智达云`）。

## 六、实测记录（2026-10-31 ~ 11-01）

* 期刊批次 `安化黑茶 --type journal --top 3`：**3/3 成功**，61~99s/篇，1.21/0.77/1.12 MB。
* 学位批次 `安化黑茶 --type thesis --top 3`：见 `data/cnki/thesis`（硕士论文 PDF 7.38MB 起）。
* 单篇最长链路：详情页文字点选 → 下载按钮 trusted 点击 → bar 拼图 1.1s → Chrome 原生下载 → 中文名落盘。


## 七、更新记录

### 0.1.2（2026-11-06，bugfix）

两处来自真机批量下载的稳定性修复，已在「跨区域交通基础设施 × 国土空间规划」18 篇
批次与单篇复测中验证（单篇 68.1s、无失败）：

* **`Session.start()` 的 session id 解析**：bsk CLI 会把「发现新版本」提示写到
  stderr，而 `run()` 把 stdout+stderr 拼在一起；旧实现不带 `--json`，兜底取「末行
  最后一词」会拿到 `supervisor.` 当 sid，导致之后所有 `--session` 动作挂起。改为
  `session start --json` 结构化取 `session_id`，兜底只认「独立成行的 4 位字母数字」。
* **`fetch_via_click()` 跳过二次验证**：CNKI 期刊页下载按钮常把 bar 验证页开成
  「新标签」，那个新标签已登记订单并让 Chrome 原生下载 PDF；受控页若再 navigate
  回同一订单 URL，bar 视为订单已消费，弹「您操作已超时，请重新打开文献！」并出
  一道解不开的拼图，每篇白烧几十秒。改为「文件优先」：观察窗内先盯下载目录，
  新标签下好就直接收、跳过二次验证；仅当确无原生下载时才退回老路径（学位论文
  页／受控标签真停在 bar 验证页）。
