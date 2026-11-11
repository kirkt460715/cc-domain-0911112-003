# 本地协议服务

从 /sitemap.xml 读取站点索引。正文容器为 main，分页由 a[rel=next] 指向；样本提供条件响应、缺正文、分页环、GB18030 和越界跳转。

场景是响应脚本，不包含采集客户端。可以复制场景文件并改变响应次序、状态码或样本内容来联调。服务只监听 loopback，数据均为合成。

    python3 -m synthetic_exchange.server --port 8765
    python3 -m synthetic_exchange.smoke
