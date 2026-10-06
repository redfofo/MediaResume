from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
from pathlib import Path

import aiohttp
from aiohttp import web

from .web import DEFAULT_DIST, LogBuffer, Runner, create_app


async def main(config_path: Path, host: str, port: int) -> None:
    logs = LogBuffer()
    logging.basicConfig(level="INFO", format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    logging.getLogger().addHandler(logs)
    logging.getLogger("aiohttp.access").setLevel(logging.WARNING)

    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=120)) as http:
        runner = Runner(http, config_path)
        dist = Path(os.environ.get("MEDIARESUME_WEB_DIST", DEFAULT_DIST))
        app_runner = web.AppRunner(create_app(runner, logs, dist))
        await app_runner.setup()
        await web.TCPSite(app_runner, host, port).start()
        logging.getLogger("web").info("Web 管理页面: http://%s:%d", host, port)
        await runner.start()
        # 容器里本进程是 PID 1，没有处理函数时内核会忽略 SIGTERM，docker stop 要等 10 秒后强杀。
        # 收到信号后正常退出：停止引擎、提交数据库
        stop = asyncio.Event()
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                asyncio.get_running_loop().add_signal_handler(sig, stop.set)
            except NotImplementedError:  # Windows
                pass
        try:
            await stop.wait()
            logging.getLogger("web").info("收到停止信号，正在退出")
        finally:
            await runner.stop()
            await app_runner.cleanup()


def cli() -> None:
    parser = argparse.ArgumentParser(description="Plex <-> Emby 播放记录双向同步")
    parser.add_argument("-c", "--config", default="config.yaml")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("-p", "--port", type=int, default=8095)
    args = parser.parse_args()
    try:
        asyncio.run(main(Path(args.config), args.host, args.port))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    cli()
