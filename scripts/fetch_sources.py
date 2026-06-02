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
    """从 arXiv HTML 版论文提取一张"有代表性"的图。
    策略(分级):
      1. 解析 <figure>+<figcaption> 配对,caption 命中关键词(framework/方法/架构等)→ 优先
      2. 没命中的话,看 alt 属性命中关键词 → 次优
      3. 都没命中 → 返回 None(宁缺毋滥,不硬塞不相关的图)
    任何异常都安全返回 None。
    """
    import re, html as htmllib
    base_id = re.sub(r"v\d+$", "", (arxiv_id or "").strip())
    if not base_id:
        return None
    html_url = f"https://arxiv.org/html/{base_id}"

    try:
        req = urllib.request.Request(html_url, headers=UA)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8", "ignore")
    except Exception as e:
        print(f"    [图] {base_id} 无 HTML 版或取不到,跳过:{e}", file=sys.stderr)
        return None

    # ========== 1. 找所有 <figure>...<figcaption>...</figcaption>...</figure> 块,把 img 和 caption 配对 ==========
    BAD_KEYWORDS = ("logo", "icon", "favicon", "ar5iv-footer", "ar5iv-logo",
                    "/mathjax", "katex", "tex2png", "creativecommons", "license",
                    "static/img", "/static/", "avatar", "spinner", "loader")
    GOOD_EXT = (".png", ".jpg", ".jpeg", ".webp", ".gif")
    BAD_EXT = (".svg",)
    GOOD_HINTS = ("/x", "/fig", "figure", "/extracted/", "/assets/x")

    # caption 关键词(命中即为"代表图")
    GOOD_CAPTION_KW = (
        # 英文
        "framework", "overview", "architecture", "pipeline",
        "our approach", "our method", "proposed", "workflow",
        "system overview", "the overall",
        # 中文
        "框架", "总览", "系统", "方法", "流程", "架构", "概览", "所提",
    )

    def looks_like_content(src):
        if not src or src.startswith("data:"):
            return False
        low = src.lower()
        if any(b in low for b in BAD_KEYWORDS):
            return False
        path = low.split("?", 1)[0].split("#", 1)[0]
        if any(path.endswith(e) for e in BAD_EXT):
            return False
        if not (any(path.endswith(e) for e in GOOD_EXT) or any(h in low for h in GOOD_HINTS)):
            return False
        return True

    def caption_hits(text):
        if not text:
            return False
        low = text.lower()
        return any(kw in low for kw in GOOD_CAPTION_KW)

    # 抽取 <figure> 块(尽量松散匹配,有的 <figure> 标签里包了 img 和 caption)
    figure_blocks = re.findall(r"<figure[^>]*>(.*?)</figure>", raw, re.IGNORECASE | re.DOTALL)
    captioned_imgs = []  # [(src, caption_text)]
    for block in figure_blocks:
        # 块内的第一张 img
        m_img = re.search(r'<img[^>]*?\ssrc=["\']([^"\']+)["\'][^>]*>', block, re.IGNORECASE)
        if not m_img:
            continue
        src = htmllib.unescape(m_img.group(1)).strip()
        # 块内的 figcaption 文本
        m_cap = re.search(r"<figcaption[^>]*>(.*?)</figcaption>", block, re.IGNORECASE | re.DOTALL)
        caption = re.sub(r"<[^>]+>", "", m_cap.group(1)).strip() if m_cap else ""
        caption = htmllib.unescape(caption)
        captioned_imgs.append((src, caption))

    # ========== 2. 优先级排序 ==========
    # 先过滤"长得像内容图"的
    captioned_imgs = [(s, c) for (s, c) in captioned_imgs if looks_like_content(s)]

    chosen = None
    # 优先级 1:caption 命中关键词
    for s, c in captioned_imgs:
        if caption_hits(c):
            chosen = s
            break
    # 优先级 2:没 caption 命中,但试一下"img 的 alt 属性"命中
    if not chosen:
        # 从原 HTML 里抽 img 标签的 src 和 alt 配对
        for m in re.finditer(r'<img([^>]*)>', raw, re.IGNORECASE):
            attrs = m.group(1)
            s_m = re.search(r'\ssrc=["\']([^"\']+)["\']', attrs, re.IGNORECASE)
            a_m = re.search(r'\salt=["\']([^"\']*)["\']', attrs, re.IGNORECASE)
            if not s_m:
                continue
            src = htmllib.unescape(s_m.group(1)).strip()
            alt = htmllib.unescape(a_m.group(1)).strip() if a_m else ""
            if looks_like_content(src) and caption_hits(alt):
                chosen = src
                break
    # 优先级 3:都没命中 → 不显示
    if not chosen:
        print(f"    [图] {base_id} 无明显代表图(caption/alt 无关键词),跳过", file=sys.stderr)
        return None

    # ========== 3. 路径补全 + 强制 https ==========
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
        clean = src.lstrip("./")
        if _re.match(rf"^{_re.escape(base_id)}(v\d+)?/", clean):
            return f"https://arxiv.org/html/{clean}"
        base = html_url if html_url.endswith("/") else html_url + "/"
        return base + clean

    return resolve(chosen)


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
