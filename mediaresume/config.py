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
    # 用户自建 Trakt App 的凭据，Redirect URI 填 urn:ietf:wg:oauth:2.0:oob
    client_id: str = ""
    client_secret: str = ""
    # 实时播放进度推送到 Trakt（正在观看 / 暂停进度 / 看完）；全量同步由页面手动触发
    scrobble: bool = True


@dataclass
class Mapping:
    # Plex 用户名（为空表示服务器所有者 / 接受所有会话）
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
    if any(m.trakt_user for m in mappings) and not (trakt.client_id and trakt.client_secret):
        raise ValueError("用户映射绑定了 Trakt 账户，需要填写 Trakt Client ID 和 Client Secret")
    sync_raw = raw.get("sync") or {}
    sync = SyncConfig(**{k: v for k, v in sync_raw.items() if k in SyncConfig.__dataclass_fields__})
    return Config(
        plex=plex,
        emby=emby,
        mappings=mappings,
        sync=sync,
        trakt=trakt,
        db_path=raw.get("db_path", "data/state.db"),
        log_level=raw.get("log_level", "INFO"),
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
