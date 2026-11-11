# 领域数据约定

schema每节点有name、kind及nullable布尔值；struct含fields，list含element（节点本身可空），叶kind为int64/boolean/string。字段名在兄弟范围唯一且不含点，列表element.name固定item。缺失可空字段规范成null，缺失必选字段报错；struct:{}仅在所有缺失子项可空时合法且规范为对应null子项。列表元素中{}和null分别保留对象存在性。read_at(offset,length)必须严格返回指定范围，调用记录用于观察投影I/O。基础uvarint为无符号64位LEB128且只接受最短编码；zigzag为有符号64位映射。嵌套列式文件格式NCF由workbench/ncf.py实现，格式版本、叶编码版本、列目录与页范围均自描述于文件页脚，正式规范见docs/ncf-format.md。

全部资料为本地合成。samples保留来源状态，读取和存档不等于业务验证通过；异常记录不被自动修改。workbench/domain.py中的现有函数可独立调用，tests/domain_checks.py演示其行为。
