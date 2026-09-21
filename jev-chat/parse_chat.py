# -*- coding: utf-8 -*-
"""
解析从 QQ / 微信 复制出来的聊天记录纯文本。

识别格式（3 行一组，组间空行）：
    昵称
    2024年01月01日  12:00
    消息内容

以「时间行」为锚点定位每条消息，比按 3 行步进更抗格式波动。

用法：
    python parse_chat.py "聊天记录.txt"
    python parse_chat.py "聊天记录.txt" -o data/parsed.jsonl
"""
import sys
import re
import json
import argparse
from pathlib import Path

sys.stdout.reconfigure(encoding='utf-8', errors='replace')

BASE = Path(__file__).parent
DATA = BASE / 'data'
DATA.mkdir(parents=True, exist_ok=True)

# 2024年01月01日  12:00  （日期与时间之间可能是 1~2 个空格）
TS = re.compile(r'^(\d{4})年(\d{1,2})月(\d{1,2})日\s+(\d{1,2}):(\d{2})$')


def parse(lines):
    msgs, anom = [], []
    anchors = [i for i, l in enumerate(lines) if TS.match(l.strip())]

    for idx, i in enumerate(anchors):
        if i == 0:
            anom.append(('时间行缺少昵称', i + 1, lines[i].strip()))
            continue

        speaker = lines[i - 1].strip()
        m = TS.match(lines[i].strip())
        y, mo, d, h, mi = (int(x) for x in m.groups())
        ts = f'{y:04d}-{mo:02d}-{d:02d} {h:02d}:{mi:02d}'

        # 内容范围：i+1 到 下一个锚点的昵称行之前
        end = anchors[idx + 1] - 1 if idx + 1 < len(anchors) else len(lines)
        body = list(lines[i + 1:end])
        while body and not body[-1].strip():
            body.pop()
        text = '\n'.join(body).strip()

        if not speaker:
            anom.append(('昵称为空', i + 1, text[:40]))

        msgs.append({
            'idx': len(msgs),
            'speaker': speaker,
            'time': ts,
            'text': text,
        })
    return msgs, anom


def main():
    ap = argparse.ArgumentParser(description='解析 QQ/微信 复制的聊天记录纯文本')
    ap.add_argument('src', help='聊天记录 txt 路径')
    ap.add_argument('-o', '--out', default=str(DATA / 'parsed.jsonl'),
                    help='输出 jsonl 路径（默认 data/parsed.jsonl）')
    args = ap.parse_args()

    SRC = Path(args.src)
    OUT = Path(args.out)
    OUT.parent.mkdir(parents=True, exist_ok=True)

    raw = SRC.read_text(encoding='utf-8', errors='replace')
    lines = [l.rstrip('\r') for l in raw.split('\n')]

    msgs, anom = parse(lines)

    with OUT.open('w', encoding='utf-8') as f:
        for m in msgs:
            f.write(json.dumps(m, ensure_ascii=False) + '\n')

    print(f'源文件      : {SRC}')
    print(f'总行数      : {len(lines)}')
    print(f'解析消息数  : {len(msgs)}')
    print(f'异常        : {len(anom)}')
    for a in anom[:20]:
        print(f'    L{a[1]}  {a[0]}  {a[2]}')

    # ---- 说话人统计 ----
    from collections import Counter
    cnt = Counter(m['speaker'] for m in msgs)
    print('\n--- 说话人 ---')
    for sp, c in cnt.most_common():
        avg = sum(len(m['text']) for m in msgs if m['speaker'] == sp) / c
        print(f'  {sp:<12} {c:>5} 条   平均 {avg:.1f} 字')

    # ---- 时间跨度 ----
    times = sorted(m['time'] for m in msgs)
    print(f'\n--- 时间 ---')
    print(f'  起: {times[0]}')
    print(f'  止: {times[-1]}')

    days = Counter(m['time'][:10] for m in msgs)
    print(f'  跨 {len(days)} 天')
    for d, c in sorted(days.items()):
        print(f'    {d}  {c:>4} 条')

    # ---- 特殊内容 ----
    print('\n--- 特殊内容 ---')
    ph = [m for m in msgs if re.fullmatch(r'\[.*?\]', m['text'])]
    print(f'  纯占位符消息（表情/图片等）: {len(ph)}')
    empty = [m for m in msgs if not m['text']]
    print(f'  空消息: {len(empty)}')
    multi = [m for m in msgs if '\n' in m['text']]
    print(f'  多行消息: {len(multi)}')

    print(f'\n输出: {OUT}')


if __name__ == '__main__':
    main()
