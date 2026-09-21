# -*- coding: utf-8 -*-
"""
Jev 判定质量测试：11 条消息，逐条看它在哪个维度还能打。

这是 KNOWN_ISSUES.md 第 3 节那张表的数据来源。

>> 用例说明 <<
原始用例取自一段真实微信对话（熟人闲聊）。为不泄露对话内容：
  - 涉及人名、游戏名、具体事件的句子，改写成等价的虚构例句
  - 时间戳全部替换为虚构值（只保留先后顺序）
  - 上下文只留下还原语气所必需的条数

Jev 的失败模式和具体句子无关（见 KNOWN_ISSUES.md 第 4 节），
所以这些用例依然能复现问题：
  - best_action 坍缩到 ask_clarify
  - emotion 被 annoyed 污染
  - 短消息落到 feedback

用法：
    python quality_test.py            # 需要 jev-chat/config.json 里有 API key
"""
import sys
import time
from pathlib import Path

sys.stdout.reconfigure(encoding='utf-8', errors='replace')

# 复用 jev-chat 的客户端和问题集（两个目录保持并列）
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'jev-chat'))

from jev_client import JevClient, load_key, parse_answers  # noqa: E402
from jev_read import (ACTION_CN, EMOTION_CN, INTENT_CN,      # noqa: E402
                      QUESTIONS, TONE_CN)

D = '对方'
M = '我'

# (标题, 对方原话, 前文)  —— 前文给 1~4 条，够还原当时的语境
CASES = [
    ('单个 emoji', '🤔', [
        (M, '10:29', '左边是状态，右边是概率'),
    ]),
    ('带火气的质问', '那你到底想干嘛啊', [
        (M, '10:34', '什么意思？'),
        (M, '10:34', '你有什么事呗？'),
        (D, '10:34', '不是'),
    ]),
    ('自辩 / 解释', '我这不是在帮忙吗', [
        (D, '10:37', '你不是要训练模型吗'),
    ]),
    ('追问结果', '所以识别到了吗', [
        (M, '10:38', '别发了啊'),
        (M, '10:38', '浪费我钱的来了'),
    ]),
    ('熟人调侃', '这ai没水平', [
        (D, '10:38', '那不就行了'),
        (M, '10:38', '没有'),
    ]),
    ('替对方设想', '如果是真的他们早就笑死了', [
        (M, '10:40', '图里那种是p的'),
        (D, '10:40', '我说白了像这种'),
    ]),
    ('下判断', '但是ai理解不了', [
        (M, '10:40', '你真神了'),
    ]),
    ('事后吐槽', '还是没水平啊', [
        (D, '10:24', '哈哈'),
        (M, '10:24', '改两下就出 bug'),
    ]),
    ('短句求助', '看不懂', [
        (M, '10:27', '[图片]'),
        (M, '10:27', '有点说法啊'),
    ]),
    ('玩梗', '我喜欢你', [
        (M, '10:29', '左边是状态，右边是概率'),
        (D, '10:29', '🤔'),
    ]),
    ('长段子', '你那个游戏又不玩了是吧，这次新出的你还不玩就退网吧，看不到你一点进步，真的。'
              '玩笑归玩笑，最开始还以为你在哪找的素材，已经分不清是游戏还是现实了，'
              '有一说一这游戏真是今年一匹黑马吧。不吹不黑，本人各类游戏加起来上万小时，'
              '市面上大多数都玩过，真感觉不出来和它有什么细节差距。'
              '别说哥们没提醒你，以后大伙儿在那边聊得热火朝天，就你在旁边急得插不上话，'
              '只能发发自己被打脸的图然后被人无视用消息刷过去，'
              '最后只能尴尬的天天复读别人的聊天内容。这一切都是因为前阵子你没玩。', [
        (M, '10:27', '有点说法啊'),
        (D, '10:28', '🤔'),
    ]),
]


def top(probs, cn, n=3):
    if not probs:
        return '-'
    items = sorted(probs.items(), key=lambda x: -x[1])[:n]
    return ' '.join(f'{cn.get(k, k)}{v*100:.0f}%' for k, v in items)


def main():
    cli = JevClient(load_key())
    try:
        for i, (title, msg, ctx) in enumerate(CASES, 1):
            state = {'target_message': msg, 'speaker': D}
            if ctx:
                state['recent_context'] = [
                    {'speaker': s, 'time': t, 'text': x} for s, t, x in ctx]
            t0 = time.time()
            try:
                ans = parse_answers(cli.ask(state, questions=QUESTIONS))
            except Exception as e:
                print(f'\n[{i}] {title}  ✗ {e}')
                continue
            raw = ans.get('_raw') or {}
            probs = lambda k: (raw.get(k) or {}).get('probabilities') or {}
            ls = ans.get('literal_same')
            tn = ans.get('tone')

            print(f'\n[{i}] {title}   「{msg[:28]}{"…" if len(msg) > 28 else ""}」   {time.time()-t0:.1f}s')
            if ls is not None:
                print(f'     潜台词   直说 {ls*100:.0f}% / 有潜台词 {(1-ls)*100:.0f}%')
            print(f'     情绪     {top(probs("emotion"), EMOTION_CN)}')
            if tn is not None:
                idx = max(0, min(len(TONE_CN) - 1, int(round(tn))))
                print(f'     语气     {tn+1:.1f}/5 {TONE_CN[idx]}')
            print(f'     真实意图 {top(probs("real_intent"), INTENT_CN)}')
            print(f'     怎么回   {top(probs("best_action"), ACTION_CN)}')
    finally:
        cli.close()


if __name__ == '__main__':
    main()
