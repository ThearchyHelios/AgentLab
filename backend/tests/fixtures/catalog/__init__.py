"""数据目录用的合成业务库夹具。

- scenic.py：一个虚构的「景区 + 园内门店」业务库（SQLite，50 张表 + 1 个视图），给数据目录的起草、关系图、
  表清单测试用，阶段 1B（助手按任务挑表）也用它。表名、数据全部虚构，不对应任何真实部署。

用法::

    from tests.fixtures.catalog import scenic

    path = scenic.build(tmp_path / "scenic.db")    # 写一个新库，返回路径；同样的参数造出同样的内容
    scenic.FK_RELATIONS        # 库里声明了外键约束的关系
    scenic.NAME_RELATIONS      # 没有外键约束、只能靠列名推断出来的关系
    scenic.NOT_INFERRED        # 看起来像关联、但命名推断必须放过的列（及原因）

conftest.py 里有个 scenic_db 夹具（每个测试进程建一次，只读使用），直接要这个参数就行。
"""
