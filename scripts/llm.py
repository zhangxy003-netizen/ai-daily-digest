"""
llm.py — 统一的 DeepSeek 调用封装(rank_papers.py 与 explain.py 共用)

解决 2026-08 中旬起的静默失败:DeepSeek V4 系列默认开启思考模式,
思考 token 会先消耗 max_tokens,导致正文为空/截断、JSON 解析失败。

这里做了五件事:
  1. 显式关闭思考(extra_body={"thinking": {"type": "disabled"}}),科普解读不需要长推理
  2. 开启 JSON 模式(response_format=json_object),保证返回合法 JSON
  3. 检查 finish_reason:被截断就自动放大 max_tokens 重试
  4. 429 / 5xx 指数退避;主模型连续失败自动降级到备用模型
  5. 每次调用记录诊断信息,便于写入 run_meta 排查

环境变量:
  DEEPSEEK_API_KEY         必填
  DIGEST_MODEL             主模型,默认 deepseek-v4-pro
  DIGEST_FALLBACK_MODEL    备用模型,默认 deepseek-v4-flash
"""
import json
import os
import re
import sys
import time

from openai import OpenAI

BASE_URL = "https://api.deepseek.com"
PRIMARY = os.environ.get("DIGEST_MODEL", "deepseek-v4-pro")
FALLBACK = os.environ.get("DIGEST_FALLBACK_MODEL", "deepseek-v4-flash")

API_KEY = os.environ.get("DEEPSEEK_API_KEY", "").strip()
HAS_KEY = bool(API_KEY)

client = OpenAI(
    api_key=API_KEY or "sk-missing",   # 新版 SDK 不接受空串;真实校验用 HAS_KEY
    base_url=BASE_URL,
    timeout=180,
    max_retries=0,          # 重试逻辑自己控制,避免 SDK 的隐式重试掩盖问题
)

# 若服务端不认 thinking 参数(400),自动降级为不传
_thinking_param_ok = True

# ---------- 峰谷避让 ----------
# DeepSeek 峰谷计费:工作日北京时间 09:00–12:00、14:00–18:00 为高峰(价格 ×2),
# 其余时段与周六周日全天为低谷。GitHub 定时任务可能延迟数小时把运行推进高峰,
# 所以在调用前检查一次,撞上高峰就等到高峰结束。DIGEST_NO_WAIT=1 可关闭。
import datetime as _dt

_PEAK_WINDOWS = [(9, 12), (14, 18)]   # 北京时间,左闭右开
_CN_TZ = _dt.timezone(_dt.timedelta(hours=8))
_waited_once = False


def _beijing_now():
    return _dt.datetime.now(_CN_TZ)


def seconds_until_off_peak(now=None):
    """返回需要等待的秒数;0 表示当前就是低谷。"""
    now = now or _beijing_now()
    if now.weekday() >= 5:            # 周六(5)/周日(6)全天低谷
        return 0
    for start, end in _PEAK_WINDOWS:
        if start <= now.hour < end:
            target = now.replace(hour=end, minute=0, second=30, microsecond=0)
            return int((target - now).total_seconds())
    return 0


def wait_for_off_peak():
    global _waited_once
    if os.environ.get("DIGEST_NO_WAIT") == "1":
        return
    secs = seconds_until_off_peak()
    if secs <= 0:
        return
    if not _waited_once:
        print(f"⏳ 当前北京时间 {_beijing_now():%H:%M} 处于 DeepSeek 高峰时段(价格×2),"
              f"等待 {secs // 60} 分钟到低谷再调用…", flush=True)
        _waited_once = True
    time.sleep(secs)


def parse_json(text):
    """从模型输出中稳健地提取 JSON。"""
    text = (text or "").strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.MULTILINE).strip()
    s, e = text.find("{"), text.rfind("}")
    if s == -1 or e == -1:
        # 可能是数组
        s, e = text.find("["), text.rfind("]")
    if s != -1 and e != -1:
        text = text[s:e + 1]
    return json.loads(text)


def _status_of(exc):
    return getattr(exc, "status_code", None) or getattr(getattr(exc, "response", None), "status_code", None)


def chat_json(messages, max_tokens=4096, temperature=0.4, tag=""):
    """
    调用模型并解析为 JSON。
    返回 (data, meta):data 为 None 表示彻底失败;meta 含 model / attempts / 失败原因列表。
    """
    global _thinking_param_ok

    # 计划:主模型两次(第二次放大 max_tokens)→ 备用模型两次
    plan = [
        (PRIMARY, max_tokens),
        (PRIMARY, max_tokens * 2),
        (FALLBACK, max_tokens * 2),
        (FALLBACK, max_tokens * 3),
    ]
    reasons = []
    wait_for_off_peak()
    for i, (model, mt) in enumerate(plan):
        kwargs = dict(
            model=model,
            messages=messages,
            temperature=temperature,
            max_tokens=mt,
            response_format={"type": "json_object"},
        )
        if _thinking_param_ok:
            kwargs["extra_body"] = {"thinking": {"type": "disabled"}}
        try:
            resp = client.chat.completions.create(**kwargs)
            choice = resp.choices[0]
            finish = choice.finish_reason
            content = choice.message.content or ""
            reasoning = getattr(choice.message, "reasoning_content", None)
            if finish == "length":
                reasons.append(f"{model}: 输出被截断(max_tokens={mt},"
                               f"{'含思考' if reasoning else '无思考'})")
                continue
            if not content.strip():
                reasons.append(f"{model}: 正文为空(finish={finish},"
                               f"{'思考占满' if reasoning else '未知'})")
                continue
            data = parse_json(content)
            usage = getattr(resp, "usage", None)
            meta = {
                "model": model,
                "attempts": i + 1,
                "prompt_tokens": getattr(usage, "prompt_tokens", None),
                "completion_tokens": getattr(usage, "completion_tokens", None),
            }
            if reasons:
                meta["recovered_from"] = reasons
            return data, meta
        except json.JSONDecodeError as e:
            reasons.append(f"{model}: JSON 解析失败 {str(e)[:80]}")
            time.sleep(2)
        except Exception as e:
            msg = str(e)
            status = _status_of(e)
            # thinking 参数不被接受 → 关掉再来,不计入计划
            if _thinking_param_ok and status == 400 and "thinking" in msg.lower():
                _thinking_param_ok = False
                reasons.append("服务端不接受 thinking 参数,已改为不传")
                plan.insert(i + 1, (model, mt))
                continue
            reasons.append(f"{model}: {type(e).__name__}"
                           f"{' ' + str(status) if status else ''} {msg[:100]}")
            if status in (429, 500, 502, 503, 504) or "rate" in msg.lower():
                time.sleep((15, 40, 90, 90)[min(i, 3)])
            else:
                time.sleep(3)
    print(f"  [{tag}] 全部尝试失败:{' | '.join(reasons)}", file=sys.stderr)
    return None, {"model": None, "attempts": len(plan), "failed": reasons}
