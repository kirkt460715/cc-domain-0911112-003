# corpus_gate 数据接入工作台

保留现有领域校验、持久化与操作入口。Python 3.11 标准库和 SQLite；运行时无需下载依赖。测试覆盖已有领域行为、错误路径和恢复。本地联调材料不会自动采集到业务库。

## 本地运行

    python3 -m compileall -q corpus_gate
    python3 -m unittest discover -s tests -v
    python3 -m corpus_gate --db :memory: demo --fixture fixtures/demo.json
    python3 -m synthetic_exchange.smoke

## 容器运行

    docker build -t local/corpus_gate .
    docker run --rm --network none local/corpus_gate

构建时准备基础镜像，正式运行不下载。run_checks.sh 是完整的测试、编译检查及实际产品演示入口，正常在 90 秒内结束。每次演示使用临时数据或内存库，不改写源样本。

## 采集联调

从 /sitemap.xml 读取站点索引。正文容器为 main，分页由 a[rel=next] 指向；样本提供条件响应、缺正文、分页环、GB18030 和越界跳转。

    python3 -m synthetic_exchange.server --port 8765

原有操作参数可通过 python3 -m corpus_gate --help 查询。
