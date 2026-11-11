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

从 /sitemap.xml 读取站点索引（支持 sitemap index 递归）。正文容器为 main，分页由 a[rel=next] 指向；样本提供条件响应、缺正文、分页环、GB18030 和越界跳转。

    python3 -m synthetic_exchange.server --port 8765

采集客户端把 sitemap 发现、分页跟随与正文提取接入既有授权、去重和审查流程，全程只在标准库内实现：

    python3 -m corpus_gate --db gate.sqlite3 crawl <job_id> --source <source_id> \
        --sitemap http://127.0.0.1:8765/sitemap.xml --context context.json
    python3 -m corpus_gate --db gate.sqlite3 crawl-job <job_id>
    python3 -m corpus_gate --db gate.sqlite3 crawl-article --source <source_id> --url 'http://127.0.0.1:8765/article/a?utm_source=manual'
    python3 -m corpus_gate --db gate.sqlite3 recrawl --source <source_id> --url <url> --context context.json
    python3 -m corpus_gate --db gate.sqlite3 reextract --source <source_id>

采集保证：

- 仅提取 main 容器内可见内容，按文档顺序保留段落、列表、表格；导航、页脚、脚本与隐藏内容（hidden 或 display:none/visibility:hidden）不入库。
- 同文不同导航/页脚不产生新版本；正文内容哈希变化才生成新版本，并在 crawl_evidence 留存每页原始抓取证据（ETag、Last-Modified、编码、页哈希、分页序号）。
- 带跟踪参数（utm_* 等）的地址按 canonical 归并；所有请求与重定向逐跳校验 scheme/主机/端口，绝不跨越授权站点来源。
- ETag/Last-Modified 未变化时复用已存原始页（304 不重写正文）；解析配置（crawl-parser）升级后可用 reextract 基于原始页重提取。
- 授权在抓取途中撤回时，尚未入库内容被挡住，作业标记为 blocked 并记录 crawl_unauthorized 原因。
- 分页环、缺正文、编码混杂、越界重定向等异常只在 crawl_failures 留下原因，绝不写入空文章。

原有操作参数可通过 python3 -m corpus_gate --help 查询。
