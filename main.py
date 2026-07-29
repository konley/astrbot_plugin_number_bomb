"""群聊数字炸弹：轮流猜数缩区间，超时催促后强制引爆，管理员拆弹。"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import random
import re
import threading
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import At, Image, Plain
from astrbot.api.star import Context, Star, register
from astrbot.core.message.message_event_result import MessageChain
from astrbot.core.star.filter.command import GreedyStr
from astrbot.core.utils.astrbot_path import get_astrbot_data_path

LOG = "[astrbot_plugin_number_bomb]"
PURE_INT = re.compile(r"^-?\d+$")
HELP_KEYS = frozenset({"帮助", "help", "?", "？", "说明书", "玩法"})
STATUS_KEYS = frozenset({"状态", "status", "进度", "情报"})
SCORE_KEYS = frozenset({"积分", "分数", "score", "护盾", "points"})
QQ_AVATAR_URL = "http://q1.qlogo.cn/g?b=qq&nk={qq}&s=640"

# kimage 互动表情：关键词/别名 → (生成器模块文件, 函数名, dual?)
# dual=True 需要胜利者头像；False 仅失败者
_KIMAGE_MEME_ROUTES: dict[str, tuple[str, str, bool]] = {
    "撅": ("do.py", "generate_do", True),
    "抽": ("lash.py", "generate_lash", True),
    "发射": ("shoot.py", "generate_shoot", False),
    "射": ("shoot.py", "generate_shoot", False),
    "摸头": ("petpet.py", "generate_petpet", False),
    "杀": ("behead.py", "generate_behead", False),
}
_DEFAULT_SINGLE_KW = [
    "发射",
    "灰飞烟灭",
    "红温",
    "闪瞎",
    "哈哈镜",
    "敲",
    "上香",
    "out",
    "控制",
    "撕衣服",
    "诈尸",
    "卖掉了",
    "拿捏",
    "这是鸡",
    "捶爆",
    "汤姆嘲笑",
    "上坟",
    "震惊",
    "恍惚",
    "风车转",
]
_DEFAULT_DUAL_KW = ["撅", "抽", "揍"]


def _resolve_kimage_meme_dir() -> Path:
    """定位 kimage/meme（与「撅 / 发射」同源）。"""
    candidates = [
        Path(get_astrbot_data_path()) / "plugins" / "kimage" / "meme",
        Path(get_astrbot_data_path()) / "addons" / "plugins" / "kimage" / "meme",
        Path("/opt/astrbot/data/plugins/kimage/meme"),
    ]
    for p in candidates:
        if p.is_dir():
            return p
    return candidates[0]


def _parse_kw_list(raw, fallback: list[str]) -> list[str]:
    """面板 list / 逗号分隔字符串 → 去重保序关键词列表。"""
    items: list[str] = []
    if raw is None:
        src = fallback
    elif isinstance(raw, str):
        src = re.split(r"[,，\s]+", raw)
    elif isinstance(raw, (list, tuple)):
        src = list(raw)
    else:
        src = fallback
    seen: set[str] = set()
    for x in src:
        s = str(x or "").strip()
        if not s or s in seen:
            continue
        seen.add(s)
        items.append(s)
    return items or list(fallback)


@dataclass
class Game:
    low: int
    high: int
    bomb: int
    max_n: int
    umo: str
    players: list[str] = field(default_factory=list)
    names: dict[str, str] = field(default_factory=dict)
    turn_idx: int = 0
    status: str = "waiting"  # waiting | playing
    nudge_count: int = 0
    timer: asyncio.Task | None = None


@register(
    "astrbot_plugin_number_bomb",
    "konley",
    "群聊数字炸弹：轮流猜数缩区间，超时催促后强制引爆，管理员拆弹",
    "0.2.5",
    "https://github.com/konley/astrbot_plugin_number_bomb",
)
class NumberBomb(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        cfg = config or {}
        self.group_blacklist = {
            str(x).strip() for x in (cfg.get("group_blacklist") or []) if str(x).strip()
        }
        self.default_max = max(1, int(cfg.get("default_max", 100) or 100))
        self.turn_timeout_sec = max(10, int(cfg.get("turn_timeout_sec", 60) or 60))
        self.max_nudge = max(1, int(cfg.get("max_nudge", 2) or 2))
        self.join_wait_timeout_sec = max(
            60, int(cfg.get("join_wait_timeout_sec", 600) or 600)
        )
        self.allow_mid_join = bool(cfg.get("allow_mid_join", True))
        self.do_stop_event = bool(cfg.get("stop_event", True))
        self.shield_cost = max(1, int(cfg.get("shield_cost", 5) or 5))
        self.enable_punish_meme = bool(cfg.get("enable_punish_meme", True))
        self.enable_meme_generator = bool(cfg.get("enable_meme_generator", True))
        self.settle_delay_sec = max(
            0.0, float(cfg.get("settle_delay_sec", 1.5) or 1.5)
        )
        self.punish_single_keywords = _parse_kw_list(
            cfg.get("punish_single_keywords"), _DEFAULT_SINGLE_KW
        )
        self.punish_dual_keywords = _parse_kw_list(
            cfg.get("punish_dual_keywords"), _DEFAULT_DUAL_KW
        )
        # 0 = 整池都试；>0 最多尝试 N 个关键词
        self.meme_retry = max(0, int(cfg.get("meme_retry", 0) or 0))
        self._games: dict[str, Game] = {}
        self._score_lock = threading.Lock()
        self._scores: dict[str, dict] = {}
        data_root = Path(get_astrbot_data_path())
        self._score_path = (
            data_root / "plugin_data" / "astrbot_plugin_number_bomb" / "scores.json"
        )
        self._tmp_dir = data_root / "plugin_data" / "astrbot_plugin_number_bomb" / "tmp"
        # kimage: "do.py:generate_do" → callable
        self._meme_fns: dict[str, object] = {}
        self._meme_dir = _resolve_kimage_meme_dir()
        # meme_generator: keyword → Meme 模板
        self._mg_by_kw: dict[str, object] = {}
        self._mg_ready = False

    async def initialize(self) -> None:
        self._load_scores()
        self._tmp_dir.mkdir(parents=True, exist_ok=True)
        self._load_meme_gens()
        logger.info(
            "%s initialize default_max=%s turn_timeout=%s max_nudge=%s "
            "shield_cost=%s punish_meme=%s mg=%s settle_delay=%s "
            "single=%s dual=%s meme_retry=%s kimage_gens=%s mg_kws=%s "
            "scores=%s blacklist=%s",
            LOG,
            self.default_max,
            self.turn_timeout_sec,
            self.max_nudge,
            self.shield_cost,
            self.enable_punish_meme,
            self.enable_meme_generator and self._mg_ready,
            self.settle_delay_sec,
            self.punish_single_keywords,
            self.punish_dual_keywords,
            self.meme_retry,
            len(self._meme_fns),
            len(self._mg_by_kw),
            len(self._scores),
            len(self.group_blacklist),
        )

    async def terminate(self) -> None:
        for gid in list(self._games.keys()):
            await self._clear_game(gid, silent=True)
        self._save_scores()
        logger.info("%s terminate", LOG)

    # ── score / shield persistence ────────────────────────────

    def _load_scores(self) -> None:
        try:
            if self._score_path.is_file():
                raw = self._score_path.read_text(encoding="utf-8-sig")
                data = json.loads(raw) if raw.strip() else {}
                users = data.get("users") if isinstance(data, dict) else {}
                if isinstance(users, dict):
                    cleaned: dict[str, dict] = {}
                    for uid, row in users.items():
                        if not isinstance(row, dict):
                            continue
                        cleaned[str(uid)] = {
                            "points": max(0, int(row.get("points", 0) or 0)),
                            "name": str(row.get("name") or ""),
                        }
                    self._scores = cleaned
                    return
            self._scores = {}
        except Exception:
            logger.exception("%s load scores failed path=%s", LOG, self._score_path)
            self._scores = {}

    def _save_scores(self) -> None:
        try:
            self._score_path.parent.mkdir(parents=True, exist_ok=True)
            payload = {"users": self._scores}
            text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
            tmp = self._score_path.with_suffix(".json.tmp")
            tmp.write_text(text, encoding="utf-8")
            tmp.replace(self._score_path)
        except Exception:
            logger.exception("%s save scores failed path=%s", LOG, self._score_path)

    def _get_points(self, uid: str) -> int:
        row = self._scores.get(str(uid)) or {}
        return max(0, int(row.get("points", 0) or 0))

    def _add_win_point(self, uid: str, name: str = "") -> int:
        """胜利 +1 积分；返回新积分。"""
        if not uid:
            return 0
        with self._score_lock:
            uid = str(uid)
            row = self._scores.get(uid) or {"points": 0, "name": ""}
            row["points"] = max(0, int(row.get("points", 0) or 0)) + 1
            if name:
                row["name"] = name
            self._scores[uid] = row
            self._save_scores()
            return int(row["points"])

    def _try_consume_shield(self, uid: str, name: str = "") -> tuple[bool, int]:
        """若积分够 shield_cost 则扣除并返回 (True, 剩余)；否则 (False, 当前积分)。"""
        if not uid:
            return False, 0
        with self._score_lock:
            uid = str(uid)
            row = self._scores.get(uid) or {"points": 0, "name": ""}
            pts = max(0, int(row.get("points", 0) or 0))
            if pts < self.shield_cost:
                return False, pts
            pts -= self.shield_cost
            row["points"] = pts
            if name:
                row["name"] = name
            self._scores[uid] = row
            self._save_scores()
            return True, pts

    def _award_winners(self, g: Game, victim: str) -> None:
        for uid in g.players:
            if uid and uid != victim:
                self._add_win_point(uid, g.names.get(uid, ""))

    # ── commands ──────────────────────────────────────────────

    @filter.command("数字炸弹")
    async def cmd_bomb(self, event: AstrMessageEvent, arg: GreedyStr = GreedyStr):
        """开局/状态/帮助/积分。"""
        if not self._gate_group(event):
            return

        raw = "" if arg is GreedyStr else str(arg or "")
        raw = raw.strip()
        if not raw:
            raw = self._tail_after_cmd(event, "数字炸弹")

        key_norm = raw.lower() if raw.isascii() else raw

        if key_norm in HELP_KEYS:
            yield event.plain_result(self._help_text())
            self._stop(event)
            return
        if key_norm in STATUS_KEYS:
            yield event.plain_result(self._status_text(event))
            self._stop(event)
            return
        if key_norm in SCORE_KEYS:
            yield event.plain_result(self._score_text(event))
            self._stop(event)
            return

        n: int | None = None
        if raw:
            if not PURE_INT.fullmatch(raw):
                yield event.plain_result(
                    "参数不对喵～ /数字炸弹 | /数字炸弹 128 | 状态 | 积分 | 帮助"
                )
                self._stop(event)
                return
            n = int(raw)

        async for r in self._do_start(event, n):
            yield r

    @filter.command("数字炸弹状态")
    async def cmd_status_nospace(self, event: AstrMessageEvent):
        if not self._gate_group(event):
            return
        yield event.plain_result(self._status_text(event))
        self._stop(event)

    @filter.command("数字炸弹帮助")
    async def cmd_help_nospace(self, event: AstrMessageEvent):
        if not self._gate_group(event):
            return
        yield event.plain_result(self._help_text())
        self._stop(event)

    @filter.command("数字炸弹积分")
    async def cmd_score_nospace(self, event: AstrMessageEvent):
        if not self._gate_group(event):
            return
        yield event.plain_result(self._score_text(event))
        self._stop(event)

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("拆弹")
    async def cmd_defuse(self, event: AstrMessageEvent):
        if not self._gate_group(event):
            return
        gid = self._gid(event)
        if not gid or gid not in self._games:
            yield event.plain_result("没有炸弹可拆喵～")
            self._stop(event)
            return
        await self._clear_game(gid, silent=True)
        logger.info(
            "%s defuse group=%s admin=%s",
            LOG,
            gid,
            event.get_sender_id(),
        )
        yield event.plain_result("管理员拆除了炸弹喵～")
        self._stop(event)

    async def _do_start(self, event: AstrMessageEvent, n: int | None):
        gid = self._gid(event)
        if not gid:
            yield event.plain_result("只能在群里玩喵～")
            self._stop(event)
            return

        if gid in self._games:
            g = self._games[gid]
            yield event.plain_result(
                f"本局进行中 [{g.low}，{g.high}]喵，/数字炸弹 状态"
            )
            self._stop(event)
            return

        max_n = self.default_max if n is None else int(n)
        if max_n < 1 or max_n > 100000:
            yield event.plain_result("上限 N 需 1～100000 喵")
            self._stop(event)
            return

        bomb = random.randint(0, max_n)
        game = Game(
            low=0,
            high=max_n,
            bomb=bomb,
            max_n=max_n,
            umo=event.unified_msg_origin,
        )
        self._games[gid] = game
        self._arm_join_timer(gid)
        logger.info(
            "%s start group=%s max=%s bomb=%s sender=%s",
            LOG,
            gid,
            max_n,
            bomb,
            event.get_sender_id(),
        )
        yield event.plain_result(
            f"数字炸弹开始喵！0～{max_n}，发整数加入～"
        )
        self._stop(event)

    def _help_text(self) -> str:
        return (
            "数字炸弹喵\n"
            f"· /数字炸弹 [N] 开局（默认0～{self.default_max}）\n"
            "· /数字炸弹 状态|积分|帮助\n"
            "· /拆弹 管理员拆除\n"
            "发区间内整数=加入/猜；猜中或无安全数=炸\n"
            f"回合{self.turn_timeout_sec}s×催{self.max_nudge}次；未踩雷+1分，"
            f"{self.shield_cost}分可自动护盾"
        )

    def _status_text(self, event: AstrMessageEvent) -> str:
        gid = self._gid(event)
        g = self._games.get(gid or "")
        if not g:
            return "没有进行中的局喵，/数字炸弹 开一局"
        turn = self._turn_uid(g)
        turn_name = g.names.get(turn or "", turn or "-")
        left = max(0, self.max_nudge - g.nudge_count)
        status_cn = "等人" if g.status == "waiting" else "进行中"
        return (
            f"{status_cn} [{g.low}，{g.high}] 轮到{turn_name} "
            f"催{g.nudge_count}/{self.max_nudge}(再{left}炸)喵"
        )

    def _score_text(self, event: AstrMessageEvent) -> str:
        uid = str(event.get_sender_id() or "")
        name = event.get_sender_name() or uid
        pts = self._get_points(uid)
        if pts <= 0:
            return f"{name} 还没有积分喵"
        can = pts // self.shield_cost
        return f"{name} 积分{pts}（可护盾{can}次，每次{self.shield_cost}）喵"

    @staticmethod
    def _tail_after_cmd(event: AstrMessageEvent, cmd: str) -> str:
        text = (event.message_str or "").strip()
        for prefix in ("/", "!", "！", ".", "。", "#"):
            if text.startswith(prefix):
                text = text[len(prefix) :].lstrip()
        if text.startswith(cmd):
            return text[len(cmd) :].strip()
        return ""

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def on_group_number(self, event: AstrMessageEvent):
        """进行中：吃纯整数作为加入/猜测。"""
        if not self._gate_group(event, silent=True):
            return
        gid = self._gid(event)
        if not gid or gid not in self._games:
            return

        text = (event.message_str or "").strip()
        if not PURE_INT.fullmatch(text):
            return

        try:
            guess = int(text)
        except ValueError:
            return

        g = self._games[gid]
        uid = str(event.get_sender_id() or "")
        name = event.get_sender_name() or uid
        if not uid:
            return

        if guess < g.low or guess > g.high:
            if uid in g.players or g.status == "waiting":
                yield event.plain_result(f"超出范围，请发 [{g.low}，{g.high}] 喵")
                self._stop(event)
            return

        if g.status == "waiting":
            async for r in self._handle_waiting_guess(event, g, gid, uid, name, guess):
                yield r
            return

        in_game = uid in g.players
        is_turn = self._turn_uid(g) == uid
        if not in_game:
            if not self.allow_mid_join:
                return
            g.players.append(uid)
            g.names[uid] = name
            turn = self._turn_uid(g)
            chain = self._chain([
                self._at(uid, name),
                Plain(" 加入排队，等 "),
                self._at(turn or "", g.names.get(turn or "", "当前玩家")),
                Plain(" 喵"),
            ])
            yield event.chain_result(chain)
            self._stop(event)
            logger.info("%s mid_join group=%s uid=%s", LOG, gid, uid)
            return

        if not is_turn:
            turn = self._turn_uid(g)
            chain = self._chain([
                Plain("还没到你，现在是 "),
                self._at(turn or "", g.names.get(turn or "", "下一位")),
                Plain(" 喵"),
            ])
            yield event.chain_result(chain)
            self._stop(event)
            return

        g.names[uid] = name
        async for r in self._apply_guess(event, g, gid, uid, name, guess):
            yield r

    # ── core rules ────────────────────────────────────────────

    async def _handle_waiting_guess(
        self,
        event: AstrMessageEvent,
        g: Game,
        gid: str,
        uid: str,
        name: str,
        guess: int,
    ):
        if uid in g.players and len(g.players) == 1:
            yield event.plain_result("你已加入，再等一人喵～")
            self._stop(event)
            return

        if uid not in g.players:
            g.players.append(uid)
        g.names[uid] = name

        if guess == g.bomb:
            async for r in self._boom(event, g, gid, uid, reason="hit"):
                yield r
            return

        self._shrink(g, guess)
        if g.low == g.high:
            victim = g.players[0] if len(g.players) < 2 else self._next_uid_after(g, uid)
            async for r in self._boom(event, g, gid, victim or uid, reason="no_safe"):
                yield r
            return

        if len(g.players) < 2:
            chain = self._chain([
                self._at(uid, name),
                Plain(f" 加入成功 [{g.low}，{g.high}]，再等一人喵～"),
            ])
            yield event.chain_result(chain)
            self._stop(event)
            self._arm_join_timer(gid)
            return

        g.status = "playing"
        g.turn_idx = self._index_after(g, uid)
        g.nudge_count = 0
        nxt = self._turn_uid(g)
        chain = self._chain([
            Plain(f"安全 [{g.low}，{g.high}] → "),
            self._at(nxt or "", g.names.get(nxt or "", "下一位")),
        ])
        yield event.chain_result(chain)
        self._stop(event)
        self._arm_turn_timer(gid)

    async def _apply_guess(
        self,
        event: AstrMessageEvent,
        g: Game,
        gid: str,
        uid: str,
        name: str,
        guess: int,
    ):
        if guess == g.bomb:
            async for r in self._boom(event, g, gid, uid, reason="hit"):
                yield r
            return

        self._shrink(g, guess)
        if g.low == g.high:
            victim = self._next_uid_after(g, uid) or uid
            async for r in self._boom(event, g, gid, victim, reason="no_safe"):
                yield r
            return

        g.turn_idx = self._index_after(g, uid)
        g.nudge_count = 0
        nxt = self._turn_uid(g)
        chain = self._chain([
            Plain(f"安全 [{g.low}，{g.high}] → "),
            self._at(nxt or "", g.names.get(nxt or "", "下一位")),
        ])
        yield event.chain_result(chain)
        self._stop(event)
        self._arm_turn_timer(gid)

    def _load_meme_gens(self) -> None:
        """预加载 kimage 生成器 + meme_generator 关键词索引（失败不崩）。"""
        self._meme_dir = _resolve_kimage_meme_dir()
        self._meme_fns = {}
        needed: set[tuple[str, str]] = set()
        for mod_file, func_name, _dual in _KIMAGE_MEME_ROUTES.values():
            needed.add((mod_file, func_name))
        for mod_file, func_name in needed:
            fn = self._import_meme_func(self._meme_dir / mod_file, func_name)
            if fn is not None:
                self._meme_fns[f"{mod_file}:{func_name}"] = fn

        self._mg_by_kw = {}
        self._mg_ready = False
        if self.enable_meme_generator:
            try:
                from meme_generator import get_memes

                memes = get_memes() or []
                for m in memes:
                    # key 优先；同名 keyword 后写不覆盖已有（保留更精确的 key）
                    self._mg_by_kw.setdefault(str(m.key), m)
                    for k in list(m.info.keywords or []):
                        ks = str(k).strip()
                        if ks:
                            self._mg_by_kw.setdefault(ks, m)
                self._mg_ready = bool(self._mg_by_kw)
            except Exception:
                logger.warning(
                    "%s meme_generator unavailable, kimage-only", LOG, exc_info=True
                )
                self._mg_by_kw = {}
                self._mg_ready = False

        logger.info(
            "%s meme backends kimage=%s mg_ready=%s mg_kws=%s dir=%s",
            LOG,
            sorted(self._meme_fns.keys()),
            self._mg_ready,
            len(self._mg_by_kw),
            self._meme_dir,
        )

    def _import_meme_func(self, path: Path, func_name: str):
        if not path.is_file():
            logger.warning("%s meme module missing path=%s", LOG, path)
            return None
        try:
            spec = importlib.util.spec_from_file_location(
                f"number_bomb_kimage_{path.stem}", path
            )
            if not spec or not spec.loader:
                return None
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            return getattr(mod, func_name, None)
        except Exception:
            logger.exception("%s import meme failed path=%s", LOG, path)
            return None

    def _resolve_kimage_route(self, keyword: str) -> tuple[str, str, bool] | None:
        """kimage 关键词 → (mod_file, func_name, dual)。"""
        kw = (keyword or "").strip()
        if not kw:
            return None
        if kw in _KIMAGE_MEME_ROUTES:
            return _KIMAGE_MEME_ROUTES[kw]
        low = kw.lower()
        for k, v in _KIMAGE_MEME_ROUTES.items():
            if k.lower() == low:
                return v
        return None

    def _resolve_mg_meme(self, keyword: str):
        """meme_generator 关键词/key → Meme 或 None。"""
        if not self._mg_ready:
            return None
        kw = (keyword or "").strip()
        if not kw:
            return None
        m = self._mg_by_kw.get(kw)
        if m is not None:
            return m
        low = kw.lower()
        for k, v in self._mg_by_kw.items():
            if str(k).lower() == low:
                return v
        return None

    @staticmethod
    def _mg_needs_dual(meme) -> bool:
        """模板至少需要 2 张图 → 当双图用。"""
        try:
            return int(meme.info.params.min_images) >= 2
        except Exception:
            return False

    def _build_punish_pool(self, has_winner: bool) -> list[dict]:
        """
        总池 = 单图关键词 + 双图关键词。
        解析顺序：kimage 路由 → meme_generator 模板。
        返回 [{keyword, backend, dual, ...}, ...]
        """
        pool: list[dict] = []
        seen: set[str] = set()

        for kw in self.punish_single_keywords + self.punish_dual_keywords:
            # 1) kimage
            route = self._resolve_kimage_route(kw)
            if route is not None:
                mod_file, func_name, dual = route
                if dual and not has_winner:
                    continue
                key = f"kimage:{mod_file}:{func_name}"
                if key in seen:
                    continue
                if f"{mod_file}:{func_name}" not in self._meme_fns:
                    logger.warning(
                        "%s kimage gen missing kw=%s key=%s", LOG, kw, key
                    )
                    continue
                seen.add(key)
                pool.append(
                    {
                        "keyword": kw,
                        "backend": "kimage",
                        "dual": dual,
                        "mod_file": mod_file,
                        "func_name": func_name,
                    }
                )
                continue

            # 2) meme_generator
            if not (self.enable_meme_generator and self._mg_ready):
                logger.warning("%s punish kw unknown/skip kw=%s", LOG, kw)
                continue
            meme = self._resolve_mg_meme(kw)
            if meme is None:
                logger.warning("%s punish kw unknown/skip kw=%s", LOG, kw)
                continue
            try:
                min_img = int(meme.info.params.min_images)
            except Exception:
                min_img = 1
            if min_img <= 0:
                # 纯文本模板，惩罚场景跳过
                logger.warning("%s punish kw no-image template skip kw=%s", LOG, kw)
                continue
            dual = self._mg_needs_dual(meme)
            if dual and not has_winner:
                continue
            mk = str(getattr(meme, "key", kw))
            key = f"mg:{mk}"
            if key in seen:
                continue
            seen.add(key)
            pool.append(
                {
                    "keyword": kw,
                    "backend": "mg",
                    "dual": dual,
                    "meme": meme,
                    "meme_key": mk,
                }
            )
        return pool

    @staticmethod
    def _download_avatar(url: str, path: str) -> bool:
        try:
            import urllib.request

            req = urllib.request.Request(
                url, headers={"User-Agent": "AstrBot/number_bomb"}
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = resp.read()
            with open(path, "wb") as f:
                f.write(data)
            return bool(data)
        except Exception:
            logger.exception("%s download avatar failed url=%s", LOG, url)
            return False

    @staticmethod
    def _read_bytes(path: str) -> bytes | None:
        try:
            with open(path, "rb") as f:
                data = f.read()
            return data or None
        except Exception:
            return None

    def _pick_winner(self, g: Game, victim: str) -> str | None:
        """优先上家；否则随机一名非受害者参与者。"""
        prev = self._prev_uid(g, victim)
        if prev and prev != victim:
            return prev
        others = [u for u in g.players if u and u != victim]
        if not others:
            return None
        return random.choice(others)

    async def _try_kimage_gif(
        self,
        *,
        keyword: str,
        mod_file: str,
        func_name: str,
        dual: bool,
        winner: str | None,
        victim: str,
        victim_path: str,
        winner_path: str,
    ) -> str | None:
        fn = self._meme_fns.get(f"{mod_file}:{func_name}")
        if fn is None:
            return None
        loop = asyncio.get_event_loop()
        uid = uuid.uuid4().hex[:8]
        out_path = str(self._tmp_dir / f"nb_o_{os.getpid()}_{uid}.gif")
        try:
            if dual:
                if not winner or winner == victim:
                    return None
                if not winner_path or not os.path.isfile(winner_path):
                    return None

                def _run_dual():
                    fn(winner_path, victim_path, out_path)

                await loop.run_in_executor(None, _run_dual)
            else:

                def _run_single():
                    fn(victim_path, out_path)

                await loop.run_in_executor(None, _run_single)

            if os.path.isfile(out_path) and os.path.getsize(out_path) > 0:
                return out_path
            try:
                os.remove(out_path)
            except OSError:
                pass
            return None
        except Exception:
            logger.warning(
                "%s kimage gif failed kw=%s gen=%s:%s",
                LOG,
                keyword,
                mod_file,
                func_name,
                exc_info=True,
            )
            try:
                if os.path.isfile(out_path):
                    os.remove(out_path)
            except OSError:
                pass
            return None

    async def _try_mg_gif(
        self,
        *,
        keyword: str,
        meme,
        dual: bool,
        winner: str | None,
        victim: str,
        victim_bytes: bytes,
        winner_bytes: bytes | None,
        wname: str,
        vname: str,
    ) -> str | None:
        """调用 meme_generator 库生成；失败返回 None。"""
        try:
            from meme_generator import Image as MemeImage
        except Exception:
            logger.warning("%s meme_generator Image import failed", LOG, exc_info=True)
            return None

        loop = asyncio.get_event_loop()
        uid = uuid.uuid4().hex[:8]
        out_path = str(self._tmp_dir / f"nb_mg_{os.getpid()}_{uid}.gif")
        try:
            params = meme.info.params
            min_img = int(params.min_images)
            max_img = int(params.max_images)
            min_txt = int(params.min_texts)
            max_txt = int(params.max_texts)
            default_texts = list(params.default_texts or [])

            need = max(min_img, 1)
            if max_img > 0:
                need = min(need, max_img)
            # 双图：胜利者在前、失败者在后（与 kimage 撅/抽一致）
            images = []
            if dual or need >= 2:
                if not winner_bytes or not winner:
                    return None
                images.append(MemeImage(wname or str(winner), winner_bytes))
                images.append(MemeImage(vname or str(victim), victim_bytes))
            else:
                images.append(MemeImage(vname or str(victim), victim_bytes))
            # 需要更多图时用失败者头像填充
            while len(images) < need:
                images.append(MemeImage(vname or str(victim), victim_bytes))
            if max_img > 0:
                images = images[:max_img]

            texts: list[str] = []
            if min_txt > 0:
                names = [wname or str(winner or ""), vname or str(victim)]
                for t in default_texts:
                    if len(texts) >= max_txt > 0:
                        break
                    if t:
                        texts.append(str(t))
                for n in names:
                    if len(texts) >= min_txt:
                        break
                    if n and n not in texts:
                        texts.append(n)
                while len(texts) < min_txt:
                    texts.append(vname or "喵")
                if max_txt > 0:
                    texts = texts[:max_txt]

            options: dict = {}
            if wname:
                options.setdefault("name", wname)

            def _run():
                return meme.generate(images, texts, options)

            result = await loop.run_in_executor(None, _run)
            if result is None:
                return None
            if not isinstance(result, (bytes, bytearray)):
                # 错误对象（ImageNumberMismatch 等）
                logger.info(
                    "%s mg generate non-bytes kw=%s type=%s result=%s",
                    LOG,
                    keyword,
                    type(result).__name__,
                    result,
                )
                return None
            data = bytes(result)
            if not data:
                return None
            with open(out_path, "wb") as f:
                f.write(data)
            return out_path
        except Exception:
            logger.warning(
                "%s mg gif failed kw=%s key=%s",
                LOG,
                keyword,
                getattr(meme, "key", "?"),
                exc_info=True,
            )
            try:
                if os.path.isfile(out_path):
                    os.remove(out_path)
            except OSError:
                pass
            return None

    async def _try_one_punish_gif(
        self,
        cand: dict,
        *,
        winner: str | None,
        victim: str,
        victim_path: str,
        winner_path: str,
        victim_bytes: bytes,
        winner_bytes: bytes | None,
        wname: str,
        vname: str,
    ) -> str | None:
        """尝试单个候选；失败返回 None（不抛）。"""
        backend = cand.get("backend")
        kw = cand.get("keyword", "")
        dual = bool(cand.get("dual"))
        if dual and (not winner or winner == victim):
            return None
        if backend == "kimage":
            return await self._try_kimage_gif(
                keyword=kw,
                mod_file=cand["mod_file"],
                func_name=cand["func_name"],
                dual=dual,
                winner=winner,
                victim=victim,
                victim_path=victim_path,
                winner_path=winner_path,
            )
        if backend == "mg":
            return await self._try_mg_gif(
                keyword=kw,
                meme=cand["meme"],
                dual=dual,
                winner=winner,
                victim=victim,
                victim_bytes=victim_bytes,
                winner_bytes=winner_bytes,
                wname=wname,
                vname=vname,
            )
        return None

    async def _make_punish_gif(
        self, winner: str | None, victim: str, *, wname: str = "", vname: str = ""
    ) -> tuple[str | None, str]:
        """
        从配置池随机抽关键词生成表情包；失败则倒退换下一个。
        后端：kimage 优先匹配，其余走 meme_generator。
        返回 (gif路径, 展示用关键词)；全失败 (None, "")。
        """
        if not self.enable_punish_meme or not victim:
            return None, ""

        has_winner = bool(winner and winner != victim)
        pool = self._build_punish_pool(has_winner)
        if not pool:
            logger.info(
                "%s punish pool empty victim=%s has_winner=%s", LOG, victim, has_winner
            )
            return None, ""

        random.shuffle(pool)
        limit = len(pool) if self.meme_retry <= 0 else min(self.meme_retry, len(pool))
        candidates = pool[:limit]

        self._tmp_dir.mkdir(parents=True, exist_ok=True)
        loop = asyncio.get_event_loop()
        tag = uuid.uuid4().hex[:8]
        victim_path = str(self._tmp_dir / f"nb_v_{os.getpid()}_{tag}.png")
        winner_path = ""
        victim_bytes: bytes | None = None
        winner_bytes: bytes | None = None

        try:
            ok_v = await loop.run_in_executor(
                None,
                self._download_avatar,
                QQ_AVATAR_URL.format(qq=victim),
                victim_path,
            )
            if not ok_v:
                logger.warning("%s victim avatar download failed uid=%s", LOG, victim)
                return None, ""
            victim_bytes = self._read_bytes(victim_path)
            if not victim_bytes:
                return None, ""

            need_dual = any(bool(c.get("dual")) for c in candidates)
            if need_dual and has_winner:
                winner_path = str(self._tmp_dir / f"nb_w_{os.getpid()}_{tag}.png")
                ok_w = await loop.run_in_executor(
                    None,
                    self._download_avatar,
                    QQ_AVATAR_URL.format(qq=winner),
                    winner_path,
                )
                if not ok_w:
                    logger.warning("%s winner avatar failed, drop dual pool", LOG)
                    candidates = [c for c in candidates if not c.get("dual")]
                    winner_path = ""
                else:
                    winner_bytes = self._read_bytes(winner_path)

            for cand in candidates:
                dual = bool(cand.get("dual"))
                kw = cand.get("keyword", "")
                if dual and (not winner_path or not has_winner or not winner_bytes):
                    logger.info("%s skip dual kw=%s no winner avatar", LOG, kw)
                    continue
                path = await self._try_one_punish_gif(
                    cand,
                    winner=winner,
                    victim=victim,
                    victim_path=victim_path,
                    winner_path=winner_path,
                    victim_bytes=victim_bytes,
                    winner_bytes=winner_bytes,
                    wname=wname or str(winner or ""),
                    vname=vname or str(victim),
                )
                if path:
                    logger.info(
                        "%s punish gif ok kw=%s backend=%s dual=%s path=%s",
                        LOG,
                        kw,
                        cand.get("backend"),
                        dual,
                        os.path.basename(path),
                    )
                    return path, kw
                logger.info(
                    "%s punish gif fallback next after kw=%s backend=%s",
                    LOG,
                    kw,
                    cand.get("backend"),
                )

            logger.info(
                "%s punish gif all failed tried=%s",
                LOG,
                [(c.get("keyword"), c.get("backend")) for c in candidates],
            )
            return None, ""
        finally:
            for p in (victim_path, winner_path):
                if p and os.path.isfile(p):
                    try:
                        os.remove(p)
                    except OSError:
                        pass

    def _schedule_tmp_cleanup(self, path: str | None, delay: float = 15.0) -> None:
        if not path:
            return

        async def _job():
            await asyncio.sleep(delay)
            try:
                os.remove(path)
            except OSError:
                pass

        asyncio.create_task(_job())

    async def _emit_settle(
        self,
        event: AstrMessageEvent | None,
        umo: str,
        chains: list[list],
    ):
        """结算文案在前；若有后续（meme 图）则等 settle_delay 再发。"""
        if not chains:
            return
        first, *rest = chains
        if event is not None:
            yield event.chain_result(first)
            if rest:
                if self.settle_delay_sec > 0:
                    await asyncio.sleep(self.settle_delay_sec)
                for c in rest:
                    yield event.chain_result(c)
            self._stop(event)
        else:
            await self._send_umo_chain(umo, first)
            if rest:
                if self.settle_delay_sec > 0:
                    await asyncio.sleep(self.settle_delay_sec)
                for c in rest:
                    await self._send_umo_chain(umo, c)

    async def _boom(
        self,
        event: AstrMessageEvent | None,
        g: Game,
        gid: str,
        victim: str,
        *,
        reason: str,
    ):
        bomb = g.bomb
        vname = g.names.get(victim, victim)
        prev = self._prev_uid(g, victim)
        pname = g.names.get(prev or "", prev or "")
        umo = g.umo
        winner = self._pick_winner(g, victim)

        # 有足够积分 → 自动兑换护盾抵消，不惩罚、不给胜利积分、不发 meme
        used, left_pts = self._try_consume_shield(victim, vname)
        if used:
            boom_chain = self._chain([
                self._at(victim, vname),
                Plain(
                    f" 用{self.shield_cost}积分护盾抵消炸弹（剩{left_pts}）喵"
                ),
            ])
            await self._clear_game(gid, silent=True)
            logger.info(
                "%s shield group=%s victim=%s reason=%s bomb=%s left=%s",
                LOG,
                gid,
                victim,
                reason,
                bomb,
                left_pts,
            )
            if event is not None:
                yield event.chain_result(boom_chain)
                self._stop(event)
            else:
                await self._send_umo_chain(umo, boom_chain)
            return

        # 真实爆炸：未踩雷者各 +1 积分（静默）
        self._award_winners(g, victim)

        # 先等 meme 生成完（用户暂不可见），再：结算文案 → 延迟 → meme 图
        gif_path, action = await self._make_punish_gif(
            winner,
            victim,
            wname=g.names.get(winner or "", winner or ""),
            vname=vname,
        )

        if reason == "timeout":
            head = f"超时炸！炸弹{bomb} "
        elif reason == "no_safe":
            head = f"无安全数！炸弹{bomb} "
        else:
            head = f"BOOM！炸弹{bomb} "

        settle_parts: list = [
            Plain(head),
            self._at(victim, vname),
            Plain(" 请真心话/大冒险"),
        ]
        if prev and prev != victim:
            settle_parts.extend([
                Plain("，"),
                self._at(prev, pname),
                Plain(" 出题"),
            ])
        settle_parts.append(Plain(" 喵"))
        settle_chain = self._chain(settle_parts)

        await self._clear_game(gid, silent=True)
        logger.info(
            "%s boom group=%s victim=%s reason=%s bomb=%s meme=%s winner=%s",
            LOG,
            gid,
            victim,
            reason,
            bomb,
            action or "-",
            winner or "-",
        )

        # 文案在前，meme 在后（图已生成完毕）
        chains: list[list] = [settle_chain]
        if gif_path:
            chains.append([Image(file=str(gif_path))])
            self._schedule_tmp_cleanup(gif_path)

        async for r in self._emit_settle(event, umo, chains):
            yield r

    @staticmethod
    def _shrink(g: Game, guess: int) -> None:
        if guess < g.bomb:
            g.low = guess + 1
        elif guess > g.bomb:
            g.high = guess - 1

    @staticmethod
    def _at(uid: str, name: str = "") -> At | Plain:
        if not uid:
            return Plain(name or "小伙伴")
        try:
            qq: int | str = int(uid)
        except ValueError:
            qq = uid
        return At(qq=qq, name=name or None)

    @staticmethod
    def _chain(parts: list) -> list:
        return [p for p in parts if p is not None]

    # ── timers ────────────────────────────────────────────────

    def _cancel_timer(self, g: Game) -> None:
        if g.timer and not g.timer.done():
            g.timer.cancel()
        g.timer = None

    def _arm_join_timer(self, gid: str) -> None:
        g = self._games.get(gid)
        if not g or g.status != "waiting":
            return
        self._cancel_timer(g)

        async def _job():
            try:
                await asyncio.sleep(self.join_wait_timeout_sec)
                cur = self._games.get(gid)
                if not cur or cur is not g or cur.status != "waiting":
                    return
                await self._send_umo(cur.umo, "等人超时，本局取消喵")
                await self._clear_game(gid, silent=True)
                logger.info("%s join_timeout group=%s", LOG, gid)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("%s join_timer error group=%s", LOG, gid)

        g.timer = asyncio.create_task(_job())

    def _arm_turn_timer(self, gid: str) -> None:
        g = self._games.get(gid)
        if not g or g.status != "playing":
            return
        self._cancel_timer(g)

        async def _job():
            try:
                await asyncio.sleep(self.turn_timeout_sec)
                cur = self._games.get(gid)
                if not cur or cur is not g or cur.status != "playing":
                    return
                cur.nudge_count += 1
                turn = self._turn_uid(cur)
                tname = cur.names.get(turn or "", turn or "小伙伴")
                left = self.max_nudge - cur.nudge_count
                if cur.nudge_count < self.max_nudge:
                    chain = self._chain([
                        self._at(turn or "", tname),
                        Plain(
                            f" 该你了 [{cur.low}，{cur.high}] "
                            f"催{cur.nudge_count}/{self.max_nudge}"
                            f"（再{left}炸）喵"
                        ),
                    ])
                    await self._send_umo_chain(cur.umo, chain)
                    self._arm_turn_timer(gid)
                    return
                async for _ in self._boom(
                    None,
                    cur,
                    gid,
                    turn or (cur.players[-1] if cur.players else ""),
                    reason="timeout",
                ):
                    pass
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("%s turn_timer error group=%s", LOG, gid)

        g.timer = asyncio.create_task(_job())

    async def _clear_game(self, gid: str, *, silent: bool = False) -> None:
        g = self._games.pop(gid, None)
        if not g:
            return
        self._cancel_timer(g)
        if not silent:
            logger.info("%s clear group=%s", LOG, gid)

    async def _send_umo(self, umo: str, text: str) -> None:
        await self._send_umo_chain(umo, [Plain(text)])

    async def _send_umo_chain(self, umo: str, chain: list) -> None:
        if not umo:
            return
        try:
            await self.context.send_message(umo, MessageChain(chain))
        except Exception:
            logger.exception("%s send_message failed umo=%s", LOG, umo)

    # ── helpers ───────────────────────────────────────────────

    def _gate_group(self, event: AstrMessageEvent, *, silent: bool = False) -> bool:
        gid = self._gid(event)
        if not gid:
            return not silent
        if gid in self.group_blacklist:
            if not silent:
                logger.info("%s blocked blacklist group=%s", LOG, gid)
            return False
        return True

    @staticmethod
    def _gid(event: AstrMessageEvent) -> str:
        return str(event.get_group_id() or "").strip()

    @staticmethod
    def _turn_uid(g: Game) -> str | None:
        if not g.players:
            return None
        return g.players[g.turn_idx % len(g.players)]

    @staticmethod
    def _index_after(g: Game, uid: str) -> int:
        if uid not in g.players or len(g.players) < 2:
            return 0
        return (g.players.index(uid) + 1) % len(g.players)

    @staticmethod
    def _next_uid_after(g: Game, uid: str) -> str | None:
        if not g.players:
            return None
        if uid not in g.players:
            return g.players[0]
        return g.players[(g.players.index(uid) + 1) % len(g.players)]

    @staticmethod
    def _prev_uid(g: Game, uid: str) -> str | None:
        if not g.players or uid not in g.players or len(g.players) < 2:
            return None
        return g.players[(g.players.index(uid) - 1) % len(g.players)]

    def _stop(self, event: AstrMessageEvent) -> None:
        if self.do_stop_event:
            event.stop_event()
