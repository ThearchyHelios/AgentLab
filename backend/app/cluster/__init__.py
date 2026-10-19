"""多进程协调（docs/design/orchestration-round/00-总方案.md §5 W2-b）：应用库是 PostgreSQL 时可以起几个工作进程（uvicorn
--workers N，或者几个容器指着同一个库、同一个数据目录），SQLite 只支持单进程。

- identity：这个进程是谁（OWNER）；workers：登记、租约、谁还活着；state：手上的那一份「谁还活着」
- leader：调度当班（全站只许一个进程做的周期活）；service：心跳、起停
- requests：发给别的进程的请求（停止、往运行里记事件）；signal：PostgreSQL 的 LISTEN / NOTIFY 叫醒
- slots：运行的并发名额（四类：交互、应用、定时、系统，各有全站上限和优先级）
- reaper：接手属主不在了的行；mutex：跨进程的互斥锁（PostgreSQL 的咨询锁）；guard：SQLite 的单进程守卫
- counters：按时间窗的计数（访问密钥限流、登录失败），PostgreSQL 记在库里、SQLite 在进程里
"""
from app.cluster.identity import owner
from app.cluster.service import leading, on_beat, on_leadership, start, started, stop

__all__ = ["leading", "on_beat", "on_leadership", "owner", "start", "started", "stop"]
