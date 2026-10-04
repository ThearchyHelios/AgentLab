"""可验证报告：GET /api/runs/{run_id}/bundle.html，把一次已封存运行的报告导出成一个自包含的 HTML 文件。

拿到文件的人不连服务、不装任何东西，在浏览器里就能核对每个数字的出处、运行记录和工件有没有被改过
（页面怎么核见 bundle/verifier.js，数据怎么拼见 bundle/build.py）。

只给已封存、而且封存核对一致的运行：封存判定照搬证据接口（api/evidence._sealed），不另写一套。没封存的
运行事件还会变，导出去的东西马上就过时；封存核对不一致的，导出去的页面只会把一条断掉的链画成核对通过。
另外要求运行已经结束（成功、失败、取消）：失败后接着跑的运行留着上一次的封存，跑完会重新封存、换掉清单哈希，
早年的运行停在审批时也封存过——这时导出去的文件，封存哈希和系统里之后记的对不上，页面上「去系统里对照」
那一步就作废了。
只读：不追加任何事件，不改运行记录。路径挂在 /api/runs/{run_id}/… 下面，runs.py 里已经有 /{run_id}/verify，
这里叫 bundle.html，不撞名。
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from fastapi import APIRouter
from fastapi.responses import Response
from sqlalchemy import select

from app.api.coded import CodedHTTPException
from app.api.evidence import RUN_NOT_FOUND, _mask_fingerprint, _sealed
from app.bundle.build import attachment_name, build_bundle, content_disposition, render_html
from app.core.artifact_store import read_bytes
from app.db.base import SessionLocal
from app.db.models import Run, RunEvent

router = APIRouter(prefix="/api/runs", tags=["bundle"])

BUNDLE_UNSEALED = "bundle_unsealed"
BUNDLE_SEAL_BROKEN = "bundle_seal_broken"
#: 运行结束的状态：封存是最后一次的，之后不会再换（除非失败的运行又被接着跑，那是另一次封存）
_FINISHED = frozenset({"succeeded", "failed", "cancelled"})
UNSEALED = "运行尚未结束封存，结束后才能导出"
SEAL_BROKEN = "封存核对不一致：{why}。不能导出可验证报告，请先在记录页核对这次运行"


@router.get("/{run_id}/bundle.html")
async def export_bundle(run_id: str) -> Response:
    """可验证报告的 HTML 文件，作为附件下载（附件名「<运行名>-可验证报告.html」）。"""
    sealed = await _sealed(run_id)
    if not sealed.sealed or sealed.status not in _FINISHED:
        raise CodedHTTPException(409, UNSEALED, BUNDLE_UNSEALED)
    if not sealed.trusted:
        why = str(sealed.verdict.get("message") or "封存后有事件被修改、删除或插入").rstrip("。")
        raise CodedHTTPException(409, SEAL_BROKEN.format(why=why), BUNDLE_SEAL_BROKEN)
    bound = sealed.seal["manifest_seq"]
    async with SessionLocal() as session:
        run = await session.get(Run, run_id)
        if run is None:          # 两次读之间被删掉了
            raise CodedHTTPException(404, "运行记录不存在", RUN_NOT_FOUND)
        info = {"id": run.id, "workflow_id": run.workflow_id, "workflow_name": run.workflow_name,
                "version": run.version, "version_hash": run.version_hash, "run_class": run.run_class,
                "status": run.status, "started_at": run.started_at, "finished_at": run.finished_at}
        # 封存之后追加的事件（按需裁判、审批质询）：_sealed 只留了封存范围内的，这几条另取，单列在页面上
        later = [tuple(r) for r in await session.execute(
            select(RunEvent.seq, RunEvent.type, RunEvent.node_id, RunEvent.data)
            .where(RunEvent.run_id == run_id, RunEvent.seq > bound).order_by(RunEvent.seq))]
    # 数据源现在设的遮罩：事后加的遮罩同样生效，和证据面板一个道理
    masks = await _mask_fingerprint()
    exported_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    # 读工件、算哈希、base64 是同步的活，快照大的时候不该占着事件循环
    page = await asyncio.to_thread(lambda: render_html(build_bundle(
        info, [*sealed.events, *later], manifest_seq=bound, manifest_hash=str(sealed.manifest_hash or ""),
        legacy=bool(sealed.seal.get("legacy")), graph=sealed.graph, source_masks=masks,
        read=lambda artifact: read_bytes(artifact, verify=False), exported_at=exported_at)))
    return Response(page, media_type="text/html; charset=utf-8", headers={
        "Content-Disposition": content_disposition(attachment_name(info), run_id=run_id),
        # 导出的是这一刻的封存件，不该被哪一层缓存下来、换给下一个请求
        "Cache-Control": "no-store",
    })
