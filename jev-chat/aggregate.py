# -*- coding: utf-8 -*-
"""
把 Jev 逐条判定聚合成「一对一 · 氛围与关系」报告。

Jev 只负责逐条窄判定，宏观图景在这里用代码拼出来。
"""
import sys
import json
import argparse
from pathlib import Path
from datetime import datetime
from collections import Counter, defaultdict

sys.stdout.reconfigure(encoding='utf-8', errors='replace')

BASE = Path(__file__).parent
DATA = BASE / 'data'


def bar(v: float, width: int = 26, ch: str = '█') -> str:
    n = int(round(v * width))
    return ch * n + '·' * (width - n)


def load(path):
    rows = []
    for line in Path(path).read_text(encoding='utf-8').splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def split_sessions(rows, gap_min=90):
    """按时间间隔切分会话"""
    sessions, cur = [], [rows[0]]
    for prev, r in zip(rows, rows[1:]):
        t1 = datetime.strptime(prev['time'], '%Y-%m-%d %H:%M')
        t2 = datetime.strptime(r['time'], '%Y-%m-%d %H:%M')
        if (t2 - t1).total_seconds() / 60 > gap_min:
            sessions.append(cur)
            cur = []
        cur.append(r)
    sessions.append(cur)
    return sessions


def build_turns(rows, gap_min=30):
    """
    把同一说话人的连续消息合并为一个「话轮」。

    必要性：有人习惯把一句话拆成多条发（「我」「觉得」「这样不行」分三条发出来，
    拼起来才是一句完整的话），按单条统计会把发言量算虚高、把对方的回应机会算没。
    """
    turns = []
    for r in rows:
        t = datetime.strptime(r['time'], '%Y-%m-%d %H:%M')
        if turns:
            prev = turns[-1]
            pt = datetime.strptime(prev['msgs'][-1]['time'], '%Y-%m-%d %H:%M')
            if (prev['speaker'] == r['speaker']
                    and (t - pt).total_seconds() / 60 <= gap_min):
                prev['msgs'].append(r)
                continue
        turns.append({'speaker': r['speaker'], 'msgs': [r]})
    return turns


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--me', required=True, help='哪个昵称是你自己')
    ap.add_argument('--in', dest='inp', default=str(DATA / 'jev_results.jsonl'),
                    help='Jev 逐条结果 jsonl')
    ap.add_argument('-o', '--out', default=str(DATA / '分析报告.txt'),
                    help='报告输出路径')
    args = ap.parse_args()
    ME = args.me
    OUT = Path(args.out)

    rows = load(args.inp)
    speakers = list(dict.fromkeys(r['speaker'] for r in rows))
    OTHER = next((s for s in speakers if s != ME), speakers[0])

    L = []
    p = L.append

    p('=' * 78)
    p('  一对一聊天 · 氛围与关系分析报告')
    p(f'  消息 {len(rows)} 条 · {rows[0]["time"][:10]} ~ {rows[-1]["time"][:10]}')
    p(f'  说话人：{OTHER}（对方） / {ME}（你自己）')
    p('  判定引擎：TypeSafe Jev 1.13 · 逐条 9 项窄判定')
    p('=' * 78)

    # ---------------- 一、总体对比 ----------------
    def stats(sp):
        sub = [r for r in rows if r['speaker'] == sp]
        n = len(sub)
        return {
            'n': n,
            'chars': sum(len(r['text']) for r in sub) / n,
            'total_chars': sum(len(r['text']) for r in sub),
            'positive': sum(r.get('positive', 0) for r in sub) / n,
            'perfunctory': sum(r.get('perfunctory', 0) for r in sub) / n,
            'avoiding': sum(r.get('avoiding', 0) for r in sub) / n,
            'requesting': sum(r.get('requesting', 0) for r in sub) / n,
            'extending': sum(r.get('extending', 0) for r in sub) / n,
            'intensity': sum(r.get('intensity', 0) or 0 for r in sub) / n,
            'engagement': sum(r.get('engagement', 0) or 0 for r in sub) / n,
        }

    s_other, s_me = stats(OTHER), stats(ME)

    p('\n【一】总体对比\n')
    p(f'  {"指标":<16}{OTHER:>12}{ME:>12}     对比')
    p('  ' + '─' * 66)

    def cmp_line(label, k, fmt='{:.2f}', invert=False):
        a, b = s_other[k], s_me[k]
        if invert:
            tag = f'{OTHER} 更{label}' if a > b else (f'你更{label}' if b > a else '持平')
        else:
            tag = f'{OTHER} 更高' if a > b else ('你更高' if b > a else '持平')
        p(f'  {label:<16}{fmt.format(a):>12}{fmt.format(b):>12}     {tag}')

    cmp_line('发言条数', 'n', '{:.0f}')
    cmp_line('平均字数', 'chars', '{:.1f}')
    cmp_line('总字数', 'total_chars', '{:.0f}')
    p('  ' + '─' * 66)
    cmp_line('积极率', 'positive')
    cmp_line('敷衍倾向', 'perfunctory', invert=True)
    cmp_line('回避倾向', 'avoiding', invert=True)
    p('  ' + '─' * 66)
    cmp_line('主动抛话', 'requesting')
    cmp_line('延续话题', 'extending')
    p('  ' + '─' * 66)
    cmp_line('情绪强度(0-4)', 'intensity')
    cmp_line('投入程度(0-4)', 'engagement')

    # ---------------- 二、主动性天平 ----------------
    p('\n【二】主动性天平\n')
    tot = s_other['n'] + s_me['n']
    p(f'  发言量   {OTHER:<10} {bar(s_other["n"]/tot, 30)} {s_other["n"]}')
    p(f'  {"":<10} {ME:<10} {bar(s_me["n"]/tot, 30)} {s_me["n"]}')

    sessions = split_sessions(rows)
    init = Counter(s[0]['speaker'] for s in sessions)
    fini = Counter(s[-1]['speaker'] for s in sessions)
    p(f'\n  会话数 {len(sessions)}（间隔 > 90 分钟切开）')
    p(f'  发起会话   {OTHER:<10} {init[OTHER]:>3} 次   {ME} {init[ME]:>3} 次')
    p(f'  结束会话   {OTHER:<10} {fini[OTHER]:>3} 次   {ME} {fini[ME]:>3} 次')
    if init[ME] > init[OTHER]:
        p(f'  → 多数会话是**你**先开口（{init[ME]}/{len(sessions)}）')
    else:
        p(f'  → 多数会话是**{OTHER}**先开口（{init[OTHER]}/{len(sessions)}）')

    # ---------------- 三、话轮与回应力度 ----------------
    p('\n【三】话轮与回应力度（核心指标）\n')

    turns = build_turns(rows)
    tn_o = sum(1 for t in turns if t['speaker'] == OTHER)
    tn_m = sum(1 for t in turns if t['speaker'] == ME)
    p(f'  话轮总数 {len(turns)}　{OTHER} {tn_o} 轮 / 你 {tn_m} 轮')
    p(f'  {OTHER} 平均每轮 {s_other["n"]/tn_o:.1f} 条，你 平均每轮 {s_me["n"]/tn_m:.1f} 条')

    def reply_size(src):
        vals = []
        for i, t in enumerate(turns[:-1]):
            if t['speaker'] != src:
                continue
            nxt = turns[i + 1]
            if nxt['speaker'] == src:
                continue
            vals.append(len(nxt['msgs']))
        return (sum(vals) / len(vals)) if vals else 0.0, len(vals)

    def topic_cont(src):
        vals = []
        for i, t in enumerate(turns[:-1]):
            if t['speaker'] != src:
                continue
            nxt = turns[i + 1]
            if nxt['speaker'] == src:
                continue
            if not any(m.get('extending', 0) >= 0.7 and m.get('requesting', 0) >= 0.5
                       for m in t['msgs']):
                continue
            vals.append(len(nxt['msgs']))
        return (sum(vals) / len(vals)) if vals else 0.0, len(vals)

    r_o, rn_o = reply_size(OTHER)   # 对方说完 → 你回几条
    r_m, rn_m = reply_size(ME)      # 你说完 → 对方回几条
    p(f'\n  回应力度（一个话轮结束后，对方下一个话轮有几条）')
    p(f'    {OTHER} 说完 → 你平均回 {r_o:.2f} 条  (n={rn_o})')
    p(f'    你说完   → {OTHER} 平均回 {r_m:.2f} 条  (n={rn_m})')

    t_o, tn2_o = topic_cont(OTHER)
    t_m, tn2_m = topic_cont(ME)
    p(f'\n  话题延续（话轮内含「抛出新话题」性质的消息时）')
    p(f'    {OTHER} 抛话题 {tn2_o} 次 → 你平均接 {t_o:.2f} 条')
    p(f'    你 抛话题 {tn2_m} 次 → {OTHER} 平均接 {t_m:.2f} 条')

    if r_o and r_m:
        ratio = r_m / r_o
        if ratio < 0.75:
            p(f'\n  ⚠ 你对 {OTHER} 的回应力度是对方的 {1/ratio:.1f} 倍')
            p('     → 你回得更长、更认真；对方回你偏简短')
        elif ratio > 1.33:
            p(f'\n  ⚠ 对方对你的回应力度是你的 {ratio:.1f} 倍')
            p(f'     → {OTHER} 回你更认真；你回对方偏简短')
        else:
            p(f'\n  → 双方回应力度接近（比值 {ratio:.2f}），互动比较对等')

    # 谁的话轮之后没人接（对话就此停下）
    last_o = sum(1 for t in turns if t['speaker'] == OTHER)
    drop_o = sum(1 for i, t in enumerate(turns[:-1])
                 if t['speaker'] == OTHER and turns[i + 1]['speaker'] == OTHER)
    tail = turns[-1]['speaker']
    p(f'\n  收尾：整段记录最后一条由 ' + ('你' if tail == ME else OTHER) + ' 发出')

    # ---------------- 四、情绪分布 ----------------
    p('\n【四】情绪分布\n')
    for sp, label in ((OTHER, '对方'), (ME, '你')):
        sub = [r for r in rows if r['speaker'] == sp]
        c = Counter(r.get('emotion', '?') for r in sub)
        p(f'  {label}（{sp}）')
        for k, v in c.most_common():
            p(f'    {k:<12} {bar(v/len(sub), 22)} {v/len(sub)*100:5.1f}% {v}')
        p('')

    # ---------------- 五、意图分布 ----------------
    p('\n【五】意图分布\n')
    for sp, label in ((OTHER, '对方'), (ME, '你')):
        sub = [r for r in rows if r['speaker'] == sp]
        c = Counter(r.get('intent', '?') for r in sub)
        p(f'  {label}（{sp}）')
        for k, v in c.most_common():
            p(f'    {k:<14} {bar(v/len(sub), 20)} {v/len(sub)*100:5.1f}% {v}')
        p('')

    # ---------------- 六、温度曲线 ----------------
    p('\n【六】温度曲线（按日聚合）\n')
    days = defaultdict(list)
    for r in rows:
        days[r['time'][:10]].append(r)
    p(f'  {"日期":<12}{"条数":>5}  {"积极":<28}{"敷衍":<28}')
    p('  ' + '─' * 74)
    for d in sorted(days):
        sub = days[d]
        pos = sum(r.get('positive', 0) for r in sub) / len(sub)
        per = sum(r.get('perfunctory', 0) for r in sub) / len(sub)
        mark = ''
        if per > 0.35:
            mark = '  ← 敷衍偏高'
        elif pos > 0.6:
            mark = '  ← 氛围热'
        p(f'  {d:<12}{len(sub):>5}  {bar(pos, 26)} {per:.2f}  {bar(per, 26)}{mark}')

    # ---------------- 七、敷衍 / 回避热点 ----------------
    p('\n【七】敷衍热点（对方最敷衍的 8 条）\n')
    hot = sorted([r for r in rows if r['speaker'] == OTHER], key=lambda r: -r.get('perfunctory', 0))[:8]
    for r in hot:
        p(f'  {r["time"]}  敷衍 {r.get("perfunctory",0):.2f}  投入 {r.get("engagement",0):.2f}  「{r["text"][:30]}」')

    p('\n【八】回避热点（回避概率最高的 8 条）\n')
    hot = sorted(rows, key=lambda r: -r.get('avoiding', 0))[:8]
    for r in hot:
        who = '你 ' if r['speaker'] == ME else '对方'
        p(f'  {r["time"]}  {who} 回避 {r.get("avoiding",0):.2f}  「{r["text"][:30]}」')

    # ---------------- 九、连续敷衍段 ----------------
    p('\n【九】连续低投入段（对方连续 ≥3 条 敷衍>0.5）\n')
    run = []
    for r in rows:
        if r['speaker'] == OTHER and r.get('perfunctory', 0) > 0.5:
            run.append(r)
        else:
            if len(run) >= 3:
                p(f'  {run[0]["time"]} ~ {run[-1]["time"]}  连续 {len(run)} 条')
                for x in run[:6]:
                    p(f'      「{x["text"][:34]}」 敷衍{x.get("perfunctory",0):.2f}')
            run = []
    if len(run) >= 3:
        p(f'  {run[0]["time"]} ~ {run[-1]["time"]}  连续 {len(run)} 条')
        for x in run[:6]:
            p(f'      「{x["text"][:34]}」 敷衍{x.get("perfunctory",0):.2f}')

    # ---------------- 十、最投入的时刻 ----------------
    p('\n【十】对方最投入的 6 条\n')
    hot = sorted([r for r in rows if r['speaker'] == OTHER], key=lambda r: -r.get('engagement', 0))[:6]
    for r in hot:
        p(f'  {r["time"]}  投入 {r.get("engagement",0):.2f}  积极 {r.get("positive",0):.2f}  「{r["text"][:34]}」')

    report = '\n'.join(L)
    OUT.write_text(report, encoding='utf-8')
    print(report)
    print(f'\n报告已保存: {OUT}')


if __name__ == '__main__':
    main()
