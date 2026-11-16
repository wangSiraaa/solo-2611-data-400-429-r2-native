# 范围删除与局部压缩起点
Python 3.11，无外部依赖。运行 `python3.11 -B demo.py`，数据库仅在临时目录生成。
run_store.py保存不可变run及当前manifest；reader.py负责读取可见键；compactor.py负责选定run的替换和旧信息回收。main.py是现有CLI。
seq在一个库内唯一，代表逻辑操作的先后；run边界与序号顺序无关。JSON是本地实验段容器，不是要求实现某个数据库二进制格式。
本起点的同run删除有效，但跨run读取及局部压缩有观察得到的错误。fixtures.json提供重叠区间和大跨度输入原件。
