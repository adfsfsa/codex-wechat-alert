# Codex 微信额度预警

独立的 Python 监测程序，通过 GitHub Actions 每小时读取官方公开公告，再用 Server酱 Turbo 推送到微信。无需 OpenAI API Key，不调用模型。Python 3.11 及以上，仅使用标准库。

## 当前状态

程序与定时工作流已经实现，已在本机验证三个官方来源可读取及解析。目标仓库为 <https://github.com/adfsfsa/codex-wechat-alert>，云端运行结果请查看 Actions。微信实际送达仍需配置个人 Server酱 Secret 后测试。

## 上线步骤

### 1. 准备 GitHub 仓库

建议为这个工具创建一个专用公开仓库。公开仓库的标准 GitHub 托管运行器目前免费；代码公开，SendKey 保存在 Secret 中。

将本目录里的文件上传到仓库根目录，尤其要包含 `.github/workflows/monitor.yml` 和 `tests/test_monitor.py`。不要把整个目录嵌套在仓库下，否则工作流不会识别。可以把仓库链接提供给本次聊天，继续协助上传。

如果使用 Git，进入解压后的本目录再执行（把地址换成自己的空仓库；无需强制推送）：

```powershell
git init -b main
git add .github .gitignore monitor.py config.json tests README.md
git -c user.name="Monitor Setup" -c user.email="monitor-setup@users.noreply.github.com" commit -m "Add Codex WeChat announcement monitor"
git remote add origin https://github.com/你的账号/你的仓库.git
git push -u origin main
```

本机如果使用 GitHub CLI 登录，先执行 `gh auth login --hostname github.com --git-protocol https --web --scopes workflow`，再执行 `gh auth setup-git`。登录由你在浏览器完成，无需把密码或 Token 发到聊天里。

### 2. 绑定 Server酱微信通知

打开 <https://sct.ftqq.com/sendkey/>，用微信登录并按控制台提示完成消息通道绑定。使用 Turbo 产品、以 `SCT` 开头的 SendKey。本版不接受 Server酱³ 的 `SC3` 密钥。

在 GitHub 仓库进入：

**Settings → Secrets and variables → Actions → Secrets → New repository secret**

- Name：`SERVERCHAN_SENDKEY`
- Secret：粘贴你的完整 SendKey。

密钥只填在 GitHub Secrets 中，不要放进代码、截图或聊天。

### 3. 先验证，再开启定时监测

1. 在仓库 **Actions → Codex WeChat Alert → Run workflow** 中选择 `dry-run`。这会读取三个源并展示运行摘要，不发微信，也不保存监测状态。确认三个来源均为 `ok`。
2. 再运行 `test-push`，确认手机微信实际收到“Codex 微信预警测试”。接口返回成功只代表服务端受理，仍需要你确认手机通知。
3. 再运行一次 `check`，建立历史消息基线。第一次成功读取每个来源时不推送已有公告，防止旧消息刷屏。
4. 在 **Settings → Secrets and variables → Actions → Variables** 添加 Repository variable：`MONITOR_ENABLED`，值为 `true`（小写）。每小时第 17 分钟计划检查一次；GitHub 可能延迟执行。

默认不会自动开启定时推送，避免密钥尚未配置就连续失败。每次正式运行会提交 `data/state.json`，保存去重记录、失败次数和推送次数，同时产生实际仓库活动。需要默认分支允许工作流机器人提交；如果分支保护阻止提交，请使用专用仓库，不要对现有项目放宽保护。

推送程序、配置、测试或工作流文件到 main 分支时，会自动执行测试和联网干运行，无需 SendKey；状态保存不会触发这个检查，避免循环运行。

### 4. 停止与排查

- 暂停：把 `MONITOR_ENABLED` 改为 `false`，或在 Actions 页面停用工作流。
- 状态：打开 Actions 最近一次运行，检查三个源是否成功，是否有排队通知，以及最后的状态保存步骤是否成功。
- 微信没收到：查看测试运行结果、Server酱推送日志、免费额度、接收通道绑定和手机通知设置。
- 状态保存失败：尽快修复仓库写入权限。服务端发送与 Git 提交无法做到原子操作，提交失败或发送响应丢失时，后续可能重复提醒。
- 如果调度本身停止，程序无法给自己报错；异常提醒仅覆盖“程序仍在运行、但来源连续读取失败”的情况。可开启 GitHub 工作流失败邮件通知作为补充。

## 监测行为

- 官方新闻 RSS：<https://openai.com/news/rss.xml>
- ChatGPT 与 Codex 更新日志：<https://learn.chatgpt.com/docs/changelog>
- 官方状态公告 RSS：<https://status.openai.com/history.rss>

只检测上述页面/RSS 实际提供的文字；没有接入 X、员工个人社交账号或登录后的内容，也不调用搜索或模型 API。RSS 中未收录的正文内容可能漏检。

要求同一条消息出现 Codex，并在相近文字中出现“额度/usage limits”等及“reset/重置”等词语。排除常规每周/自动恢复说明、密码重置、否定重置的典型表达。规则不是自然语言理解，仍可能误报或漏报。所有提醒都要求核对原文，只有明确词语表明全局范围时才标注“明确提及全局范围（规则判断）”，不保证用户套餐属于公告覆盖范围。

正常情况下，仅检查新出现或正文发生变化的条目；原文发布日期须在最近 7 天内。每个来源首次成功读取只建立基线；其他来源失败后恢复也单独建立基线。对较早公告的后续修改可能因 7 天窗口被忽略。

去重依据为原文链接、标题和正文的 SHA-256。相同条目不重复发送；正文变化或不同官方链接可能再次提醒。推送失败保留队列，下次检查重试；队列超过 7 天丢弃。官方新闻摘要变化也可能触发复核提醒。

每天按北京时间限制本程序最多 4 次推送请求，**包含测试和失败尝试**；为免费版每日 5 条留出余量。若你用同一 SendKey 给其他程序推送，这个本地计数无法覆盖那些请求。超出本程序上限的新通知排队到次日。来源连续失败 3 次后尝试发送异常提醒，最多每日一次；同样受每日总上限约束。

## 本地运行

```powershell
python -m unittest discover -s tests -v
python monitor.py --dry-run
```

正式运行和微信测试需要环境变量 `SERVERCHAN_SENDKEY`。尽量直接在 GitHub Secrets 中配置，避免本地命令历史记录密钥。

`--dry-run` 不写状态、不发通知。没有状态时它只报告基线信息。不能用干运行结果推断目前不存在重置公告。

## 成本和服务边界

截至 2026-10-08，Server酱 Turbo 文档列出免费版每日最多 5 条，免费消息保留 1 天；卡片显示与正文体验取决于接收通道。每小时检查不会占用推送额度，只在发送请求时占用。实际套餐以服务商控制台为准。

GitHub 公开仓库连续 60 天没有活动可能停用定时任务；本程序成功持久化状态会产生提交，但无法防止权限问题、账号限制、平台故障或工作流停用。GitHub 不保证整点或实时执行。此工具适合辅助提醒，不能保证提前预警或零漏报。

官方依据：

- <https://sct.ftqq.com/docs/getting-started/faq/>
- <https://sct.ftqq.com/docs/getting-started/channels/>
- <https://docs.github.com/en/actions/concepts/billing-and-usage>
- <https://docs.github.com/en/actions/how-tos/troubleshoot-workflows>
- <https://docs.github.com/en/actions/how-tos/manage-workflow-runs/disable-and-enable-workflows>
