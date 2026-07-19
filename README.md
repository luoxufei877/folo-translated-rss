# Folo 免费中文 RSS 桥接器

这个项目解决两个问题：

1. Folo 无法稳定抓取 Google News 搜索 RSS；流水线先抓取，再生成 Folo 可读取的静态 RSS。
2. Folo 免费版不提供所需的批量翻译；流水线使用用户自己的 OpenAI 兼容低价 API 翻译标题和简介。

最终只需在 Folo 中订阅四个聚合源：经济、科技、国际、国内。标题和简介为中文，文章链接保持指向原始来源。

## 本地测试

运行 `run-local.ps1`。翻译缓存写入 `.cache/translations.json`，生成结果位于 `public/feeds/`。

## 免费托管

推荐使用公开 GitHub 仓库和 GitHub Pages：

- Actions 每天北京时间 11:30 和 19:30 运行，电脑无需开机。
- 在仓库 Actions secrets 中设置 `AI_API_KEY`、`AI_API_BASE`、`AI_MODEL`。
- Pages 的 Source 设为 **GitHub Actions**。
- 成功运行后，从 Pages 首页下载 `folo-translated.opml` 并导入 Folo。

API 密钥不得写入代码、OPML、RSS或仓库文件。
