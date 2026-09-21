# jev-wechat-live

用 **TypeSafe Jev** 实时解读微信消息的桌面浮层 —— 对方发来一句话，旁边弹出一块小窗，
告诉你 TA 现在什么情绪、这句话真正想说什么、你该怎么回。

> ⚠️ **这是个没做完的实验项目，准确率不达标。**
>
> Jev 在「读懂言外之意」这类任务上表现明显不如预期，我们试了改提示词、加上下文、
> 做 A/B 对照，**没能解决**。问题现象、11 条真实消息的逐条对照、A/B 实验数据、
> 以及我们对失败原因的判断，全在 **[KNOWN_ISSUES.md](KNOWN_ISSUES.md)**。
>
> 代码是完整能跑的，管线、解密、浮层、去抖全部工作正常 —— **预测部分不准**。
> 发出来是希望有人能接手下半场。

---

## 仓库里有什么

| 目录 | 是什么 |
|---|---|
| [`wechat-live/`](wechat-live/) | **实时浮层**。盯住一个微信联系人，新消息一到就调 Jev 判定，弹窗展示 |
| [`jev-chat/`](jev-chat/) | **底层库**。Jev API 客户端 + 两套问题集 + 聊天记录批量分析。`wechat-live` 依赖它 |
| [`experiments/`](experiments/) | **复现脚本**。`KNOWN_ISSUES.md` 里那些测试数据的生成代码 |

两部分是配套的：`wechat-live` 通过 `sys.path` 引入 `jev-chat/` 里的
`jev_client.py`（API 客户端）和 `jev_read.py`（7 项即时解读问题集）。
所以两个目录要**保持并列**，别拆开。

---

## 效果

```
┌──────────────────────────────────┐
│ ● JEV 实时解读  张三 ▾    ⏸  ✕  │
├──────────────────────────────────┤
│ 10:29  对方                      │
│ 「这个方案你什么时候给我」        │
│                                  │
│ 潜台词                            │
│ 话里有话                不是 68% │
│                                  │
│ 情绪状态                          │
│ 烦躁             58% ██████████  │
│ 敷衍             21% ████        │
│ 平静             13% ██          │
│                                  │
│ 语气亲疏                          │
│ 2.4 / 5         客气、公事公办   │
│                                  │
│ 真实意图                          │
│ ★ 约时间         41% ███████     │
│   给反馈 / 不满  33% ██████      │
│                                  │
│ 关系状态                          │
│ 明显不对劲       52% █████████   │
│                                  │
│ 对方期待                          │
│ ★ 要具体方案     61% ███████████ │
│                                  │
│ 怎么回                            │
│ ★ 给具体方案     47% ████████    │
│   直接回答       26% █████       │
│   先追问澄清     19% ███         │
└──────────────────────────────────┘
```

端到端延迟约 1 秒，单次判定约 **$0.0001**。

---

## 它是怎么工作的

`wechat-live` 不注入微信，也不 hook 任何东西 —— 它**直接读微信本地的加密数据库**：

```
Weixin.exe 写入 session.db（加密 + WAL）
      │
      │  每 200ms 轮询：比对 mtime，没变就 0 开销跳过
      ▼
   解密 session.db（0.5 MB，约 9 ms）→ 读 SessionTable
      │
      │  目标会话 last_timestamp 变大 = 有新消息
      ▼
   解密 message_*.db（80 MB，约 0.45 s，启动时一次）
   之后靠 WAL 增量 patch 保持最新
      │
      │  按 local_id 游标增量取新消息，real_sender_id 判断谁发的
      ▼
   动态静默窗口（0.6 ~ 3.0 s，看结尾标点和已发了几片）
      │
      │  碎片合并成一条，写进上下文
      ▼
   按时间断档切段 → 本次对话 + 上次对话，两段都带时间戳
      │
      ▼
   调 Jev（上次那段要不要算进来由它判断）→ 浮层渲染
```

微信 4.0 (xwechat) 库格式要点：

- SQLCipher 4：每页 4096 字节，末尾 80 字节保留区，最后 16 字节是 IV；第 1 页前 16 字节是 salt
- 会话消息表名 = `Msg_` + md5(username)
- 发送者：`real_sender_id` → `Name2Id.rowid` → `user_name`
- 同一个会话的表**可以在多个 `message_*.db` 里同时存在**（新库建好老表不删，只是冻结）

---

## 安装

### 1. 前置：解密微信数据库

本项目**不包含**密钥提取功能，需要先用
[**ylytdeng/wechat-decrypt**](https://github.com/ylytdeng/wechat-decrypt) 拿到密钥
（`all_keys.json`）。

> 微信 4.1+ 不再在内存里明文缓存密钥，老工具扫 `x'<hex>'` 模式已经扫不到。
> 我们最后用的是 `wcdb-key-tool` 那条路 —— 定位运行时 `Config.Cipher` 对象 →
> 解混淆 → **逐库 HMAC 校验**，校验通过才采信。详见 `wechat-live/README.md` 的
> 「注意事项」。

### 2. Python 依赖

```bash
pip install httpx pywin32
```

| 包 | 用途 |
|---|---|
| `httpx` | 调 OpenRouter Decisions API |
| `pywin32` | 浮层跟随微信窗口位置（`win32gui`） |
| `tkinter` | 浮层 GUI（Python 自带，不用装） |

### 3. 填配置

两个目录各有一份模板，都要复制成 `config.json` 再改：

```bash
# jev-chat：填 API key
cp jev-chat/config.example.json jev-chat/config.json

# wechat-live：填 wxid / 数据库路径 / 密钥文件路径
cp wechat-live/config.example.json wechat-live/config.json
```

OpenRouter API key 去 <https://openrouter.ai/keys> 申请。

`wechat-live/config.json` 里三个必填项：

| 字段 | 说明 |
|---|---|
| `me` | 你自己的 wxid（用来区分消息是谁发的） |
| `db_dir` | 微信 `db_storage` 目录的绝对路径 |
| `keys_file` | `wechat-decrypt` 导出的 `all_keys.json` 路径 |

查 wxid 的方法在 `wechat-live/README.md` 的「怎么查 wxid」一节。

### 4. 跑

```bash
cd wechat-live
python live.py            # 正式使用
python live.py --dry      # 只打印抓到的新消息，不调 Jev（调链路，不花钱）
python live.py --replay 3 # 把最后 3 条当新消息重放，验证整条链路
```

也可以双击 `run_live.bat`。

**启动后浮层默认是空的** —— 一个人都不盯，标题栏显示「未选 ▾」。
点它 → 搜备注 / 拼音首字母 → 选中，才开始监听（默认这样设计，想启动就自动盯人，
把 wxid 写进 `config.json` 的 `targets`）。

单独用 `jev-chat` 分析聊天记录：

```bash
cd jev-chat
python parse_chat.py "聊天记录.txt"      # 解析
python jev_client.py                     # 逐条判定
python aggregate.py --me "你的昵称"       # 聚合成报告
python jev_read.py "所以呢？" -c "前文..." # 即时解读单条
```

---

## 隐私说明

这个仓库**不含任何真实数据**：

- 所有配置里的 wxid、路径、API key 都是显式占位符
- 人名一律是「张三 / 李四」这类虚构示例
- 示例聊天记录里的日期、时间戳是虚构的（只保留先后顺序）
- 实验用例里涉及真实身份、具体事件的句子做过等价改写

---

## 已知问题

**准确率不达标**，详见 **[KNOWN_ISSUES.md](KNOWN_ISSUES.md)**，简要版：

- 「最佳动作」系统性退化成「先追问澄清」（11 条里 6 条）
- 情绪判定被「烦躁」污染（11 条里 5 条判成烦躁，其中一条给了 100%）
- 短消息、单个 emoji 基本判不了（`🤔` 被判成「给反馈 / 不满」46%）
- 上下文窗口够不到的关键线索是问题之一，但 A/B 实验证明**不是根因** ——
  把线索直接喂进去，判定几乎没变

**我们的判断**（可能是错的，欢迎反驳）：问题出在**问题集的选项设计**上。
Jev 必须在给定的 criteria map 里选一个，而这些选项是按「语言学家会怎么分类」写的，
不是按「这句话实际发生了什么」写的。当真实情况不属于任何一个选项时，
它只能挑一个**看起来最接近的**——于是全都落到「追问澄清」这种安全但无用的答案上。
详见 KNOWN_ISSUES.md 的「我们猜的原因」。

---

## 致谢与依赖

- [**TypeSafe Jev**](https://openrouter.ai/) —— 决策模型，本项目的核心
- [**ylytdeng/wechat-decrypt**](https://github.com/ylytdeng/wechat-decrypt) —— 微信数据库解密
- [**Zumka1991/jev-telegram-admin**](https://github.com/Zumka1991/jev-telegram-admin) ——
  Decisions API 的端点格式和「上下文污染」防护措辞参考自这个项目

## 许可

[MIT](LICENSE) © 2026 duckegg0623-create

拿去改、拿去发、拿去商用都行，保留版权声明即可。希望有人能把这半场打完。
