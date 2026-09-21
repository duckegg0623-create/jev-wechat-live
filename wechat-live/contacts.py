# -*- coding: utf-8 -*-
"""
联系人查询 —— 用备注名 / 昵称 / 微信号 / wxid / 拼音 找人。

contact.db 只有几 MB（全量解密 0.04 s），所以不做增量，
文件没变就直接在内存里搜（几千行，常驻 1 MB 上下，不心疼）。

拼音是微信自己维护的，白捡的：
    remark_quan_pin        备注全拼      zhangsan
    remark_pin_yin_initial 备注首字母    ZS
所以「zs」「zhangsan」「张三」「zhangsan001」都能找到同一个人。

线程：浮层主线程要直接调 search()（要即时出结果），monitor 线程也调，
所以连接开 check_same_thread=False 并自己加锁。
"""
import os
import sqlite3
import threading
from pathlib import Path

import wxdump

# ---- 打分档位：数字越大越靠前 ----
_S_UID = 100         # wxid 完全命中（最精确）
_S_REMARK = 98       # 备注完全命中
_S_ALIAS = 96        # 微信号完全命中
_S_PY_EXACT = 94     # 拼音完全命中（zs / zhangsan）
_S_REMARK_PRE = 85
_S_NICK_PRE = 75
_S_PY_PRE = 72
_S_REMARK_IN = 65
_S_PY_IN = 60
_S_NICK_IN = 50
_S_ALIAS_IN = 45
_S_UID_IN = 30


def kind_of(username: str) -> str:
    """user / group / mp / openim —— 现在只有 user 能被监听"""
    u = username or ''
    if u.endswith('@chatroom'):
        return 'group'
    if u.startswith('gh_'):
        return 'mp'
    if u.endswith('@openim'):
        return 'openim'
    return 'user'


KIND_CN = {'user': '', 'group': '群聊', 'mp': '公众号', 'openim': '企业微信'}


def _score(r, ql):
    """0 = 不匹配。字符串都已在 _load 里转小写。"""
    un, rm, nk = r['username'], r['remark'], r['nick_name']
    al, rp, ri, ni = r['alias'], r['rpy'], r['rpyi'], r['npyi']

    if un == ql:
        return _S_UID
    if rm:
        if rm == ql:
            return _S_REMARK
        if rm.startswith(ql):
            return _S_REMARK_PRE
    if al == ql:
        return _S_ALIAS
    if rp == ql or ri == ql:
        return _S_PY_EXACT
    if rm and ql in rm:
        return _S_REMARK_IN
    if nk.startswith(ql):
        return _S_NICK_PRE
    if (rp and rp.startswith(ql)) or (ri and ri.startswith(ql)):
        return _S_PY_PRE
    if (rp and ql in rp) or (ri and ql in ri):
        return _S_PY_IN
    if ql in nk:
        return _S_NICK_IN
    if al and ql in al:
        return _S_ALIAS_IN
    if ni and ql in ni:
        return _S_NICK_IN
    if ql in un:
        return _S_UID_IN
    return 0


class ContactBook:
    """contact.db 的只读查询。搜不到人时不会抛异常，只返回空列表。"""

    def __init__(self, db_path, key, cache_dir, log=print):
        self.db_path = str(db_path)
        self.key = key
        self.cache = Path(cache_dir) / 'contact.dec.db'
        self.cache.parent.mkdir(parents=True, exist_ok=True)
        self.log = log

        self._lock = threading.Lock()
        self._conn = None
        self._stamp = None
        self._rows = []

    # ---- 载入 ----

    def _stamp_now(self):
        """主库和 -wal 都要看（WAL 模式下写入只改 -wal，只盯主库会漏）"""
        out = []
        for p in (self.db_path, self.db_path + '-wal'):
            try:
                st = os.stat(p)
                out.append((st.st_mtime, st.st_size))
            except OSError:
                out.append((0, 0))
        return tuple(out)

    def _read(self):
        need_full = True
        try:
            if self.cache.exists():
                need_full = self.cache.stat().st_mtime < os.path.getmtime(self.db_path)
        except OSError:
            need_full = True

        if need_full:
            wxdump.decrypt_to_file(self.db_path, self.key, str(self.cache))
        wxdump.patch_wal(self.db_path + '-wal', str(self.cache), self.key)

        if self._conn is not None:
            self._conn.close()
        self._conn = sqlite3.connect(str(self.cache), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row

        self._rows = []
        for r in self._conn.execute(
            "SELECT username, alias, remark, nick_name, remark_quan_pin, "
            "remark_pin_yin_initial, pin_yin_initial FROM contact "
            "WHERE username != '' AND username IS NOT NULL"
        ):
            un = r['username'] or ''
            rm = r['remark'] or ''
            nk = r['nick_name'] or ''
            al = r['alias'] or ''
            self._rows.append({
                'username': un,
                'username_l': un.lower(),
                'remark': rm,
                'nick_name': nk,
                'alias': al,
                'rpy': (r['remark_quan_pin'] or '').lower(),
                'rpyi': (r['remark_pin_yin_initial'] or '').lower(),
                'npyi': (r['pin_yin_initial'] or '').lower(),
                'display': rm or nk or al or un,
                'sub': al or nk or '',
                'kind': kind_of(un),
            })

    def refresh(self, force=False):
        """返回是否重新读了库。文件没动就直接返回，不重复解密。"""
        with self._lock:
            stamp = self._stamp_now()
            if not force and stamp == self._stamp and self._conn is not None:
                return False
            if self.key is None:
                return False
            try:
                self._read()
            except Exception as e:
                self.log(f'[!] 联系人库读取失败: {e}')
                return False
            self._stamp = stamp
            return True

    def count(self):
        return len(self._rows)

    # ---- 查询 ----

    def search(self, q, limit=9):
        """按名字找人，返回 [{username, display, sub, kind, score}]"""
        q = (q or '').strip()
        if not q:
            return []
        self.refresh()
        with self._lock:
            rows = self._rows

        ql = q.lower()
        hits = []
        for r in rows:
            s = _score(r, ql)
            if s:
                hits.append((s, r))
        # 同分时短名字优先（更精确），再按拼音稳一下顺序
        hits.sort(key=lambda x: (-x[0], len(x[1]['display']), x[1]['display']))

        out = []
        for s, r in hits[:limit]:
            out.append({'username': r['username'], 'display': r['display'],
                        'sub': r['sub'], 'kind': r['kind'], 'score': s})
        return out

    def close(self):
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None


# ---------------- 自测 ----------------
if __name__ == '__main__':
    import json
    import sys

    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    BASE = Path(__file__).parent
    cfg = json.loads((BASE / 'config.json').read_text(encoding='utf-8'))
    keys = wxdump.load_keys(cfg['keys_file'])

    book = ContactBook(str(Path(cfg['db_dir']) / 'contact' / 'contact.db'),
                       wxdump.key_for(keys, 'contact\\contact.db'), BASE / 'data')
    import time
    t0 = time.perf_counter()
    book.refresh()
    print(f'载入 {book.count()} 个联系人，{time.perf_counter()-t0:.2f}s')

    for q in ('张', 'zs', 'zhangsan', 'zhangsan001', '李四', 'ls', 'wxid_abcdefghij'):
        t0 = time.perf_counter()
        rs = book.search(q)
        dt = (time.perf_counter() - t0) * 1000
        print(f'\n搜 {q!r}  ({dt:.1f} ms)')
        for r in rs:
            tag = KIND_CN.get(r['kind'], r['kind'])
            print(f"   {r['score']:>4}  {r['display']:<18} {r['sub']:<16} "
                  f"{r['username']:<26} {tag}")
        if not rs:
            print('   （无结果）')
    book.close()
