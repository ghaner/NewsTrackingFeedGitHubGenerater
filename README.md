# 海外短剧九席 · 事件追踪 RSS 订阅源

把「海外短剧付费应用前十榜单里，中国厂商拿下 9 席」这条新闻做成**可 URL 订阅的 RSS 2.0 源**：GitHub Actions 每日增量抓取后续报道 → 并入事件时间轴 → 重新生成 `duanju.xml` → GitHub Pages 提供公网订阅地址。

方法论来自 `feed-github-generater`（配置驱动、零依赖生成器、失败不覆盖旧 feed、Pages 出公网 URL）＋ `news-timeline-builder`（去重／时间对齐／冲突合并的确定性规则）。

## 目录

```
2026-11-14_短剧事件RSS订阅/
├── .github/workflows/generate.yml   定时抓取 → 合并 → 生成 → 提交
├── src/
│   ├── generate_feed.py             生成器（在原技能版基础上扩展：file 模式 + event_json 解析 + pubDate/guid）
│   ├── sources.json                 ★ 源配置（改这里就能改标题／条数／描述模板）
│   ├── rebuild.py                   增量合并 + 时间轴重算（不联网、不调模型）
│   ├── fetch_articles.py            抓取器（Google News RSS，来自 news-timeline-builder）
│   ├── schema.py / dedupe.py / align.py / merge.py   确定性规则（原样复用）
│   └── store.py                     参考用（本仓库由 rebuild.py 直接写 event.json）
├── data/                            事件数据（articles.jsonl + event.json + analysis.json + meta.json）
├── duanju.xml                       ← 订阅文件（每次运行重新生成）
├── index.html                       Pages 首页，自动显示并复制订阅地址
├── preview.bat                      本机生成并打开首页
├── publish.bat                      本机生成 + 提交 + 推送
├── .nojekyll                        让 Pages 原样发布 XML
└── README.md
```

## 发布到 GitHub Pages

目标地址已按 `ghaner/duanju-feed` 写死在 `src/sources.json` 的 `home_url` / `feed_url`：

```
https://ghaner.github.io/duanju-feed/duanju.xml     ← 订阅地址
https://ghaner.github.io/duanju-feed/               ← 首页（自动显示并可复制订阅地址）
```

**唯一需要手工做的一步：在 GitHub 上建一个空仓库**（Web 端 New repository，名字 `duanju-feed`，public，不要勾任何初始化文件）。WorkBuddy 的 GitHub 连接器是 App 身份、没有建库权限，这一步代替不了。

建好之后：

```bash
cd "F:/Informatics/新闻分析/2026-11-14_短剧事件RSS订阅"
git remote add origin https://github.com/ghaner/duanju-feed.git
git push -u origin main
```

推送完成后，工作流会自动接管：

1. `Update Event Feed` 每日 UTC 23:30（北京 07:30）跑一次，也可以 **Actions → Update Event Feed → Run workflow** 立刻手动跑一次。
2. 其中的 `deploy` 任务用 `actions/configure-pages` 的 `enablement: true` **自动创建 Pages 站点**，所以不需要去 Settings 里手点。
3. 若 `deploy` 因为组织策略报 “Get Pages site failed”，到 **Settings → Pages → Source** 选 **GitHub Actions**，再重跑一次即可（**不要**选 `Deploy from a branch`，本仓库的发布走 artifact，两者不能混用）。
4. 若 `update` 步骤报 403 无法推送：**Settings → Actions → General → Workflow permissions** 选 `Read and write permissions`，保存后重跑。

顺带说明：`deploy` 只把 `index.html`、`duanju.xml`、`.nojekyll` 三个文件打包上传（见 workflow 的 `Stage site` 步骤），`.git`、`src/`、`data/` 不会公开。

## 订阅

把上面的 `.xml` 地址粘进 Feedly / Inoreader / Fluent Reader / NetNewsWire 即可。条目结构：

- 标题：`[日期] 进展标题`
- 正文：进展摘要 + 关键数据（同名事实多值时用「/」并列，并标【存在分歧】）+ 置信度 + 是否冲突节点 + 来源链接
- `pubDate`：该节点的最早可信发稿时间（RFC822），阅读器按此排序
- `guid`：来源链接 + 节点 ID，同一来源多节点也不会重复

## 更新机制与韧性

| 情况 | 行为 |
| --- | --- |
| 抓到新报道 | 三重去重（URL／标题／正文相似度）→ 时间对齐 → 重算节点 → 追加版本快照 → 重新生成 XML |
| 无新增 | `rebuild.py` 打印 `NO_NEW`，不改 `event.json`，feed 保持上一版 |
| Google News 不可达 | `fetch_articles.py` 吞掉异常输出空文件，流程不中断 |
| 生成 0 条 | `generate_feed.py` 跳过该源，不覆盖旧 XML；工作流的校验步骤会拒绝提交空 feed |

## 本地使用

- 双击 `preview.bat`：重新生成 `duanju.xml` 并打开 `index.html`。
- 双击 `publish.bat`：生成 + 提交 + 推送（需已配置远端与凭据）。
- 只改展示：编辑 `src/sources.json` 里的 `feed_title` / `feed_desc` / `template` / `limit`，重跑即可，无需改代码。

## 已知限制（务必知道）

1. **CI 与本地是两套口径**：Actions 里没有大模型，抓到的每条报道独立成一个节点（`event_label`＝标题），精度相当于「占位监测」；本地 `news-timeline-builder` 产出的 16 节点时间轴含人工归类与冲突合并，精度更高。二者不自动同步——本仓库的 `data/` 是发布侧的独立副本。
2. **Google News RSS 的可用性不由我们控制**：GitHub Actions 的海外出口通常可访问；本地首次测试若走代理可能失败（会被安全跳过）。
3. **榜单数字均为媒体引述**：DataEye / Sensor Tower 的原始报告未公开口径，「付费前十」是否含广告收入仍不明确，feed 中保留原文来源链接供核对。
4. 中文标题里的方括号与斜杠不影响 RSS 良构性（已用 CDATA + XML 转义处理）。

## 手工重建全部产物

```bash
cd "F:/Informatics/新闻分析/2026-11-14_短剧事件RSS订阅"
python src/rebuild.py        # 有 data/incoming.jsonl 时才会重算
python src/generate_feed.py  # 重新生成 duanju.xml
```
