# -*- coding: utf-8 -*-
"""
微信数据库解密与读取（自包含实现，不依赖 wechat-decrypt 的代码）

微信 4.0 (xwechat) 的库是 SQLCipher 4：
  - 每页 4096 字节，末尾 80 字节保留区，其中最后 16 字节是 IV
  - 第 1 页前 16 字节是 salt
  - 消息表命名：Msg_ + md5(username)
  - 发送者：real_sender_id → Name2Id.rowid → user_name

依赖：pycryptodome、zstandard
"""
import hashlib
import json
import os
import sqlite3
import struct
import time
from pathlib import Path

import zstandard as zstd
from Crypto.Cipher import AES

PAGE_SZ = 4096
KEY_SZ = 32
SALT_SZ = 16
IV_SZ = 16
RESERVE_SZ = 80
SQLITE_HDR = b'SQLite format 3\x00'
WAL_HDR_SZ = 32
WAL_FRAME_HDR_SZ = 24
WAL_FRAME_SZ = WAL_FRAME_HDR_SZ + PAGE_SZ

_zstd = zstd.ZstdDecompressor()

# 一个消息库常被好几个会话共用（80 MB 的 message_0.db 装着大部分聊天）。
# 这里记下「哪个版本的主库已经全量解过了」，checkpoint 之后第一个发现的会话
# 负责重解，其余的直接复用 —— 不然加了三个目标就要解三遍 80 MB。
# 用主库 mtime 当版本号比较，比看副本文件的 mtime 靠谱（不受时钟精度影响）。
_full_decrypted = {}


# ---------------- 解密 ----------------

def _decrypt_page(key: bytes, page: bytes, pgno: int) -> bytes:
    iv = page[PAGE_SZ - RESERVE_SZ: PAGE_SZ - RESERVE_SZ + IV_SZ]
    if pgno == 1:
        dec = AES.new(key, AES.MODE_CBC, iv).decrypt(page[SALT_SZ: PAGE_SZ - RESERVE_SZ])
        return bytes(SQLITE_HDR + dec + b'\x00' * RESERVE_SZ)
    dec = AES.new(key, AES.MODE_CBC, iv).decrypt(page[:PAGE_SZ - RESERVE_SZ])
    return dec + b'\x00' * RESERVE_SZ


def decrypt_to_file(src, key, dst):
    """整库解密写入 dst，返回页数。"""
    total = (os.path.getsize(src) + PAGE_SZ - 1) // PAGE_SZ
    done = 0
    with open(src, 'rb') as fi, open(dst, 'wb') as fo:
        for pgno in range(1, total + 1):
            page = fi.read(PAGE_SZ)
            if not page:
                break
            if len(page) < PAGE_SZ:
                page += b'\x00' * (PAGE_SZ - len(page))
            fo.write(_decrypt_page(key, page, pgno))
            done += 1
    return done


def patch_wal(wal_path, dst_db, key):
    """
    把 WAL 里的有效 frame 解密后覆盖到已解密的 db 副本上。
    幂等：重复执行结果一致。返回 patch 的页数。
    """
    if not os.path.exists(wal_path):
        return 0
    wal_sz = os.path.getsize(wal_path)
    if wal_sz < WAL_HDR_SZ:
        return 0

    patched = 0
    with open(wal_path, 'rb') as wf, open(dst_db, 'r+b') as df:
        hdr = wf.read(WAL_HDR_SZ)
        s1 = struct.unpack('>I', hdr[16:20])[0]
        s2 = struct.unpack('>I', hdr[20:24])[0]

        while wf.tell() + WAL_FRAME_SZ <= wal_sz:
            fh = wf.read(WAL_FRAME_HDR_SZ)
            if len(fh) < WAL_FRAME_HDR_SZ:
                break
            pgno = struct.unpack('>I', fh[0:4])[0]
            f1 = struct.unpack('>I', fh[8:12])[0]
            f2 = struct.unpack('>I', fh[12:16])[0]
            ep = wf.read(PAGE_SZ)
            if len(ep) < PAGE_SZ:
                break
            # 跳过上一个 WAL 周期遗留的 frame
            if f1 != s1 or f2 != s2:
                continue
            if pgno == 0 or pgno > 10_000_000:
                continue
            df.seek((pgno - 1) * PAGE_SZ)
            df.write(_decrypt_page(key, ep, pgno))
            patched += 1
    return patched


def load_keys(path) -> dict:
    """读 all_keys.json，返回 {相对路径: enc_key_bytes}"""
    raw = json.loads(Path(path).read_text(encoding='utf-8'))
    out = {}
    for k, v in raw.items():
        if isinstance(v, dict) and v.get('enc_key'):
            out[k] = bytes.fromhex(v['enc_key'])
        elif isinstance(v, str) and len(v) == 64:
            out[k] = bytes.fromhex(v)
    return out


def key_for(keys: dict, rel_path: str):
    """按 'session/session.db' 这种相对路径取 key，兼容 \ 与 /"""
    norm = rel_path.replace('/', '\\')
    for k, v in keys.items():
        if k.replace('/', '\\') == norm:
            return v
    return None


def missing_message_keys(db_dir, keys) -> list:
    """
    db_storage/message 下有哪些消息库我们没密钥。

    微信每隔一阵就新开一个分片库（用着用着 `message` 目录下就多出来一个），而
    all_keys.json 只是**某一次**提取的快照 —— 新库没密钥就永远解不开，之后
    所有新消息都写进那个库。现象很坑：进程在跑、日志干净、目标也切过去了，
    就是一条消息都不来。实测这么瞎了三天才被发现，所以启动时必须喊一声。

    顺带一个坑：`main.py decrypt` 并不会重新提取 —— 它看到 all_keys.json
    存在就跳过（ensure_keys 里 `if keys: return`），而且 decrypt 那步本身
    还有 argparse 冲突会直接报错退出。要真重新提取只能用 wcdb-key-tool。
    """
    mdir = Path(db_dir) / 'message'
    if not mdir.is_dir():
        return []
    out = []
    for p in sorted(mdir.glob('message_*.db')):
        if any(x in p.name for x in ('-wal', '-shm', 'fts', 'resource')):
            continue
        if key_for(keys, f'message\\{p.name}') is None:
            out.append(p.name)
    return out


# 微信把引用回复、链接分享、语音通话记录这些也都存成 local_type=1 的「文本」，
# 内容是 XML。只按 local_type 过滤的话，这些会混进上下文 ——
# 一大片标签既污染之后每一次解读，又可能被当成判定对象直接喂给 Jev。
_NOISE_PREFIX = ('<msg', '<?xml', '<voipmsg', '<appmsg', '<sysmsg',
                 '<img', '<emoji', '<weapp', '<voip')


def is_noise_text(text) -> bool:
    """是不是那种「看着像文本、其实是 XML 富媒体」的消息"""
    t = (text or '').lstrip()
    return t.startswith('<') and t.lower().startswith(_NOISE_PREFIX)


def zstd_text(v):
    """session 摘要可能是 zstd 压缩的 bytes"""
    if isinstance(v, bytes):
        try:
            return _zstd.decompress(v).decode('utf-8', 'replace')
        except Exception:
            return ''
    return v or ''


# ---------------- 会话表 ----------------

class SessionReader:
    """
    session.db 很小（约 0.5 MB），每次全量解密只要 ~46ms。
    这里用 mtime 短路：文件没动就不解密。
    """

    def __init__(self, db_path, key):
        self.db_path = db_path
        self.key = key
        self._stamp = None
        self._conn = None
        self._tmp = db_path + '.live_tmp'

    def _file_stamp(self):
        """
        主库和 -wal 都要看。

        SQLite 在 WAL 模式下，写入只改 -wal，主库要等 checkpoint 才更新。
        只盯主库 mtime 的话，新消息到来时 stamp 根本不变 —— 检测不到，
        得等 WAL 攒满 4 MB 自动 checkpoint（可能几分钟）才有反应。
        """
        if not os.path.exists(self.db_path):
            return None
        out = []
        for p in (self.db_path, self.db_path + '-wal'):
            try:
                st = os.stat(p)
                out.append((st.st_mtime, st.st_size))
            except OSError:
                out.append((0, 0))
        return tuple(out)

    def changed(self) -> bool:
        return self._file_stamp() != self._stamp

    def refresh(self):
        """返回是否有变化"""
        stamp = self._file_stamp()
        if stamp is None:
            return False
        if stamp == self._stamp and self._conn is not None:
            return False
        if self._conn is not None:
            self._conn.close()
            self._conn = None
        decrypt_to_file(self.db_path, self.key, self._tmp)
        # 主库解出来的是上一次 checkpoint 的内容，新消息还在 WAL 里，必须补上
        patch_wal(self.db_path + '-wal', self._tmp, self.key)
        self._conn = sqlite3.connect(self._tmp)
        self._conn.row_factory = sqlite3.Row
        self._stamp = stamp
        return True

    def sessions(self) -> dict:
        """{username: {ts, summary, sender, sender_name, unread, last_type}}"""
        out = {}
        for r in self._conn.execute("""
            SELECT username, unread_count, summary, last_timestamp,
                   last_msg_type, last_msg_sender, last_sender_display_name
            FROM SessionTable WHERE last_timestamp > 0
        """):
            out[r[0]] = {
                'ts': r[3] or 0,
                'summary': zstd_text(r[2]),
                'last_type': r[4],
                'sender': r[5] or '',
                'sender_name': r[6] or '',
                'unread': r[1] or 0,
            }
        return out

    def close(self):
        if self._conn is not None:
            self._conn.close()
            self._conn = None
        try:
            os.remove(self._tmp)
        except OSError:
            pass


# ---------------- 消息表 ----------------

def table_name(username: str) -> str:
    return 'Msg_' + hashlib.md5(username.encode()).hexdigest()


class MessageStore:
    """
    定位并常驻维护某个会话消息表的解密副本。

    微信把消息按会话分片到 message_0/1/2.db，表名可算（Msg_+md5）但库名不可算，
    所以依次全量解密候选库、查 sqlite_master 找表。

    全量解密一个 80 MB 级的库实测只要 ~0.4 秒（AES 本身很快，
    其中大半是 sqlite 查询开销），
    所以这里不做截断探测 —— 截断副本会被 SQLite 的 rootpage 校验拒掉。

    运行期靠 WAL 增量 patch 保持最新；主库被 checkpoint 改写时才重新全量解密。
    """

    def __init__(self, db_dir, username, keys, cache_dir, log=print):
        self.db_dir = Path(db_dir)
        self.username = username
        self.keys = keys
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.tbl = table_name(username)
        self.log = log

        self.db_path = None
        self.wal_path = None
        self.key = None
        self.cache = None

        self._conn = None
        self._db_mtime = None
        self._wal_mtime = None

    # ---- 打开与刷新 ----

    def _open(self):
        if self._conn is not None:
            self._conn.close()
        # check_same_thread=False：浮层现加的目标是在后台加载线程里建的，
        # 建完才交给主循环用 —— 连接必然跨线程（踩过：日志里刷满
        # "SQLite objects created in a thread can only be used in that same thread"）。
        # 两边不会同时碰它，这种「移交」是安全的。
        self._conn = sqlite3.connect(str(self.cache), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row

    def _stamp(self):
        try:
            db_m = os.path.getmtime(self.db_path)
            wal_m = os.path.getmtime(self.wal_path) if os.path.exists(self.wal_path) else 0
            return db_m, wal_m
        except OSError:
            return None

    def _candidates(self):
        mdir = self.db_dir / 'message'
        if not mdir.is_dir():
            return []
        cands = [p for p in mdir.glob('message_*.db')
                 if not any(x in p.name for x in ('-wal', '-shm', 'fts', 'resource'))]
        cands.sort(key=lambda p: p.stat().st_size, reverse=True)
        return cands

    def _peak(self, path):
        """这个解密副本里目标表的最新消息时间；没有这张表返回 None。"""
        try:
            conn = sqlite3.connect(str(path))
        except sqlite3.Error:
            return None
        try:
            hit = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (self.tbl,)
            ).fetchone()
            if hit is None:
                return None
            r = conn.execute(f"SELECT MAX(create_time) FROM {self.tbl}").fetchone()
            return r[0] or 0
        except sqlite3.DatabaseError:
            return None
        finally:
            conn.close()

    def locate(self):
        """
        在所有候选库里找含目标表的，选**消息最新**的那个。返回是否成功。

        不是「第一个命中就用」—— 微信会把同一个会话分片到好几个库、且老表不删
        （实测某个人的表：message_0.db 里二十几条、message_2.db 里六十条；新库一建好，
        老表就冻结不再写入）。按大小排序取第一个，会永远停在 80 MB 那个老库上，
        新消息全在新库里看不见 —— 现象是「切过去一条都没有」，很像监听坏了。

        代价是每个候选库都得解开看一眼（102 MB，首次约 1 秒；之后走缓存）。
        缓存复用：解密副本比主库新，说明主库之后没被 checkpoint 改过，可以直接
        拿来用（再补一次 WAL patch 即可）。WAL patch 会刷新副本 mtime，
        所以这个判断能自我维持。
        """
        best = None                      # (最新时间, db, key, cache)
        for db in self._candidates():
            key = key_for(self.keys, f'message\\{db.name}')
            if key is None:
                continue
            cache = self.cache_dir / (db.stem + '.dec.db')

            reused = cache.exists() and cache.stat().st_mtime >= db.stat().st_mtime
            t0 = time.perf_counter()
            if reused:
                note = '复用缓存'
            else:
                pages = decrypt_to_file(str(db), key, str(cache))
                _full_decrypted[str(db)] = db.stat().st_mtime
                note = f'{pages} 页 / {db.stat().st_size/1024/1024:.0f} MB'
            patch_wal(str(db) + '-wal', str(cache), key)
            dt = time.perf_counter() - t0

            peak = self._peak(cache)
            if peak is None:
                # 没有目标表。只有「这次才解出来、又没命中」的才删 ——
                # 复用来的缓存多半是另一个会话正在用的（80 MB 那个），
                # 删了既可能被文件锁挡下，又让对方下次得重解一遍。
                if not reused:
                    try:
                        cache.unlink()
                    except OSError:
                        pass
                continue

            when = time.strftime('%m-%d %H:%M', time.localtime(peak))
            self.log(f'  {db.name} 有目标表，最新 {when}（{note}，{dt:.2f}s）')
            if best is None or peak > best[0]:
                best = (peak, db, key, cache)

        if best is None:
            return False

        _, db, key, cache = best
        self.db_path = db
        self.wal_path = str(db) + '-wal'
        self.key = key
        self.cache = cache
        self.log(f'  选中 {db.name}')
        db_m, wal_m = self._stamp()
        self._db_mtime, self._wal_mtime = db_m, wal_m
        self._open()
        return True

    def refresh(self):
        """检查并增量更新，返回是否有变化"""
        if self.db_path is None:
            return False
        stamp = self._stamp()
        if stamp is None:
            return False
        db_m, wal_m = stamp
        if db_m == self._db_mtime and wal_m == self._wal_mtime:
            return False

        if db_m != self._db_mtime:
            # 主库被 checkpoint 改写了：WAL 从头开始，副本（旧主库+旧WAL）作废，
            # 必须全量重来，光靠 patch 补不回来。
            #
            # 但同一个库可能被好几个会话共用（80 MB 的 message_0.db 装着大部分聊天），
            # 别人可能已经替我们解过一遍了 —— 副本比主库新就说明不用再来一次，
            # 否则加三个人就得解三遍 80 MB。
            if _full_decrypted.get(str(self.db_path)) == db_m:
                patch_wal(self.wal_path, str(self.cache), self.key)
                self._open()
                self.log('  主库 checkpoint，副本已是最新（别的会话解过了）')
            else:
                t0 = time.perf_counter()
                decrypt_to_file(str(self.db_path), self.key, str(self.cache))
                _full_decrypted[str(self.db_path)] = db_m
                patch_wal(self.wal_path, str(self.cache), self.key)
                self._open()
                self.log(f'  主库 checkpoint，重新解密 {time.perf_counter()-t0:.2f}s')
        else:
            patch_wal(self.wal_path, str(self.cache), self.key)

        self._db_mtime, self._wal_mtime = db_m, wal_m
        return True

    # ---- 查询 ----

    def sender_map(self):
        """Name2Id.rowid → user_name"""
        m = {}
        try:
            for r in self._conn.execute("SELECT rowid, user_name FROM Name2Id"):
                v = r[1]
                m[r[0]] = v.decode('utf-8', 'replace') if isinstance(v, bytes) else v
        except sqlite3.OperationalError:
            pass
        return m

    def recent(self, n=12, since_id=None):
        """
        取最近 n 条消息（按 local_id 正序）。

        since_id: 只取 local_id 大于它的，用于兜住轮询间隙里漏掉的消息。

        游标用 local_id 而不是 create_time —— 同一分钟内连发的多条消息
        create_time 完全相同，用 `create_time > 上次` 比较会把它们漏掉
        （踩过：重放 2 条只抓到 1 条）。
        """
        sql = (f"SELECT local_id, local_type, real_sender_id, create_time, message_content "
               f"FROM {self.tbl} ")
        args = []
        if since_id is not None:
            sql += "WHERE local_id > ? "
            args.append(since_id)
        sql += "ORDER BY local_id DESC LIMIT ?"
        args.append(n)
        try:
            rows = self._conn.execute(sql, args).fetchall()
        except sqlite3.OperationalError:
            return []
        return [{'id': r[0], 'local_type': r[1], 'sender_id': r[2], 'ts': r[3],
                 'text': zstd_text(r[4])} for r in reversed(rows)]

    def close(self):
        if self._conn is not None:
            self._conn.close()
            self._conn = None


# ---------------- 自测 ----------------

if __name__ == '__main__':
    import sys
    import time

    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

    BASE = Path(__file__).parent
    cfg = json.loads((BASE / 'config.json').read_text(encoding='utf-8'))
    keys = load_keys(cfg['keys_file'])
    print(f'载入 {len(keys)} 个库密钥')

    db_dir = cfg['db_dir']
    me = cfg['me']

    # --- session ---
    skey = key_for(keys, 'session\\session.db')
    sr = SessionReader(str(Path(db_dir) / 'session' / 'session.db'), skey)
    t0 = time.perf_counter()
    sr.refresh()
    sess = sr.sessions()
    print(f'session: {len(sess)} 个会话，耗时 {(time.perf_counter()-t0)*1000:.0f} ms')

    for t in cfg['targets']:
        s = sess.get(t['username'])
        if s:
            print(f"  目标 {t['name']}: {s['summary'][:40]!r} unread={s['unread']} "
                  f"sender={s['sender']!r}")
        else:
            print(f"  目标 {t['name']}: 会话不存在")

    # --- 消息库 ---
    import datetime
    tgt = cfg['targets'][0]
    store = MessageStore(db_dir, tgt['username'], keys, BASE / 'data')
    t0 = time.perf_counter()
    ok = store.locate()
    print(f'定位+解密总耗时 {time.perf_counter()-t0:.2f} s，成功={ok}')

    if ok:
        smap = store.sender_map()
        rows = store.recent(10)
        print(f'\n最近 {len(rows)} 条：')
        for r in rows:
            who = smap.get(r['sender_id'], '?')
            tag = '[对方]' if who == tgt['username'] else ('[我]' if who == me else who[:12])
            t = datetime.datetime.fromtimestamp(r['ts']).strftime('%m-%d %H:%M')
            print(f"  {t} t={r['local_type']:<3} {tag:<5} {r['text'][:52]!r}")

        # WAL 增量刷新测试
        t0 = time.perf_counter()
        ch = store.refresh()
        print(f'\nrefresh: 变化={ch} 耗时 {(time.perf_counter()-t0)*1000:.0f} ms')
        store.close()

    sr.close()
