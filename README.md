# The AI Reader · AI 论文日读

> 一个 Serverless 全自动 AI 资讯聚合站。每天追踪 Hugging Face 热门论文 + arXiv 新作 + 国内外大厂动态,用 DeepSeek-V4 生成中文深度解读,让看 AI 像读早报一样轻松。

🔗 **在线访问:** [ai.rockzxy.top](https://ai.rockzxy.top) · 备用 [ai-daily-digest-seven.vercel.app](https://ai-daily-digest-seven.vercel.app)

---

## 这是什么

> *"AI 领域每天涌出大量论文和动态,信息量大、英文阅读门槛高——我想要一个像每日早报一样,五分钟看完今天 AI 最值得关注进展的工具。市面上的 AI 资讯站要么内容堆砌、要么解读太浅,所以自己做一个。"*

它每天会做这些事,**全程无人值守**:

1. **抓取候选** — 从 Hugging Face Daily Papers(社区投票的每日热门论文)、arXiv 最新作、国内外大厂博客 / GitHub Releases 抓取 20+ 篇候选
2. **AI 打分精选** — DeepSeek-V4 按「前沿性 / 影响力 / 科普价值」打分,叠加社区热度作先验,精选 Top 5 论文
3. **三段式深度解读** — 生成 350~500 字的中文解读(背景 + 核心做法 + 意义与局限),配自动图解、关键术语、研究主题、面试视角的总结
4. **论文配图提取** — 从 arXiv HTML 版抓取代表性图(优先 caption 含 framework / architecture / 方法 等关键词的图,没合适的不显示)
5. **按期归档 + 主题聚合** — 每天一期,期号递增,历史永久保留;预设 10 个研究主题自动分类,可视化主题关系图谱
6. **真实跨用户互动** — 浏览量与点赞由 Supabase 跨设备真实累加

## 核心特性

- **不按时间抓最新,按热度+价值精选** — Hugging Face 社区投票 × DeepSeek 打分双重筛选,过滤低质量论文
- **三类内容统一架构** — 论文 / 行业新闻 / 发布报告,可按类型 × 主题双维度筛选
- **结构化深度解读** — 三段式讲解 + 自动生成的对比表 / 流程图 / 数据卡图解 + 关键术语解释 + 求职视角总结
- **知识图谱可视化** — 主题节点 + 共现连线,直观呈现 AI 研究方向之间的关联(老师建议的"知识体系"思路落地)
- **全局历史搜索** — 跨所有期的标题 / 摘要 / 解读全文检索,匹配高亮
- **响应式 + PWA** — 手机可添加到主屏,像 App 一样使用
- **学术期刊风设计** — 衬线排版、克制配色、纯系统字体,大陆与海外访问都不卡

## 技术栈

`Python` · `DeepSeek API` · `GitHub Actions` · `Supabase` · `原生 HTML/CSS/JS` · `Vercel` · `自有域名`

## 系统架构

```
        ┌──────────────────────────────────────────────────────┐
        │  数据源(多源容错,任一失败不影响整体)              │
        │  Hugging Face Daily(主源 · 带 upvotes 社区热度)    │
        │  arXiv API(兜底 · cs.AI / cs.LG / cs.CL)            │
        │  大厂 RSS / GitHub Releases(新闻 · 报告)            │
        └────────────────────┬─────────────────────────────────┘
                             ↓ fetch_sources.py
        ┌──────────────────────────────────────────────────────┐
        │  AI 打分精选 · rank_papers.py                        │
        │  DeepSeek-V4 按 前沿性/影响力/科普价值 打分          │
        │  叠加 HF 社区热度作先验,取 Top 5                    │
        │  对精选论文从 arXiv HTML 提取代表性配图              │
        └────────────────────┬─────────────────────────────────┘
                             ↓ explain.py
        ┌──────────────────────────────────────────────────────┐
        │  三段式深度解读 · DeepSeek-V4-pro                    │
        │  通俗讲解(背景 + 做法 + 意义) + 图解 + 术语 + 主题   │
        │  严格校验主题在预设清单内,失败重试 3 次              │
        └────────────────────┬─────────────────────────────────┘
                             ↓
        ┌──────────────────────────────────────────────────────┐
        │  按期归档:每天一期,期号递增                        │
        │  data/feed.json(最新) + archive/YYYY-MM-DD.json     │
        │  + issues.json(期目录索引)                          │
        └────────────────────┬─────────────────────────────────┘
                             ↓
                  GitHub Actions 每日定时驱动整条流水线
                             ↓
                       Vercel 自动重新部署
                             ↓
                    访客访问 · Supabase 真实计数
```

## 工程亮点

- **Serverless 零运维** — 无服务器、无数据库管理,GitHub Actions 编排 + Vercel 静态托管,配置一次后零维护
- **多源容错抓取** — 任何单一数据源失败(超时 / 403 / 解析异常)都被静默跳过,不阻塞整体流水线
- **LLM 调用的工程化** — 严格 JSON 输出 + 三次重试 + 防注水/防编造 prompt 约束 + 主题白名单校验
- **配图提取的启发式优化** — 不是简单取首图,而是 caption 关键词匹配 +「宁缺毋滥」策略,避免显示无关图
- **Git 推送冲突自愈** — 自动 `git pull --rebase --autostash` + 三次重试,化解定时任务与手动修改的并发冲突
- **定时任务避开 GitHub 拥堵时段** — UTC `01:17`(冷门半点)而非 `00:30`(全球高峰)
- **真实跨用户计数** — Supabase RPC + 行级安全策略,前端只用 publishable key,无服务端代码
- **全前端检索** — 历史归档懒加载到内存检索,无后端无数据库,匹配高亮

## 真实踩过的坑

> 这一节诚实记录了项目从想法到稳定运行真实遇到的工程问题,以及如何定位与解决——比"跑通就停"的 demo 更经得起追问。

1. **Python 标准库命名冲突** — 把脚本命名 `select.py` 导致和标准库 `select` 模块循环导入崩溃,改名 `rank_papers.py` 解决
2. **Prompt 模板与 JSON 大括号冲突** — `.format()` 把模板里的 JSON `{}` 误认占位符抛 `KeyError`,改用显式 `.replace()`
3. **GitHub Actions Git 推送被拒** — 定时任务与手动修改撞上 push,加 `git pull --rebase --autostash` 三次重试
4. **GitHub 定时任务整点拥堵跳过** — `cron "30 0 * * *"` 经常延迟/漏跑,改到 `"17 1 * * *"` 冷门时段稳定触发
5. **arXiv 相对路径自带论文 id 前缀** — HTML 里出现 `2605.26302v1/x1.png` 这种"带 id"路径,直接拼接会 id 重复 404,加路径判断
6. **X 讨论热度无法接入** — 2026 年 X 加强外链抑制 + API 收费,改用 HF 社区投票数(upvotes)作替代信号(本就是研究者社区的热度投票)
7. **配图相关性优化** — 最初按文档顺序取首图,常拿到非代表图;改为 caption 关键词匹配(framework / overview / architecture / 方法 / 架构),没命中则不显示

## 目录结构

```
ai-daily-digest/
├── index.html              主页(报头 / 类型筛选 / 主题筛选 / 搜索框 / 期号导航)
├── about.html              关于本站(技术架构 + 踩坑记录)
├── manifest.json           PWA 配置
├── sw.js                   Service Worker
├── vercel.json             Vercel 部署配置
├── supabase_setup.sql      数据库初始化(stats 表 + bump 函数)
├── scripts/
│   ├── fetch_sources.py    抓取:HF Daily + arXiv + 大厂 RSS/GitHub + 配图提取
│   ├── rank_papers.py      精选:DeepSeek 打分 × 社区热度 → Top N
│   └── explain.py          解读:DeepSeek 三段式深度解读 + 主题白名单校验
├── data/
│   ├── feed.json           最新一期(前端读取)
│   ├── issues.json         期目录索引
│   ├── archive/
│   │   └── YYYY-MM-DD.json 按日期归档,永久保留
│   ├── raw.json            原始抓取(中间产物)
│   └── selected.json       打分精选后(中间产物)
└── .github/workflows/
    └── daily.yml           每日定时编排
```

## 部署上线

简版流程(完整步骤可加我微信交流):

1. **传上 GitHub**(网页拖拽即可,注意 `.github` 隐藏文件夹要一起传)
2. **Vercel 一键导入**(vercel.com → Continue with GitHub → Import → Deploy)
3. **配 DeepSeek Key**(Settings → Secrets and variables → Actions → 新增 `DEEPSEEK_API_KEY`)
4. **可选:Supabase 真实计数**(建项目 → 跑 `supabase_setup.sql` → 把 URL / Publishable Key 填到 `index.html`)
5. **可选:自有域名**(任意注册商买域名 → DNS A 记录 `@ → 76.76.21.21` 指向 Vercel)

部署后 GitHub Actions 每天北京时间 09:17 自动跑,Vercel 自动重新部署,**全程无人工干预**。

## 关于作者

我是一名正在准备校招的求职者。这是我用业余时间从想法到上线、独立完成的全栈项目,既是为了让自己每天看 AI 更轻松,也是一次完整的工程实战:

> *识别真实需求 → 设计技术方案 → 处理真实环境的边界情况 → 持续优化*

—— 而不是一个跑通就停的 demo。

这片小站如果对你有用,或者你有其他建议,欢迎与我交流。

📮 **联系方式**:微信 `Yan_TrT`

---

<sub>构建者:[@zhangxy003-netizen](https://github.com/zhangxy003-netizen) · License: MIT</sub>
