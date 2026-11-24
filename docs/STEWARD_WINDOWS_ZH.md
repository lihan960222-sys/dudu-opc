# 飞书大管家 · Windows 本地开发版

安装完成后，双击根目录的 `start-steward.cmd` 即可打开工作台。已经配好的飞书网关会一起启动。后台服务运行在家中主机，员工使用的工作目录也在这台电脑上。「职责简介」告诉管家什么任务该派给谁，`AGENTS.md` 规定员工如何执行、验证和汇报。

## 管家会主动做什么

单机管家每两分钟检查一次已有任务，关注阻塞、待验收、逾期、缺少验收条件和待分配的工作。建议包含原任务编号和具体选项。选择整理资料，管家生成一份基于现有记录的简报；选择跟进，任务板会新增一项由管家负责的任务。同一建议重复提交只受理一次。选「稍后」的建议可在后台重新查看。原任务状态变化后，旧建议会过期。

飞书网关的私有配置可启用 `proactive_suggestions`，通过管理权限配对的设备把选项发到老板和管家的私聊。回复必须对应到具体建议。日常任务继续使用原任务编号及审批流程。

## 从自己的手机或电脑打开后台

1. 在家中主机和远端设备登录同一 Tailscale 私网，先在家中浏览器设置后台所有者密码。
2. 在项目目录运行 `powershell -ExecutionPolicy RemoteSigned -File scripts/windows/enable-private-access.ps1`。它会把私网 HTTPS 地址指向本机服务，检查现有映射和公开分享状态，并把私网主机名保存到本机配置。
3. 在没有任务执行时重新启动 Web 组件，在已加入私网的设备打开脚本返回的地址，用后台原账号登录。Windows 登录后可通过 `scripts/windows/register-steward.ps1 -IncludeFeishu` 自动恢复两个组件。要在用户登录前运行，需要使用同一 Windows 账号凭据登记启动任务，保证系统能解密该账号的本机密钥。

Tailscale Serve 只给已获准访问的私网设备提供入口。[Tailscale Serve 说明](https://tailscale.com/kb/1312/serve)

## 从飞书转到 Codex 继续工作

在后台打开对应任务的聊天，查看「在 Codex 续接」。那里给出当前工作目录和这个任务专用的会话 ID。结束正在执行的那一轮后，在该目录打开终端，运行页面上的 `codex resume` 命令。切回飞书前，先退出占用该会话的 Codex 客户端。这样同一项目文件、同一原生会话都能接下去。后台可以列出桌面新增的原生轮次，飞书保留它自己收到的消息和交付。

## Windows 上的 Codex 响应速度

如果 Windows 已经配了本机代理，可以在私有数据目录的 `codex-network.json` 写入 `{"use_windows_proxy": true}`，再启动 Web 组件。程序只识别 Windows 为两个模型地址指定的同一本机 HTTP 代理。它在启动独立 Codex 进程时设置 HTTPS 代理，所以这个进程发起的其他 HTTPS 请求也可能经过代理。回环地址直接连接，已有的代理环境变量优先。飞书和其他引擎沿用各自的网络设置。把值改成 `false` 并重启 Web 即可关闭。速度取决于本机网络与模型当时的响应。

## 初次使用

1. 本次交付的工作目录已生成网页并装好后端环境，保留其所在路径即可启动。如果把代码移到另一台机器，先安装 Python 3.14、uv、Node 22+ 和 npm，在 backend 下执行 `uv sync --locked`，在 frontend 下执行 `npm ci`，然后从仓库根目录执行 `node scripts/build-ui.js`。使用 Codex 的 Windows 账户需要先完成 `codex login`。
2. 在管理页创建一个管家和至少一个专业角色，分别选 Codex 和实际可用的模型，在团队设置中指定负责人。给每个角色选择独立目录，在目录根部编辑 AGENTS.md，拖入旧资料和产物。[角色配置](ROLE_WORKSPACES_ZH.md)
3. 同一功能的后续要求和附件归入原任务。角色公共资料可跨本角色任务读取；功能任务各有独立目录和 Codex 会话。专业任务的工具可列文件、读取 UTF-8 文本；打开本地工具并经授权后，可写入本任务目录。此版本未提供任意命令执行、二进制工程工具或 Omniverse 扩展。启用本地工具前请了解原版普通管家会话的权限。在启动 Web 组件的 PowerShell 设置 `$env:LOCAL_TOOLS_ENABLED='true'` 可以开启；默认关闭。
4. 在自己的飞书租户开通管家和专业机器人的应用身份，按接入文档配置本机网关。首次配对使用本机页面生成的一次性配对码，长期设备令牌放进网关环境。不要将密钥贴到聊天或角色目录。使用仓库的启动脚本时，默认配置文件为 `%LOCALAPPDATA%\SuparagentSteward\feishu.json`。[飞书接入与消息示例](FEISHU.md)

从另一个 PowerShell 窗口启动已配置的飞书组件：

```powershell
.\scripts\windows\start-steward.ps1 -Component Feishu
```

## 重启与备份

本地后台在启动时检查数据库版本和运行锁。飞书网关会加载已落盘的任务、消息与附件状态。待执行的工作可继续，操作是否已生效不能判断时保留记录供核对。以管家回复的任务编号为接单凭证。尚未被本机接收的离线消息能否补收，须在你的租户测试。

若要自动运行，可用下列脚本登记 Windows 计划任务。默认在本 Windows 用户登录时启动。加 `-AtBoot` 则使用同一用户的 Windows 凭据在系统启动时运行，Windows 需要该用户有批处理登录权限。需要桌面的程序仍要等交互式登录。重复运行会核对本项目的登录启动项，旧版启动项可就地更新。

```powershell
.\scripts\windows\register-steward.ps1 -IncludeFeishu -WhatIf
.\scripts\windows\register-steward.ps1 -IncludeFeishu
```

角色和数据库必须由同一个 Windows 用户运行。私有数据默认写入该用户的 LocalAppData。数据库加密密钥单独保存在 key.dpapi，受 Windows 当前用户保护。备份时关闭两个组件，一并保存数据目录、角色目录和网关状态；迁移到其他 Windows 账户还需要单独迁移加密密钥。保留现有文件，再用副本演练恢复。

## 代码与验证

分支基于上游 f2ae9d0。后端直接使用上游 agent、thread、团队委派、任务板和 SQLite 迁移。Windows 本机启动脚本位于 scripts/windows，飞书接入位于 backend/app/feishu。真实租户消息收发、手机展示、开机前登录身份及专业软件运行需要在目标环境验收。


## 本机语音转写

在仓库根目录运行 `backend\.venv\Scripts\python.exe scripts\windows\install-local-whisper.py`，安装约 78.2 MB 的多语言 Whisper Tiny 模型。安装程序从固定版本下载四个文件并逐一校验 SHA-256，保存在当前 Windows 账户的 `%LOCALAPPDATA%\SuparagentSteward\models\faster-whisper-tiny`。再次运行会校验已有文件。模型使用 MIT 许可。[模型来源](https://huggingface.co/Systran/faster-whisper-tiny/tree/d90ca5fe260221311c53c58e660288d3deb8d356)

通过 Windows 启动器运行管家后，管理页和飞书语音都使用本机模型。首次转写会将模型载入内存。PyAV 在本机解码音频，转写使用 CPU，无需安装 CUDA、ffmpeg 或 ffprobe 命令行工具。转写文字会交给当前聊天使用，可能出现人名、数字或方言识别错误。视频的抽帧处理独立于语音转写。
