"""
fetch_sources.py — 抓取原始内容(论文 + 国内外大厂动态)
输出 data/raw.json,交给 explain.py 做 AI 解读。

设计原则:
- arXiv 走官方 API(稳定、免 key)
- 公司动态走可配置 feed 列表(RSS / GitHub releases),任何一个拉不到就跳过,不影响整体
- 只抓元信息(标题/摘要/链接),不抓正文图片,规避版权与排版风险
"""
import json, time, datetime, sys, urllib.request, urllib.error
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
UA = {"User-Agent": "ai-daily-digest/1.0 (personal learning project)"}
TIMEOUT = 20

# ============ 配置区:你可随时增删 ============

# 论文来源策略:
#   主源 = Hugging Face Daily Papers(社区投票的每日热门 AI 论文,带 upvotes 热度)
#   兜底 = arXiv 最新(主源拿不到时补充)
# 先抓较多候选,再交给 rank_papers.py 用 DeepSeek 打分精选出 Top N。
HF_DAILY_API = "https://huggingface.co/api/daily_papers"
HF_CANDIDATES = 20         # 从 HF 取多少篇热门候选

ARXIV_CATEGORIES = ["cs.AI", "cs.LG", "cs.CL"]
ARXIV_PER_CAT = 6          # arXiv 兜底:每分类取几篇
ARXIV_CANDIDATES = 10      # arXiv 候选上限

PAPER_CANDIDATES_TOTAL = 24  # 论文候选总数上限(交给打分阶段)

# 公司动态 feed 列表。type 决定归类(news / report)
# kind: "rss" 标准 RSS/Atom;"github" 抓 GitHub 组织 releases
FEEDS = [
    # ---- 国外 · 前沿实验室 ----
    {"name": "OpenAI",         "kind": "rss", "url": "https://openai.com/blog/rss.xml",                 "type": "news", "region": "海外"},
    {"name": "Anthropic",      "kind": "rss", "url": "https://www.anthropic.com/rss.xml",               "type": "news", "region": "海外"},
    {"name": "Google DeepMind","kind": "rss", "url": "https://deepmind.google/blog/rss.xml",            "type": "news", "region": "海外"},
    {"name": "Meta AI",        "kind": "rss", "url": "https://ai.meta.com/blog/rss/",                   "type": "news", "region": "海外"},
    {"name": "Mistral AI",     "kind": "rss", "url": "https://mistral.ai/news/feed.xml",                "type": "news", "region": "海外"},
    {"name": "Hugging Face",   "kind": "rss", "url": "https://huggingface.co/blog/feed.xml",            "type": "news", "region": "海外"},
    {"name": "Google Research","kind": "rss", "url": "https://research.google/blog/rss/",               "type": "news", "region": "海外"},
    {"name": "Apple ML",       "kind": "rss", "url": "https://machinelearning.apple.com/rss.xml",       "type": "news", "region": "海外"},
    {"name": "NVIDIA",         "kind": "rss", "url": "https://blogs.nvidia.com/blog/category/deep-learning/feed/", "type": "news", "region": "海外"},
    {"name": "Cohere",         "kind": "rss", "url": "https://cohere.com/blog/rss.xml",                 "type": "news", "region": "海外"},
    {"name": "AWS ML",         "kind": "rss", "url": "https://aws.amazon.com/blogs/machine-learning/feed/", "type": "news", "region": "海外"},
    {"name": "AI2",            "kind": "rss", "url": "https://allenai.org/blog/feed.xml",               "type": "news", "region": "海外"},

    # ---- 国内 · 前沿团队(优先 GitHub releases,RSS 多不稳定) ----
    {"name": "DeepSeek",       "kind": "github", "url": "deepseek-ai",  "type": "news", "region": "国内"},
    {"name": "通义千问 Qwen",  "kind": "github", "url": "QwenLM",        "type": "news", "region": "国内"},
    {"name": "智谱 GLM",       "kind": "github", "url": "THUDM",         "type": "news", "region": "国内"},
    {"name": "月之暗面 Kimi",  "kind": "github", "url": "MoonshotAI",    "type": "news", "region": "国内"},
    {"name": "百川 Baichuan",  "kind": "github", "url": "baichuan-inc",  "type": "news", "region": "国内"},
    {"name": "MiniMax",        "kind": "github", "url": "MiniMax-AI",    "type": "news", "region": "国内"},
    {"name": "阶跃星辰 Step",  "kind": "github", "url": "stepfun-ai",    "type": "news", "region": "国内"},
    {"name": "零一万物 Yi",    "kind": "github", "url": "01-ai",         "type": "news", "region": "国内"},
    {"name": "上海 AI Lab",    "kind": "github", "url": "OpenGVLab",     "type": "news", "region": "国内"},
    {"name": "智源 BAAI",      "kind": "github", "url": "FlagOpen",      "type": "news", "region": "国内"},

    # ---- 报告类 ----
    {"name": "Stanford HAI",   "kind": "rss", "url": "https://hai.stanford.edu/news/rss.xml",           "type": "report", "region": "海外"},
]

NEWS_KEEP = 4      # 公司动态最终保留条数
RECENT_DAYS = 3    # 只要最近 N 天内的条目

# ============ 工具函数 ============

def fetch_url(url):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return r.read()

def parse_date(s):
    if not s:
        return None
    s = s.strip()
    for fmt in ("%a, %d %b %Y %H:%M:%S %z", "%a, %d %b %Y %H:%M:%S %Z",
                "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d"):
        try:
            d = datetime.datetime.strptime(s, fmt)
            return d.replace(tzinfo=None)
        except ValueError:
            continue
    return None

def is_recent(d):
    if d is None:
        return True  # 拿不到日期时不卡,交给后续按顺序取
    return (datetime.datetime.now() - d).days <= RECENT_DAYS


def extract_paper_figure(arxiv_id, timeout=8):
    """尝试从 arXiv HTML 版论文提取首图 URL。取不到返回 None(降级,不报错)。
    只存 URL,不下载转存。

    考虑的边界情况:
    - img 标签 src 可能用单/双引号、属性顺序不定
    - 排除 logo、icon、数学公式 PNG、KaTeX、装饰图等
    - 路径可能是绝对/协议相对/根相对/纯相对,带不带查询串
    - HTML 实体编码(&amp;)需要解码
    - 论文可能没 HTML 版(404)、超时、编码异常
    - 提取后做"长得像内容图"的启发式判断(扩展名、尺寸暗示等)
    """
    import re, html as htmllib
    # 清理 id:去版本号、去空白
    base_id = re.sub(r"v\d+$", "", (arxiv_id or "").strip())
    if not base_id:
        return None
    html_url = f"https://arxiv.org/html/{base_id}"

    # 单独短超时,避免某篇卡死拖垮整个流水线
    try:
        req = urllib.request.Request(html_url, headers=UA)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8", "ignore")
    except Exception as e:
        print(f"    [图] {base_id} 无 HTML 版或取不到,跳过:{e}", file=sys.stderr)
        return None

    # 单/双引号 src 都匹配
    imgs = re.findall(r'<img[^>]*?\ssrc=["\']([^"\']+)["\']', raw, re.IGNORECASE)
    if not imgs:
        return None

    # 反 HTML 实体 + 去空白
    imgs = [htmllib.unescape(s).strip() for s in imgs if s.strip()]

    BAD_KEYWORDS = (
        "logo", "icon", "favicon", "ar5iv-footer", "ar5iv-logo",
        "/mathjax", "katex", "tex2png", "creativecommons", "license",
        "static/img", "/static/", "avatar", "spinner", "loader",
    )
    GOOD_EXT = (".png", ".jpg", ".jpeg", ".webp", ".gif")
    BAD_EXT = (".svg",)   # SVG 多为 logo/icon
    # arXiv 论文图通常文件名带 x1/x2... 或 fig/figure/extracted 路径
    GOOD_HINTS = ("/x", "/fig", "figure", "/extracted/", "/assets/x")

    def looks_like_content(src):
        if not src or src.startswith("data:"):
            return False
        low = src.lower()
        # 黑名单
        if any(b in low for b in BAD_KEYWORDS):
            return False
        # 去掉 query/fragment 看扩展名
        path = low.split("?", 1)[0].split("#", 1)[0]
        if any(path.endswith(e) for e in BAD_EXT):
            return False
        # 必须是图片扩展名(或带 good hint 的路径)
        if not (any(path.endswith(e) for e in GOOD_EXT) or any(h in low for h in GOOD_HINTS)):
            return False
        return True

    candidates = [s for s in imgs if looks_like_content(s)]
    # 优先选带 good hint 的(更可能是论文配图)
    candidates.sort(key=lambda s: 0 if any(h in s.lower() for h in GOOD_HINTS) else 1)
    if not candidates:
        return None

    first = candidates[0]

    # 路径补全 + 强制 https(避免 https 网站加载 http 图被浏览器拦截)
    # 注意 arXiv 一个坑:HTML 里的相对路径有时是 "{id}v1/x1.png" 这种带版本号的,
    # 直接拼到 /html/{id}/ 后面会出现 id 重复(/html/2605.x/2605.xv1/x1.png),
    # 实际应拼到 /html/ 根。所以做一个判断。
    import re as _re
    def resolve(src):
        if src.startswith("http://"):
            return "https://" + src[len("http://"):]
        if src.startswith("https://"):
            return src
        if src.startswith("//"):
            return "https:" + src
        if src.startswith("/"):
            return "https://arxiv.org" + src
        # 去掉 ./ 前缀
        clean = src.lstrip("./")
        # 如果路径以论文 id(可能带版本号)开头,从 /html/ 根拼,避免重复
        if _re.match(rf"^{_re.escape(base_id)}(v\d+)?/", clean):
            return f"https://arxiv.org/html/{clean}"
        # 普通相对路径:拼到 HTML 页目录下
        base = html_url if html_url.endswith("/") else html_url + "/"
        return base + clean

    return resolve(first)


# ============ arXiv ============

def fetch_hf_daily():
    """Hugging Face Daily Papers:社区投票的每日热门 AI 论文。带 upvotes 热度。"""
    out = []
    try:
        raw = fetch_url(f"{HF_DAILY_API}?limit={HF_CANDIDATES}")
        papers = json.loads(raw)
    except Exception as e:
        print(f"  [HF Daily] 跳过:{e}", file=sys.stderr)
        return out
    for p in papers:
        paper = p.get("paper", p)
        pid = paper.get("id", "")
        title = (paper.get("title") or "").strip().replace("\n", " ")
        summary = (paper.get("summary") or "").strip().replace("\n", " ")
        upvotes = paper.get("upvotes", p.get("upvotes", 0)) or 0
        authors = paper.get("authors", [])
        author_names = [a.get("name", "") for a in authors] if authors else []
        pub = parse_date((p.get("publishedAt") or paper.get("publishedAt") or "")[:10])
        if not title or not pid:
            continue
        out.append({
            "id": f"paper-{pid}",
            "type": "paper",
            "source": "Hugging Face Daily",
            "region": "海外",
            "title": title,
            "authors": ", ".join(author_names[:3]) + (" et al." if len(author_names) > 3 else ""),
            "published": pub.strftime("%Y-%m-%d") if pub else "",
            "url": f"https://arxiv.org/abs/{pid}",
            "raw_summary": summary,
            "hotness": int(upvotes),   # 社区投票数,作为热度信号传给打分阶段
        })
    # 按 upvotes 降序(已经是热门,但确保排序)
    out.sort(key=lambda x: x.get("hotness", 0), reverse=True)
    return out


def fetch_arxiv():
    items = []
    ns = {"a": "http://www.w3.org/2005/Atom"}
    for cat in ARXIV_CATEGORIES:
        q = (f"http://export.arxiv.org/api/query?search_query=cat:{cat}"
             f"&sortBy=submittedDate&sortOrder=descending&max_results={ARXIV_PER_CAT}")
        try:
            raw = fetch_url(q)
            root = ET.fromstring(raw)
        except Exception as e:
            print(f"  [arXiv:{cat}] 跳过:{e}", file=sys.stderr)
            continue
        for entry in root.findall("a:entry", ns):
            title = (entry.findtext("a:title", "", ns) or "").strip().replace("\n", " ")
            summary = (entry.findtext("a:summary", "", ns) or "").strip().replace("\n", " ")
            link = (entry.findtext("a:id", "", ns) or "").strip()
            pub = parse_date(entry.findtext("a:published", "", ns))
            authors = [a.findtext("a:name", "", ns) for a in entry.findall("a:author", ns)]
            arxiv_id = link.split("/abs/")[-1] if "/abs/" in link else link.rsplit("/", 1)[-1]
            items.append({
                "id": f"paper-{arxiv_id}",
                "type": "paper",
                "source": f"arXiv · {cat}",
                "region": "海外",
                "title": title,
                "authors": ", ".join(authors[:3]) + (" et al." if len(authors) > 3 else ""),
                "published": pub.strftime("%Y-%m-%d") if pub else "",
                "url": link,
                "raw_summary": summary,
                "hotness": 0,   # arXiv 裸抓没有热度信号
            })
        time.sleep(3)  # arXiv 礼貌限速
    # 去重 + 取候选上限
    seen, uniq = set(), []
    for it in items:
        if it["id"] in seen:
            continue
        seen.add(it["id"])
        uniq.append(it)
    return uniq[:ARXIV_CANDIDATES]

# ============ RSS / Atom ============

def fetch_rss(feed):
    out = []
    try:
        raw = fetch_url(feed["url"])
        root = ET.fromstring(raw)
    except Exception as e:
        print(f"  [{feed['name']}] RSS 跳过:{e}", file=sys.stderr)
        return out
    # 兼容 RSS(channel/item)与 Atom(entry)
    items = root.findall(".//item")
    atom = "{http://www.w3.org/2005/Atom}"
    if not items:
        items = root.findall(f".//{atom}entry")
    for it in items[:3]:
        title = (it.findtext("title") or it.findtext(f"{atom}title") or "").strip()
        desc = (it.findtext("description") or it.findtext(f"{atom}summary") or "").strip()
        link = it.findtext("link") or ""
        if not link:
            le = it.find(f"{atom}link")
            link = le.get("href") if le is not None else ""
        pub = parse_date(it.findtext("pubDate") or it.findtext(f"{atom}published") or it.findtext(f"{atom}updated"))
        if not is_recent(pub):
            continue
        if not title:
            continue
        out.append({
            "id": f"news-{abs(hash(link or title)) % 10**8}",
            "type": feed["type"],
            "source": feed["name"],
            "region": feed["region"],
            "title": title,
            "authors": feed["name"],
            "published": pub.strftime("%Y-%m-%d") if pub else "",
            "url": link.strip(),
            "raw_summary": _strip_html(desc)[:800],
        })
    return out

def _strip_html(s):
    import re
    return re.sub(r"<[^>]+>", "", s or "").strip()

# ============ GitHub releases(国内团队兜底) ============

def fetch_github(feed):
    out = []
    api = f"https://api.github.com/orgs/{feed['url']}/repos?sort=pushed&per_page=5"
    try:
        repos = json.loads(fetch_url(api))
    except Exception as e:
        print(f"  [{feed['name']}] GitHub 跳过:{e}", file=sys.stderr)
        return out
    for repo in repos[:2]:
        pushed = parse_date((repo.get("pushed_at") or "")[:10])
        if not is_recent(pushed):
            continue
        desc = repo.get("description") or ""
        if not desc:
            continue
        out.append({
            "id": f"news-gh-{repo.get('id')}",
            "type": feed["type"],
            "source": f"{feed['name']} · GitHub",
            "region": feed["region"],
            "title": repo.get("full_name", ""),
            "authors": feed["name"],
            "published": pushed.strftime("%Y-%m-%d") if pushed else "",
            "url": repo.get("html_url", ""),
            "raw_summary": desc[:800],
        })
    return out

# ============ 主流程 ============

def main():
    print("→ 抓取论文候选(Hugging Face Daily Papers 主源)…")
    hf = fetch_hf_daily()
    print(f"  HF Daily 得到 {len(hf)} 篇热门候选")

    print("→ 抓取 arXiv 最新作兜底…")
    arxiv = fetch_arxiv()
    print(f"  arXiv 得到 {len(arxiv)} 篇候选")

    # 合并去重(HF 优先,保留其热度信号),作为论文候选池
    seen, paper_candidates = set(), []
    for it in hf + arxiv:
        if it["id"] in seen:
            continue
        seen.add(it["id"])
        paper_candidates.append(it)
    paper_candidates = paper_candidates[:PAPER_CANDIDATES_TOTAL]
    print(f"  论文候选池共 {len(paper_candidates)} 篇(将由 rank_papers.py 打分精选)")

    print("→ 抓取公司动态 / 报告…")
    news = []
    for feed in FEEDS:
        got = fetch_github(feed) if feed["kind"] == "github" else fetch_rss(feed)
        if got:
            print(f"  [{feed['name']}] {len(got)} 条")
        news.extend(got)
        time.sleep(1)

    reports = [n for n in news if n["type"] == "report"]
    pure_news = [n for n in news if n["type"] == "news"]
    pure_news.sort(key=lambda x: x["published"], reverse=True)
    pure_news = pure_news[:NEWS_KEEP]

    raw = {
        "fetched_at": datetime.date.today().isoformat(),
        "paper_candidates": paper_candidates,   # 待打分精选
        "news": pure_news,                       # 新闻直接用
        "reports": reports[:1],                  # 报告直接用
    }
    DATA.mkdir(exist_ok=True)
    (DATA / "raw.json").write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"✓ 写入 data/raw.json:{len(paper_candidates)} 篇论文候选 + "
          f"{len(pure_news)} 条新闻 + {len(reports[:1])} 条报告")

if __name__ == "__main__":
    main()
