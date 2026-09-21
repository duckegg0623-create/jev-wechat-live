# -*- coding: utf-8 -*-
"""
Jev 解读浮层 —— 无边框置顶小窗，自动贴在微信窗口旁边。

线程模型：tkinter 只能在主线程碰。
live 线程通过 push() 投递数据到 queue，主线程用 after() 轮询消费。
"""
import queue
import sys
import tkinter as tk
from pathlib import Path

try:
    import win32gui
except ImportError:
    win32gui = None

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent / 'jev-chat'))
from contacts import KIND_CN  # noqa: E402
# 标签表直接复用 jev_read 的，别在两处各维护一份
from jev_read import (ACTION_CN, EMOTION_CN, INTENT_CN,  # noqa: E402
                      NEED_CN, RELATION_CN, TONE_CN)

# ---- 配色 ----
BG = '#0f1117'
CARD = '#171a24'
LINE = '#252a38'
FG = '#e8eaf2'
FG_DIM = '#7f879c'
FG_FAINT = '#4a5165'
RED = '#ff4d6d'
ORANGE = '#ffa53d'
GREEN = '#3ddc84'
BLUE = '#4d9fff'
PURPLE = '#a78bfa'

FONT = 'Microsoft YaHei UI'
BAR_FULL = '█'
BAR_EMPTY = '·'


def enable_dpi_awareness():
    """
    声明进程 DPI 感知。

    不声明的话，Windows 会把整个窗口位图拉伸到实际 DPI（125% 屏上就是放大 1.25 倍），
    文字明显发虚。声明之后 tkinter 按真实 DPI 渲染，字是锐的；
    win32gui 返回的窗口坐标也同步变成物理像素，两者坐标系仍然一致。
    """
    try:
        import ctypes
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)   # PER_MONITOR_AWARE_V2
        except Exception:
            ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass


def _clamp(v, lo=0.0, hi=1.0):
    return max(lo, min(hi, v))


class Overlay:
    def __init__(self, cfg, target_names=None, on_close=None,
                 on_pause=None, on_switch=None, on_add=None, contact_book=None):
        self.cfg = cfg
        self.on_close = on_close
        self.on_pause = on_pause       # 点 ⏸ / ▶ 时调
        self.on_switch = on_switch     # 切换分析对象时调，参数是索引
        self.on_add = on_add           # 搜到新人时调，参数 (username, name, kind)
        self.book = contact_book       # ContactBook，为空就只能切已监听的
        self.ocfg = cfg.get('overlay', {})
        self.win_class = cfg.get('wechat_window_class', 'Qt51514QWindowIcon')
        self.q = queue.Queue()

        self.target_names = list(target_names or [])
        self.cur_target = 0
        self.paused = False
        self._search = None            # 换人面板
        self._s_results = []
        self._dot_color = FG_FAINT

        self._hwnd = None
        self._last_rect = None
        self._drag = None
        self._manual = False   # 用户拖动过就停止自动跟随
        self._docked = False   # 微信不在时是否已停靠到屏幕角落

        enable_dpi_awareness()

        self.root = tk.Tk()
        self.root.title('Jev 解读')
        self.root.overrideredirect(True)
        self.root.attributes('-topmost', True)
        self.root.attributes('-alpha', self.ocfg.get('alpha', 0.95))
        self.root.configure(bg=BG)

        # config 里的宽度按 96 dpi 的逻辑像素写，这里换算成实际物理像素
        try:
            self.scale = self.root.winfo_fpixels('1i') / 96.0
        except Exception:
            self.scale = 1.0
        self.scale = max(1.0, round(self.scale, 2))
        self.base_width = self.ocfg.get('width', 400)
        self.px_width = int(self.base_width * self.scale)

        self._build()
        self.root.update_idletasks()
        self._place_initial()

        self.root.after(120, self._drain)
        self.root.after(150, self._follow)

    # ---------------- 构建界面 ----------------

    def _build(self):
        w = self.px_width

        # 标题栏
        bar = tk.Frame(self.root, bg=CARD, height=30)
        self.bar = bar
        bar.pack(fill='x')
        bar.pack_propagate(False)

        self.dot = tk.Label(bar, text='●', bg=CARD, fg=FG_FAINT, font=(FONT, 9))
        self.dot.pack(side='left', padx=(10, 4))

        self.title_lbl = tk.Label(bar, text='JEV 实时解读', bg=CARD, fg=FG,
                                  font=(FONT, 9, 'bold'))
        self.title_lbl.pack(side='left')

        # ---- 右侧控件 ----
        # 注意 pack(side='right') 是从右往左依次排，所以先 pack 的在最右边
        close = tk.Label(bar, text='✕', bg=CARD, fg=FG_DIM, font=(FONT, 10),
                         cursor='hand2')
        close.pack(side='right', padx=(2, 10))
        close.bind('<Button-1>', lambda e: self._quit())

        self.pause_btn = tk.Label(bar, text='⏸', bg=CARD, fg=FG_DIM,
                                  font=(FONT, 10), cursor='hand2')
        self.pause_btn.pack(side='right', padx=4)
        self.pause_btn.bind('<Button-1>', lambda e: self._toggle_pause())

        # 目标名永远可点 —— 只有一个目标时它是「换人」入口，
        # 多个目标时还能顺便切回来
        self.target_lbl = tk.Label(
            bar, text='', bg=CARD, fg=FG_DIM, font=(FONT, 8), cursor='hand2')
        self.target_lbl.pack(side='right', padx=4)
        self.target_lbl.bind('<Button-1>', self._open_search)
        # 走统一入口，别在这儿另写一份 —— 没有目标时要显示「未选  ▾」，
        # 让用户知道这块能点（启动后一个人都不预设是正常用法）
        self._update_target_label()

        # 拖动：标题栏按下即拖（右侧控件区除外）
        for widget in (bar, self.dot, self.title_lbl):
            widget.bind('<Button-1>', self._drag_start)
            widget.bind('<B1-Motion>', self._drag_move)

        tk.Frame(self.root, bg=LINE, height=1).pack(fill='x')

        # 正文
        body = tk.Frame(self.root, bg=BG)
        body.pack(fill='both', expand=True, padx=12, pady=(8, 10))
        self.body = body

        self.msg_time = tk.Label(body, text='', bg=BG, fg=FG_FAINT,
                                 font=(FONT, 8), anchor='w')
        self.msg_time.pack(fill='x')

        self.msg_text = tk.Label(body, text='等待消息…', bg=BG, fg=FG,
                                 font=(FONT, 11), anchor='w', justify='left',
                                 wraplength=w - 40)
        self.msg_text.pack(fill='x', pady=(2, 10))

        self.content = tk.Frame(body, bg=BG)
        self.content.pack(fill='both', expand=True)

        self.root.geometry(f'{w}x300')

    def _clear(self):
        for c in self.content.winfo_children():
            c.destroy()

    # ---------------- 渲染 ----------------

    def _section(self, title, color=None):
        f = tk.Frame(self.content, bg=BG)
        f.pack(fill='x', pady=(6, 0))
        tk.Label(f, text=title, bg=BG, fg=color or FG_DIM,
                 font=(FONT, 8, 'bold'), anchor='w').pack(fill='x')
        return f

    def _bar_row(self, parent, label, pct, color, width=18, star=False):
        row = tk.Frame(parent, bg=BG)
        row.pack(fill='x', pady=1)
        name = ('★ ' if star else '') + label
        # 15 是量着最长那几个标签定的（「客气、公事公办」7 个汉字），
        # 再短进度条起点就不齐了
        tk.Label(row, text=name, bg=BG, fg=FG if star else FG_DIM,
                 font=(FONT, 9), anchor='w', width=15).pack(side='left')
        n = int(round(pct * width))
        b = tk.Frame(row, bg=BG)
        b.pack(side='left')
        tk.Label(b, text=BAR_FULL * n, bg=BG, fg=color,
                 font=(FONT, 8)).pack(side='left')
        tk.Label(b, text=BAR_EMPTY * (width - n), bg=BG, fg='#2a3042',
                 font=(FONT, 8)).pack(side='left')
        tk.Label(row, text=f'{pct*100:.0f}%', bg=BG, fg=FG_DIM,
                 font=(FONT, 8), width=4, anchor='e').pack(side='right')

    def _kv(self, parent, key, val, color=FG, key_color=None, key_width=15):
        row = tk.Frame(parent, bg=BG)
        row.pack(fill='x', pady=1)
        tk.Label(row, text=key, bg=BG, fg=key_color or FG_DIM, font=(FONT, 9),
                 anchor='w', width=key_width).pack(side='left')
        tk.Label(row, text=val, bg=BG, fg=color, font=(FONT, 9, 'bold'),
                 anchor='w').pack(side='left')

    def render(self, msg, ans, meta):
        """msg: 文本; ans: parse_answers 结果; meta: {time, name, kind}"""
        self._clear()

        ts = meta.get('time', '')
        kind = meta.get('kind', '')
        self.msg_time.config(text=f'{ts}   {kind}')
        self.msg_text.config(text=f'「{msg}」')

        raw = ans.get('_raw', {})
        probs = lambda k: (raw.get(k) or {}).get('probabilities') or {}

        # 状态点：这段对话气氛不对劲时变色。暂停时一律灰掉
        off = probs('relationship').get('off', 0)
        self._dot_color = RED if off >= 0.6 else (ORANGE if off >= 0.35 else GREEN)
        self.dot.config(fg=FG_FAINT if self.paused else self._dot_color)

        # ---- 潜台词 ----
        ls = ans.get('literal_same')
        if ls is not None:
            if ls >= 0.5:
                f = self._section('潜台词')
                self._kv(f, '说的就是想的', f'是  {ls*100:.0f}%', GREEN)
            else:
                f = self._section('潜台词', PURPLE)
                self._kv(f, '话里有话', f'不是  {(1-ls)*100:.0f}%', PURPLE)

        # ---- 情绪状态 ----
        p = probs('emotion')
        if p:
            f = self._section('情绪状态')
            for k, v in sorted(p.items(), key=lambda x: -x[1])[:3]:
                self._bar_row(f, EMOTION_CN.get(k, k), v, BLUE)

        # ---- 语气亲疏（score 返的是 0~4 档位均值，+1 变成 1~5 刻度）----
        tn = ans.get('tone')
        if tn is not None:
            i = max(0, min(len(TONE_CN) - 1, int(round(tn))))
            scaled = tn + 1
            color = GREEN if scaled >= 3.5 else (ORANGE if scaled <= 2.5 else FG)
            f = self._section('语气亲疏')
            self._kv(f, f'{scaled:.1f} / 5', TONE_CN[i], color, key_color=color)

        # ---- 真实意图 ----
        p = probs('real_intent')
        if p:
            f = self._section('真实意图')
            for k, v in sorted(p.items(), key=lambda x: -x[1])[:3]:
                self._bar_row(f, INTENT_CN.get(k, k), v, BLUE)

        # ---- 关系状态 ----
        p = probs('relationship')
        if p:
            f = self._section('关系状态', ORANGE if off >= 0.5 else None)
            for k, v in sorted(p.items(), key=lambda x: -x[1])[:2]:
                self._bar_row(f, RELATION_CN.get(k, k), v,
                              ORANGE if k == 'off' else PURPLE)

        # ---- 对方期待 ----
        p = probs('need')
        if p:
            f = self._section('对方期待')
            for k, v in sorted(p.items(), key=lambda x: -x[1])[:3]:
                self._bar_row(f, NEED_CN.get(k, k), v, PURPLE)

        # ---- 怎么回 ----
        p = probs('best_action')
        if p:
            f = self._section('怎么回')
            for i, (k, v) in enumerate(sorted(p.items(), key=lambda x: -x[1])[:3]):
                self._bar_row(f, ACTION_CN.get(k, k), v,
                              GREEN if i == 0 else FG_FAINT, star=(i == 0))

        self._resize()

    def render_status(self, title, detail=''):
        self._clear()
        self.msg_time.config(text='')
        self.msg_text.config(text=title)
        if detail:
            f = tk.Frame(self.content, bg=BG)
            f.pack(fill='x', pady=(6, 0))
            tk.Label(f, text=detail, bg=BG, fg=FG_DIM, font=(FONT, 8),
                     anchor='w', justify='left', wraplength=360).pack(fill='x')
        self._resize()

    def _resize(self):
        """
        先把窗口撑到屏幕高度再量内容。
        否则窗口被初始的小尺寸约束着，winfo_reqheight() 量到的是被压缩后的高度，
        底部内容会被裁掉（踩过这个坑）。
        """
        w = self.px_width
        self.root.geometry(f'{w}x{self.root.winfo_screenheight()}')
        self.root.update_idletasks()
        h = self.root.winfo_reqheight()
        self.root.geometry(f'{w}x{h}')
        self.root.update_idletasks()

    # ---------------- 控制条 ----------------

    def _toggle_pause(self):
        self.set_paused(not self.paused)
        if self.on_pause:
            try:
                self.on_pause()
            except Exception as e:
                print(f'[!] 暂停回调失败: {e}')

    def set_paused(self, paused):
        self.paused = bool(paused)
        self.pause_btn.config(text='▶' if self.paused else '⏸',
                              fg=ORANGE if self.paused else FG_DIM)
        self.dot.config(fg=FG_FAINT if self.paused else self._dot_color)

    def _set_target_name(self, name):
        if name in self.target_names:
            self.cur_target = self.target_names.index(name)
        self._update_target_label()

    def _update_target_label(self):
        name = self.target_names[self.cur_target] if self.target_names else ''
        if len(name) > 8:
            name = name[:7] + '…'
        self.target_lbl.config(text=(name or '未选') + '  ▾')

    # ---------------- 换人面板 ----------------

    def _open_search(self, e=None):
        """点目标名 → 上面列已在监听的，下面按名字/备注/拼音搜联系人"""
        if self._search is not None:
            self._close_search()
            return

        top = tk.Toplevel(self.root)
        top.overrideredirect(True)
        top.attributes('-topmost', True)
        top.configure(bg=LINE)
        self._search = top

        card = tk.Frame(top, bg=CARD)
        card.pack(fill='both', expand=True, padx=1, pady=1)

        self._s_entry = tk.Entry(
            card, bg='#0b0d13', fg=FG, insertbackground=FG, font=(FONT, 10),
            relief='flat', highlightthickness=1, highlightbackground=LINE,
            highlightcolor=BLUE)
        self._s_entry.pack(fill='x', padx=8, pady=(8, 4), ipady=4)
        self._s_entry.bind('<KeyRelease>', lambda e: self._do_search())
        self._s_entry.bind('<Return>', lambda e: self._pick_first())
        self._s_entry.bind('<Escape>', lambda e: self._close_search())

        self._s_list = tk.Frame(card, bg=CARD)
        self._s_list.pack(fill='both', expand=True, padx=8)

        self._s_hint = tk.Label(card, text='', bg=CARD, fg=FG_FAINT,
                                font=(FONT, 8), anchor='w', justify='left',
                                wraplength=self.px_width - 26)
        self._s_hint.pack(fill='x', padx=8, pady=(4, 8))

        self._s_results = []
        self._render_search()
        self._place_search()
        # 无边框窗口得手动抢焦点，否则键盘输入进不来
        top.focus_force()
        self._s_entry.focus_set()

    def _place_search(self):
        top = self._search
        if top is None:
            return
        top.update_idletasks()
        w = self.px_width
        h = top.winfo_reqheight()
        x = self.root.winfo_x()
        y = self.root.winfo_y() + self.bar.winfo_height() + 2
        sh = self.root.winfo_screenheight()
        if y + h > sh:
            y = max(0, sh - h - 8)
        top.geometry(f'{w}x{h}+{x}+{y}')

    def _s_head(self, text):
        tk.Label(self._s_list, text=text, bg=CARD, fg=FG_FAINT,
                 font=(FONT, 8), anchor='w').pack(fill='x', pady=(6, 2))

    def _s_row(self, title, sub, onclick, active=False, dim=False):
        bg = '#232a3d' if active else CARD
        row = tk.Frame(self._s_list, bg=bg)
        row.pack(fill='x', pady=1)
        fg = FG_FAINT if dim else (FG if active else FG_DIM)
        parts = [row]
        lbl = tk.Label(row, text=title, bg=bg, fg=fg, font=(FONT, 9),
                       anchor='w', cursor='hand2')
        lbl.pack(side='left', padx=(6, 0), ipady=2)
        parts.append(lbl)
        if sub:
            s = tk.Label(row, text=sub, bg=bg, fg=FG_FAINT, font=(FONT, 8),
                         anchor='e', cursor='hand2')
            s.pack(side='right', padx=(0, 6))
            parts.append(s)
        for w in parts:
            w.bind('<Button-1>', onclick)

    def _render_search(self):
        for c in self._s_list.winfo_children():
            c.destroy()

        if self.target_names:
            self._s_head('已在监听')
            for i, n in enumerate(self.target_names):
                cur = (i == self.cur_target)
                self._s_row(('● ' if cur else '    ') + n, '',
                            lambda e, i=i: self._pick_monitored(i), active=cur)

        if self._s_results:
            self._s_head('搜索结果')
            for r in self._s_results:
                kind_cn = KIND_CN.get(r['kind'], '')
                sub = r['sub'] or r['username']
                if kind_cn:
                    sub = f'{sub}  [{kind_cn}]'
                self._s_row(r['display'], sub,
                            lambda e, r=r: self._pick_contact(r),
                            dim=(r['kind'] != 'user'))

    def _do_search(self):
        q = self._s_entry.get().strip()
        if not q:
            self._s_results = []
            self._s_hint.config(text='输入名字 / 备注，或拼音首字母（如 zs）')
        elif self.book is None:
            self._s_results = []
            self._s_hint.config(text='联系人库没载入，只能切已经在监听的人')
        else:
            self._s_results = self.book.search(q)
            self._s_hint.config(
                text='回车选第一个 · Esc 关闭' if self._s_results
                else f'没找到「{q}」')
        self._render_search()
        self._place_search()

    def _pick_first(self):
        if self._s_results:
            self._pick_contact(self._s_results[0])

    def _pick_contact(self, r):
        """选中搜索结果里的某个人"""
        if r['kind'] != 'user':
            self._s_hint.config(
                text=f'{KIND_CN.get(r["kind"], r["kind"])}暂时监听不了，'
                     f'只支持一对一的聊天')
            return
        self._close_search()
        if self.on_add:
            try:
                self.on_add(r['username'], r['display'], r['kind'])
            except Exception as e:
                print(f'[!] 添加回调失败: {e}')

    def _pick_monitored(self, i):
        self._close_search()
        if i == self.cur_target:
            return
        self.cur_target = i
        self._update_target_label()
        if self.on_switch:
            try:
                self.on_switch(i)
            except Exception as e:
                print(f'[!] 切换回调失败: {e}')

    def _close_search(self):
        if self._search is None:
            return
        try:
            self._search.destroy()
        except Exception:
            pass
        self._search = None
        self._s_results = []

    # ---------------- 跟随微信 ----------------

    def _find_wechat(self):
        if win32gui is None:
            return None
        found = []

        def cb(h, _):
            if not win32gui.IsWindowVisible(h):
                return
            if win32gui.GetClassName(h) != self.win_class:
                return
            r = win32gui.GetWindowRect(h)
            # 最小化时窗口被挪到屏幕外
            if r[0] < -10000 or r[1] < -10000:
                return
            found.append((h, r))

        try:
            win32gui.EnumWindows(cb, None)
        except Exception:
            return None
        if not found:
            return None
        found.sort(key=lambda x: (x[1][2] - x[1][0]) * (x[1][3] - x[1][1]), reverse=True)
        return found[0]

    def _follow(self):
        if not self._manual:
            got = self._find_wechat()
            if got is None:
                self._dock()
            else:
                hwnd, rect = got
                if self._docked:
                    self._docked = False
                    self.root.deiconify()
                    self.root.attributes('-topmost', True)
                if rect != self._last_rect:
                    self._hwnd = hwnd
                    self._last_rect = rect
                    self._place(rect)
        self.root.after(150, self._follow)

    def _dock(self):
        """微信不在（最小化/关闭）时退到屏幕右上角待命，而不是消失"""
        if self._docked:
            return
        self._docked = True
        self._last_rect = None
        self._resize()
        w = self.px_width
        sw = self.root.winfo_screenwidth()
        pad = int(24 * self.scale)
        top = int(48 * self.scale)
        self.root.geometry(f'+{sw - w - pad}+{top}')
        self.root.deiconify()
        self.root.attributes('-topmost', True)

    def _place_initial(self):
        got = self._find_wechat()
        if got:
            self._last_rect = got[1]
            self._place(got[1])
        else:
            self._dock()

    def _place(self, rect):
        left, top, right, bottom = rect
        w = self.px_width
        self.root.update_idletasks()
        h = self.root.winfo_reqheight()
        ox = int(self.ocfg.get('offset_x', 14) * self.scale)
        oy = int(self.ocfg.get('offset_y', 0) * self.scale)
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()

        side = self.ocfg.get('side', 'right')
        if side == 'right' and right + ox + w <= sw:
            x = right + ox
        elif left - ox - w >= 0:
            x = left - ox - w
        else:
            x = right + ox          # 右边放不下也硬放右侧

        y = top + oy
        x = max(0, min(x, sw - w))
        y = max(0, min(y, sh - h))
        self.root.geometry(f'{w}x{h}+{x}+{y}')

    # ---------------- 拖动 ----------------

    def _drag_start(self, e):
        self._drag = (e.x_root, e.y_root,
                      self.root.winfo_x(), self.root.winfo_y())

    def _drag_move(self, e):
        if not self._drag:
            return
        x0, y0, wx, wy = self._drag
        self._manual = True
        self.root.geometry(f'+{wx + e.x_root - x0}+{wy + e.y_root - y0}')

    def _quit(self):
        self._close_search()
        if self.on_close:
            try:
                self.on_close()
            except Exception:
                pass
        self.root.destroy()

    # ---------------- 线程间通信 ----------------

    def push(self, kind, payload):
        """live 线程调用，线程安全"""
        self.q.put((kind, payload))

    def _drain(self):
        try:
            while True:
                kind, payload = self.q.get_nowait()
                if kind == 'result':
                    self.render(payload['msg'], payload['ans'], payload['meta'])
                elif kind == 'status':
                    self.render_status(payload.get('title', ''), payload.get('detail', ''))
                elif kind == 'target':
                    self._set_target_name(payload)
                elif kind == 'targets':
                    # monitor 后台加载完一个新人
                    self.target_names = list(payload)
                    self._update_target_label()
                    if self._search is not None:
                        self._render_search()
                        self._place_search()
                elif kind == 'paused':
                    self.set_paused(payload)
        except queue.Empty:
            pass
        self.root.after(120, self._drain)

    def run(self):
        self.root.mainloop()


# ---------------- 面板预览（调 UI 用） ----------------

DEMO_ANS = {
    '_raw': {
        'emotion': {'type': 'choice', 'choice': 'annoyed', 'probabilities': {
            'annoyed': 0.58, 'perfunctory': 0.21, 'calm': 0.13, 'serious': 0.08}},
        'real_intent': {'type': 'choice', 'choice': 'make_plan', 'probabilities': {
            'make_plan': 0.41, 'feedback': 0.33, 'ask_info': 0.18,
            'vent': 0.08}},
        'relationship': {'type': 'choice', 'choice': 'off', 'probabilities': {
            'off': 0.52, 'reserved': 0.30, 'normal': 0.15, 'easy': 0.03}},
        'need': {'type': 'choice', 'choice': 'action', 'probabilities': {
            'action': 0.61, 'answer': 0.22, 'agreement': 0.11,
            'listening': 0.06}},
        'best_action': {'type': 'choice', 'choice': 'give_plan', 'probabilities': {
            'give_plan': 0.47, 'answer_directly': 0.26, 'ask_clarify': 0.19,
            'empathize': 0.08}},
    },
    'literal_same': 0.32,
    'tone': 1.35,
}


if __name__ == '__main__':
    import json
    from pathlib import Path

    cfg = json.loads((Path(__file__).parent / 'config.json').read_text(encoding='utf-8'))
    ov = Overlay(cfg)
    ov.render('所以呢？', DEMO_ANS,
              {'time': '10:29', 'name': '示例联系人', 'kind': '对方'})
    ov.run()
