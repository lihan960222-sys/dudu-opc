# 在 Windows 上接入飞书

网关单独启动。它从管家的长连接收取私聊，用专业机器人的应用身份在指定群里发布进度。启用前先在测试租户操作。

1. 为管家和每个专业角色分别创建企业自建应用，启用机器人。把专业机器人加入工作群。给发言的应用申请 `im:message:send_as_bot` 和收发媒体所需的 `im:resource`。给管家额外申请接收单聊消息的 `im:message.p2p_msg:readonly` 权限，在事件订阅中选择长连接并订阅 `im.message.receive_v1`，发布应用版本。记录租户标识、应用标识、管家应用中用户的 open_id 和专业机器人可见的目标群 chat_id。权限名称和审批以应用控制台为准。[飞书消息接口](https://open.feishu.cn/document/server-docs/im-v1/message/reply) · [官方长连接 SDK](https://github.com/larksuite/oapi-sdk-python)
2. 在本地后端创建角色，记下数字 agent ID；为角色配置本地目录和模型。在团队设置里将管家设为负责人，专业角色加入同一团队。复制 `backend/feishu.example.json` 到本机私有目录，填写这些 ID，独立本机后端固定使用的 owner_id（local-owner）、项目和地址。每个专业角色独占一个飞书应用。更换用户、租户或重新分配群时，新建 state_dir，避免历史投递流向新的收件方。
3. 把各应用密钥设在网关进程读取的环境变量中，变量名对应配置中的 secret_env。网关与执行后端使用不同的进程环境，勿把这些密钥传给执行进程。网关与后端必须使用同一数据库及凭据加密配置。若后端要求设备令牌，从 Settings → Devices 创建 chat 范围的一次性配对码，然后在本机执行下文的配对命令。配置中填写 `"backend_token_env": "FEISHU_BACKEND_TOKEN"`，网关会以 Authorization: Bearer 方式提交设备令牌。chat 范围可发起会话并上传附件。本模块适用本机独立后端；桌面壳的临时启动令牌另有启动流程。
4. 从 backend 目录安装依赖并启动。先运行后端数据库迁移并启动本机服务，再运行下面的网关命令。默认只接收并保存任务与附件。确认本机任务执行环境可限制目录访问后，把 isolated_runner_enabled 改为 true，再重启网关。已有 held 事件可按下文恢复。

```powershell
uv sync --frozen
uv pip install --python .venv/Scripts/python.exe -r requirements-feishu.txt
$env:FEISHU_CONFIG = 'C:\SuparagentPrivate\feishu.json'
.venv/Scripts/python.exe -m app.feishu
```

## 配对启用身份验证的本机后端

在启动网关的同一 PowerShell 窗口执行。令牌保存在本次进程环境；后台长期运行时，应在网关专用账户的安全配置中保管。

```powershell
$pairBody = @{code = (Read-Host '输入一次性配对码'); label = '飞书管家网关'; cookie = $false} | ConvertTo-Json
$paired = Invoke-RestMethod -Method Post -Uri 'http://127.0.0.1:8000/api/v1/auth/pair' -ContentType 'application/json' -Body $pairBody
$env:FEISHU_BACKEND_TOKEN = $paired.token
```

## 从手机派单

启用执行后，可以直接私聊管家，例如「请让焊接组检查阀体的局部应变」。管家读取自己目录的 AGENTS.md 和默认模型，结合允许的角色及最近 120 项任务的标题判断归属。候选不明确时，管家会要求确认。也可以在管家私聊发送第一行「新建 焊接 局部应变」，下一行写具体要求。管家回复任务编号，专业 bot 在指定群发一条根消息。回复管家的任务回执，可以追加文字、图片、语音、视频或文件；也可输入「续接 焊接 局部应变」或「查询 焊接 局部应变」。单独发来的附件找不到任务时，回复管家的确认问题并填写「#任务编号」。群聊的回复不会触发接单。

接收原件保存在 state_dir/attachments 下，每项任务单独成目录。网关记下原名、消息、资源标识和 SHA-256。启动执行时，原件经当前后端的会话附件入口处理，转写、文档解析、视频处理及视觉能力取决于已配置的引擎和工具。无法处理时私聊会提示核对；原件保留。每个文件的本机接收上限为 50 MiB，后端还可能施加更低限制。

需要按版本或验收标准区分功能时，可以在任务描述中分行写「版本：v2」和「验收：空目录也能打开」。这些字段会进入本地任务记录，执行时会随上下文交给专业角色。

## 发送本地产物

专业 bot 可以在本轮结束时列出可公开的交付文件。必须先将文件写入自己角色的 `任务/任务编号/产物/飞书`，再在本轮最终 JSON 的 artifacts 数组中填写相对路径，例如 `["产物/飞书/result.txt"]`。网关只提取明确列出的文件，逐项检查目录、链接和大小，保存本轮副本，随后分别回传至管家私聊和专业 bot 的群话题。summary 是给用户的结论，group_update 是给工作群的简短进展。

手动指定附件时，把已审阅的文件放在 artifact_root 下的任务编号目录，例如 `42/result.png`。从 backend 目录运行一次下面的命令。private 使用管家身份，group 使用该任务的专业 bot，在原群话题中回复。两个身份各自上传并缓存资源标识。图片限制 10 MiB，普通文件、音频和视频限制 30 MiB。audio 使用 Opus，media 使用 MP4。

```powershell
.venv/Scripts/python.exe -m app.feishu --task 42 --artifact result.png --kind image --audience group
.venv/Scripts/python.exe -m app.feishu --task 42 --artifact result.png --kind image --audience private
```

## 停机、恢复和核对

网关重启会读取未处理消息，恢复已绑定的下载，并查询已取得 turn ID 的运行结果。网关在发起执行前落盘保存完整请求及唯一编号。后端把请求与 turn 放在同一笔事务中登记。连接中断后网关使用相同编号和内容续查，后端会返回原 turn。已在运行的任务遇到查询错误会继续核对。飞书发送结果不明时，投递记录进入 verify。运行 `python -m app.feishu --status` 查看事件及投递编号。核实故障已修复后，用 `--resume-held 事件编号` 重新处理 held 事件。发送结果不明时，先检查飞书原群话题或管家私聊。找到消息后用 `--confirm-message 投递编号 om_消息编号` 登记；确认未发送才用 `--retry-unsent 投递编号`。若记录来自未保存请求编号的旧网关，需在本地任务会话核对执行情况；新网关不会自动重发这些旧请求。

关闭网关后备份配置、state_dir、artifact_root，以及同一时间点的后端数据库与角色目录。网关状态中含私聊和附件，应限制本机账户访问。手机和电脑上的真实话题显示、飞书权限、设备回执及音视频预览，仍需在目标租户验收。

## 专业任务的上下文和状态

在本地后端设置环境变量 `STEWARD_STRICT_OWNER`，值与飞书配置里的 `owner_id` 一致，再重启后端。该工作区的专业角色只能在对应的功能任务会话中执行。网页上的普通专业角色聊天和旧式委派会提示从管家任务进入。其它工作区继续使用上游的团队规则。

同一项功能沿用任务板卡片。专业角色开始处理后，卡片进入「In progress」。本轮成功后转入「Review」，失败或取消时转入「Waiting」。用户验收后，可在任务板把卡片移到「Done」。引用已完成的任务补充要求，会重新打开原卡片。查询直接读取卡片状态。管家派单记录保留每次交办的父子 job、实际执行的 turn 和专业角色的回报。模型的输出仅代表该轮执行结果，验收由用户确认。

需要在同一角色的多个任务间使用已确认的领域事实时，可在角色目录创建 UTF-8 文件「资料/领域事实.json」。每条记录填写内容、来源和适用版本。例如：

```json
[{"fact":"本项目的长度单位为毫米","source":"设计说明第 3 页，负责人已确认","version":"v2"}]
```

`version` 写具体版本时，只有该版本的任务会收到这条事实。写 `*` 表示适用于该角色的所有版本。临时发现随任务记录保存，角色负责人可核对后再写入该文件。

## 在私聊里确认文件写入

当一轮工作需要许可时，管家会私聊发送任务编号、任务内的完整相对路径、写入字节数和 SHA-256。写入会替换该路径下的同名文件。直接引用这条审批消息，回复「确认」或「拒绝」，只对这一次写入生效。群消息和引用其它回执的答复均不能授权。任务已经结束、审批已过期、后台已失去本轮控制权时，原审批会失效。计划模式继续禁止写入。

飞书回传许可时如果网络中断，网关会提示在本地管理页核对。历史回执不会授权新的操作。需要检查完整文件内容时，可在本地管理页查看待审批的原始请求。
