"""
rank_papers.py — 论文候选打分精选(在 fetch 和 explain 之间)
读取 data/raw.json 的 paper_candidates,用 DeepSeek 按价值打分,取 Top N。
输出 data/selected.json,交给 explain.py 做深度解读。

打分维度:前沿性、影响力、科普价值。结合 HF 社区投票热度(hotness)作为先验。
用便宜的方式批量打分(一次请求评多篇),控制成本。
"""
import json, os, sys, re
from pathlib import Path
from openai import OpenAI

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
MODEL = "deepseek-v4-pro"
BASE_URL = "https://api.deepseek.com"
PAPER_KEEP = 5   # 最终精选保留几篇论文

client = OpenAI(api_key=os.environ.get("DEEPSEEK_API_KEY", ""), base_url=BASE_URL)

SCORE_SYSTEM = """你是 AI 领域的资深编辑,负责从一批候选论文里挑出最值得向大众科普的几篇。
你的评判标准:
1. 前沿性 — 是否代表当前热门或新兴方向(大模型、Agent、推理、多模态、效率优化等)。
2. 影响力 — 是否来自重要团队、或可能被广泛关注和引用。
3. 科普价值 — 概念是否能讲给非专业读者听、是否有趣有启发。
避免选择:过于狭窄的理论推导、增量改进、纯工程细节、与 AI 主线关系不大的论文。
严格只返回 JSON。"""

SCORE_PROMPT = """以下是今天的候选论文(含社区投票数 votes,可作为热度参考)。
请为每篇打一个 0~100 的综合分(前沿性+影响力+科普价值),并简短说明理由。

{papers}

返回 JSON 数组,每个元素形如 {{"index": 候选编号, "score": 分数, "reason": "10字内理由"}}。
按你的真实判断打分,不要全部给高分。只返回 JSON 数组,不要其他内容。"""


def parse_json(text):
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.MULTILINE).strip()
    s, e = text.find("["), text.rfind("]")
    if s != -1 and e != -1:
        text = text[s:e + 1]
    return json.loads(text)


def score_candidates(candidates):
    """批量打分。失败则降级为按 hotness 排序。"""
    listing = []
    for i, c in enumerate(candidates):
        votes = c.get("hotness", 0)
        listing.append(f"[{i}] (votes={votes}) {c['title']}\n    摘要:{c.get('raw_summary','')[:300]}")
    prompt = SCORE_PROMPT.format(papers="\n\n".join(listing))

    try:
        resp = client.chat.completions.create(
            model=MODEL,
            messages=[{"role": "system", "content": SCORE_SYSTEM},
                      {"role": "user", "content": prompt}],
            temperature=0.3,
            max_tokens=1500,
        )
        scores = parse_json(resp.choices[0].message.content)
        score_map = {s["index"]: s.get("score", 0) for s in scores if "index" in s}
        for i, c in enumerate(candidates):
            # 综合分 = AI 打分为主,社区热度作小幅加权先验
            ai = score_map.get(i, 0)
            c["_score"] = ai + min(c.get("hotness", 0), 50) * 0.2
        print("  ✓ DeepSeek 打分完成")
    except Exception as e:
        print(f"  打分失败,降级为按社区热度排序:{e}", file=sys.stderr)
        for c in candidates:
            c["_score"] = c.get("hotness", 0)

    candidates.sort(key=lambda x: x.get("_score", 0), reverse=True)
    return candidates


def main():
    raw_path = DATA / "raw.json"
    if not raw_path.exists():
        print("✗ 找不到 data/raw.json,请先运行 fetch_sources.py", file=sys.stderr)
        sys.exit(1)
    raw = json.loads(raw_path.read_text(encoding="utf-8"))
    candidates = raw.get("paper_candidates", [])

    if not candidates:
        print("  无论文候选,跳过打分")
        selected = []
    elif not client.api_key:
        print("  未设置 API key,降级为按社区热度取前 N", file=sys.stderr)
        candidates.sort(key=lambda x: x.get("hotness", 0), reverse=True)
        selected = candidates[:PAPER_KEEP]
    else:
        print(f"→ 对 {len(candidates)} 篇论文候选打分,精选 Top {PAPER_KEEP}…")
        ranked = score_candidates(candidates)
        selected = ranked[:PAPER_KEEP]
        for c in selected:
            print(f"  ★ {c.get('_score',0):.0f}分 | {c['title'][:55]}")

    # 清理打分用的临时字段
    for c in selected:
        c.pop("_score", None)
        c.pop("hotness", None)

    # 对精选出的论文提取首图(只存 URL,取不到则无图,降级不报错)
    try:
        from fetch_sources import extract_paper_figure
        import time as _t
        for c in selected:
            if c.get("type") != "paper":
                continue
            # 从 id 或 url 里取 arxiv 号
            aid = c.get("id", "").replace("paper-", "")
            if not aid and "/abs/" in c.get("url", ""):
                aid = c["url"].split("/abs/")[-1]
            if not aid:
                continue
            img = extract_paper_figure(aid)
            if img:
                c["image"] = img
                print(f"  🖼 取到首图:{c['title'][:40]}")
            _t.sleep(1)
    except Exception as e:
        print(f"  首图提取整体跳过:{e}", file=sys.stderr)

    out = {
        "fetched_at": raw.get("fetched_at"),
        "items": selected + raw.get("news", []) + raw.get("reports", []),
    }
    (DATA / "selected.json").write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"✓ 写入 data/selected.json:{len(selected)} 篇精选论文 + "
          f"{len(raw.get('news', []))} 条新闻 + {len(raw.get('reports', []))} 条报告")


if __name__ == "__main__":
    main()
