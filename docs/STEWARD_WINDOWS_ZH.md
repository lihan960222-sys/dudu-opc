# 飞书大管家 · Windows 本地开发版

双击仓库根目录的 **start-steward.cmd**，保持窗口打开，再用浏览器访问 **http://127.0.0.1:8000**。初次打开时按页面提示设置本机所有者口令。前端和数据库服务都在本机运行。账号、角色目录及飞书应用由你配置。

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

若要自动运行，可用下列脚本登记 Windows 计划任务。默认在本 Windows 用户登录时启动。加 `-AtBoot` 则使用同一用户的 Windows 凭据在系统启动时运行，Windows 需要该用户有批处理登录权限。需要桌面的程序仍要等交互式登录。脚本只在你实际运行时登记，不替换已有同名任务。

```powershell
.\scripts\windows\register-steward.ps1 -IncludeFeishu -WhatIf
.\scripts\windows\register-steward.ps1 -IncludeFeishu
```

角色和数据库必须由同一个 Windows 用户运行。私有数据默认写入该用户的 LocalAppData。数据库加密密钥单独保存在 key.dpapi，受 Windows 当前用户保护。备份时关闭两个组件，一并保存数据目录、角色目录和网关状态；迁移到其他 Windows 账户还需要单独迁移加密密钥。保留现有文件，再用副本演练恢复。

## 代码与验证

分支基于上游 f2ae9d0。后端直接使用上游 agent、thread、团队委派、任务板和 SQLite 迁移。Windows 本机启动脚本位于 scripts/windows，飞书接入位于 backend/app/feishu。真实租户消息收发、手机展示、开机前登录身份及专业软件运行需要在目标环境验收。
