"""持久账本命令行入口。

``python3 -m ledger.cli --db var/ledger.db --host 127.0.0.1 --port 8081``
"""

import argparse
import logging
from typing import Optional

from .http_server import serve


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(description="独立 HTTP 持久账本")
    parser.add_argument("--db", required=True, help="SQLite 账本库路径")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8081)
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    httpd = serve(args.db, args.host, args.port)
    logging.getLogger("ledger").info(
        "持久账本监听 http://%s:%s （库 %s）", args.host, args.port, args.db)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.shutdown()
        httpd.ledger_store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
