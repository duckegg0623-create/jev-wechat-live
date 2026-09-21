# -*- coding: utf-8 -*-
"""
消息碎片合并。

微信里一句话被拆成好几条发出来是常态：「我」「觉得」「这样不行」。
不管的话有两个后果：

  1. JEV 把三片当成三条独立消息 → 直接误判
  2. 碎片进了上下文，之后**每一次**解读的 recent_context 里
     都躺着这堆碎片 → 污染会一直滚下去

所以预热（从库里读历史）和实时（对方正在发）两条路都得走这里。
"""
import re

# 句末标点：出现这些基本可以认定说完了
END_PUNCT = '。！？!?…～~'
# 句中停顿：出现这些多半还有下半句
MID_PUNCT = '，,、；;：:'

_CJK = re.compile(r'[　-〿一-鿿＀-￯]')


def _is_cjk(ch):
    return bool(_CJK.match(ch))


def smart_join(parts):
    """
    拼碎片。

    中文之间直接连（「我」+「觉得」=「我觉得」），
    其他情况补个空格（「ok」+「ok」不该粘成「okok」）。
    """
    out = ''
    for p in parts:
        p = (p or '').strip()
        if not p:
            continue
        if out and not (_is_cjk(out[-1]) or _is_cjk(p[0])):
            out += ' '
        out += p
    return out


def looks_finished(text):
    """
    这条看起来说完了吗。
    True=说完了  False=明显没说完  None=看不出来
    """
    t = (text or '').strip()
    if not t:
        return False
    if t[-1] in END_PUNCT:
        return True
    if t[-1] in MID_PUNCT:
        return False
    return None


def silence_window(parts, cfg=None):
    """
    收完最后一条之后，还要静多久才认定对方说完了。

    不用固定值 —— 「好的。」和「我」显然不该等一样久。
    """
    cfg = cfg or {}
    d_def = cfg.get('default', 1.5)
    d_end = cfg.get('end_punct', 0.6)
    d_mid = cfg.get('mid_punct', 2.0)
    d_short = cfg.get('short', 2.2)

    last = (parts[-1] or '').strip() if parts else ''
    fin = looks_finished(last)

    if fin is True:
        base = d_end
    elif fin is False:
        base = d_mid
    elif len(last) <= 5:
        base = d_short          # 「嗯」「？」这种，多半还有下句
    else:
        base = d_def

    # 已经在分片了 = 对方正连着说，再多给点时间。
    # 加成要封顶：对方已经发了 5 片的话，说明节奏就这样，
    # 再一直往上加只会让浮层越来越慢。
    if len(parts) > 1:
        base += 0.4 * min(len(parts) - 1, 2)
    return base


def should_merge(prev_text, prev_ts, cur_ts, gap=8.0):
    """
    这一条要不要并到上一条里。

    三个条件都满足才并：同一个人（调用方保证）、间隔够近、上一句没说完。
    「好的。」说完就完了，隔几秒再来一句是新的意思，不能并。
    """
    if cur_ts - prev_ts > gap:
        return False
    if looks_finished(prev_text) is True:
        return False
    return True


def group(msgs, gap=8.0):
    """
    把同一人连发的碎片并成一条。

    输入 [{speaker, text, ts, ...}]，输出同结构 + parts（保留原始分片）。
    """
    out = []
    for m in msgs:
        text = (m.get('text') or '').strip()
        if not text:
            continue
        if out and out[-1]['speaker'] == m['speaker'] \
                and should_merge(out[-1]['text'], out[-1]['ts'], m['ts'], gap):
            out[-1]['parts'].append(text)
            out[-1]['text'] = smart_join(out[-1]['parts'])
            out[-1]['ts'] = m['ts']
        else:
            out.append({
                'speaker': m['speaker'],
                'text': text,
                'ts': m['ts'],
                'parts': [text],
            })
    return out


# ---------------- 自测 ----------------

if __name__ == '__main__':
    import sys
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

    cases = [
        (['我', '觉得', '这样不行'], '中文分片直接连'),
        (['ok', 'ok'], '英文分片要空格'),
        (['你说的对。', '那我们明天去吧'], '句末标点不该并'),
        (['嗯', '？'], '超短消息'),
        (['那', '要不', '我们', '还是', '算了吧'], '五片'),
    ]
    for parts, desc in cases:
        print(f'{desc:<12} {parts} → {smart_join(parts)!r}')
        print(f'{"":<12}   静默窗口 {silence_window(parts):.1f}s  '
              f'说完了={looks_finished(parts[-1])}')

    print('\n--- group() ---')
    msgs = [
        {'speaker': '对方', 'text': '我', 'ts': 100},
        {'speaker': '对方', 'text': '觉得', 'ts': 101},
        {'speaker': '对方', 'text': '这样不行', 'ts': 103},
        {'speaker': '我', 'text': '为什么', 'ts': 110},
        {'speaker': '对方', 'text': '你自己想。', 'ts': 118},
        {'speaker': '对方', 'text': '每次都这样', 'ts': 125},
    ]
    for g in group(msgs):
        print(f'  [{g["ts"]}] {g["speaker"]}: {g["text"]!r}   ← {len(g["parts"])} 片')
