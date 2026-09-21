# -*- coding: utf-8 -*-
"""
A/B 实验：同一条消息，只改「能不能看到 51 分钟前的关键线索」，看判定变不变。

这是 KNOWN_ISSUES.md 第 5 节那张表的数据来源。

假设（已被推翻）：长段子被判「开玩笑」，是因为 150 字上下文窗口
够不到 51 分钟前那三句关键线索（对方明说了是在配合测试）。

    A = 现在 150 字上下文实际能取到的 12 条
    B = A + 更早那三句线索

结果：判定几乎没变（开玩笑 76% → 70%），假设推翻。
说明 Jev 是对当前这句做字面分类，不做多步推理 —— 把线索给它，它也用不上。

>> 用例说明 <<
和 quality_test.py 一样，例句做了等价替换，去掉具体人名和游戏名。

用法：
    python ab_context_test.py
"""
import sys
import time
from pathlib import Path

sys.stdout.reconfigure(encoding='utf-8', errors='replace')

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'jev-chat'))

from jev_client import JevClient, load_key, parse_answers  # noqa: E402
from jev_read import (ACTION_CN, EMOTION_CN, INTENT_CN,      # noqa: E402
                      QUESTIONS, RELATION_CN, TONE_CN)

TARGET = (
    '你那个游戏又不玩了是吧，这次新出的你还不玩就退网吧，看不到你一点进步，真的。'
    '玩笑归玩笑，最开始还以为你在哪找的素材，已经分不清是游戏还是现实了，'
    '有一说一这游戏真是今年一匹黑马吧。不吹不黑，本人各类游戏加起来上万小时，'
    '市面上大多数都玩过，真感觉不出来和它有什么细节差距。难怪这类游戏差评这么多，'
    '它真的是动作游戏爱好者福音好吧。很多老游戏也都玩过了，'
    '只有它能给我一种刚接触动作游戏时的感动。别说哥们没提醒你，'
    '以后大伙儿在那边聊得热火朝天，就你在旁边急得插不上话，'
    '只能发发自己被打脸的图然后被人无视用消息刷过去，'
    '最后只能尴尬的天天复读别人的聊天内容。这一切都是因为前阵子你没玩。'
)

# A：150 字窗口从目标往前，取到这些就到头了
# 时间为虚构值（保持先后顺序和间隔），下同
A_CTX = [
    {'speaker': '我', 'time': '10:23', 'text': '改两下就出 bug'},
    {'speaker': '对方', 'time': '10:24', 'text': '哈哈'},
    {'speaker': '对方', 'time': '10:24', 'text': '还是没水平啊'},
    {'speaker': '我', 'time': '10:25', 'text': '失败才是常态'},
    {'speaker': '我', 'time': '10:25', 'text': '要是一次就能跑通我早去大厂上班了'},
    {'speaker': '对方', 'time': '10:25', 'text': '招'},
    {'speaker': '我', 'time': '10:26', 'text': '没招了'},
    {'speaker': '我', 'time': '10:26', 'text': '[图片]'},
    {'speaker': '对方', 'time': '10:27', 'text': '看不懂'},
    {'speaker': '我', 'time': '10:27', 'text': '[图片]'},
    {'speaker': '我', 'time': '10:27', 'text': '有点说法啊'},
    {'speaker': '对方', 'time': '10:28', 'text': '🤔'},
]

# B 多出来的：51 分钟前的「这是在配合测试」线索
B_EXTRA = [
    {'speaker': '对方', 'time': '09:37', 'text': '你不是要训练模型吗'},
    {'speaker': '对方', 'time': '09:37', 'text': '我这不是在帮忙吗'},
    {'speaker': '我', 'time': '09:37', 'text': '现在只是测试发消息能不能识别到'},
]


def top3(probs, cn, n=3):
    if not probs:
        return '-'
    items = sorted(probs.items(), key=lambda x: -x[1])[:n]
    return ' / '.join(f'{cn.get(k, k)} {v*100:.0f}%' for k, v in items)


def run(cli, name, ctx):
    state = {'target_message': TARGET, 'speaker': '对方'}
    if ctx:
        state['recent_context'] = ctx
    t0 = time.time()
    ans = parse_answers(cli.ask(state, questions=QUESTIONS))
    raw = ans.get('_raw') or {}
    probs = lambda k: (raw.get(k) or {}).get('probabilities') or {}

    print(f'\n===== {name}   ({time.time()-t0:.1f}s) =====')
    ls = ans.get('literal_same')
    if ls is not None:
        print(f'  潜台词    说的就是想的 {ls*100:.0f}%  /  话里有话 {(1-ls)*100:.0f}%')
    print(f'  情绪      {top3(probs("emotion"), EMOTION_CN)}')
    tn = ans.get('tone')
    if tn is not None:
        i = max(0, min(len(TONE_CN) - 1, int(round(tn))))
        print(f'  语气      {tn+1:.1f}/5  {TONE_CN[i]}')
    print(f'  真实意图  {top3(probs("real_intent"), INTENT_CN)}')
    print(f'  关系状态  {top3(probs("relationship"), RELATION_CN, 2)}')
    print(f'  怎么回    {top3(probs("best_action"), ACTION_CN)}')
    return ans


def main():
    cli = JevClient(load_key())
    try:
        run(cli, 'A ── 现在（150 字，看不到更早那三句）', A_CTX)
        run(cli, 'B ── 加上更早的三句线索', B_EXTRA + A_CTX)
    finally:
        cli.close()


if __name__ == '__main__':
    main()
