"""测试/演示用的单进程 uvicorn 启动器：靠哨兵文件优雅退出，不依赖信号。

用法：python -m tests._server <port> <shutdown_flag_path>
父进程创建 flag 文件后，本进程结束当前请求、关闭服务器并正常退出。
沙箱或容器里禁止对子进程发信号时，这是唯一可靠的清理方式。
"""
import os
import sys
import threading
import time

import uvicorn


def main() -> int:
    port = int(sys.argv[1])
    flag = sys.argv[2]
    config = uvicorn.Config(
        "app.main:app", host="127.0.0.1", port=port, log_level="warning",
        timeout_graceful_shutdown=5,
    )
    server = uvicorn.Server(config)

    def watch() -> None:
        while not os.path.exists(flag):
            time.sleep(0.05)
        server.should_exit = True

    threading.Thread(target=watch, daemon=True).start()
    server.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
