from __future__ import annotations

import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

import yaml


@dataclass
class PlexConfig:
    url: str
    token: str


@dataclass
class EmbyConfig:
    url: str
    api_key: str


@dataclass
class TraktConfig:
    # 用户自建 Trakt App 的凭据，Redirect URI 填 urn:ietf:wg:oauth:2.0:oob。
    # Trakt 新建的 App 不再发放 Client Secret，留空即可；旧 App 有的话可以填
    client_id: str = ""
    client_secret: str = ""
    # 实时播放进度推送到 Trakt（正在观看 / 暂停进度 / 看完）
    scrobble: bool = True
    # 定时全量同步到 Trakt / 从 Trakt 全量同步的间隔（小时），0 表示只手动执行
    push_interval: int = 0
    pull_interval: int = 0


@dataclass
class Mapping:
    # Plex 用户名（为空表示服务器所有者）
    plex_user: Optional[str]
    # Emby 用户名或用户 Id
    emby_user: str
    # 该 Plex 用户的 token，为空时使用 plex.token（所有者）
    plex_token: Optional[str] = None
    # 已授权的 Trakt 用户名，为空表示该映射不推送到 Trakt
    trakt_user: Optional[str] = None


@dataclass
class SyncConfig:
    reconcile_interval: int = 900
    # 增量轮询间隔：Emby 的所有变化、Plex 的标记已看
    poll_interval: int = 30
    # Plex 标记未看的检测间隔（需要拉取已看集合，开销较大）
    unwatch_poll_interval: int = 120
    # 播放中向对端推送进度的最小间隔
    progress_interval: int = 60
    # 写入某端后，在此秒数内忽略该端同一条目的事件（防回环）
    echo_window: int = 10
    # 只打印将要执行的写操作，不实际写入（首次运行建议开启观察）
    dry_run: bool = False


@dataclass
class Config:
    plex: PlexConfig
    emby: EmbyConfig
    mappings: list[Mapping]
    sync: SyncConfig = field(default_factory=SyncConfig)
    trakt: TraktConfig = field(default_factory=TraktConfig)
    db_path: str = "data/state.db"
    log_level: str = "INFO"


# 同步参数的取值下限（与 Web 页面一致）：过小会让轮询 / 对账空转，拖垮 Plex、Emby
SYNC_MIN = {"reconcile_interval": 60, "poll_interval": 5, "unwatch_poll_interval": 30, "progress_interval": 10, "echo_window": 1}
LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")


def _as_int(value: Any, name: str, minimum: int) -> int:
    # bool 是 int 的子类，yaml 里写成 true/false 时也要拒绝
    if isinstance(value, bool):
        raise ValueError(f"{name} 必须是整数")
    try:
        n = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name} 必须是整数") from None
    if n < minimum:
        raise ValueError(f"{name} 不能小于 {minimum}")
    return n


def _as_bool(value: Any, name: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in ("true", "false"):
        return value.strip().lower() == "true"
    raise ValueError(f"{name} 必须是 true 或 false")


def parse_config(raw: dict[str, Any]) -> Config:
    """从 dict 构造并校验配置，校验失败抛出 ValueError"""
    try:
        plex = PlexConfig(url=str(raw["plex"]["url"]).strip().rstrip("/"), token=str(raw["plex"]["token"]).strip())
        emby = EmbyConfig(url=str(raw["emby"]["url"]).strip().rstrip("/"), api_key=str(raw["emby"]["api_key"]).strip())
    except (KeyError, TypeError) as e:
        raise ValueError(f"缺少配置项: {e}") from None
    if not (plex.url and plex.token):
        raise ValueError("Plex 地址和 token 不能为空")
    if not (emby.url and emby.api_key):
        raise ValueError("Emby 地址和 API 密钥不能为空")
    mappings = [
        Mapping(
            plex_user=m.get("plex_user") or None,
            emby_user=str(m.get("emby_user") or "").strip(),
            plex_token=m.get("plex_token") or None,
            trakt_user=str(m.get("trakt_user") or "").strip() or None,
        )
        for m in raw.get("mappings") or []
    ]
    if not mappings:
        raise ValueError("至少需要一个用户映射")
    if any(not m.emby_user for m in mappings):
        raise ValueError("用户映射中的 Emby 用户不能为空")
    trakt_raw = raw.get("trakt") or {}
    trakt = TraktConfig(
        **{k: v for k, v in trakt_raw.items() if k in TraktConfig.__dataclass_fields__ and v is not None},
    )
    trakt.client_id, trakt.client_secret = str(trakt.client_id).strip(), str(trakt.client_secret).strip()
    trakt.push_interval = _as_int(trakt.push_interval, "Trakt 定时全量同步间隔（小时）", 0)
    trakt.pull_interval = _as_int(trakt.pull_interval, "Trakt 定时全量同步间隔（小时）", 0)
    trakt.scrobble = _as_bool(trakt.scrobble, "trakt.scrobble")
    if any(m.trakt_user for m in mappings) and not trakt.client_id:
        raise ValueError("用户映射绑定了 Trakt 账户，需要填写 Trakt Client ID")
    sync_raw = raw.get("sync") or {}
    sync = SyncConfig(**{k: v for k, v in sync_raw.items() if k in SyncConfig.__dataclass_fields__ and v is not None})
    for name, minimum in SYNC_MIN.items():
        setattr(sync, name, _as_int(getattr(sync, name), f"sync.{name}", minimum))
    sync.dry_run = _as_bool(sync.dry_run, "sync.dry_run")
    log_level = str(raw.get("log_level") or "INFO").strip().upper()
    if log_level not in LOG_LEVELS:
        raise ValueError(f"log_level 必须是 {' / '.join(LOG_LEVELS)} 之一")
    db_path = str(raw.get("db_path") or "data/state.db").strip()
    return Config(
        plex=plex,
        emby=emby,
        mappings=mappings,
        sync=sync,
        trakt=trakt,
        db_path=db_path,
        log_level=log_level,
    )


def load_config(path: str | Path) -> Config:
    return parse_config(yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {})


def config_to_dict(cfg: Config) -> dict[str, Any]:
    return asdict(cfg)


def save_config(cfg: Config, path: str | Path) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(yaml.safe_dump(config_to_dict(cfg), allow_unicode=True, sort_keys=False), encoding="utf-8")
    os.replace(tmp, path)
