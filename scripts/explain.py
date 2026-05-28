"""
explain.py — 用 DeepSeek-V4 为每条原始内容生成中文解读
读取 data/raw.json,输出 data/feed.json(前端直接用)。

每条产出三段式:
  explain  通俗中文解读
  figure   图解要点(结构化,前端渲染成对比表/流程图/数据卡)
  summary  一句话总结(价值 / 对求职者的意义)

依赖:openai 库(DeepSeek 兼容 OpenAI 接口)
环境变量:DEEPSEEK_API_KEY
"""
import json, os, sys, time, re, datetime
from pathlib import Path
from openai import OpenAI

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
MODEL = "deepseek-v4-pro"
BASE_URL = "https://api.deepseek.com"

client = OpenAI(api_key=os.environ.get("DEEPSEEK_API_KEY", ""), base_url=BASE_URL)

SYSTEM = """你是一位擅长把 AI 前沿研究讲给非专业读者听的中文科普编辑。
你的解读要通俗、准确、有信息量,多用生活化类比,避免堆砌术语,首次出现的英文缩写要解释。面向的读者是对 AI 感兴趣、正在求职的学生。

行文风格:
- 像一个懂行的朋友在讲解,行文自然,不要用"综上所述""总而言之""值得一提的是"这类八股腔。
- 类比要新鲜、具体、贴合内容,避免每篇都用"想象一下""打个比方"这类雷同开头。
- 类比是为了帮助理解,但"核心做法"那段必须讲清真实的技术要点,不能只停留在比喻上。

严格要求:
1. 只返回一个 JSON 对象,不要输出任何额外文字,不要用 markdown 代码块包裹。
2. 图解(figure)只能基于摘要里真实存在的信息。若信息不足以填满规定行数/项数,就少填(对比表可只填 2 行、数据卡可只填 2 项),绝不编造数字或对比项。
3. 数据卡(stat)里的数字必须来自摘要原文;若摘要没有任何具体数字,用定性程度词(如"显著提升")代替,不要捏造百分比。
4. 中文解读要准确,不确定的细节宁可说得概括,也不要臆测具体技术细节。"""

PROMPT_TEMPLATE = """请阅读以下{kind_label}的标题与摘要,生成中文深度解读。

标题:{title}
来源:{source}
摘要:{summary}

请返回一个 JSON 对象,字段如下:
{
  "title_zh": "中文标题(准确、简洁)",
  "tldr": "一句话摘要速读,40字以内,只讲事实——它做了什么、是什么。",
  "explain": "深度解读,3段,每段用\\n分隔,总字数 350~500 字。第一段:背景与问题(它要解决什么痛点,用新鲜贴切的类比切入);第二段:核心做法(讲清它具体怎么做的,是全文重点,必须讲到真实技术要点,不能只靠比喻);第三段:意义与局限(带来什么影响,有什么尚未解决的问题)。",
  "figure": {图解要求},
  "terms": [ {"term":"英文/专业术语","desc":"一句话通俗解释,30字内"}, 共1~2个关键术语 ],
  "summary": "一句话总结,角度要和 tldr 不同——tldr 讲事实,summary 讲价值判断:点出它对 AI 求职者的实际意义,比如涉及哪个热门方向、对应什么岗位、面试可能怎么考。",
  "tags": ["3个中文标签,优先用具体技术方向词(如'强化学习''多模态''推理优化''Agent'),少用宽泛词(如'AI''深度学习')"],
  "topics": ["从给定主题清单里选1~2个最贴合的,必须原样照抄清单里的主题名,不要自创"]
}

可选主题清单(topics 只能从这里选,原样照抄):{topics_list}

写作要求:
- 宁可写满 350 字的干货,也不要为凑字数重复、注水或写空泛的套话。
- tldr 和 summary 不要内容重复:tldr 是"它做了什么",summary 是"为什么值得关注/对求职者的意义"。
- terms 只选 1~2 个真正关键、读者可能不懂的术语;若全文没有需要解释的专业术语,terms 返回空数组 []。
- 严格遵守系统说明里关于不编造、行文自然的要求。

只返回 JSON,不要任何其他内容。"""

# 按内容类型给不同的图解模板要求
FIGURE_SPEC = {
    "paper": """{
    "kind": "compare",
    "title": "图解标题(如 '本文方法 vs 传统方法')",
    "rows": [ {"label":"对比维度","a":"传统/基线做法","b":"本文做法"}, ... 共3行 ]
  }(用对比表呈现这篇论文相对已有方法的关键差异)""",
    "news": """{
    "kind": "flow",
    "title": "图解标题",
    "steps": ["步骤1","步骤2","步骤3","步骤4"]
  }(用流程图呈现这个产品/技术的工作流程或这件事的演进)""",
    "report": """{
    "kind": "stat",
    "title": "图解标题",
    "stats": [ {"num":"关键数字如 +38%","label":"含义"}, ... 共3个 ]
  }(用数据卡呈现报告里3个最值得关注的数字。若摘要无明确数字,可用定性程度词如'显著上升')""",
}

KIND_LABEL = {"paper": "学术论文", "news": "AI 行业新闻", "report": "行业报告"}

# 预设研究主题(DeepSeek 从中选 1~2 个,保证归类统一可聚合)
TOPICS = [
    "大模型/LLM", "推理/Reasoning", "Agent/智能体", "多模态",
    "强化学习/RL", "效率优化", "检索增强/RAG", "AI安全/对齐",
    "具身智能/机器人", "应用落地",
]
TOPICS_STR = "、".join(TOPICS)

# ============ few-shot 范例:给模型一个"输入→标准输出"样板 ============
# 每种类型一组。真正请求前作为对话历史插入,显著提升输出格式与质量的稳定性。
FEWSHOT = {
    "paper": [
        {"role": "user", "content": """请阅读以下学术论文的标题与摘要,生成中文解读。

标题:FlashAttention: Fast and Memory-Efficient Exact Attention with IO-Awareness
来源:arXiv · cs.LG
摘要:Transformers are slow and memory-hungry on long sequences. We propose FlashAttention, an IO-aware exact attention algorithm that uses tiling to reduce memory reads/writes between GPU high-bandwidth memory and on-chip SRAM, achieving 3x speedup without approximation.

请按系统说明返回 JSON。"""},
        {"role": "assistant", "content": json.dumps({
            "title_zh": "FlashAttention:更快、更省显存的精确注意力算法",
            "tldr": "通过减少 GPU 显存读写,让注意力计算快 3 倍且不损失精度。",
            "explain": "注意力机制是大模型的核心,它让模型在处理一句话时,能权衡每个词与其他所有词的关系。但当文本变长,这个计算就变得又慢又吃显存。问题的关键常被误解:瓶颈往往不在计算量本身,而在数据搬运——就像做一桌菜,真正费时的不是切菜炒菜,而是反复跑去冰箱拿食材,时间全耗在往返的路上。GPU 也是如此,数据在大容量但慢速的显存、和小容量但极快的片上缓存之间来回搬,拖垮了整体速度。\n FlashAttention 的核心做法是分块计算(tiling):把巨大的注意力矩阵切成小块,一次只把一块搬进高速缓存、在里面把相关计算全部做完,再换下一块,从而把昂贵的显存读写次数大幅压低。更妙的是,它通过巧妙的数学重组,在不保存完整中间矩阵的前提下仍能算出完全正确的结果——也就是说,它是精确的,没有任何近似或精度损失。\n这一优化的意义在于,它几乎不改变模型效果,却能带来约 3 倍的提速和显著的显存节省,因此迅速成为现代大模型训练与推理的标准组件。它的局限是收益依赖具体硬件特性,且实现较为底层、移植到不同芯片上需要额外工程适配。",
            "figure": {
                "kind": "compare",
                "title": "标准注意力 vs FlashAttention",
                "rows": [
                    {"label": "显存读写", "a": "频繁往返", "b": "分块,大幅减少"},
                    {"label": "速度", "a": "基准", "b": "约 3× 提速"},
                    {"label": "精度", "a": "精确", "b": "同样精确(无近似)"}
                ]
            },
            "terms": [
                {"term": "Attention 注意力", "desc": "模型衡量一个词与句中其他词关联程度的机制。"},
                {"term": "SRAM 片上缓存", "desc": "GPU 上容量小但速度极快的存储,靠近计算单元。"}
            ],
            "summary": "它已成为现代大模型训练推理的标配优化,理解它能帮你答好'如何优化 Transformer 效率'这类高频面试题。",
            "tags": ["注意力机制", "推理优化", "显存"],
            "topics": ["效率优化", "大模型/LLM"]
        }, ensure_ascii=False)}
    ],
    "news": [
        {"role": "user", "content": """请阅读以下 AI 行业新闻的标题与摘要,生成中文解读。

标题:Anthropic launches Claude for Enterprise with expanded context
来源:Anthropic
摘要:Anthropic announced Claude for Enterprise, offering expanded context windows, enhanced security controls, and native integrations to help companies deploy Claude across teams securely.

请按系统说明返回 JSON。"""},
        {"role": "assistant", "content": json.dumps({
            "title_zh": "Anthropic 推出企业版 Claude,扩展上下文与安全能力",
            "tldr": "面向企业的 Claude 版本,主打更长上下文、更强安全与系统集成。",
            "explain": "一个 AI 模型能力再强,企业要真正放心用起来,还隔着好几道门槛。最现实的三道是:数据安全(公司机密会不会泄露)、系统集成(能不能接进现有的办公和业务工具)、以及处理能力(能不能一次读完一份很长的合同或文档)。很多企业不是不想用 AI,而是卡在这些门槛上。\n这次发布的企业版 Claude,正是针对性地补齐这几块:扩展上下文窗口让模型一次能消化更多内容,增强的安全控制满足企业合规要求,原生集成让它能直接嵌入企业已有的工作流。换句话说,这不是模型本身变聪明了,而是把'能力'打包成了'企业能直接落地的产品'。\n这背后反映了一个更大的趋势:大模型竞争的下半场,正从'谁的模型分数更高'转向'谁能让企业用得安全、顺手、划算'。它的局限在于,企业级落地的真正难点往往在具体业务流程的打磨,产品化只是第一步。",
            "figure": {
                "kind": "flow",
                "title": "企业部署 Claude 的关键环节",
                "steps": ["接入现有系统", "配置安全权限", "导入长文档", "团队协作使用"]
            },
            "terms": [
                {"term": "上下文窗口 Context Window", "desc": "模型一次能读入并参考的最大文本量。"}
            ],
            "summary": "对求职者来说,'大模型落地企业'是当下最热的方向之一,相关的解决方案、安全、集成岗位需求正在快速增长。",
            "tags": ["Anthropic", "企业应用", "大模型落地"],
            "topics": ["应用落地", "Agent/智能体"]
        }, ensure_ascii=False)}
    ],
    "report": [
        {"role": "user", "content": """请阅读以下行业报告的标题与摘要,生成中文解读。

标题:State of AI Report 2025
来源:Air Street Capital
摘要:The annual report finds that enterprise adoption of generative AI rose sharply year over year, inference costs continued to fall significantly, and demand for AI engineering roles grew substantially across sectors.

请按系统说明返回 JSON。"""},
        {"role": "assistant", "content": json.dumps({
            "title_zh": "2025 年 AI 现状报告",
            "tldr": "年度行业报告:企业采用率上升、推理成本下降、AI 岗位需求大增。",
            "explain": "每年都会有几份重量级的行业报告,试图用数据回答一个问题:这一年 AI 到底走到哪了。和单个公司的宣传不同,这类报告通常不绑定具体产品立场,而是综合大量公开数据看整体趋势,因此是快速建立行业全局观的高效入口,尤其适合想入行的人。\n这份报告点出了三个相互关联的趋势。其一,企业采用生成式 AI 的比例同比明显上升,说明 AI 正从尝鲜走向真实生产;其二,模型推理成本继续大幅下降,这是采用率上升的直接推手——用得起,才用得多;其三,各行各业对 AI 工程人才的需求显著增长,这正是前两个趋势在就业市场上的投影。\n把三者串起来看,是一条清晰的正循环:成本下降→企业更敢用→落地需求增加→更需要懂工程的人。它的局限是报告多为宏观统计,具体到某个细分岗位或地区,情况仍需结合本地数据判断。",
            "figure": {
                "kind": "stat",
                "title": "报告核心趋势",
                "stats": [
                    {"num": "显著上升", "label": "企业生成式 AI 采用率"},
                    {"num": "大幅下降", "label": "模型推理成本"},
                    {"num": "明显增长", "label": "AI 工程岗位需求"}
                ]
            },
            "terms": [],
            "summary": "校招必读——岗位需求与技能趋势章节能帮你判断哪些方向在涨,面试时引用报告数据也更显专业。",
            "tags": ["行业报告", "趋势", "就业"],
            "topics": ["应用落地"]
        }, ensure_ascii=False)}
    ],
}


def build_prompt(item):
    t = item["type"]
    figure = FIGURE_SPEC.get(t, FIGURE_SPEC["news"])
    return (PROMPT_TEMPLATE
            .replace("{kind_label}", KIND_LABEL.get(t, "内容"))
            .replace("{title}", item["title"])
            .replace("{source}", item["source"])
            .replace("{summary}", item.get("raw_summary", "")[:1200])
            .replace("{topics_list}", TOPICS_STR)
            .replace("{图解要求}", figure))


def parse_json(text):
    """从模型输出中稳健地提取 JSON。"""
    text = text.strip()
    # 去掉可能的 markdown 围栏
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.MULTILINE).strip()
    # 截取第一个 { 到最后一个 }
    s, e = text.find("{"), text.rfind("}")
    if s != -1 and e != -1:
        text = text[s:e + 1]
    return json.loads(text)


def explain_one(item):
    prompt = build_prompt(item)
    shots = FEWSHOT.get(item["type"], [])
    messages = [{"role": "system", "content": SYSTEM}] + shots + \
               [{"role": "user", "content": prompt}]
    for attempt in range(3):
        try:
            resp = client.chat.completions.create(
                model=MODEL,
                messages=messages,
                temperature=0.4,
                max_tokens=1800,
            )
            data = parse_json(resp.choices[0].message.content)
            # 合并原始元信息 + AI 解读
            return {
                "id": item["id"],
                "type": item["type"],
                "source": item["source"],
                "region": item.get("region", ""),
                "title": item["title"],
                "title_zh": data.get("title_zh", item["title"]),
                "authors": item.get("authors", ""),
                "published": item.get("published", ""),
                "url": item["url"],
                "tldr": data.get("tldr", ""),
                "explain": data.get("explain", ""),
                "figure": data.get("figure"),
                "terms": data.get("terms", []),
                "summary": data.get("summary", ""),
                "tags": data.get("tags", [])[:3],
                "topics": [t for t in data.get("topics", []) if t in TOPICS][:2],
            }
        except Exception as e:
            print(f"  [{item['id']}] 第{attempt+1}次失败:{e}", file=sys.stderr)
            time.sleep(2 * (attempt + 1))
    return None  # 三次都失败则丢弃该条


def main():
    raw_path = DATA / "selected.json"
    if not raw_path.exists():
        print("✗ 找不到 data/selected.json,请先运行 fetch_sources.py 和 rank_papers.py", file=sys.stderr)
        sys.exit(1)
    raw = json.loads(raw_path.read_text(encoding="utf-8"))

    if not client.api_key:
        print("✗ 未设置 DEEPSEEK_API_KEY 环境变量", file=sys.stderr)
        sys.exit(1)

    out = []
    for item in raw["items"]:
        print(f"→ 解读:{item['title'][:50]}…")
        res = explain_one(item)
        if res:
            out.append(res)
        time.sleep(1)

    if not out:
        print("✗ 今日没有成功解读的内容,跳过写入(保留昨日数据)", file=sys.stderr)
        return

    today = raw.get("fetched_at") or datetime.date.today().isoformat()
    save_archive(today, out)


def save_archive(today, items):
    """累积归档:存当天归档 + 更新期目录 + 写最新一期 feed.json。"""
    archive_dir = DATA / "archive"
    archive_dir.mkdir(parents=True, exist_ok=True)
    index_path = DATA / "issues.json"

    # 读已有期目录
    if index_path.exists():
        index = json.loads(index_path.read_text(encoding="utf-8"))
    else:
        index = {"issues": []}

    # 若今天已存在(同日重复运行),覆盖当天那期;否则新增一期
    existing = next((it for it in index["issues"] if it["date"] == today), None)
    if existing:
        issue_no = existing["issue_no"]
    else:
        issue_no = (max([it["issue_no"] for it in index["issues"]], default=0) + 1)

    # 写当天归档文件
    archive_obj = {
        "date": today,
        "issue_no": issue_no,
        "items": items,
    }
    (archive_dir / f"{today}.json").write_text(
        json.dumps(archive_obj, ensure_ascii=False, indent=2), encoding="utf-8")

    # 更新期目录(去重后按日期倒序)
    index["issues"] = [it for it in index["issues"] if it["date"] != today]
    index["issues"].append({"date": today, "issue_no": issue_no, "count": len(items)})
    index["issues"].sort(key=lambda x: x["date"], reverse=True)
    index["latest"] = today
    index["total_issues"] = len(index["issues"])
    index_path.write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")

    # feed.json = 最新一期(向后兼容前端)
    feed = {"updated_at": today, "issue_no": issue_no,
            "total_issues": index["total_issues"], "items": items}
    (DATA / "feed.json").write_text(json.dumps(feed, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"✓ 第 {issue_no} 期({today})已归档,{len(items)} 条 | 累计 {index['total_issues']} 期")


if __name__ == "__main__":
    main()
