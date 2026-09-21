# -*- coding: utf-8 -*-
"""
Jev 微信实时解读

流程：
    微信写 session.db ──(轮询 mtime)──> 发现目标会话时间戳变大
      └─> message 库 WAL 增量 patch，取新消息全文 + 判断发送者
            └─> 动态静默窗口（0.6~3.0 s，看结尾标点和已发片数）
                  └─> 碎片合并进上下文（带时间）──> 调 Jev 解读
                        └─> 浮层实时显示
                              └─> 标题栏可暂停 / 切换分析对象

用法：
    python live.py                # 启动浮层
    python live.py --dry          # 不调 Jev，只在终端打印抓到的新消息（验证链路，不花钱）
    python live.py --replay 2     # 把最后 2 条当新消息重放一遍（验证整条链路，约 $0.0003）
"""
import argparse
import datetime
import json
import queue
import sys
import threading
import time
from pathlib import Path

sys.stdout.reconfigure(encoding='utf-8', errors='replace')

BASE = Path(__file__).parent
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(BASE.parent / 'jev-chat'))

import wxdump                                    # noqa: E402
import msgmerge                                  # noqa: E402
from contacts import ContactBook, KIND_CN        # noqa: E402
from overlay import Overlay                      # noqa: E402
from jev_client import JevClient, load_key, parse_answers   # noqa: E402
from jev_read import QUESTIONS                   # noqa: E402


def fmt_time(ts):
    """上下文里的时间。今天给 'HH:MM'，昨天/前天标出来，更早补上日期 ——
    对方隔了三天才回一句，和隔了三十秒，是完全不同的两件事。

    光给 HH:MM 不够：今天早上和今天下午都是 'HH:MM'，Jev 得自己做减法才能
    看出中间空了一大段。标上「昨天/前天」它一眼就看出跨度了。"""
    d = datetime.datetime.fromtimestamp(ts)
    delta = (datetime.date.today() - d.date()).days
    if delta == 0:
        return d.strftime('%H:%M')
    if delta == 1:
        return '昨天 ' + d.strftime('%H:%M')
    if delta == 2:
        return '前天 ' + d.strftime('%H:%M')
    return d.strftime('%m-%d %H:%M')


def split_sessions(items, gap_s):
    """
    按时间断档把消息序列切成若干段「对话」。

    相邻两条隔了 gap_s 以上就断开 —— 微信上一对一聊天里那基本就是
    「这次聊完了，下次再聊」。返回 [[...], [...]]，最后一段是离现在最近的。
    """
    segs, cur, prev = [], [], None
    for it in items:
        if prev is not None and it['ts'] - prev > gap_s:
            segs.append(cur)
            cur = []
        cur.append(it)
        prev = it['ts']
    if cur:
        segs.append(cur)
    return segs


def take_tail(items, max_chars, max_items=60):
    """
    从末尾往前取，累计字数不超过 max_chars，条数不超过 max_items。至少取一条。

    按字数而不是条数：「嗯」「好」这类短消息按条数取会白白占位，
    而一条几百字的倾诉按条数取又可能把整段上下文撑爆。

    但纯按字数也有反过来的一面 —— 一串「嗯嗯」「哈哈」字数没多少、条数却很多，
    每条还都要带上说话人和时间的 JSON 开销，塞进去只是白烧 token 还稀释信息。
    所以再压一道条数上限兜底。
    """
    out, total = [], 0
    for it in reversed(items):
        n = len(it['text'])
        if out and (total + n > max_chars or len(out) >= max_items):
            break
        out.append(it)
        total += n
    out.reverse()
    return out


def ctx_for_state(items):
    """内部存的带 ts，发给 Jev 时只留说话人/时间/内容。"""
    return [{'speaker': it['speaker'], 'time': fmt_time(it['ts']),
             'text': it['text']} for it in items]


class Target:
    def __init__(self, name, username):
        self.name = name
        self.username = username
        self.store = None
        self.smap = {}
        # ctx 里存的是**合并后**的消息，每条带原始时间戳。
        # 不合并的话，对方分三次发的一句话会在上下文里变成三条独立消息，
        # 而且会一直躺着，污染之后每一次解读。
        # 存 ts 而不是格式化好的字符串 —— 切「本次/上次对话」要按时间间隔算，
        # 而且得等发出去之前才决定用哪种时间格式。
        self.ctx = []          # [{'speaker', 'ts', 'text'}]
        self.last_ts = 0       # session 表用的时间戳（判断有没有新消息）
        self.last_id = 0       # 消息表用的 local_id 游标（防漏防重）
        self.pending = []      # 攒着的原始消息，等静默窗口关闭再合并
        self.last_new = 0.0
        self.debounce = 1.5    # 动态静默窗口，每收一条重算


class LiveMonitor(threading.Thread):
    def __init__(self, cfg, overlay, dry=False, replay=0, keys=None,
                 contact_book=None):
        super().__init__(daemon=True)
        self.cfg = cfg
        self.ov = overlay
        self.dry = dry
        self.replay = replay
        self.stop_flag = threading.Event()

        self.db_dir = cfg['db_dir']
        self.me = cfg['me']
        self.keys = keys
        self.contacts = contact_book

        # 控制状态。浮层线程会直接改这些（都是简单赋值，GIL 下够用）
        self.targets = []
        self.idx = 0                        # 当前分析对象
        self.paused = False
        self.interpret_self = cfg.get('interpret_self', False)
        self.merge_gap = cfg.get('merge_gap_s', 8.0)
        self.silence_cfg = cfg.get('silence', {})
        self.warm = cfg.get('warmup_count', 100)
        # 上下文按**字数**给，不按条数
        self.ctx_chars = cfg.get('context_chars', 150)    # 本次对话，够判就行
        self.prev_chars = cfg.get('prev_chars', 800)      # 上次对话，多给点
        self.session_gap = cfg.get('session_gap_s', 7200)  # 隔 2 小时算上一次

        # 动态加载：后台线程把建好的 Target 放进来，主循环取走 ——
        # self.targets 只让主循环改，省得考虑加锁
        self._add_q = queue.Queue()
        self._adding = None                 # 正在加载谁（防止重复点）
        self._log_lock = threading.Lock()
        self._last_pull_err = None          # 同类错误不刷屏

        self.cli = None if dry else JevClient(load_key())

    def log(self, s):
        # 后台加载线程也会 log，不加锁会和主线程的 print 互相插行
        with self._log_lock:
            print(s, flush=True)

    def status(self, title, detail=''):
        self.ov.push('status', {'title': title, 'detail': detail})

    # ---------------- 主循环 ----------------

    def run(self):
        cfg = self.cfg
        if not self.keys:
            try:
                self.keys = wxdump.load_keys(cfg['keys_file'])
            except Exception as e:
                self.log(f'[!] 密钥载入失败: {e}')
                self.status('密钥载入失败', str(e))
                return

        sr = wxdump.SessionReader(
            str(Path(self.db_dir) / 'session' / 'session.db'),
            wxdump.key_for(self.keys, 'session\\session.db'))

        # ---- 预热 config 里写死的目标。留空就一个人都不加载，等用户自己选 ----
        self.targets = []
        for i, t in enumerate(cfg.get('targets') or []):
            self.status(f'预热 {t["name"]}…', '正在定位并解密消息库')
            tg, err = self._make_target(t['username'], t['name'],
                                        replay=self.replay if i == 0 else 0)
            if tg is None:
                self.log(f'[!] {t["name"]}: {err}')
                self.status(f'加载 {t["name"]} 失败', err)
                continue
            self.targets.append(tg)

        # 一个人都没有**不能直接退出** —— config 留空是正常的用法（启动后自己挑），
        # 写了人但全加载失败也得让用户能手动选，不然只能对着黑窗口发呆。
        if not self.targets:
            if cfg.get('targets'):
                self.log('[!] config 里的目标一个都没加载成功，可以在浮层上手动选')
            self._sync_target_label()
            self.status('还没选监听对象', '点标题栏的名字，按备注 / 拼音首字母搜')
        else:
            self._sync_target_label()
            self.status('监听中…', '等对方发消息')

        poll = cfg.get('poll_ms', 200) / 1000.0

        while not self.stop_flag.is_set():
            time.sleep(poll)

            # 浮层在搜索面板里选了个新人 → 后台线程建好了放队列里
            self._drain_addq()

            try:
                changed = sr.refresh()
            except Exception as e:
                self.log(f'[!] session 读取失败: {e}')
                continue

            if changed:
                sess = sr.sessions()
                for tg in self.targets:
                    s = sess.get(tg.username)
                    # 注意是 < 不是 <=：同一分钟内连发的消息 session 时间戳相同，
                    # 用 <= 会跳过。真正的去重交给 local_id 游标。
                    if not s or s['ts'] < tg.last_ts:
                        continue
                    self._pull_new(tg, self.me, cfg)

            # 静默窗口关闭 → 合并碎片、写进上下文、（没暂停的话）解读
            for tg in self.targets:
                if tg.pending and time.time() - tg.last_new > tg.debounce:
                    self._flush(tg)

    # ---------------- 目标构建 ----------------

    def _make_target(self, username, name, replay=0):
        """
        定位消息库 + 预热上下文。失败返回 (None, 原因)。

        config 里写死的和浮层里现搜的都是走这条路 —— 只有一处 locate 逻辑。
        """
        tg = Target(name, username)
        tg.store = wxdump.MessageStore(self.db_dir, username, self.keys,
                                       BASE / 'data', log=self.log)
        if not tg.store.locate():
            return None, '找不到消息表（可能从没聊过天，或密钥过期了）'

        tg.smap = tg.store.sender_map()
        hist = tg.store.recent(self.warm)

        # --replay N：把最后 N 条让出来当作「新消息」，用于验证整条链路
        n_replay = min(replay, max(0, len(hist) - 1)) if replay else 0
        head = hist[:len(hist) - n_replay] if n_replay else hist

        raw = []
        skipped = 0
        for m in head:
            if m['local_type'] != 1:
                continue                           # 只看文本
            who = tg.smap.get(m['sender_id'], '')
            if not who:
                continue
            text = (m['text'] or '').strip()
            if not text:
                continue
            if wxdump.is_noise_text(text):
                skipped += 1
                continue                           # 引用/链接/通话记录等 XML
            raw.append({'speaker': '我' if who == self.me else name,
                        'text': text, 'ts': m['ts'], 'id': m['id']})

        # 从库里读出来的历史同样是碎的，一样要合并
        grouped = msgmerge.group(raw, self.merge_gap)
        for g in grouped:
            tg.ctx.append({'speaker': g['speaker'],
                           'ts': g['ts'],
                           'text': g['text']})

        if raw:
            tg.last_ts = raw[-1]['ts']
            tg.last_id = raw[-1]['id']
        else:
            tg.last_ts = int(time.time())
            tg.last_id = 0

        extra = f'，跳过 {skipped} 条 XML' if skipped else ''
        extra += f'（重放最后 {n_replay} 条）' if n_replay else ''
        self.log(f'{name}: 预热 {len(raw)} 条{extra} → 合并成 {len(grouped)} 条上下文，'
                 f'游标 ts={tg.last_ts} id={tg.last_id}')
        return tg, None

    # ---------------- 取新消息 ----------------

    def _pull_new(self, tg, me, cfg):
        try:
            tg.store.refresh()
            new = tg.store.recent(60, since_id=tg.last_id)
        except Exception as e:
            # 这是 200ms 一次的热路径，同一种错连着报会把日志刷爆
            # （踩过：一个跨线程错误刷了 10 KB 日志，真正的信息全被埋了）
            msg = str(e)
            if msg != self._last_pull_err:
                self._last_pull_err = msg
                self.log(f'[!] 消息库刷新失败: {msg}')
            return
        self._last_pull_err = None

        for m in new:
            tg.last_ts = max(tg.last_ts, m['ts'])
            tg.last_id = max(tg.last_id, m['id'])
            if m['local_type'] != 1:
                continue                            # 图片/语音/表情等先跳过
            text = (m['text'] or '').strip()
            if not text or wxdump.is_noise_text(text):
                continue                            # 空 / 引用、链接、通话记录等 XML
            who = tg.smap.get(m['sender_id'], '')
            if not who:
                continue
            is_me = (who == me)

            # 先进 pending，等静默窗口关闭再一起合并进 ctx。
            # 这里**不能**直接 append 到 ctx —— 那样碎片会一条条堆进去，
            # 并且一直留着，污染之后每一次解读。
            tg.pending.append({'speaker': '我' if is_me else tg.name,
                               'text': text, 'ts': m['ts'], 'is_me': is_me})
            parts = [p['text'] for p in tg.pending]
            tg.debounce = msgmerge.silence_window(parts, self.silence_cfg)
            tg.last_new = time.time()

            t = datetime.datetime.fromtimestamp(m['ts']).strftime('%H:%M:%S')
            self.log(f'  ← [{t}] {"我" if is_me else tg.name}: {text[:40]}'
                     f'   等 {tg.debounce:.1f}s')

    # ---------------- 合并与解读 ----------------

    def _absorb(self, tg):
        """
        把 pending 里的碎片合并后写进上下文，返回合并结果（**不解读**）。

        顺序很重要：先合并进上下文，再决定要不要解读。
        暂停、切走的时候也照样写 —— 这样一恢复就能接着用，
        而不是对着一片空白重新开始。
        """
        batch, tg.pending = tg.pending, []
        if not batch:
            return []
        merged = msgmerge.group(batch, self.merge_gap)
        for g in merged:
            tg.ctx.append({'speaker': g['speaker'],
                           'ts': g['ts'],
                           'text': g['text']})
        # 上限放宽到 400 条：得够装下「上次对话」那一整段。80 条在话说得多的
        # 时候会把上次的整段挤出去，按断档切段就切不出来了。
        tg.ctx = tg.ctx[-400:]
        return merged

    def _flush(self, tg):
        """静默窗口关了，说明对方说完了。"""
        merged = self._absorb(tg)
        if not merged:
            return

        # 判定对象：合并之后、由对方发出的那一条
        if self.interpret_self:
            target = merged[-1]
        else:
            hers = [g for g in merged if g['speaker'] != '我']
            if not hers:
                return
            target = hers[-1]

        n_parts = len(target.get('parts') or [])
        frag = f'（{n_parts} 片合成）' if n_parts > 1 else ''

        # 目标在 tg.ctx 里的下标。不能把整批 merged 都排除掉 —— 同一批里排在目标
        # 前面的那几条（「对方问 → 我答 → 对方又问」里的前两步）恰恰是最直接的前文，
        # 排掉等于让 Jev 凭空判断这条在回应什么（踩坑记录 10）。
        start = len(tg.ctx) - len(merged)
        tpos = start + next((i for i, g in enumerate(merged) if g is target),
                            len(merged) - 1)

        # 之前的历史按时间断档切段：最后一段是本次对话，倒数第二段是上次。
        # 两段都给 Jev，让**它**判断这条是在接上次的话还是开新话题 ——
        # 「这个」「那个」到底指哪一次说的，代码判不准，语言模型看得出来。
        segs = split_sessions(tg.ctx[:tpos], self.session_gap)
        recent = take_tail(segs[-1], self.ctx_chars) if segs else []
        prev = take_tail(segs[-2], self.prev_chars) if len(segs) >= 2 else []

        state = {'target_message': target['text'], 'speaker': tg.name}
        if recent:
            state['recent_context'] = ctx_for_state(recent)
        if prev:
            state['previous_conversation'] = ctx_for_state(prev)

        if self.dry:
            self.log(f'[dry] 会解读：{target["text"][:60]!r}{frag}  '
                     f'本次 {len(recent)} 条 / 上次 {len(prev)} 条')
            self.log('      state = ' + json.dumps(state, ensure_ascii=False)[:600])
            return
        if self.paused:
            self.log(f'  [暂停] 只记上下文不解读：{target["text"][:40]!r}{frag}')
            return
        if tg is not self.current_target():
            return

        try:
            resp = self.cli.ask(state, questions=QUESTIONS)
        except Exception as e:
            self.log(f'[!] Jev 调用失败: {e}')
            self.status('解读失败', str(e)[:140])
            return

        ans = parse_answers(resp)
        self.ov.push('result', {
            'msg': target['text'],
            'ans': ans,
            'meta': {'time': fmt_time(target['ts']), 'name': tg.name, 'kind': '对方'},
        })
        cost = (resp.get('usage') or {}).get('cost', 0) or 0
        top = max((ans.get('_raw', {}).get('real_intent', {}).get('probabilities')
                   or {'?': 0}).items(), key=lambda x: x[1])[0]
        self.log(f'  → 解读完成 ${cost:.6f}  意图={top}  '
                 f'本次{len(recent)}条/上次{len(prev)}条  {target["text"][:30]!r}{frag}')

    # ---------------- 控制（浮层调） ----------------

    def current_target(self):
        if not self.targets:
            return None
        i = max(0, min(self.idx, len(self.targets) - 1))
        return self.targets[i]

    def _sync_target_label(self):
        # 空也要推 —— 浮层得把标题栏刷成「未选 ▾」，不然用户不知道那里能点
        tg = self.current_target()
        self.ov.push('target', tg.name if tg else '')

    def toggle_pause(self):
        self.paused = not self.paused
        self.log(f'[控制] {"⏸ 暂停分析" if self.paused else "▶ 继续分析"}')
        self.ov.push('paused', self.paused)
        return self.paused

    def switch_target(self, i):
        if not (0 <= i < len(self.targets)):
            return
        old = self.current_target()
        self.idx = i
        tg = self.targets[i]

        # 切走的那个人：攒着的碎片先并进上下文（只是不解读），
        # 这样切回来是连续的，而不是中间缺一段。
        # 注意被切走的人**继续收消息**，只是不再调 Jev。
        if old is not None and old is not tg:
            n = len(self._absorb(old))
            if n:
                self.log(f'[控制] {old.name} 转后台，{n} 条并入上下文')

        self.log(f'[控制] 切到 {tg.name}（上文 {len(tg.ctx)} 条）')
        self._sync_target_label()
        self.ov.push('paused', self.paused)
        return self.paused

    # ---------------- 按名字找人来监听 ----------------

    def request_add(self, username, name, kind='user'):
        """浮层在搜索面板里选了一个人。加载要几秒，扔后台做。"""
        if kind != 'user':
            self.status(f'{KIND_CN.get(kind, kind)}监听不了', '只支持一对一的聊天')
            return
        if username == self.me:
            self.status('那是你自己', '')
            return
        for i, tg in enumerate(self.targets):
            if tg.username == username:
                self.switch_target(i)
                return
        if self._adding:
            self.status(f'正在加载 {self._adding}', '稍等一下…')
            return

        self._adding = name
        # --replay 是调试用的，只作用在第一个加载的人身上
        replay = self.replay
        self.replay = 0
        self.log(f'[控制] 加载 {name} （{username}）…')
        self.status(f'加载 {name} 中…', '正在定位并解密消息库，几秒')
        threading.Thread(target=self._load_worker, args=(username, name, replay),
                         daemon=True).start()

    def _load_worker(self, username, name, replay=0):
        """后台线程：建 Target。结果经队列交回主循环 —— self.targets 只让主循环改。"""
        try:
            tg, err = self._make_target(username, name, replay=replay)
        except Exception as e:
            tg, err = None, str(e)[:90]
        self._add_q.put((tg, err))

    def _drain_addq(self):
        while True:
            try:
                tg, err = self._add_q.get_nowait()
            except queue.Empty:
                return
            self._adding = None
            if tg is None:
                self.log(f'[!] 加载失败: {err}')
                self.status('加载失败', err)
                continue
            self.targets.append(tg)
            self.log(f'[控制] {tg.name} 加入监听（上文 {len(tg.ctx)} 条）')
            self.ov.push('targets', [t.name for t in self.targets])
            self.switch_target(len(self.targets) - 1)
            self.status('监听中…', f'已切到 {tg.name}')


def main():
    ap = argparse.ArgumentParser(description='Jev 微信实时解读')
    ap.add_argument('--dry', action='store_true',
                    help='不调 Jev，只在终端打印抓到的新消息（验证链路，不花钱）')
    ap.add_argument('--replay', type=int, default=0, metavar='N',
                    help='把最后 N 条消息当作新消息重放，用于验证整条链路')
    args = ap.parse_args()

    cfg = json.loads((BASE / 'config.json').read_text(encoding='utf-8'))

    try:
        keys = wxdump.load_keys(cfg['keys_file'])
    except Exception as e:
        print(f'[!] 密钥载入失败: {e}')
        return

    # 联系人库：搜索面板要即时出结果，所以在这先建好、预热
    book = ContactBook(str(Path(cfg['db_dir']) / 'contact' / 'contact.db'),
                       wxdump.key_for(keys, 'contact\\contact.db'),
                       BASE / 'data')
    if book.key is None:
        print('[!] all_keys.json 里缺 contact\\contact.db 的密钥，搜人功能用不了')
    else:
        t0 = time.perf_counter()
        book.refresh()
        print(f'  联系人库 {book.count()} 人（{time.perf_counter()-t0:.2f}s）')

    # 缺密钥的消息库：微信新开分片后会出现，不喊出来就会闷声瞎掉（踩过）
    lost = wxdump.missing_message_keys(cfg['db_dir'], keys)
    if lost:
        print()
        print(f'[!] 有 {len(lost)} 个消息库没有密钥：{", ".join(lost)}')
        print('    微信新建分片库了，写进这些库的消息一条都读不到。')
        print('    双击 tools\\wcdb-key-tool\\rekey.bat 重新提取（会弹 UAC），然后重启本程序。')
        print()

    ov = Overlay(cfg, target_names=[t['name'] for t in cfg['targets']],
                 contact_book=book)
    mon = LiveMonitor(cfg, ov, dry=args.dry, replay=args.replay,
                      keys=keys, contact_book=book)
    ov.on_pause = mon.toggle_pause
    ov.on_switch = mon.switch_target
    ov.on_add = mon.request_add
    ov.on_close = lambda: mon.stop_flag.set()

    names = ' / '.join(t['name'] for t in (cfg.get('targets') or []))
    lines = [
        '=' * 62,
        '  Jev 微信实时解读',
        f'  目标：{names or "（启动后自己选）"}',
        f'  轮询：{cfg.get("poll_ms", 200)} ms',
        f'  上下文：本次 {cfg.get("context_chars", 150)} 字 / '
        f'上次 {cfg.get("prev_chars", 800)} 字'
        f'（隔 {cfg.get("session_gap_s", 7200) // 60} 分钟算一次对话）',
        f'  合并间隔：{cfg.get("merge_gap_s", 15)} s   静默窗口：按消息动态计算',
        f'  模式：{"DRY（不调 Jev）" if args.dry else "实时解读"}',
        '=' * 62,
        '  点标题栏的目标名 → 搜名字/备注/拼音换人监听',
        '  [⏸] 暂停  [✕] 退出',
        '',
    ]
    # 先一口气打完再起线程 —— 否则预热日志会插进来把横幅劈成两半
    print('\n'.join(lines), flush=True)

    mon.start()

    try:
        ov.run()
    except KeyboardInterrupt:
        pass
    finally:
        mon.stop_flag.set()
        print('\n已停止')


if __name__ == '__main__':
    main()
