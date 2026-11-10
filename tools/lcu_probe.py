"""探测英雄联盟客户端连接与选人状态（学习工具）。

用法：
    python -m tools.lcu_probe [客户端根目录]

默认客户端根目录通过 EOLIAN_LEARN_CLIENT_DIR 环境变量或内置探测路径确定。
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from eolian.endpoints import Gameflow
from eolian.lcu import LcuError, RiotClient, default_client_root, discover


def find_client_root() -> Path | None:
    root = default_client_root()
    return root if root.is_dir() else None


def main() -> int:
    # GBK 等旧编码控制台无法输出部分 Unicode 符号；做兜底避免崩溃
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    root = Path(sys.argv[1]) if len(sys.argv) > 1 else find_client_root()
    if root is None or not root.is_dir():
        print("未找到客户端目录；请传入路径或设置 EOLIAN_LEARN_CLIENT_DIR")
        return 2
    print(f"客户端根目录: {root}")
    try:
        lockfile = discover(root)
    except LcuError as exc:
        print(f"[失败] {exc}")
        return 1
    print(f"[连接] pid={lockfile.pid} port={lockfile.port} protocol={lockfile.protocol}")
    client = RiotClient(lockfile)
    gameflow = Gameflow(client)
    try:
        phase = gameflow.phase()
    except LcuError as exc:
        print(f"[失败] 客户端未运行或凭据过期: {exc}")
        print("       提示：请先启动英雄联盟客户端（lockfile 生效后重试）")
        return 1
    print(f"游戏阶段: {phase}")
    if phase == "ChampSelect":
        state = gameflow.champ_select_state()
        from eolian.catalog import SkinCatalog, champion_id_of_skin

        catalog = SkinCatalog()
        champion = catalog.champion(state.champion_id)
        print(
            f"选人中: 英雄={champion.name if champion else state.champion_id} "
            f"当前皮肤={state.selected_skin_id} cell={state.local_cell_id}"
        )
        if champion:
            for skin in catalog.skins_of(champion.id):
                mark = "◀ 当前" if skin.id == state.selected_skin_id else ""
                print(f"  skin{skin.skin_num:>3}  {skin.name}  {mark}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
