# -*- coding: utf-8 -*-
"""
Jev 即时解读 —— 给一条别人发来的消息，立刻出「TA 在想什么 / 我该怎么回」。

通用聊天分析，不预设关系类型：朋友、同事、家人、对象都能用。

用法：
    python jev_read.py "这个方案你什么时候能给我？"
    python jev_read.py "行吧" -c "方案改完了吗|我今天下午发你"
    python jev_read.py -i            # 交互模式，连续输入
"""
import sys
import json
import argparse
from pathlib import Path

sys.stdout.reconfigure(encoding='utf-8', errors='replace')

sys.path.insert(0, str(Path(__file__).parent))
from jev_client import JevClient, load_key  # noqa: E402

_ONLY_TARGET = (
    '判定对象只有 target_message 这一条（对方刚发给我的）。'

    'recent_context 是**本次**对话的前文，每条都标了说话人和时间，'
    '要用它判断：这一条在回应什么、里面的代词指向谁、'
    '和上一句隔了多久、当前聊到哪一步了。'

    'previous_conversation 是**更早一次**的对话 —— 和本次之间隔了一段没说话的'
    '时间，两段接不上。它要不要算进来由你判断：'
    '如果 target_message 在延续那次的话题、回应其中的内容、'
    '或提到了那次聊过的事（「那个」「上次说的」之类），就把那次的内容一起算进来；'
    '如果它开的是全新话题、跟那次没关系，就当那次不存在。'
    '注意两段的先后和时间间隔本身也是信息：'
    '隔了三天才回一句，和隔了三十秒，含义完全不同。'

    '但最终结论只描述 target_message 这一条 ——'
    '不要直接把上文的问题、情绪或旧账算到它头上。 '
)

QUESTIONS = {
    # ---- 潜台词 ----
    'literal_same': {
        'type': 'noul',
        'instructions': (
            _ONLY_TARGET +
            '这条消息字面上说的，和它真正想表达的意思，是同一件事吗？'
            '中文里很多话不说本意：反问常常是在表达不满，'
            '「随便」「都行」常常是心里有想法但不想先说，'
            '「没事」常常是有事。'
            '凡是字面和实际意图对不上的，都算"不是同一件事"。'
        ),
        'criteria': {
            'true': '表面说的就是真实想说的，直来直去。',
            'false': '表面是一个说法，真实想表达的是别的东西。',
        },
    },
    # ---- 情绪状态 ----
    'emotion': {
        'type': 'choice',
        'instructions': _ONLY_TARGET + '对方发这条消息时，最主要的情绪状态是什么？',
        'criteria': {
            'calm': '平静，没有情绪起伏，就是正常说话。',
            'happy': '心情好，开心、轻松、有兴致。',
            'annoyed': '烦躁、不耐烦、有火气。',
            'down': '低落、失落、提不起劲。',
            'excited': '兴奋、激动，情绪高于平常。',
            'perfunctory': '敷衍、应付，不想多说。',
            'confused': '困惑、没搞明白，在问清楚。',
            'serious': '认真、郑重，在说正事。',
        },
    },
    # ---- 语气亲疏（打分）----
    'tone': {
        'type': 'score',
        'instructions': (
            _ONLY_TARGET +
            '这条消息的语气，在「冷淡生分」到「热络亲近」之间处于哪一档？'
            '看用词、语气词、称呼、句子长短、有没有主动把话展开。'
        ),
        'criteria': [
            '冷淡、生分，有明显距离感',
            '客气、公事公办，礼貌但不亲近',
            '普通、平和，正常交流',
            '熟络、随意，有亲近感',
            '热络、亲密，语气里有温度',
        ],
    },
    # ---- 真实意图 ----
    'real_intent': {
        'type': 'choice',
        'instructions': (
            _ONLY_TARGET +
            '对方发这条消息，真正的意图最可能是什么？'
            '注意：人常常不直接说本意，而是用反问、抱怨、暗示来表达。'
            '凡是问句、反问句、带抱怨或审视意味的，优先考虑情绪类意图，'
            '不要轻易判成"单纯问信息"。'
        ),
        'criteria': {
            'ask_info': '问信息，想得到一个答案。',
            'ask_help': '求助，需要我帮忙做某件事。',
            'share': '分享、告知，把一件事说给我听。',
            'vent': '吐槽、发牢骚，想说说而已。',
            'joke': '开玩笑、打趣、逗我。',
            'make_plan': '约时间、定安排、推进某件事。',
            'smalltalk': '闲聊、客套、维系联系。',
            'feedback': '给反馈、提意见，包括表达不满。',
        },
    },
    # ---- 关系状态 ----
    'relationship': {
        'type': 'choice',
        'instructions': (
            _ONLY_TARGET +
            '结合上文看，你们此刻这段对话的气氛是哪一种？'
            '注意这条消息在整段对话里起的作用，不只看它自己的措辞。'
        ),
        'criteria': {
            'easy': '轻松聊天，氛围愉快、随便。',
            'normal': '正常交流，平常状态，没什么特别。',
            'reserved': '有点拘谨、端着，或者有客气的距离感。',
            'off': '明显不对劲，气氛紧、有话没说出来。',
        },
    },
    # ---- 对方期待 ----
    'need': {
        'type': 'choice',
        'instructions': _ONLY_TARGET + '对方发这条消息，期待我给TA什么样的回应？',
        'criteria': {
            'answer': '要一个答案或信息。',
            'action': '要我动手做点什么、给个方案。',
            'listening': '要我听TA说、接住情绪。',
            'agreement': '要我表态、认同、站在TA那边。',
            'company': '要我陪着、一起做点什么。',
            'nothing': '不需要我做什么，随口一说。',
        },
    },
    # ---- 怎么回 ----
    'best_action': {
        'type': 'choice',
        'instructions': (
            _ONLY_TARGET +
            '我最好的回应方式是什么？'
            '结合现在的气氛、对方的情绪和真实意图，选最合适的一种。'
        ),
        'criteria': {
            'answer_directly': '直接正面回答。',
            'ask_clarify': '先追问澄清，确认TA到底什么意思。',
            'give_plan': '给出具体方案或下一步怎么做。',
            'empathize': '先接情绪，别急着讲道理。',
            'joke': '轻松一点，开个玩笑把气氛放软。',
            'turn_down': '婉拒，说明做不到或不去。',
            'hold': '先不接，等会儿再说。',
            'verify_first': '先翻记录、查证再答，别凭记忆硬答。',
        },
    },
}

# ---- 标签（CLI 版）----

EMOTION_CN = {
    'calm': '平静', 'happy': '开心', 'annoyed': '烦躁', 'down': '低落',
    'excited': '兴奋', 'perfunctory': '敷衍', 'confused': '困惑',
    'serious': '认真',
}
TONE_CN = ['冷淡、生分', '客气、公事公办', '普通、平和', '熟络、随意', '热络、亲密']
INTENT_CN = {
    'ask_info': '问信息', 'ask_help': '求助', 'share': '分享 / 告知',
    'vent': '吐槽', 'joke': '开玩笑', 'make_plan': '约时间',
    'smalltalk': '闲聊 / 客套', 'feedback': '给反馈 / 不满',
}
RELATION_CN = {
    'easy': '轻松聊天', 'normal': '正常交流',
    'reserved': '有点拘谨', 'off': '明显不对劲',
}
NEED_CN = {
    'answer': '要答案', 'action': '要具体方案', 'listening': '要倾听',
    'agreement': '要表态 / 认同', 'company': '要陪同', 'nothing': '不需要什么',
}
ACTION_CN = {
    'answer_directly': '直接回答', 'ask_clarify': '先追问澄清',
    'give_plan': '给具体方案', 'empathize': '先接情绪',
    'joke': '开个玩笑', 'turn_down': '婉拒',
    'hold': '先不接', 'verify_first': '先查证再答',
}


def bar(v: float, width: int = 24) -> str:
    n = int(round(v * width))
    return '█' * n + '·' * (width - n)


def pct(v):
    return f'{v*100:>3.0f}%'


def disp_width(s):
    """终端里一个汉字占两格。str.ljust 按字符数算，中文标签会参差不齐。"""
    return sum(2 if ord(c) > 0x2E7F else 1 for c in s)


def pad(s, n):
    return s + ' ' * max(0, n - disp_width(s))


def bar_block(p, title, probs, cn, limit=4):
    """画一个 choice 类型的概率块"""
    if not probs:
        return
    p(f'\n  ▸ {title}')
    for i, (k, v) in enumerate(sorted(probs.items(), key=lambda x: -x[1])[:limit]):
        mark = '★' if i == 0 else ' '
        p(f'    {mark} {pad(cn.get(k, k), 20)} {pct(v)}  {bar(v, 18)}')


def render(msg, ans, ctx=None):
    L = []
    p = L.append
    W = 62

    p('╔' + '═' * W + '╗')
    p('║' + '  Jev 解读'.ljust(W) + '║')
    p('╚' + '═' * W + '╝')

    if ctx:
        p('\n  上文：')
        for c in ctx:
            p(f'    · {c}')
    p(f'\n  对方消息：「{msg}」\n')
    p('  ' + '─' * W)

    raw_all = ans.get('_raw', {})
    probs_of = lambda k: (raw_all.get(k) or {}).get('probabilities') or {}

    # ---- 潜台词 ----
    ls = ans.get('literal_same')
    if ls is not None:
        p('\n  ▸ 潜台词')
        p(f'      说的就是想的   {pct(ls)}  {bar(ls)}')
        p(f'      话里有话       {pct(1-ls)}  {bar(1-ls)}')
        if ls < 0.5:
            p('      ⚠ 字面不等于本意，别照字面接')

    # ---- 情绪状态 ----
    bar_block(p, '情绪状态', probs_of('emotion'), EMOTION_CN, 3)

    # ---- 语气亲疏 ----
    tn = ans.get('tone')
    if tn is not None:
        i = max(0, min(len(TONE_CN) - 1, int(round(tn))))
        p('\n  ▸ 语气亲疏')
        p(f'      {tn+1:.1f} / 5   {bar((tn+1)/5, 20)}')
        p(f'      {TONE_CN[i]}')

    # ---- 真实意图 ----
    bar_block(p, '真实意图', probs_of('real_intent'), INTENT_CN)

    # ---- 关系状态 ----
    bar_block(p, '关系状态', probs_of('relationship'), RELATION_CN, 2)

    # ---- 对方期待 ----
    bar_block(p, '对方期待', probs_of('need'), NEED_CN, 3)

    # ---- 怎么回 ----
    bar_block(p, '怎么回', probs_of('best_action'), ACTION_CN)

    p('\n  ' + '─' * W)
    return '\n'.join(L)


def ask(cli, msg, ctx=None):
    state = {'target_message': msg}
    if ctx:
        state['recent_context'] = [{'speaker': '对方', 'text': c} for c in ctx]
    resp = cli.ask(state, questions=QUESTIONS)
    from jev_client import parse_answers
    ans = parse_answers(resp)
    return ans, resp.get('usage', {})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('message', nargs='?', help='对方发的消息')
    ap.add_argument('-c', '--context', default='', help='上文，多条用 | 分隔')
    ap.add_argument('-i', '--interactive', action='store_true', help='交互模式')
    args = ap.parse_args()

    cli = JevClient(load_key())
    ctx = [x.strip() for x in args.context.split('|') if x.strip()] if args.context else None

    try:
        if args.interactive or not args.message:
            print('Jev 即时解读 —— 输入对方的消息，回车出结果')
            print('（上文可用 -c 指定；直接回车退出）\n')
            while True:
                try:
                    m = input('对方说> ').strip()
                except (EOFError, KeyboardInterrupt):
                    break
                if not m:
                    break
                ans, usage = ask(cli, m, ctx)
                print('\n' + render(m, ans, ctx))
                print(f'\n  [成本 ${usage.get("cost",0):.6f}]\n')
                ctx = (ctx or []) + [m]
        else:
            ans, usage = ask(cli, args.message, ctx)
            print('\n' + render(args.message, ans, ctx))
            print(f'\n  成本 ${usage.get("cost", 0):.6f}')
    finally:
        cli.close()


if __name__ == '__main__':
    main()
