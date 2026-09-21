# -*- coding: utf-8 -*-
"""
Jev (TypeSafe System One) 客户端 —— OpenRouter Decisions API

端点与请求格式参照 Zumka1991/jev-telegram-admin 的实战实现，
含其踩过的「上下文污染」防护措辞。

用法：
    python jev_client.py --test          # 只跑前 3 条，验证通路
    python jev_client.py --limit 50      # 跑前 50 条
    python jev_client.py                 # 跑全部
"""
import sys
import json
import time
import argparse
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

import httpx

sys.stdout.reconfigure(encoding='utf-8', errors='replace')

# ---------------- 配置 ----------------

BASE = Path(__file__).parent
CONFIG = BASE / 'config.json'
DATA = BASE / 'data'
DATA.mkdir(exist_ok=True)

DEFAULTS = {
    'openrouter_api_key': '',
    'proxy': 'http://127.0.0.1:7897',
    # 只有这一个端点。曾经还有个 endpoint_fallback 指向
    # https://openrouter.ai/api/v1/api/alpha/decisions —— 那个 URL 是畸形的
    # （/api 重复），100% 返回 404。它唯一的实际作用是把前一个端点的真实错误
    # 覆盖掉：上游 503 时，报出来的是它的 404，看着像端点写错了，其实是服务在抖。
    'endpoint': 'https://openrouter.ai/api/alpha/decisions',
    'model': '~typesafe/jev-latest',
    'context_n': 6,
    'workers': 8,
}

def load_config() -> dict:
    cfg = dict(DEFAULTS)
    if CONFIG.exists():
        try:
            cfg.update(json.loads(CONFIG.read_text(encoding='utf-8')))
        except Exception as e:
            print(f'[警告] config.json 解析失败({e})，使用默认配置')
    if not cfg.get('openrouter_api_key'):
        raise SystemExit(f'未找到 OpenRouter key，请在 {CONFIG} 里填 openrouter_api_key')
    return cfg


CFG = load_config()
ENDPOINT = CFG['endpoint']
MODEL = CFG['model']
PROXY = CFG['proxy']

IN = DATA / 'parsed.jsonl'
OUT = DATA / 'jev_results.jsonl'

CTX_N = CFG['context_n']    # 每条消息带前 N 条作上下文
WORKERS = CFG['workers']


def load_key() -> str:
    return CFG['openrouter_api_key']


# ---------------- 问题集：一对一 · 氛围与关系 ----------------

# 防上下文污染。注意措辞：**不能说「别用上文」**。
# 原版写的是「recent_context 只是背景上下文，不要把它的内容或情绪算到
# target_message 头上」，实测效果是模型连指代都不解析了 —— 给它一句「就那个」，
# 它不知道「那个」指什么。模型分不清"别用上文做结论"和"别用上文"。
# 改成正面说明上下文该怎么用，只在最后收一句「结论只描述这一条」。
_ONLY_TARGET = (
    '判定对象只有 target_message 这一条。'
    'recent_context 是你们之前的对话，每条都标了说话人和时间，'
    '要用它来判断：这一条在回应什么、里面的代词指向谁、'
    '和上一句隔了多久、当前聊到哪一步了。'
    '但最终结论只描述 target_message 这一条 ——'
    '不要直接把上文的问题、情绪或旧账算到它头上。 '
)

QUESTIONS = {
    # ---- 五道是非题 ----
    'positive': {
        'type': 'noul',
        'instructions': _ONLY_TARGET + '这条消息带有积极情绪吗？包括关心、赞同、热情、玩笑打趣、主动分享。',
        'criteria': {
            'true': '明显正面：关心对方、赞同、热情、开玩笑、主动分享事情。',
            'false': '中性陈述，或负面、冷淡、敷衍。',
        },
    },
    'perfunctory': {
        'type': 'noul',
        'instructions': _ONLY_TARGET + '这条消息显得敷衍或冷淡吗？指用极短回应应付（如「嗯」「哦」「好的」「再说吧」「随便」），或明显不愿把话题展开。',
        'criteria': {
            'true': '简短应付、冷淡、不愿展开、明显在结束话题。',
            'false': '有实质内容、正常回应，或主动把话题接下去。',
        },
    },
    'avoiding': {
        'type': 'noul',
        'instructions': _ONLY_TARGET + '这条消息在回避或推脱某个话题吗？比如岔开话题、用模糊说辞搪塞、拖着不正面回应。',
        'criteria': {
            'true': '明显回避、岔开、搪塞、拖延正面回应。',
            'false': '正面回应了，或只是普通的没听清、需要再说一遍。',
        },
    },
    'requesting': {
        'type': 'noul',
        'instructions': _ONLY_TARGET + '这条消息在提出请求、期待对方回应，或主动把话题抛给对方吗？',
        'criteria': {
            'true': '提问、请求帮助、邀约、主动引出新话题等对方接话。',
            'false': '只是陈述、感慨、汇报，不期待对方接话。',
        },
    },
    'extending': {
        'type': 'noul',
        'instructions': _ONLY_TARGET + '这条消息是在把对话往下延续，还是在收尾？指内容本身有没有给对话留出继续的空间。',
        'criteria': {
            'true': '有展开空间：补充信息、追问、引出新话题、表达情绪等着对方接。',
            'false': '收尾性质：道别、结束语、单方面通知、或敷衍到没有下文。',
        },
    },

    # ---- 两道分类题 ----
    'emotion': {
        'type': 'choice',
        'instructions': _ONLY_TARGET + '这条消息最主要的情绪是什么？',
        'criteria': {
            'warm': '热情、亲近、明显的关心或兴奋。',
            'friendly': '友好、轻松、正常的正面互动。',
            'neutral': '中性、事务性、陈述事实，不带情绪色彩。',
            'cold': '冷淡、疏离、敷衍、距离感。',
            'annoyed': '不耐烦、烦躁、抱怨、嫌弃。',
            'anxious': '焦虑、担心、紧张、不安。',
            'sad': '低落、失落、难过、委屈。',
        },
    },
    'intent': {
        'type': 'choice',
        'instructions': _ONLY_TARGET + '这条消息的主要意图是什么？',
        'criteria': {
            'chat': '闲聊、维系关系、分享日常、开玩笑。',
            'ask_help': '求助、请对方帮忙或解答。',
            'vent': '倾诉、吐槽、寻求情绪支持。',
            'coordinate': '事务协调：约时间、办事、传递信息。',
            'polite_close': '客套或收尾：道谢、道别、礼貌性回应。',
            'probe': '试探：探对方口风、态度、想法。',
            'notify': '通知、告知、单方面说明。',
        },
    },

    # ---- 两道打分题 ----
    'intensity': {
        'type': 'score',
        'instructions': _ONLY_TARGET + '这条消息的情绪强度有多高？从平淡到强烈。',
        'criteria': [
            '完全平淡，纯陈述',
            '略有情绪色彩',
            '情绪明显',
            '情绪强烈',
            '情绪非常强烈，几乎溢出来',
        ],
    },
    'engagement': {
        'type': 'score',
        'instructions': (
            _ONLY_TARGET +
            '这条消息体现出说话人对这段对话的投入程度有多高？'
            '投入高 = 认真回应、主动展开、给出细节、关心对方在说什么。'
            '投入低 = 敷衍、答非所问、一个字打发了事。'
        ),
        'criteria': [
            '完全没投入，敷衍到极点',
            '投入很低',
            '一般',
            '投入较高，认真回应',
            '投入很高，主动展开并关心对方',
        ],
    },
}


# ---------------- 客户端 ----------------

class JevClient:
    def __init__(self, key: str):
        self.key = key
        self.client = httpx.Client(
            timeout=60,
            proxy=PROXY,
            headers={
                'Authorization': f'Bearer {key}',
                'Content-Type': 'application/json',
                'HTTP-Referer': 'https://local.chat-analysis',
                'X-OpenRouter-Title': 'Chat Relationship Analysis',
            },
        )

    # 上游抖动是常态，实测过的两种：503「no healthy upstream」、529
    # 「system_overloaded」。同一条请求隔几秒重试就能过。不重试的话，抖一下
    # 这条消息就白解读了 —— 对方不会因为浮层报错就重发一遍。
    # 这些错误都是秒回的，重试代价很小；真撞上网络超时说明链路断了，
    # 那会儿慢一点也无所谓（反正本来也解读不了）。
    RETRY_CODES = (429, 500, 502, 503, 504, 529)

    def ask(self, state: dict, questions: dict = None, model: str = MODEL,
            retries: int = 3) -> dict:
        payload = {
            'model': model,
            'state': state,
            'questions': questions or QUESTIONS,
            'provider': {'allow_fallbacks': True},
        }
        # 攒着每一次的错误。原来只留最后一个，结果被后一个端点覆盖，
        # 报出来的永远是最没信息量的那个。
        errs = []
        for attempt in range(retries):
            try:
                r = self.client.post(ENDPOINT, json=payload)
            except httpx.HTTPError as e:            # 连接失败 / 超时 / 代理没开
                errs.append(f'网络 {type(e).__name__}: {e}')
                if attempt < retries - 1:
                    time.sleep(1.5 * (attempt + 1))
                continue

            if r.status_code == 200:
                return r.json()

            errs.append(f'HTTP {r.status_code}: {r.text[:200]}')

            # 4xx（429 除外）是我们自己的问题 —— payload 不合法之类，重试没用
            if r.status_code != 429 and 400 <= r.status_code < 500:
                break
            if attempt < retries - 1:
                time.sleep(1.5 * (attempt + 1))

        raise RuntimeError(' ‖ '.join(errs))

    def close(self):
        self.client.close()


def parse_answers(data: dict) -> dict:
    """把 Jev 返回的 answers 拉平成好用的结构。"""
    answers = data.get('answers') or {}
    out = {'_model': data.get('model'), '_raw': answers}
    for k, v in answers.items():
        if not isinstance(v, dict):
            continue
        if v.get('type') == 'noul' and v.get('noul') is not None:
            out[k] = round(max(0.0, min(1.0, float(v['noul']))), 4)
        elif v.get('type') == 'choice' and v.get('choice'):
            out[k] = str(v['choice'])
        elif v.get('type') == 'score' and v.get('score') is not None:
            out[k] = v['score']
    return out


# ---------------- 主流程 ----------------

def build_state(msgs: list, i: int) -> dict:
    ctx = [
        {'speaker': m['speaker'], 'text': m['text']}
        for m in msgs[max(0, i - CTX_N):i]
    ]
    state = {'target_message': msgs[i]['text'], 'speaker': msgs[i]['speaker']}
    if ctx:
        state['recent_context'] = ctx
    return state


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--test', action='store_true', help='只跑前 3 条')
    ap.add_argument('--limit', type=int, default=0)
    ap.add_argument('--workers', type=int, default=WORKERS)
    args = ap.parse_args()

    msgs = [json.loads(l) for l in IN.read_text(encoding='utf-8').splitlines() if l.strip()]
    print(f'载入 {len(msgs)} 条消息 from {IN}')

    if args.test:
        limit = 3
    elif args.limit:
        limit = args.limit
    else:
        limit = len(msgs)

    key = load_key()
    print(f'Key: {key[:14]}...  ({len(key)} 字符)')
    print(f'端点: {ENDPOINT}')
    print(f'模型: {MODEL}')
    print(f'问题数: {len(QUESTIONS)}   上下文: 前 {CTX_N} 条   并发: {args.workers}')
    print(f'待处理: {limit} 条\n')

    cli = JevClient(key)

    # ---- 连通性测试 ----
    if args.test:
        print('=' * 70)
        print('【连通性测试】单条请求，展示完整往返')
        print('=' * 70)
        st = build_state(msgs, 0)
        print('state:', json.dumps(st, ensure_ascii=False, indent=2)[:800])
        t0 = time.time()
        try:
            resp = cli.ask(st)
        except Exception as e:
            print(f'\n❌ 调用失败: {e}')
            cli.close()
            return
        dt = time.time() - t0
        print(f'\n✅ 成功  耗时 {dt:.2f}s')
        print('原始响应:')
        print(json.dumps(resp, ensure_ascii=False, indent=2)[:2500])
        print('\n解析后:')
        print(json.dumps(parse_answers(resp), ensure_ascii=False, indent=2)[:1500])
        cli.close()
        return

    # ---- 批量 ----
    t0 = time.time()
    results = [None] * limit
    errors = []

    def work(i):
        for attempt in range(3):
            try:
                data = cli.ask(build_state(msgs, i))
                return i, parse_answers(data), None
            except Exception as e:
                if attempt == 2:
                    return i, None, str(e)
                time.sleep(1.5 * (attempt + 1))

    done = 0
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = [ex.submit(work, i) for i in range(limit)]
        for f in as_completed(futs):
            i, res, err = f.result()
            done += 1
            if err:
                errors.append((i, err))
                print(f'  [{done}/{limit}] #{i} ❌ {err[:110]}')
            else:
                results[i] = res
                if done % 25 == 0 or done <= 5:
                    print(f'  [{done}/{limit}] #{i} "{msgs[i]["text"][:24]}" ok')

    dt = time.time() - t0

    with OUT.open('w', encoding='utf-8') as f:
        for i, r in enumerate(results):
            if r is None:
                continue
            rec = {'idx': i, **{k: msgs[i][k] for k in ('speaker', 'time', 'text')}, **r}
            f.write(json.dumps(rec, ensure_ascii=False) + '\n')

    ok = sum(1 for r in results if r)
    print(f'\n完成: {ok}/{limit} 成功, {len(errors)} 失败, 耗时 {dt:.1f}s')
    print(f'输出: {OUT}')
    cli.close()


if __name__ == '__main__':
    main()
