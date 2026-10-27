# Qoder 开发与发布指引（队内操作单，提交前删除本行提示按需保留）

> 本文件是给队员的操作单：说明哪些环节必须在 Qoder 产品里完成、要留下什么记录。Qoder 赛道要求"主要开发工作在 Qoder 中完成 + 开发记录可核对 + 网站用 Qoder Sites 发布"，这三件事只能在 Qoder 里做，本仓库资产是它的开发基底与证据载体。

## 1. 在 Qoder 中打开本项目

1. Qoder 桌面端 → 打开文件夹 → 选本仓库根（或 `qoder/` 目录）。
2. 让 Qoder 索引完成后，先让它读 `README.md` 与 `docs/DESIGN.md` 建立上下文（Quest/Chat 里直接说"先阅读 README 和 DESIGN 熟悉项目"）。

## 2. 用 Qoder 做真实的开发迭代（开发记录的核心素材）

建议按顺序做以下任务（每个任务在 Qoder 里独立一个 Quest 会话，完成后截图）：

| # | 任务（直接粘给 Qoder） | 产出与验证 |
|---|---|---|
| 1 | 「阅读 app/seekmem/retrieval.py 与 static/app.js，给问答页增加'检索耗时'显示：把 retrieval.elapsed_ms 显示在来源列表上方」 | 刷新页面提问可见耗时 |
| 2 | 「给资料库页增加导出：在文档列表加一列'导出'按钮，点击下载该文档的分块 JSON（后端加 GET /api/documents/{id}/chunks）」 | 浏览器下载 JSON，内容含 embedding 可选排除 |
| 3 | 「review 整个 app 目录的代码，指出至少 3 个可靠性或安全问题并修复（如 SQL 注入面、超时、错误处理）」 | Qoder 的 review 记录 + 修复 diff |
| 4 | 「给 site/index.html 的'常见问题'区块补充 3 条 FAQ，内容基于 docs/RUNBOOK.md 的常见问题节」 | 站点更新 |

每个任务完成后的**对话截图**存入 `evidence/qoder_records/`（命名 `task1-quest.png` 等）。这四件事都产生了真实代码变更，Qoder 在其中的作用（理解代码库、生成改动、自查修复）是可核对、可演示的。

## 3. 用 Qoder Sites 发布项目网站

1. Qoder → Sites → 新建站点 → 选择本仓库 `site/` 目录作为站点根。
2. 让 Qoder 预览确认无样式错乱后，点「发布」。
3. **可见范围必须设为「任何人都可访问」**（企业账号无法设置公开，需用个人账号）。
4. 复制分配的 `*.qoder.site` 域名，填入提交材料"项目网站网址"，并回填到 `site/index.html` 顶部横幅的占位处（改完需再次点「发布」更新线上版本）。
5. 用浏览器无痕窗口打开验证未登录可访问，截图存 `evidence/qoder_records/site-anonymous.png`。

## 4. Qoder 账号 UID

Qoder 个人中心复制账号 UID，写入提交表单，并记录在 `evidence/qoder_records/uid.txt`（一行 UID 即可）。

## 5. 提交材料对照表

| 提交项 | 位置 |
|---|---|
| 项目网站网址 | Qoder Sites 域名（§3） |
| 代码仓库 | 战队 powercontext 仓库 crq-ecosystem 分支（qoder/ 目录）+ commit SHA |
| 运行说明 | `docs/RUNBOOK.md` |
| 验证样例 | `docs/VERIFY.md` + `evidence/`（7/7 通过留档） |
| Qoder 开发记录 | `evidence/qoder_records/`（对话截图 + UID + 站点无痕访问截图） |
| 使用效果材料 | 持续补充：站点「访问统计」截图 + 真实用户反馈（隐去个人信息） |
