"""报表装载方：把一个文件中的多行变化作为一个业务提交发布。

整批通过 Engine.apply 原子提交——要么全部变化同属一个 commit，要么整批失败、
引擎状态（行堆、二级索引、commit、已取得快照）保持原状，不再逐行发布半成品。
"""


def apply(engine, operations):
    return engine.apply(operations)
