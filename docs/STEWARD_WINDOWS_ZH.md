# 飞书大管家 · Windows 本地开发版

双击桌面 **Dudu** 打开工作台，也可以运行项目根目录的 `start-steward.cmd`。启动器会拉起本机网页和已配置的飞书网关。电脑重启后，等你再次双击图标才开始接收任务。

关闭从桌面图标打开的管理窗口时，选择 **仅关闭网页** 可让飞书和员工继续工作，选择 **同时停止服务** 会中断正在执行的工作，本机工作台和飞书机器人也会下线。也可以在本机管理窗口点「更多 → 退出嘟嘟」调出相同选择。普通浏览器标签页、手机或其他电脑上的网页关闭时，家中服务继续运行。

在家中电脑上也可以用浏览器打开 [本机工作台](http://127.0.0.1:8000/)。首次使用这个浏览器时，输入工作台的老板密码。更换手机或电脑后，使用下文的 Tailscale 私网地址。

工作区的「对话」「员工」「项目」在电脑上位于左侧，在手机上位于顶部。改岗位、头像、默认模型或工作目录，点「员工」并选中对应的人。从聊天切过去，再点「对话」，可以接着写刚才的草稿。运行记录、家庭主机、任务板和设置在「更多」里。

## 管家会主动做什么

服务运行期间，管家每两小时检查一次卡住、逾期和待验收的任务。上次检查时间保存在本机。停用超过两小时，下次手动启动后补查一次。选「整理资料」会生成简报，选「跟进」会在原任务下派工，选「稍后」可以回后台再看。原任务状态变化后，旧建议会过期。

飞书网关的私有配置可启用 `proactive_suggestions`，通过管理权限配对的设备把选项发到老板和管家的私聊。回复必须对应到具体建议。日常任务继续使用原任务编号及审批流程。

## 从自己的手机或电脑打开后台

1. 在家中主机和远端设备登录同一 Tailscale 私网，先在家中浏览器设置后台所有者密码。
2. 在项目目录运行 `powershell -ExecutionPolicy RemoteSigned -File scripts/windows/enable-private-access.ps1`。它会把私网 HTTPS 地址指向本机服务，检查现有映射和公开分享状态，并把私网主机名保存到本机配置。
3. 在没有任务执行时重新启动 Web 组件，在已加入私网的设备打开脚本返回的地址，用后台原账号登录。出门前在家中电脑双击 Dudu，关闭管理窗口时选「仅关闭网页」，就能继续在外通过飞书派工。

Tailscale Serve 只给已获准访问的私网设备提供入口。[Tailscale Serve 说明](https://tailscale.com/kb/1312/serve)

## 从飞书转到 Codex 继续工作

对于用 GPT 的 Codex 员工，在后台打开对应任务的对话，点顶部的终端图标「续接会话」。页面会给出工作目录和会话 ID。结束正在执行的那一轮后，在该目录打开终端，运行页面上的 `codex resume` 命令。切回飞书前，先退出占用该会话的 Codex 客户端。后台可以列出桌面新增的原生轮次，飞书保留它自己收到的消息和交付。使用 DeepSeek 的管家有独立的原生会话目录，可回到原后台对话继续使用团队工具和审批。

## 管家用 DeepSeek，员工用 GPT

1. 用老板账号打开「员工」，选中管家，在「基本资料」里保留 Codex 执行引擎。默认模型可以选 `deepseek-flash` 或 `deepseek-v4-pro`。需要在飞书里看图时选 Flash。[DeepSeek 模型说明](https://api-docs.deepseek.com/quick_start/pricing/)
2. 展开「管理 DeepSeek 密钥」，输入 DeepSeek API Key，点「验证并保存密钥」，再保存员工资料。已配置的密钥可直接使用。后台向 DeepSeek 官方核对模型列表，Windows 用当前登录账户加密保存密钥。员工继续使用各自岗位设置里的模型。管家和员工使用同一套派工、验收和汇报工具。[DeepSeek 的 Codex 接入说明](https://api-docs.deepseek.com/quick_start/agent_integrations/codex/)
3. 在原来的飞书私聊继续找管家。第一次切换供应商会建立一个对应模型的管理会话，以后复用。项目和工单归属保留，旧对话仍可在后台查看。指定联系人私聊摘录遵循单独的个人授权，已勾选的 OpenAI 摘要仍交给 GPT 处理。

## Windows 上的 Codex 响应速度

如果 Windows 已经配了本机代理，可以在私有数据目录的 `codex-network.json` 写入 `{"use_windows_proxy": true}`，再启动 Web 组件。程序只识别 Windows 为两个模型地址指定的同一本机 HTTP 代理。它在启动独立 Codex 进程时设置 HTTPS 代理，所以这个进程发起的其他 HTTPS 请求也可能经过代理。回环地址直接连接，已有的代理环境变量优先。飞书和其他引擎沿用各自的网络设置。把值改成 `false` 并重启 Web 即可关闭。速度取决于本机网络与模型当时的响应。

## 初次使用

1. 本次交付的工作目录已生成网页并装好后端环境，保留其所在路径即可启动。如果把代码移到另一台机器，先安装 Python 3.14、uv、Node 22+ 和 npm，在 backend 下执行 `uv sync --locked`，在 frontend 下执行 `npm ci`，然后从仓库根目录执行 `node scripts/build-ui.js`。使用 Codex 的 Windows 账户需要先完成 `codex login`。
2. 在管理页创建一个管家和至少一个专业角色，分别选 Codex 和实际可用的模型，在团队设置中指定负责人。给每个角色选择独立目录，在目录根部编辑 AGENTS.md，拖入旧资料和产物。[角色配置](ROLE_WORKSPACES_ZH.md)
3. 同一功能的后续要求和附件归入原任务。默认执行方式使用本任务目录和岗位已获准的文件工具。在支持项目终端的版本中，老板可从「员工 → 选中员工 → 目录与权限」打开「在项目目录使用终端」。此后从岗位的项目根目录执行，在原工单接着办。使用 OpenAI 模型的 Codex 员工需先在「基本资料」完成自己的 OpenAI 登录。[员工项目设置](ROLE_WORKSPACES_ZH.md)
4. 在自己的飞书租户开通管家和专业机器人的应用身份，按接入文档配置本机网关。首次配对使用本机页面生成的一次性配对码，长期设备令牌放进网关环境。不要将密钥贴到聊天或角色目录。使用仓库的启动脚本时，默认配置文件为 `%LOCALAPPDATA%\SuparagentSteward\feishu.json`。[飞书接入与消息示例](FEISHU.md)

从另一个 PowerShell 窗口启动已配置的飞书组件：

```powershell
.\scripts\windows\start-steward.ps1 -Component Feishu
```

## 重启与备份

本地后台在启动时检查数据库版本和运行锁。飞书网关会加载已落盘的任务、消息与附件状态。待执行的工作可继续，操作是否已生效不能判断时保留记录供核对。以管家回复的任务编号为接单凭证。尚未被本机接收的离线消息能否补收，须在你的租户测试。

工作台和飞书服务均按需启动。使用过旧版登录启动项的电脑可运行 `scripts/windows/register-steward.ps1 -IncludeFeishu`，把本安装的两个组件改成手动启动。

角色和数据库必须由同一个 Windows 用户运行。私有数据默认写入该用户的 LocalAppData。数据库加密密钥单独保存在 key.dpapi，受 Windows 当前用户保护。备份时关闭两个组件，一并保存数据目录、角色目录和网关状态；迁移到其他 Windows 账户还需要单独迁移加密密钥。保留现有文件，再用副本演练恢复。

## 代码与验证

分支基于上游 f2ae9d0。后端直接使用上游 agent、thread、团队委派、任务板和 SQLite 迁移。Windows 本机启动脚本位于 `scripts/windows`，飞书接入位于 `backend/app/feishu`。真实租户消息收发、手机展示和专业软件运行需要在目标环境验收。


## 本机语音转写

在仓库根目录运行 `backend\.venv\Scripts\python.exe scripts\windows\install-local-whisper.py`，安装约 78.2 MB 的多语言 Whisper Tiny 模型。安装程序从固定版本下载四个文件并逐一校验 SHA-256，保存在当前 Windows 账户的 `%LOCALAPPDATA%\SuparagentSteward\models\faster-whisper-tiny`。再次运行会校验已有文件。模型使用 MIT 许可。[模型来源](https://huggingface.co/Systran/faster-whisper-tiny/tree/d90ca5fe260221311c53c58e660288d3deb8d356)

通过 Windows 启动器运行管家后，管理页和飞书语音都使用本机模型。首次转写会将模型载入内存。PyAV 在本机解码音频，转写使用 CPU，无需安装 CUDA、ffmpeg 或 ffprobe 命令行工具。转写文字会交给当前聊天使用，可能出现人名、数字或方言识别错误。视频的抽帧处理独立于语音转写。
