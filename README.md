# 可穿戴离线资料的嵌套列式编码与投影读取资料库

嵌套合成记录、字节随机读取接口和基础整数校验编码库。 资料在samples，数据语义见docs/domain.md，本地存档说明见docs/catalog.md，嵌套列式文件（NCF）的正式格式说明见docs/ncf-format.md。

## 运行

需要Python 3.11以上，仅使用标准库。

```bash
python -m workbench.cli inspect samples
python -m workbench.cli ingest samples state/raw.sqlite3
python -m workbench.cli catalog state/raw.sqlite3
python -m workbench.cli encode samples/schemas.json samples/records.json output/sleep.ncf --page-row-limit 1000
python -m workbench.cli validate-ncf output/sleep.ncf
python -m workbench.cli project output/sleep.ncf nights.item.segments.item.item
python -m workbench.cli decode output/sleep.ncf --columns id --row-start 0 --row-end 3
python -m unittest discover -s tests -v
python -m compileall -q workbench tests
```

inspect查看文件行数和摘要；ingest将原始包放入SQLite；catalog列出原始包。encode把JSON行按schema编码为NCF嵌套列式文件，validate-ncf全量校验所有页与块，project按叶列点路径投影读取，decode支持行范围。测试覆盖已有资料读取、存档幂等、领域原语及NCF往返/空值区分/投影I/O/损坏定位，compileall检查源码编译。业务计算不会由存档命令自动执行。

## 容器

```bash
docker build -t local-workbench .
docker run --rm --network none local-workbench
docker run --rm --network none local-workbench python -m unittest discover -s tests -v
```

程序不需要外部账号或常驻服务，容器默认查看合成资料。state与output为本地工作目录，不进入版本控制。workbench/io.py负责JSON与文件边界，catalog.py负责原始资料事务，domain.py只提供上述已有能力。
