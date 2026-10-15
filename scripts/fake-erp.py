"""起一个假的外部系统（付款），供浏览器验收「审批流程应用 + 写入外部系统」（设计文档 §7.2）。

    ~/miniforge3/envs/agentlab/bin/python scripts/fake-erp.py [--port 8765] [--mode ok]

接口（路径以环境开头，test 和 prod 各记各的账）：
  POST /{test|prod}/payments                 认 Idempotency-Key：同一个键只记一笔
  GET  /{test|prod}/payments/lookup?idempotency_key=…   查到 200，查不到 404
  POST /_fake/mode {"mode": "ok" | "timeout_after_write" | "error_500" | "error_4xx"}   切换注入的情况
  GET  /_fake/state                          收到的写请求、查询、记下的付款
  POST /_fake/reset                          清空

在 AgentLab 里登记成一个端点：test 环境 base_url = http://127.0.0.1:端口/test，prod 环境 = …/prod，开内网访问
（只有管理员能开）。付款能力绑定这个端点、POST /payments，治理成写入、查询能力填下面这个；查询能力 GET /payments/lookup，
参数 idempotency_key。实现在 backend/tests/fakes/fake_erp.py（测试里不起服务，直接用它的替身）。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent / "backend"


def main() -> None:
    parser = argparse.ArgumentParser(description="假的外部系统（付款），只供验收")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--mode", default="ok", choices=("ok", "timeout_after_write", "error_500", "error_4xx"))
    parser.add_argument("--hang", type=float, default=30.0, help="timeout_after_write 时挂多久（秒）")
    args = parser.parse_args()

    sys.path.insert(0, str(ROOT))
    import uvicorn

    from tests.fakes.fake_erp import FakeErp, app

    erp = FakeErp(mode=args.mode, hang_s=args.hang)
    print(f"假外部系统：http://{args.host}:{args.port}/{{test|prod}}/payments，注入：{args.mode}", flush=True)
    uvicorn.run(app(erp), host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
