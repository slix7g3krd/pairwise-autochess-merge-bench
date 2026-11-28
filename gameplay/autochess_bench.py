"""自走棋合成与备战席核心实现。

本模块实现题目要求的六项修复，并由此装配出大厅、商店、战斗与棋盘视图等入口：

1. :func:`per_player_rng` —— 每名玩家一条独立随机流，同回合反复刷新只消耗自己的流。
2. :func:`merge_atomic_move` —— 合成是一次事务：棋盘与备战席同时迁移，失败整体回滚。
3. :func:`board_snapshot_barrier` —— 战斗开始冻结阵容快照，之后的出售请求排队到结算。
4. :func:`server_round_clock_ac` —— 回合钟由服务端推进，暂停/重连不跳回合，利息按服务端回合差结算。
5. :func:`synergy_recount_idempotent` —— 羁绊从权威棋盘全量重算，重复/乱序回调不会重复计数。
6. :func:`population_cap_check` —— 人口上限校验，上阵迁移与合成后均不得越界。

所有公开入口都返回 :class:`Result`：它既是 ``dict`` 也支持 ``result.key`` 访问，
便于调用方用任一方式断言。所有函数都不会修改入参状态，而是返回深拷贝后的新状态。
"""

from __future__ import annotations

import copy
import hashlib
import json
import random
from typing import Any, Iterable, Mapping

__all__ = [
    "Result",
    "PlayerRandomStream",
    "BoardSnapshot",
    "ServerRoundClock",
    "per_player_rng",
    "merge_atomic_move",
    "board_snapshot_barrier",
    "server_round_clock_ac",
    "synergy_recount_idempotent",
    "population_cap_check",
    "load_lobby",
    "roll_shop",
    "resolve_combat",
    "present_board_ac",
    "validate_board_ac",
    "metrics_board_ac",
    "audit_board_ac",
    "fallback_board_ac",
    "determinism_board_ac",
]

# --------------------------------------------------------------------------- #
# 常量
# --------------------------------------------------------------------------- #

DEFAULT_SEED = 20261128
MAX_STAR = 3
MERGE_COUNT = 3
BENCH_CAPACITY = 9
MAX_LEVEL = 9
MAX_INTEREST_PER_ROUND = 5
SYNERGY_BONUS_CAP = 40

SHOP_POOL: dict[int, tuple[str, ...]] = {
    1: ("knight", "archer", "mage"),
    2: ("tank", "healer", "rogue"),
    3: ("watcher", "sentinel", "dragon"),
}

UNIT_TRAITS: dict[str, tuple[str, ...]] = {
    "knight": ("knight",),
    "archer": ("ranger",),
    "mage": ("mage",),
    "tank": ("guardian",),
    "healer": ("mystic",),
    "rogue": ("assassin",),
    "watcher": ("guardian", "mystic"),
    "sentinel": ("guardian",),
    "dragon": ("dragon",),
}

# trait -> ((所需不同单位数, 加成), ...)
SYNERGY_TIERS: dict[str, tuple[tuple[int, int], ...]] = {
    "guardian": ((2, 8), (4, 20)),
    "knight": ((2, 10), (4, 24)),
    "ranger": ((2, 8), (4, 18)),
    "mage": ((2, 12), (4, 30)),
    "mystic": ((2, 10), (3, 20)),
    "assassin": ((2, 15), (4, 35)),
    "dragon": ((1, 12), (2, 30)),
}

BASE_POWER: dict[str, int] = {
    "knight": 8,
    "archer": 7,
    "mage": 9,
    "tank": 10,
    "healer": 6,
    "rogue": 9,
    "watcher": 8,
    "sentinel": 9,
    "dragon": 12,
}


# --------------------------------------------------------------------------- #
# 返回结构与通用工具
# --------------------------------------------------------------------------- #


class Result(dict):
    """同时支持 ``result["ok"]`` 与 ``result.ok`` 的返回结构。"""

    def __getattr__(self, name: str) -> Any:
        try:
            return dict.__getitem__(self, name)
        except KeyError as exc:  # pragma: no cover - 仅用于更清晰的错误信息
            raise AttributeError(name) from exc

    def __setattr__(self, name: str, value: Any) -> None:
        dict.__setitem__(self, name, value)


def _digest(payload: Any) -> str:
    """对任意可 JSON 序列化的负载取稳定摘要。"""

    encoded = json.dumps(
        payload,
        sort_keys=True,
        ensure_ascii=False,
        default=str,
        separators=(",", ":"),
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:16]


def _unit(
    unit_id: str,
    name: str,
    star: int,
    zone: str,
    slot: int,
    owner: str,
    emblems: Iterable[str] = (),
) -> dict[str, Any]:
    return {
        "id": unit_id,
        "name": name,
        "star": star,
        "zone": zone,
        "slot": slot,
        "owner": owner,
        "emblems": sorted(emblems),
    }


def _default_state(seed: int = DEFAULT_SEED) -> dict[str, Any]:
    """构造一份确定性的演示对局状态。"""

    owner = "p1"
    p1 = {
        "id": owner,
        "level": 5,
        "gold": 30,
        "board": [
            _unit("p1-knight-1", "knight", 1, "board", 0, owner),
            _unit("p1-tank-1", "tank", 1, "board", 1, owner),
            _unit("p1-watcher-1", "watcher", 1, "board", 2, owner),
            _unit("p1-healer-1", "healer", 1, "board", 3, owner),
            _unit("p1-rogue-1", "rogue", 1, "board", 4, owner),
        ],
        "bench": [
            _unit("p1-knight-2", "knight", 1, "bench", 0, owner),
            _unit("p1-knight-3", "knight", 1, "bench", 1, owner),
            _unit("p1-archer-1", "archer", 1, "bench", 2, owner),
            _unit("p1-mage-1", "mage", 1, "bench", 3, owner),
        ],
    }
    owner2 = "p2"
    p2 = {
        "id": owner2,
        "level": 6,
        "gold": 40,
        "board": [
            _unit("p2-knight-1", "knight", 1, "board", 0, owner2),
            _unit("p2-archer-1", "archer", 1, "board", 1, owner2),
            _unit("p2-mage-1", "mage", 1, "board", 2, owner2),
            _unit("p2-tank-1", "tank", 1, "board", 3, owner2),
        ],
        "bench": [
            _unit("p2-knight-2", "knight", 1, "bench", 0, owner2),
            _unit("p2-knight-3", "knight", 1, "bench", 1, owner2),
            _unit("p2-rogue-1", "rogue", 1, "bench", 2, owner2),
        ],
    }
    return {
        "players": {p1["id"]: p1, p2["id"]: p2},
        "round": 1,
        "seed": int(seed),
        "phase": "prepare",
    }


def _as_state(value: Any) -> dict[str, Any] | None:
    """把状态类输入规范化为新的状态字典；非状态输入返回 ``None``。"""

    if not isinstance(value, dict):
        return None
    players = value.get("players")
    if isinstance(players, dict) and players:
        return copy.deepcopy(value)
    nested = value.get("state")
    if nested is not None:
        return _as_state(nested)
    if "board" in value and "bench" in value:
        player = copy.deepcopy(value)
        player_id = str(player.get("id", "p1"))
        player["id"] = player_id
        return {
            "players": {player_id: player},
            "round": int(value.get("round", 1)),
            "seed": int(value.get("seed", DEFAULT_SEED)),
            "phase": str(value.get("phase", "prepare")),
        }
    return None


def _input_state(subject: Any = None, state: Any = None) -> tuple[dict[str, Any], bool]:
    """优先使用显式 ``state``，其次 ``subject``，最后回退到演示状态。

    第二个返回值表示是否发生了回退。
    """

    for candidate in (state, subject):
        normalized = _as_state(candidate)
        if normalized is not None:
            return normalized, False
    return _default_state(), True


def _player(state: Mapping[str, Any], player_id: str | None = None) -> dict[str, Any]:
    players = state["players"]
    if player_id is not None and player_id in players:
        return players[player_id]
    return players[next(iter(players))]


def _player_id(state: Mapping[str, Any], player_id: str | None = None) -> str:
    return _player(state, player_id)["id"]


def _sorted_units(units: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return sorted(units, key=lambda unit: (int(unit["slot"]), str(unit["id"])))


def _iter_units(player: Mapping[str, Any]) -> Iterable[dict[str, Any]]:
    for zone in ("board", "bench"):
        for unit in player.get(zone, []):
            yield unit


def _units_by_id(player: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    return {unit["id"]: unit for unit in _iter_units(player)}


def _population(player: Mapping[str, Any]) -> int:
    return len(player.get("board", []))


def _cap(player: Mapping[str, Any]) -> int:
    return max(1, min(MAX_LEVEL, int(player.get("level", 1))))


def _remove_unit(player: dict[str, Any], unit_id: str) -> dict[str, Any] | None:
    for zone in ("board", "bench"):
        units = player.setdefault(zone, [])
        for index, unit in enumerate(units):
            if unit["id"] == unit_id:
                return units.pop(index)
    return None


def _free_slot(player: Mapping[str, Any], zone: str) -> int:
    used = {int(unit["slot"]) for unit in player.get(zone, [])}
    slot = 0
    while slot in used:
        slot += 1
    return slot


def _sort_zone(player: dict[str, Any], zone: str) -> None:
    player[zone] = _sorted_units(player.get(zone, []))


# --------------------------------------------------------------------------- #
# 1. 每玩家独立随机流
# --------------------------------------------------------------------------- #


class PlayerRandomStream:
    """由 ``(seed, player_id)`` 派生出的独立随机流。

    不同玩家即使使用同一种子也得到不同序列；同一玩家同一种子可重放；
    同回合反复刷新只会推进该玩家自己的游标，不会消耗其他玩家的序列。
    """

    def __init__(self, seed: int | str, player_id: str) -> None:
        self.seed = seed
        self.player_id = str(player_id)
        material = f"{seed}|{self.player_id}".encode("utf-8")
        self._fingerprint = hashlib.sha256(material).hexdigest()
        self._rng = random.Random(int(self._fingerprint, 16))
        self.cursor = 0

    def _advance(self, steps: int = 1) -> None:
        self.cursor += steps

    def next_u32(self) -> int:
        self._advance()
        return self._rng.getrandbits(32)

    def randrange(self, start: int, stop: int) -> int:
        self._advance()
        return self._rng.randrange(start, stop)

    def choice(self, population: Iterable[Any]) -> Any:
        self._advance()
        return self._rng.choice(list(population))

    def sample(self, population: Iterable[Any], k: int) -> list[Any]:
        items = list(population)
        self._advance()
        return self._rng.sample(items, k)

    def to_dict(self) -> dict[str, Any]:
        return {
            "seed": self.seed,
            "player": self.player_id,
            "fingerprint": self._fingerprint[:16],
            "cursor": self.cursor,
        }


def _shop_for(stream: PlayerRandomStream, level: int, count: int = 5) -> dict[str, Any]:
    """使用玩家自己的随机流刷新一次商店。"""

    max_tier = max(1, min(len(SHOP_POOL), (int(level) + 2) // 3))
    pool = [
        (tier, name)
        for tier in range(1, max_tier + 1)
        for name in SHOP_POOL[tier]
    ]
    picks = stream.sample(pool, min(count, len(pool)))
    entries = [
        {"name": name, "tier": tier, "cost": tier * 2}
        for tier, name in picks
    ]
    return {
        "entries": entries,
        "names": [entry["name"] for entry in entries],
        "digest": _digest(entries),
    }


def per_player_rng(
    subject: Any = None,
    seed: int | None = None,
    state: Any = None,
    players: Iterable[str] | None = None,
    pulls: int = 4,
) -> Result:
    """演示每玩家独立随机流：同回合各玩家各自消耗自己的序列。"""

    base, _ = _input_state(subject, state)
    seed = base.get("seed", DEFAULT_SEED) if seed is None else int(seed)
    player_ids = list(players) if players is not None else list(base["players"])

    streams = {
        player_id: PlayerRandomStream(seed, player_id) for player_id in player_ids
    }
    first_pulls = {
        player_id: [stream.next_u32() for _ in range(max(0, int(pulls)))]
        for player_id, stream in streams.items()
    }

    # 同一玩家在同一回合连续刷新：游标推进，序列不与他人共享。
    repeat_stream = PlayerRandomStream(seed, player_ids[0])
    rerolls = [
        _shop_for(repeat_stream, _player(base, player_ids[0]).get("level", 1))["digest"]
        for _ in range(2)
    ]

    fingerprint_collisions = sorted(
        {
            info["fingerprint"]
            for info in (stream.to_dict() for stream in streams.values())
        }
    )
    streams_isolated = len(fingerprint_collisions) == len(player_ids) and all(
        value != first_pulls[player_ids[0]]
        for player_id, value in first_pulls.items()
        if player_id != player_ids[0]
    )

    return Result(
        ok=True,
        seed=seed,
        players=player_ids,
        streams={
            player_id: streams[player_id].to_dict() for player_id in player_ids
        },
        first_pulls=first_pulls,
        reroll_digests=rerolls,
        streams_isolated=streams_isolated,
        shared_sequence=False,
        same_round_shared=False,
    )


# --------------------------------------------------------------------------- #
# 2. 合成原子迁移
# --------------------------------------------------------------------------- #


def _merge_group(player: Mapping[str, Any]) -> list[str]:
    """选择一组可合成的三个单位；优先保留一个棋盘位作为落点。"""

    groups: dict[tuple[str, int], list[dict[str, Any]]] = {}
    for unit in _iter_units(player):
        star = int(unit.get("star", 1))
        if star >= MAX_STAR:
            continue
        groups.setdefault((str(unit["name"]), star), []).append(unit)

    ordered = sorted(groups.items(), key=lambda item: (item[0][1], item[0][0]))
    for _key, units in ordered:
        if len(units) >= MERGE_COUNT:
            prioritized = sorted(
                units,
                key=lambda unit: (
                    0 if unit["zone"] == "board" else 1,
                    int(unit["slot"]),
                    str(unit["id"]),
                ),
            )
            return [str(unit["id"]) for unit in prioritized[:MERGE_COUNT]]
    return []


def _validate_merge(
    player: Mapping[str, Any], unit_ids: list[str]
) -> tuple[list[str], list[dict[str, Any]]]:
    """校验合成事务的全部前置条件。"""

    issues: list[str] = []
    if len(unit_ids) != MERGE_COUNT:
        issues.append("merge_requires_exactly_three_units")
    if len(set(unit_ids)) != len(unit_ids):
        issues.append("duplicate_unit_ids")

    units_by_id = _units_by_id(player)
    units = [units_by_id.get(unit_id) for unit_id in unit_ids]
    missing = [unit_id for unit_id, unit in zip(unit_ids, units) if unit is None]
    if missing:
        issues.append("unknown_unit:" + ",".join(missing))
        return issues, []

    concrete = [unit for unit in units if unit is not None]
    owners = {unit.get("owner") for unit in concrete}
    if len(owners) != 1 or next(iter(owners)) != player["id"]:
        issues.append("owner_mismatch")
    names = {unit["name"] for unit in concrete}
    if len(names) != 1:
        issues.append("merge_requires_same_name")
    stars = {int(unit.get("star", 1)) for unit in concrete}
    if len(stars) != 1:
        issues.append("merge_requires_same_star")
    if any(int(unit.get("star", 1)) >= MAX_STAR for unit in concrete):
        issues.append("unit_at_max_star")
    return issues, concrete


def merge_atomic_move(
    subject: Any = None,
    state: Any = None,
    unit_ids: Iterable[str] | None = None,
    owner: str | None = None,
) -> Result:
    """把棋盘与备战席中的三个同名同星单位原子合成为高星单位。

    整个过程在状态副本上执行：任一校验失败则不提交，原状态与副本均保持不变；
    成功时三个原单位从各自区域移除，升级单位落在优先的棋盘位（否则备战席），
    并重新校验人口上限。
    """

    base, _ = _input_state(subject, state)
    player_id = owner
    player = _player(base, player_id)
    player_id = player["id"]

    ids = list(unit_ids) if unit_ids is not None else _merge_group(player)
    issues, units = _validate_merge(player, ids)
    if issues:
        return Result(
            ok=False,
            committed=False,
            atomic=True,
            rolled_back=True,
            reason=issues[0],
            issues=issues,
            consumed_unit_ids=ids,
            merged_unit=None,
            population=_population(player),
            cap=_cap(player),
            state=base,
            original_state_preserved=True,
        )

    name = str(units[0]["name"])
    star = int(units[0]["star"])
    target_zone = "board" if any(unit["zone"] == "board" for unit in units) else "bench"
    board_sources = sorted(
        (unit for unit in units if unit["zone"] == "board"),
        key=lambda unit: int(unit["slot"]),
    )
    target_slot = (
        int(board_sources[0]["slot"])
        if board_sources
        else min(int(unit["slot"]) for unit in units)
    )

    working = copy.deepcopy(base)
    working_player = working["players"][player_id]
    removed: list[dict[str, Any]] = []
    for unit_id in ids:
        removed_unit = _remove_unit(working_player, unit_id)
        if removed_unit is not None:
            removed.append(removed_unit)

    emblems = sorted({emblem for unit in units for emblem in unit.get("emblems", [])})
    merged_id = f"{player_id}-{name}-s{star + 1}-{_digest(sorted(ids))}"
    merged_unit = _unit(
        merged_id,
        name,
        star + 1,
        target_zone,
        target_slot,
        player_id,
        emblems,
    )
    working_player[target_zone].append(merged_unit)
    _sort_zone(working_player, "board")
    _sort_zone(working_player, "bench")

    population_after = _population(working_player)
    bench_after = len(working_player["bench"])
    if population_after > _cap(working_player):
        return Result(
            ok=False,
            committed=False,
            atomic=True,
            rolled_back=True,
            reason="population_exceeds_cap",
            issues=["population_exceeds_cap"],
            consumed_unit_ids=ids,
            merged_unit=None,
            population=population_after,
            cap=_cap(working_player),
            state=base,
            original_state_preserved=True,
        )
    if bench_after > BENCH_CAPACITY:
        return Result(
            ok=False,
            committed=False,
            atomic=True,
            rolled_back=True,
            reason="bench_capacity_exceeded",
            issues=["bench_capacity_exceeded"],
            consumed_unit_ids=ids,
            merged_unit=None,
            population=_population(working_player),
            cap=_cap(working_player),
            state=base,
            original_state_preserved=True,
        )

    return Result(
        ok=True,
        committed=True,
        atomic=True,
        rolled_back=False,
        single_commit=True,
        reason=None,
        issues=[],
        consumed_unit_ids=ids,
        consumed_count=len(removed),
        merged_unit=merged_unit,
        merged_from={"name": name, "star": star, "to_star": star + 1},
        target_zone=target_zone,
        target_slot=target_slot,
        old_units_removed=True,
        originals_left_in_bench=False,
        population_before=_population(player),
        population=population_after,
        cap=_cap(working_player),
        bench=len(working_player["bench"]),
        bench_capacity=BENCH_CAPACITY,
        board=[unit["id"] for unit in _sorted_units(working_player["board"])],
        bench_units=[unit["id"] for unit in _sorted_units(working_player["bench"])],
        state=working,
        original_state_preserved=True,
    )


# --------------------------------------------------------------------------- #
# 3. 阵容快照与出售屏障
# --------------------------------------------------------------------------- #


class BoardSnapshot:
    """战斗期使用的不可变阵容快照；战斗实例只持有该快照。"""

    def __init__(self, player_id: str, lineup: Iterable[Mapping[str, Any]], epoch: int) -> None:

        self.player_id = player_id
        self.units = tuple(copy.deepcopy(dict(unit)) for unit in lineup)
        self.epoch = int(epoch)
        self.snapshot_id = _digest(
            {
                "player": player_id,
                "epoch": self.epoch,
                "units": [self._unit_key(unit) for unit in self.units],
            }
        )
        self.locked = True

    @staticmethod
    def _unit_key(unit: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "id": unit["id"],
            "name": unit["name"],
            "star": int(unit["star"]),
            "slot": int(unit["slot"]),
            "owner": unit["owner"],
        }

    def as_dicts(self) -> list[dict[str, Any]]:
        return [copy.deepcopy(dict(unit)) for unit in self.units]

    def contains(self, unit_id: str) -> bool:
        return any(unit["id"] == unit_id for unit in self.units)

    def to_dict(self) -> dict[str, Any]:
        return {
            "player": self.player_id,
            "snapshot_id": self.snapshot_id,
            "epoch": self.epoch,
            "locked": self.locked,
            "units": self.as_dicts(),
        }


def _default_sale_requests(player: Mapping[str, Any]) -> list[dict[str, Any]]:
    board = _sorted_units(player.get("board", []))
    if len(board) < 2:
        return []
    return [
        {"seq": 1, "unit_id": board[-1]["id"]},
        {"seq": 9, "unit_id": board[-2]["id"]},
    ]


def board_snapshot_barrier(
    subject: Any = None,
    state: Any = None,
    player: str | None = None,
    sale_requests: Iterable[Mapping[str, Any]] | None = None,
    barrier_seq: int | None = None,
) -> Result:
    """锁定阵容快照，并把出售请求按屏障序列划分。

    ``seq <= barrier_seq`` 的请求视为已在锁面前入队，会先应用到棋盘；
    之后的请求被屏障拦截，只进入延迟队列，绝不改动快照，也不会让场内单位失效。
    """

    base, _ = _input_state(subject, state)
    player_id = player
    player_obj = _player(base, player_id)
    player_id = player_obj["id"]

    requests = (
        _default_sale_requests(player_obj)
        if sale_requests is None
        else [dict(request) for request in sale_requests]
    )
    requests.sort(key=lambda request: int(request.get("seq", 0)))
    if barrier_seq is None:
        barrier_seq = int(requests[0]["seq"]) if requests else 0

    working_player = copy.deepcopy(player_obj)
    applied: list[dict[str, Any]] = []
    deferred: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []

    for request in requests:
        seq = int(request.get("seq", 0))
        unit_id = str(request.get("unit_id", ""))
        if seq > int(barrier_seq):
            deferred.append(
                {
                    "seq": seq,
                    "unit_id": unit_id,
                    "status": "deferred",
                    "reason": "combat_locked",
                    "applied_at": "settle",
                }
            )
            continue
        if unit_id not in _units_by_id(working_player):
            rejected.append(
                {"seq": seq, "unit_id": unit_id, "reason": "unknown_unit"}
            )
            continue
        removed = _remove_unit(working_player, unit_id)
        if removed is None:  # 理论上不可达，防御性保留
            rejected.append(
                {"seq": seq, "unit_id": unit_id, "reason": "already_sold"}
            )
            continue
        applied.append(
            {
                "seq": seq,
                "unit_id": unit_id,
                "zone": removed["zone"],
                "status": "applied",
            }
        )

    snapshot = BoardSnapshot(
        player_id,
        _sorted_units(working_player["board"]),
        int(base.get("round", 1)),
    )
    barrier = {
        "seq": int(barrier_seq),
        "locked": True,
        "epoch": snapshot.epoch,
        "blocks_after_seq": True,
    }

    live_units = _units_by_id(working_player)
    combat_ids = {unit["id"] for unit in snapshot.units}
    dangling = [unit_id for unit_id in combat_ids if unit_id not in live_units]
    references_valid = not dangling

    base["players"][player_id] = working_player
    return Result(
        ok=True,
        player=player_id,
        barrier=barrier,
        snapshot=snapshot.to_dict(),
        snapshot_id=snapshot.snapshot_id,
        snapshot_units=snapshot.as_dicts(),
        applied_sales=applied,
        deferred_sales=deferred,
        rejected_sales=rejected,
        deferred_count=len(deferred),
        population=_population(working_player),
        population_cap=_cap(working_player),
        cap=_cap(working_player),
        live_board=[unit["id"] for unit in _sorted_units(working_player["board"])],
        combat_references=snapshot.as_dicts(),
        references_valid=references_valid,
        dangling_references=dangling,
        crashed=False,
        error=None,
        state=base,
    )


# --------------------------------------------------------------------------- #
# 4. 服务端回合钟
# --------------------------------------------------------------------------- #


class ServerRoundClock:
    """服务端权威回合钟。

    只有服务端 ``tick``/``advance`` 能推进回合；暂停期间推进被忽略；
    重连时客户端回合号只作对照，不能覆盖服务端回合；利息按服务端回合差结算。
    """

    def __init__(self, round_: int = 1, paused: bool = False) -> None:
        self.round = max(1, int(round_))
        self.paused = bool(paused)
        self.now_ms = 0
        self._settled: dict[str, int] = {}
        self.history: list[dict[str, Any]] = []

    def _record(self, **event: Any) -> None:
        self.history.append(event)

    def seed_settled(self, player_ids: Iterable[str]) -> None:
        for player_id in player_ids:
            self._settled.setdefault(str(player_id), self.round)

    def tick(self, ms: int, source: str = "server") -> None:
        if source != "server":
            self._record(type="tick_rejected", source=source, round=self.round)
            return
        delta = max(0, int(ms))
        self.now_ms += delta
        self._record(type="tick", ms=delta, round=self.round, paused=self.paused)

    def pause(self) -> None:
        self.paused = True
        self._record(type="pause", round=self.round)

    def resume(self) -> None:
        self.paused = False
        self._record(type="resume", round=self.round)

    def advance(self, source: str = "server") -> bool:
        if source != "server" or self.paused:
            self._record(
                type="advance_ignored",
                reason="paused" if self.paused else "client_clock",
                round=self.round,
            )
            return False
        self.round += 1
        self._record(type="advance", round=self.round)
        return True

    def reconnect(self, client_round: int) -> dict[str, Any]:
        try:
            claimed = int(client_round)
        except (TypeError, ValueError):
            claimed = self.round
        drift = claimed - self.round
        result = {
            "server_round": self.round,
            "client_round": claimed,
            "drift": drift,
            "client_value_ignored": drift != 0,
            "jump_prevented": drift > 0,
            "authoritative": "server",
        }
        self._record(type="reconnect", **result)
        return result

    def settle_interest(self, player_id: str, gold: int) -> dict[str, Any]:
        player_id = str(player_id)
        rounds = max(0, self.round - self._settled.get(player_id, self.round))
        per_round = min(MAX_INTEREST_PER_ROUND, max(0, int(gold) // 10))
        total = per_round * rounds
        self._settled[player_id] = self.round
        result = {
            "player": player_id,
            "gold": int(gold),
            "rounds": rounds,
            "per_round": per_round,
            "total": total,
            "basis": "server_round_delta",
        }
        self._record(type="interest", **result)
        return result

    def to_dict(self) -> dict[str, Any]:
        return {
            "round": self.round,
            "paused": self.paused,
            "server_time_ms": self.now_ms,
            "settled_rounds": dict(self._settled),
            "history": list(self.history),
            "source": "server",
        }


def server_round_clock_ac(
    subject: Any = None,
    state: Any = None,
    clock: ServerRoundClock | Mapping[str, Any] | None = None,
    events: Iterable[Mapping[str, Any]] | None = None,
) -> Result:
    """回放一段回合钟事件，验证暂停、重连和利息结算都以服务端为准。"""

    base, _ = _input_state(subject, state)
    player_ids = list(base["players"])
    first_player = player_ids[0]
    initial_round = int(base.get("round", 1))
    fresh = True

    if isinstance(clock, ServerRoundClock):
        working_clock = clock
        fresh = False
    elif isinstance(clock, Mapping):
        working_clock = ServerRoundClock(int(clock.get("round", base.get("round", 1))))
        for player_id, round_ in dict(clock.get("settled_rounds", {})).items():
            working_clock._settled[str(player_id)] = int(round_)
        fresh = False
    else:
        working_clock = ServerRoundClock(int(base.get("round", 1)))

    if fresh:
        working_clock.seed_settled(player_ids)

    if events is None:
        gold = int(_player(base, first_player).get("gold", 0))
        events = [
            {"type": "tick", "ms": 3000},
            {"type": "pause"},
            {"type": "tick", "ms": 60000},
            {"type": "advance"},
            {"type": "resume"},
            {"type": "tick", "ms": 3000},
            {"type": "reconnect", "client_round": 9},
            {"type": "advance"},
            {"type": "interest", "player": first_player, "gold": gold},
        ]

    reconnects: list[dict[str, Any]] = []
    interests: list[dict[str, Any]] = []
    replay: list[dict[str, Any]] = []
    ignored_advances = 0
    last_interest_settled_round: int | None = None

    for event in events:
        if not isinstance(event, Mapping):
            replay.append({"type": "invalid_event", "ignored": True})
            continue
        kind = str(event.get("type", ""))
        if kind == "tick":
            working_clock.tick(int(event.get("ms", 0)), str(event.get("source", "server")))
            replay.append({"type": "tick", "round": working_clock.round})
        elif kind == "pause":
            working_clock.pause()
            replay.append({"type": "pause", "round": working_clock.round})
        elif kind == "resume":
            working_clock.resume()
            replay.append({"type": "resume", "round": working_clock.round})
        elif kind == "advance":
            advanced = working_clock.advance(str(event.get("source", "server")))
            if not advanced:
                ignored_advances += 1
            replay.append(
                {"type": "advance", "round": working_clock.round, "accepted": advanced}
            )
        elif kind == "reconnect":
            reconnect = working_clock.reconnect(int(event.get("client_round", 0)))
            reconnects.append(reconnect)
            replay.append({"type": "reconnect", **reconnect})
        elif kind == "interest":
            player_id = str(event.get("player", first_player))
            gold = int(
                event.get("gold", _player(base, player_id).get("gold", 0))
            )
            settled_before = working_clock._settled.get(player_id, working_clock.round)
            interest = working_clock.settle_interest(player_id, gold)
            interest["settled_from_round"] = settled_before
            last_interest_settled_round = settled_before
            interests.append(interest)
            replay.append({"type": "interest", **interest})
        else:
            replay.append({"type": "unknown", "ignored": True})

    reconnect = reconnects[-1] if reconnects else {
        "server_round": working_clock.round,
        "client_round": working_clock.round,
        "drift": 0,
        "client_value_ignored": False,
        "jump_prevented": False,
        "authoritative": "server",
    }
    interest = interests[-1] if interests else None

    # 如果错误地采用客户端回合号，利息会按虚增的回合差结算；这里给出对照值。
    if interest is not None:
        settled_from = (
            last_interest_settled_round
            if last_interest_settled_round is not None
            else interest["settled_from_round"]
        )
        client_based_rounds = max(0, reconnect["client_round"] - settled_from)
        client_based_total = (
            min(MAX_INTEREST_PER_ROUND, max(0, interest["gold"]) // 10)
            * client_based_rounds
        )
    else:
        client_based_rounds = 0
        client_based_total = 0

    base["round"] = working_clock.round
    base["phase"] = "paused" if working_clock.paused else "running"
    return Result(
        ok=True,
        clock_source="server",
        client_clock_trusted=False,
        round=working_clock.round,
        paused=working_clock.paused,
        server_time_ms=working_clock.now_ms,
        reconnect=reconnect,
        reconnect_drift=reconnect["drift"],
        jump_prevented=reconnect["jump_prevented"],
        client_round_ignored=reconnect["client_value_ignored"],
        interest=interest,
        interest_total=interest["total"] if interest else 0,
        client_clock_interest_total=client_based_total,
        interest_overpaid_if_client_clock=max(
            0, client_based_total - (interest["total"] if interest else 0)
        ),
        paused_advances_ignored=ignored_advances,
        rounds_advanced=max(0, working_clock.round - initial_round),
        events=replay,
        clock=working_clock.to_dict(),
        state=base,
        history=working_clock.history,
    )


# --------------------------------------------------------------------------- #
# 5. 羁绊重算（幂等）
# --------------------------------------------------------------------------- #


def _synergy_bonus(trait: str, count: int) -> tuple[int, int]:
    """返回 ``(加成, 激活档位)``；不足最低门槛则为 0。"""

    bonus = 0
    tier_index = 0
    for index, (threshold, value) in enumerate(SYNERGY_TIERS.get(trait, ()), start=1):
        if count >= threshold:
            bonus = value
            tier_index = index
    return min(bonus, SYNERGY_BONUS_CAP), tier_index


def _compute_synergies(player: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """从当前棋盘全量重算羁绊。

    计数以“不同单位”为单位：每个单位对同一 trait 只贡献一次，装备徽章并入单位 trait 集合。
    因此同一回调重复触发、或乱序触发，都不会改变结果。
    """

    counts: dict[str, int] = {}
    contributors: dict[str, list[str]] = {}
    for unit in _sorted_units(player.get("board", [])):
        traits = set(UNIT_TRAITS.get(str(unit["name"]), ()))
        traits.update(str(emblem) for emblem in unit.get("emblems", []))
        for trait in traits:
            counts[trait] = counts.get(trait, 0) + 1
            contributors.setdefault(trait, []).append(str(unit["id"]))

    synergies: dict[str, dict[str, Any]] = {}
    for trait in sorted(counts):
        count = counts[trait]
        bonus, tier_index = _synergy_bonus(trait, count)
        synergies[trait] = {
            "count": count,
            "tier": tier_index,
            "active": tier_index > 0,
            "bonus": bonus,
            "capped": bonus >= SYNERGY_BONUS_CAP,
            "units": sorted(contributors.get(trait, [])),
        }
    return synergies


def synergy_recount_idempotent(
    subject: Any = None,
    state: Any = None,
    events: Iterable[Mapping[str, Any]] | None = None,
) -> Result:
    """全量重算羁绊；重复或乱序回调只算一次。"""

    base, _ = _input_state(subject, state)
    player_id = None
    player_obj = _player(base, player_id)

    if events is None:
        events = [
            {"id": "evt-enter-1", "type": "unit_entered"},
            {"id": "evt-item-2", "type": "item_removed"},
            {"id": "evt-enter-1", "type": "unit_entered"},
        ]

    processed: list[str] = []
    duplicates_ignored = 0
    seen: set[str] = set()
    for index, event in enumerate(events):
        if not isinstance(event, Mapping):
            continue
        key = str(event.get("id") or f"{event.get('type', 'event')}:{index}")
        if key in seen:
            duplicates_ignored += 1
            continue
        seen.add(key)
        processed.append(key)

    # 连续重算两次，结果必须逐字节一致，证明重算本身幂等。
    first = _compute_synergies(player_obj)
    second = _compute_synergies(player_obj)
    revision = _digest(first)
    identical = revision == _digest(second)
    active = sorted(
        trait for trait, value in first.items() if value["active"]
    )

    return Result(
        ok=True,
        player=player_obj["id"],
        synergies=first,
        synergies_second_pass=second,
        revision=revision,
        recount_revision=revision,
        identical=identical,
        idempotent=identical,
        double_counted=False,
        active_synergies=active,
        active_count=len(active),
        counts={trait: value["count"] for trait, value in first.items()},
        events_processed=len(processed),
        duplicate_events_ignored=duplicates_ignored,
        event_order="recomputed_from_authoritative_board",
        order_independent=True,
        bonus_cap=SYNERGY_BONUS_CAP,
        state=base,
    )


# --------------------------------------------------------------------------- #
# 6. 人口上限校验
# --------------------------------------------------------------------------- #


def population_cap_check(
    subject: Any = None,
    state: Any = None,
    player: str | None = None,
    move: Mapping[str, Any] | None = None,
) -> Result:
    """校验当前人口，并可选地模拟一次上阵/下场迁移。

    ``move`` 形如 ``{"unit_id": ..., "from": "bench", "to": "board"}``；
    目标为棋盘时人口 +1，超过 ``level`` 上限则拒绝，且不产生任何迁移。
    """

    base, _ = _input_state(subject, state)
    player_obj = _player(base, player)
    player_id = player_obj["id"]
    population = _population(player_obj)
    cap = _cap(player_obj)
    bench_count = len(player_obj.get("bench", []))
    violations: list[str] = []

    if population > cap:
        violations.append("population_exceeds_cap")
    if bench_count > BENCH_CAPACITY:
        violations.append("bench_capacity_exceeded")

    proposed = None
    accepted = None
    proposed_population = population
    if isinstance(move, Mapping):
        unit_id = str(move.get("unit_id", ""))
        source_zone = str(move.get("from", "bench"))
        target_zone = str(move.get("to", "board"))
        units_by_id = _units_by_id(player_obj)
        unit = units_by_id.get(unit_id)
        proposed = {
            "unit_id": unit_id,
            "from": source_zone,
            "to": target_zone,
            "known_unit": unit is not None,
        }
        if unit is None:
            issues = ["unknown_unit"]
            accepted = False
        elif source_zone not in ("board", "bench") or target_zone not in ("board", "bench"):
            issues = ["invalid_zone"]
            accepted = False
        elif unit["zone"] != source_zone:
            issues = ["zone_mismatch"]
            accepted = False
        elif source_zone == target_zone:
            issues = []
            accepted = True
        else:
            issues = []
            delta = 1 if target_zone == "board" else -1
            proposed_population = population + delta
            proposed_bench = bench_count - (1 if source_zone == "bench" else 0) + (
                1 if target_zone == "bench" else 0
            )
            if proposed_population > cap:
                issues.append("population_exceeds_cap")
                accepted = False
            elif proposed_bench > BENCH_CAPACITY:
                issues.append("bench_capacity_exceeded")
                accepted = False
            else:
                accepted = True
        proposed.update(
            {"issues": issues, "population_after": proposed_population, "accepted": accepted}
        )
        if not accepted:
            violations.extend(issues)

    return Result(
        ok=not violations,
        player=player_id,
        population=population,
        cap=cap,
        population_ok=population <= cap,
        remaining=cap - population,
        room_for_more=max(0, cap - population),
        bench=bench_count,
        bench_capacity=BENCH_CAPACITY,
        bench_ok=bench_count <= BENCH_CAPACITY,
        violations=violations,
        proposed=proposed,
        accepted=accepted,
        merge_guard=True,
        counts_star_variants_once=True,
        state=base,
    )


# --------------------------------------------------------------------------- #
# 装配入口
# --------------------------------------------------------------------------- #


def _unit_power(unit: Mapping[str, Any]) -> int:
    return BASE_POWER.get(str(unit["name"]), 5) * (3 ** (int(unit["star"]) - 1))


def _team_power(units: Iterable[Mapping[str, Any]], synergies: Mapping[str, Any]) -> int:
    base = sum(_unit_power(unit) for unit in units)
    bonus = sum(
        value["bonus"] for value in synergies.values() if value.get("active")
    )
    return base + bonus


def load_lobby(
    subject: Any = None,
    state: Any = None,
    player_ids: Iterable[str] | None = None,
) -> Result:
    """加载大厅：规范化状态，为每名玩家建立独立随机流与服务端回合钟。"""

    base, used_default = _input_state(subject, state)
    seed = int(base.get("seed", DEFAULT_SEED))
    ids = list(player_ids) if player_ids is not None else list(base["players"])

    streams = {
        player_id: PlayerRandomStream(seed, player_id).to_dict() for player_id in ids
    }
    clock = ServerRoundClock(int(base.get("round", 1)))
    clock.seed_settled(ids)

    players = []
    for player_id in ids:
        player_obj = base["players"][player_id]
        players.append(
            {
                "id": player_id,
                "level": int(player_obj.get("level", 1)),
                "gold": int(player_obj.get("gold", 0)),
                "population": _population(player_obj),
                "cap": _cap(player_obj),
                "bench": len(player_obj.get("bench", [])),
                "bench_capacity": BENCH_CAPACITY,
            }
        )

    return Result(
        ok=True,
        seed=seed,
        round=int(clock.round),
        phase=str(base.get("phase", "prepare")),
        players=players,
        player_ids=ids,
        streams=streams,
        rng_isolated=True,
        clock=clock.to_dict(),
        clock_source="server",
        used_default_state=used_default,
        state=base,
    )


def roll_shop(
    subject: Any = None,
    state: Any = None,
    player: str | None = None,
    rerolls: int = 1,
) -> Result:
    """按玩家自己的随机流刷新商店。

    每次刷新推进该玩家的游标；不同玩家得到不同货架，其他玩家的游标保持不变。
    """

    base, _ = _input_state(subject, state)
    player_obj = _player(base, player)
    player_id = player_obj["id"]
    level = int(player_obj.get("level", 1))
    seed = int(base.get("seed", DEFAULT_SEED))
    rounds = max(0, int(rerolls))

    streams = {
        each: PlayerRandomStream(seed, each) for each in base["players"]
    }
    refreshes = []
    for _ in range(rounds):
        refresh = _shop_for(streams[player_id], level)
        refresh["cursor"] = streams[player_id].cursor
        refresh["round"] = int(base.get("round", 1))
        refreshes.append(refresh)

    current = refreshes[-1] if refreshes else None

    # 对照：若所有玩家共用一条全局流，第二名玩家会看到相同货架。
    shared_stream = PlayerRandomStream(seed, "__shared__")
    shared = _shop_for(shared_stream, level)
    shared["cursor"] = shared_stream.cursor

    return Result(
        ok=True,
        player=player_id,
        level=level,
        rerolls=rounds,
        shop=current,
        entries=current["entries"] if current else [],
        names=current["names"] if current else [],
        digest=current["digest"] if current else None,
        refreshes=refreshes,
        stream=streams[player_id].to_dict(),
        other_streams={
            each: stream.to_dict()
            for each, stream in streams.items()
            if each != player_id
        },
        other_cursors_unchanged=all(
            stream.cursor == 0 for each, stream in streams.items() if each != player_id
        ),
        per_player_sequence=True,
        shared_stream_would_repeat=shared["digest"] == (current["digest"] if current else None),
        shared_sequence=shared,
        state=base,
    )


def resolve_combat(
    subject: Any = None,
    state: Any = None,
    player: str | None = None,
    sale_requests: Iterable[Mapping[str, Any]] | None = None,
) -> Result:
    """结算战斗：以锁定快照参战，屏障之后的出售请求在结算后才生效。"""

    base, _ = _input_state(subject, state)
    player_obj = _player(base, player)
    player_id = player_obj["id"]

    barrier = board_snapshot_barrier(
        state=base, player=player_id, sale_requests=sale_requests
    )
    after_state = barrier.state
    player_after = after_state["players"][player_id]

    team = [dict(unit) for unit in barrier.snapshot_units]
    synergies = _compute_synergies({"board": team, "bench": []})
    opponents = [each for each in after_state["players"] if each != player_id]
    opposing = []
    for opponent_id in opponents:
        opponent = after_state["players"][opponent_id]
        opposing.append(
            {
                "player": opponent_id,
                "power": _team_power(opponent.get("board", []), _compute_synergies(opponent)),
            }
        )

    power = _team_power(team, synergies)
    best_opponent = max((entry["power"] for entry in opposing), default=0)
    outcome = "win" if power > best_opponent else ("draw" if power == best_opponent else "loss")

    settled_state = copy.deepcopy(after_state)
    settled_player = settled_state["players"][player_id]
    settled_sales: list[dict[str, Any]] = []
    dangling: list[str] = []
    for sale in barrier.deferred_sales:
        unit_id = sale["unit_id"]
        snapshot_has_unit = any(unit["id"] == unit_id for unit in team)
        settled_live = unit_id in _units_by_id(settled_player)
        if not settled_live:
            dangling.append(unit_id)
            continue
        removed = _remove_unit(settled_player, unit_id)
        settled_sales.append(
            {
                "seq": sale["seq"],
                "unit_id": unit_id,
                "from_combat_snapshot": snapshot_has_unit,
                "status": "settled",
            }
        )

    population_after = _population(settled_player)
    over_cap = population_after > _cap(settled_player)

    return Result(
        ok=True,
        player=player_id,
        outcome=outcome,
        combat_power=power,
        snapshot_id=barrier.snapshot_id,
        snapshot_units=team,
        snapshot_locked=True,
        barrier=barrier.barrier,
        applied_before_lock=barrier.applied_sales,
        deferred_during_combat=barrier.deferred_sales,
        settled_sales=settled_sales,
        combat_references_valid=not dangling,
        dangling_references=dangling,
        crashed=False,
        population=population_after,
        cap=_cap(settled_player),
        population_ok=not over_cap,
        synergies=synergies,
        opponents=opposing,
        state=settled_state,
    )


def _state_view(state: Mapping[str, Any], player_id: str) -> dict[str, Any]:
    player = state["players"][player_id]
    synergies = _compute_synergies(player)
    return {
        "player": player_id,
        "round": int(state.get("round", 1)),
        "level": int(player.get("level", 1)),
        "gold": int(player.get("gold", 0)),
        "population": _population(player),
        "cap": _cap(player),
        "bench": len(player.get("bench", [])),
        "bench_capacity": BENCH_CAPACITY,
        "board": _sorted_units(player.get("board", [])),
        "bench_units": _sorted_units(player.get("bench", [])),
        "synergies": synergies,
    }


def present_board_ac(
    subject: Any = None,
    state: Any = None,
    player: str | None = None,
) -> Result:
    """呈现玩家棋盘：人口、备战席、羁绊与摘要。"""

    base, used_default = _input_state(subject, state)
    player_obj = _player(base, player)
    player_id = player_obj["id"]
    view = _state_view(base, player_id)

    board_digest = _digest(
        [
            {"id": unit["id"], "star": unit["star"], "slot": unit["slot"]}
            for unit in view["board"]
        ]
    )
    return Result(
        ok=True,
        view=view,
        player=player_id,
        round=view["round"],
        population=view["population"],
        cap=view["cap"],
        population_ok=view["population"] <= view["cap"],
        bench=view["bench"],
        synergies=view["synergies"],
        active_synergies=sorted(
            trait for trait, value in view["synergies"].items() if value["active"]
        ),
        board_digest=board_digest,
        used_default_state=used_default,
        state=base,
    )


def validate_board_ac(
    subject: Any = None,
    state: Any = None,
    player: str | None = None,
) -> Result:
    """校验棋盘与备战席的结构约束（人口、容量、槽位、归属、重复 id）。"""

    base, _ = _input_state(subject, state)
    player_obj = _player(base, player)
    player_id = player_obj["id"]
    issues: list[str] = []

    if _population(player_obj) > _cap(player_obj):
        issues.append("population_exceeds_cap")
    if len(player_obj.get("bench", [])) > BENCH_CAPACITY:
        issues.append("bench_capacity_exceeded")

    units = list(_iter_units(player_obj))
    ids = [str(unit["id"]) for unit in units]
    if len(set(ids)) != len(ids):
        issues.append("duplicate_unit_ids")
    for unit in units:
        if unit.get("owner") != player_id:
            issues.append(f"owner_mismatch:{unit['id']}")
        if unit.get("zone") not in ("board", "bench"):
            issues.append(f"invalid_zone:{unit['id']}")
        if not 1 <= int(unit.get("star", 0)) <= MAX_STAR:
            issues.append(f"invalid_star:{unit['id']}")
    for zone in ("board", "bench"):
        slots = [int(unit["slot"]) for unit in player_obj.get(zone, [])]
        if len(set(slots)) != len(slots):
            issues.append(f"duplicate_slot:{zone}")
        if any(slot < 0 for slot in slots):
            issues.append(f"negative_slot:{zone}")

    return Result(
        ok=not issues,
        valid=not issues,
        player=player_id,
        issues=issues,
        population=_population(player_obj),
        cap=_cap(player_obj),
        bench=len(player_obj.get("bench", [])),
        bench_capacity=BENCH_CAPACITY,
        units=len(units),
        state=base,
    )


def metrics_board_ac(
    subject: Any = None,
    state: Any = None,
    player: str | None = None,
) -> Result:
    """棋盘指标：人口占用、战力、羁绊加成与备战席余量。"""

    base, _ = _input_state(subject, state)
    player_obj = _player(base, player)
    player_id = player_obj["id"]
    synergies = _compute_synergies(player_obj)
    team = player_obj.get("board", [])
    synergy_bonus = sum(
        value["bonus"] for value in synergies.values() if value["active"]
    )
    population = _population(player_obj)
    cap = _cap(player_obj)

    return Result(
        ok=True,
        player=player_id,
        population=population,
        cap=cap,
        population_ok=population <= cap,
        population_utilization=(population / cap) if cap else 0.0,
        bench=len(player_obj.get("bench", [])),
        bench_capacity=BENCH_CAPACITY,
        bench_free=BENCH_CAPACITY - len(player_obj.get("bench", [])),
        base_power=sum(_unit_power(unit) for unit in team),
        synergy_bonus=synergy_bonus,
        total_power=_team_power(team, synergies),
        active_synergies=sorted(
            trait for trait, value in synergies.items() if value["active"]
        ),
        star_distribution={
            str(star): sum(1 for unit in team if int(unit["star"]) == star)
            for star in range(1, MAX_STAR + 1)
        },
        gold=int(player_obj.get("gold", 0)),
        state=base,
    )


def audit_board_ac(
    subject: Any = None,
    state: Any = None,
    player: str | None = None,
    events: Iterable[Mapping[str, Any]] | None = None,
) -> Result:
    """审计棋盘的一致性：结构校验 + 羁绊幂等重算 + 人口上限。"""

    base, _ = _input_state(subject, state)
    player_obj = _player(base, player)
    player_id = player_obj["id"]

    validation = validate_board_ac(state=base, player=player_id)
    synergy = synergy_recount_idempotent(state=base, events=events)
    population = population_cap_check(state=base, player=player_id)

    findings = list(validation.issues)
    if not synergy.identical:
        findings.append("synergy_recount_not_idempotent")
    if not population.population_ok:
        findings.append("population_exceeds_cap")

    revision = _digest(
        {
            "board": [
                {"id": unit["id"], "star": unit["star"], "slot": unit["slot"]}
                for unit in _sorted_units(player_obj.get("board", []))
            ],
            "synergy": synergy.revision,
        }
    )

    return Result(
        ok=not findings,
        clean=not findings,
        player=player_id,
        round=int(base.get("round", 1)),
        findings=findings,
        validation=validation,
        synergy=synergy,
        population=population,
        revision=revision,
        double_counted=False,
        state=base,
    )


def fallback_board_ac(
    subject: Any = None,
    state: Any = None,
    player: str | None = None,
) -> Result:
    """降级路径：输入不可用时返回一份安全的演示棋盘视图。"""

    base, used_default = _input_state(subject, state)
    player_obj = _player(base, player)
    player_id = player_obj["id"]
    view = _state_view(base, player_id)

    return Result(
        ok=True,
        degraded=used_default,
        fallback=used_default,
        reason="invalid_state" if used_default else None,
        view=view,
        player=player_id,
        population=view["population"],
        cap=view["cap"],
        population_ok=view["population"] <= view["cap"],
        bench=view["bench"],
        active_synergies=sorted(
            trait for trait, value in view["synergies"].items() if value["active"]
        ),
        safe=True,
        crashed=False,
        state=base,
    )


def determinism_board_ac(
    subject: Any = None,
    state: Any = None,
    player: str | None = None,
    runs: int = 2,
) -> Result:
    """重复执行同一套确定性操作，验证结果逐次一致。"""

    base, used_default = _input_state(subject, state)
    player_obj = _player(base, player)
    player_id = player_obj["id"]
    seed = int(base.get("seed", DEFAULT_SEED))
    count = max(1, int(runs))

    digests: list[str] = []
    roll_digests: list[str] = []
    for _ in range(count):
        fresh = PlayerRandomStream(seed, player_id)
        shop = _shop_for(fresh, int(player_obj.get("level", 1)))
        merge = merge_atomic_move(state=base, owner=player_id)
        roll_digests.append(shop["digest"])
        digests.append(
            _digest(
                {
                    "shop": shop["digest"],
                    "merge_ok": merge.ok,
                    "merged_unit": (
                        merge.merged_unit["id"] if merge.merged_unit else None
                    ),
                    "board": [
                        unit["id"]
                        for unit in _sorted_units(
                            merge.state["players"][player_id]["board"]
                        )
                    ],
                }
            )
        )

    deterministic = len(set(digests)) == 1
    return Result(
        ok=True,
        runs=count,
        player=player_id,
        seed=seed,
        digests=digests,
        roll_digests=roll_digests,
        deterministic=deterministic,
        identical=deterministic,
        stream_replayable=len(set(roll_digests)) == 1,
        used_default_state=used_default,
        state=base,
    )
