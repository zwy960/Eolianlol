"""皮肤目录加载与映射。

数据来源（优先级从高到低）：
1. ``%LOCALAPPDATA%\\Eolian\\catalog\\`` —— 界面上点"刷新目录"从 LCU 现拉的缓存；
2. 包内 ``data/skins.json`` / ``data/champion-summary.json``（打包时的快照）。

两者结构一致：``skinId → 皮肤记录``（LCU ``/lol-game-data/assets/v1/skins.json``
的快照），``champion-summary.json`` 是英雄摘要。

映射规则（docs/02-lcu-protocol.md）：``skinId = championId * 1000 + skinNum``。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path

from .settings import APP_DATA_DIR

DEFAULT_SKINS_JSON = Path(__file__).resolve().parent.parent / "data" / "skins.json"
DEFAULT_CHAMPIONS_JSON = (
    Path(__file__).resolve().parent.parent / "data" / "champion-summary.json"
)

# LCU 端点（客户端本地 HTTPS）
SKINS_API = "/lol-game-data/assets/v1/skins.json"
CHAMPIONS_API = "/lol-game-data/assets/v1/champion-summary.json"

# 从 LCU 拉下来的目录缓存
DEFAULT_CACHE_DIR = APP_DATA_DIR / "catalog"


class CatalogError(RuntimeError):
    """目录拉取/校验失败。"""



@dataclass(frozen=True)
class Champion:
    id: int
    name: str
    alias: str


@dataclass(frozen=True)
class Skin:
    id: int
    champion_id: int
    skin_num: int
    name: str
    is_base: bool
    raw: dict


def champion_id_of_skin(skin_id: int) -> int:
    """skinId → championId（除法取整）。"""
    return skin_id // 1000


def skin_num_of(skin_id: int) -> int:
    """skinId → skinNum（取余）。"""
    return skin_id % 1000


class SkinCatalog:
    """加载并索引皮肤目录（缓存优先，包内快照兜底）。"""

    def __init__(self, skins_path: str | Path | None = None,
                 champions_path: str | Path | None = None,
                 cache_dir: str | Path | None = None):
        self._cache_dir = Path(cache_dir) if cache_dir else DEFAULT_CACHE_DIR
        self._skins: dict[int, Skin] = {}
        self._champions: dict[int, Champion] = {}
        self._by_champion: dict[int, list[Skin]] = {}
        self.skins_path = DEFAULT_SKINS_JSON
        self.champions_path = DEFAULT_CHAMPIONS_JSON
        self.source = "bundled"
        self._resolve(skins_path, champions_path)
        self._load()

    def _resolve(self, skins_path: str | Path | None,
                 champions_path: str | Path | None) -> None:
        self.skins_path, self.champions_path, self.source = resolve_catalog_paths(
            skins_path, champions_path, self._cache_dir
        )

    def reload(self) -> str:
        """重新解析路径并重读文件（刷新目录后调用）。返回数据来源。"""
        self._skins.clear()
        self._champions.clear()
        self._by_champion.clear()
        self._resolve(None, None)
        self._load()
        return self.source

    def _load(self) -> None:
        raw_skins = json.loads(self.skins_path.read_text(encoding="utf-8"))
        raw_champions = json.loads(self.champions_path.read_text(encoding="utf-8"))
        for champ in raw_champions:
            cid = int(champ.get("id") or 0)
            if cid <= 0:
                continue
            self._champions[cid] = Champion(cid, champ.get("name") or "", champ.get("alias") or "")
        for skin_id_str, record in raw_skins.items():
            skin_id = int(skin_id_str)
            cid = champion_id_of_skin(skin_id)
            self._skins[skin_id] = Skin(
                id=skin_id,
                champion_id=cid,
                skin_num=skin_num_of(skin_id),
                name=record.get("name") or "",
                is_base=bool(record.get("isBase")),
                raw=record,
            )
            self._by_champion.setdefault(cid, []).append(self._skins[skin_id])
        for skins in self._by_champion.values():
            skins.sort(key=lambda s: (s.skin_num, s.id))

    def __len__(self) -> int:
        return len(self._skins)

    @property
    def champions(self) -> dict[int, Champion]:
        return dict(self._champions)

    def champion(self, champion_id: int) -> Champion | None:
        return self._champions.get(champion_id)

    def skin(self, skin_id: int) -> Skin | None:
        return self._skins.get(skin_id)

    def skins_of(self, champion_id: int) -> list[Skin]:
        """某英雄的全部皮肤（含基础皮肤，按 skinNum 升序）。"""
        return list(self._by_champion.get(champion_id, []))

    def skin_of(self, champion_id: int, skin_num: int) -> Skin | None:
        return self._skins.get(champion_id * 1000 + skin_num)


# ---------------------------------------------------------------- 目录刷新


def _valid_skins(data: object) -> bool:
    if not isinstance(data, dict) or not data:
        return False
    probe = next(iter(data.items()))
    key, record = probe
    return str(key).isdigit() and isinstance(record, dict)


def _valid_champions(data: object) -> bool:
    return (
        isinstance(data, list)
        and bool(data)
        and isinstance(data[0], dict)
        and "id" in data[0]
    )


def resolve_catalog_paths(skins_path: str | Path | None = None,
                          champions_path: str | Path | None = None,
                          cache_dir: str | Path | None = None
                          ) -> tuple[Path, Path, str]:
    """决定读哪份目录：显式参数 > LCU 缓存 > 包内快照。"""
    if skins_path and champions_path:
        return Path(skins_path), Path(champions_path), "explicit"
    base = Path(cache_dir) if cache_dir else DEFAULT_CACHE_DIR
    cached_skins = base / "skins.json"
    cached_champions = base / "champion-summary.json"
    if cached_skins.is_file() and cached_champions.is_file():
        try:
            if (_valid_skins(json.loads(cached_skins.read_text(encoding="utf-8")))
                    and _valid_champions(json.loads(
                        cached_champions.read_text(encoding="utf-8")))):
                return cached_skins, cached_champions, "cache"
        except (OSError, ValueError):
            pass  # 缓存坏了就当没有，回退包内快照
    return (
        Path(skins_path) if skins_path else DEFAULT_SKINS_JSON,
        Path(champions_path) if champions_path else DEFAULT_CHAMPIONS_JSON,
        "bundled",
    )


def write_catalog(skins: object, champions: object,
                  dest_dir: str | Path | None = None) -> dict[str, object]:
    """校验并原子写入目录缓存（两个文件要么都成功，要么都不动）。"""
    if not _valid_skins(skins):
        raise CatalogError("skins.json 结构不对（应为 skinId → 记录 的字典）")
    if not _valid_champions(champions):
        raise CatalogError("champion-summary.json 结构不对（应为含 id 的数组）")
    dest = Path(dest_dir) if dest_dir else DEFAULT_CACHE_DIR
    dest.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for name, payload in (("skins.json", skins),
                          ("champion-summary.json", champions)):
        target = dest / name
        temp = target.with_suffix(".tmp")
        temp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        temp.replace(target)
        written.append(target)
    return {
        "skins": written[0],
        "champions": written[1],
        "skins_count": len(skins),          # type: ignore[arg-type]
        "champions_count": len(champions),  # type: ignore[arg-type]
        "at": int(time.time()),
    }


def refresh_from_lcu(client_root: str | Path | None = None,
                     dest_dir: str | Path | None = None) -> dict[str, object]:
    """从本地客户端拉最新目录并落盘到缓存目录。"""
    from .lcu import LcuError, RiotClient, discover
    from .settings import load_settings

    root = Path(client_root) if client_root else load_settings().client_root
    try:
        lockfile = discover(root)
    except LcuError as exc:
        raise CatalogError(str(exc)) from exc
    client = RiotClient(lockfile)
    try:
        skins = client.get_json(SKINS_API)
        champions = client.get_json(CHAMPIONS_API)
    except LcuError as exc:
        raise CatalogError(f"LCU 目录拉取失败: {exc}") from exc
    return write_catalog(skins, champions, dest_dir)


def catalog_age_days(cache_dir: str | Path | None = None) -> float | None:
    """缓存目录的天数（没有缓存返回 None）。"""
    base = Path(cache_dir) if cache_dir else DEFAULT_CACHE_DIR
    target = base / "skins.json"
    try:
        return (time.time() - target.stat().st_mtime) / 86400.0
    except OSError:
        return None
