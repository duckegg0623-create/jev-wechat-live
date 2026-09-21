# -*- coding: utf-8 -*-
"""批量演示：从 parsed.jsonl 里等距抽 N 条，逐条跑即时解读（每条带前 6 条上下文）。"""
import sys
import json
from pathlib import Path

sys.stdout.reconfigure(encoding='utf-8', errors='replace')
sys.path.insert(0, str(Path(__file__).parent))

from jev_client import JevClient, load_key, parse_answers   # noqa: E402
from jev_read import QUESTIONS, render                       # noqa: E402

PARSED = Path(__file__).parent / 'data' / 'parsed.jsonl'
CTX_N = 6
SAMPLE_N = 6

# 想固定测某几条，把下标写进 TARGETS，下面会优先用它；留空就走抽样。
TARGETS = []


def pick(msgs, n):
    """等距抽样 —— 别把 6 条都挤在同一天，要覆盖整段对话"""
    if len(msgs) <= n:
        return list(range(len(msgs)))
    step = len(msgs) / n
    return [int(i * step) for i in range(n)]


def main():
    if not PARSED.exists():
        raise SystemExit(f'没找到 {PARSED}\n先跑：python parse_chat.py "聊天记录.txt"')
    msgs = [json.loads(l) for l in PARSED.read_text(encoding='utf-8').splitlines() if l.strip()]
    if not msgs:
        raise SystemExit(f'{PARSED} 是空的')

    cli = JevClient(load_key())
    total = 0.0
    ok = 0

    try:
        for idx in (TARGETS or pick(msgs, SAMPLE_N)):
            m = msgs[idx]
            recent = msgs[max(0, idx - CTX_N):idx]
            ctx_disp = [f"{x['speaker']}：{x['text'][:40]}" for x in recent]

            state = {'target_message': m['text'], 'speaker': m['speaker']}
            if recent:
                state['recent_context'] = [
                    {'speaker': x['speaker'], 'text': x['text']} for x in recent
                ]

            try:
                resp = cli.ask(state, questions=QUESTIONS)
            except Exception as e:
                print(f'\n[失败] {m["time"]} {m["text"][:20]} — {e}')
                continue

            ans = parse_answers(resp)
            usage = resp.get('usage', {})
            cost = usage.get('cost', 0) or 0
            total += cost
            ok += 1

            print(f'\n\n{"#" * 66}')
            print(f'#  第 {ok} 条  ·  {m["time"]}  ·  说话人：{m["speaker"]}')
            print(f'{"#" * 66}')
            print(render(m['text'], ans, ctx_disp))
            print(f'\n  [tokens {usage.get("input_tokens")} · ${cost:.6f}]')

    finally:
        cli.close()

    print(f'\n\n{"=" * 66}')
    print(f'  共 {ok} 条 · 总成本 ${total:.6f}（约 {total*7.1:.4f} 元）')
    print(f'{"=" * 66}')


if __name__ == '__main__':
    main()
