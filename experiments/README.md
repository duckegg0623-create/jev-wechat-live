# experiments · 复现脚本

[`../KNOWN_ISSUES.md`](../KNOWN_ISSUES.md) 里的测试数据，就是用这两个脚本跑出来的。

| 脚本 | 产出 | 对应文档章节 |
|---|---|---|
| `quality_test.py` | 11 条消息的七维判定，对照「实际发生了什么」 | 第 3 节 · 第 4 节 |
| `ab_context_test.py` | 同一条消息，只改上下文，看判定变不变 | 第 5 节 |

## 怎么跑

需要 `jev-chat/config.json` 里有可用的 API key：

```bash
cp ../jev-chat/config.example.json ../jev-chat/config.json
# 填 openrouter_api_key

python quality_test.py       # 约 $0.002，10 秒左右
python ab_context_test.py    # 约 $0.001
```

两个脚本都从 `../jev-chat/` 引入客户端和问题集，所以**目录要保持现在的相对位置**。

## 关于用例

原始用例取自一段真实微信对话。为不泄露对话内容：

- 涉及人名、游戏名、具体事件的句子，改写成等价的虚构例句
- 时间戳全部替换为虚构值，只保留先后顺序
- 上下文只留下还原语气所必需的条数

**Jev 的判定结果和概率是原始的，没有改。** 因为它的失败模式（`best_action`
坍缩到 `ask_clarify`、`emotion` 被 `annoyed` 污染）和具体句子无关 ——
换成别的句子，大概率还是同样的错法。

## 自己造用例

格式很简单，`quality_test.py` 的 `CASES` 就是：

```python
(标题, 对方原话, [(说话人, 时间, 前文), ...])
```

把它换成你自己的对话，就能看 Jev 在你的场景下表现如何。
**建议保留这份「实际是什么」的对照表** —— 只看 Jev 的输出，很容易觉得它判得挺有道理。
