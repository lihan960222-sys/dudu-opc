# 在 Windows 上接入飞书

网关单独启动。嘟嘟通过长连接收取老板的私聊。每个专业机器人用自己的应用身份在指定群里发进度；开通只读咨询的专业机器人还会回复老板主动发来的私聊。启用前先在测试租户操作。

1. 为管家和每个专业角色分别创建企业自建应用，启用机器人。把专业机器人加入工作群。给发言的应用申请 `im:message:send_as_bot` 和收发媒体所需的 `im:resource`。给管家额外申请接收单聊消息的 `im:message.p2p_msg:readonly` 权限，在事件订阅中选择长连接并订阅 `im.message.receive_v1`，发布应用版本。记录租户标识、应用标识、管家应用中用户的 open_id 和专业机器人可见的目标群 chat_id。权限名称和审批以应用控制台为准。[飞书消息接口](https://open.feishu.cn/document/server-docs/im-v1/message/reply) · [官方长连接 SDK](https://github.com/larksuite/oapi-sdk-python)
2. 在本地后端创建角色，记下数字 agent ID；为角色配置本地目录和模型。在团队设置里将管家设为负责人，专业角色加入同一团队。复制 `backend/feishu.example.json` 到本机私有目录，填写这些 ID，独立本机后端固定使用的 owner_id（local-owner）、项目和地址。每个专业角色独占一个飞书应用。更换用户、租户或重新分配群时，新建 state_dir，避免历史投递流向新的收件方。
3. 把各应用密钥设在网关进程读取的环境变量中，变量名对应配置中的 secret_env。网关与执行后端使用不同的进程环境，勿把这些密钥传给执行进程。网关与后端必须使用同一数据库及凭据加密配置。若后端要求设备令牌，从 Settings → Account & privacy → Devices 创建一次性配对码。收发聊天和附件可选择 chat；启用主动建议时选择包含 manage 的范围，然后在本机执行下文的配对命令。配置中填写 `"backend_token_env": "FEISHU_BACKEND_TOKEN"`，网关会以 Authorization: Bearer 方式提交设备令牌。chat 范围可发起会话并上传附件。本模块适用本机独立后端；桌面壳的临时启动令牌另有启动流程。
4. 从 backend 目录安装依赖并启动。先运行后端数据库迁移并启动本机服务，再运行下面的网关命令。默认只接收并保存任务与附件。确认本机任务执行环境可限制目录访问后，把 isolated_runner_enabled 改为 true，再重启网关。已有 held 事件可按下文恢复。

```powershell
uv sync --frozen
uv pip install --python .venv/Scripts/python.exe -r requirements-feishu.txt
$env:FEISHU_CONFIG = 'C:\SuparagentPrivate\feishu.json'
# 首次配置时，在桌面打开与团队管家的普通聊天。用这段聊天的编号替换 123。
.venv/Scripts/python.exe -m app.feishu --bind-steward-thread 123
.venv/Scripts/python.exe -m app.feishu
```

绑定命令会核对当前老板、团队负责人和会话状态，并将编号写入本机私有配置的 steward_thread。首次配置时运行一次即可，后续启动沿用同一段聊天。普通私聊和任务派工各自使用持久化请求编号，后端已接收但回执丢失时，网关重启后仍会找回原回复。

## 配对启用身份验证的本机后端

在启动网关的同一 PowerShell 窗口执行。令牌保存在本次进程环境；后台长期运行时，应在网关专用账户的安全配置中保管。

```powershell
$pairBody = @{code = (Read-Host '输入一次性配对码'); label = '飞书管家网关'; cookie = $false} | ConvertTo-Json
$paired = Invoke-RestMethod -Method Post -Uri 'http://127.0.0.1:8000/api/v1/auth/pair' -ContentType 'application/json' -Body $pairBody
$env:FEISHU_BACKEND_TOKEN = $paired.token
```

## 从手机派单

启用执行后，可以直接私聊管家，例如「请让焊接组检查阀体的局部应变」。管家读取自己目录的 AGENTS.md 和默认模型，结合允许的角色及最近 120 项任务的标题判断归属。候选不明确时，管家会要求确认。也可以在管家私聊发送第一行「新建 焊接 局部应变」，下一行写具体要求。管家回复任务编号，专业 bot 在指定群发一条根消息。回复管家的任务回执，可以追加文字、图片、语音、视频或文件；也可输入「续接 焊接 局部应变」或「查询 焊接 局部应变」。单独发来的附件找不到任务时，回复管家的确认问题并填写「#任务编号」。老板也可以在已绑定任务的专业机器人群话题里回复员工的消息，或在该话题中 @该员工提问。员工会在原话题引用本任务的状态和已公开记录答复。群里不能下单或批准操作。专业应用需订阅 im.message.receive_v1 并申请 im:message.group_at_msg:readonly，用 @ 提问可在仅接收提及消息的配置下工作。后台的团队讨论与飞书话题分别保留记录，群里能看到已发布的进度和交付，内部咨询原文保留在本机。[飞书群消息权限说明](https://open.feishu.cn/document/uAjLw4CM/ukTMukTMukTM/reference/im-v1/guide/faq)

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

## 专业机器人的身份、工作群和私聊

在飞书开发者后台分别设置嘟嘟、焊接机器人和 Omniverse 机器人的应用名称、图标和可用范围，然后发布应用版本。工作群里的头像和名称来自实际发送消息的应用。本地角色关联使用 agent_id。每个角色的 chat_id 指定一个工作群；发布前，把对应的机器人加入该群并授予发言权限。[飞书应用配置](https://open.feishu.cn/document/historical-version/quickly-develop-interactive-cards/step-one-create-and-configure-the-application?lang=zh-CN) · [发布与审核](https://open.feishu.cn/document/best-practices/intro-to-custom-app-review?lang=zh-CN)

要让老板从手机直接问专业机器人，在该角色下填写 owner_open_id。该值须来自老板在**此应用**下的身份记录。给这个应用开通单聊接收权限，启用长连接并订阅 im.message.receive_v1。所有应用使用同一 tenant_key。省略 owner_open_id 时，该机器人仍可在工作群汇报。

老板可发「#42 的验收条件是什么？」。机器人读取本项目中分配给自己的任务、当前状态、适用版本、验收条件，以及自己已在群里公开的最新记录。回复始终引用已核实的老板原消息。不确定任务时，机器人列出自己可查询的任务编号。派工、审批、提交附件和项目改动请交给嘟嘟；专业机器人私聊这一入口只查询已有记录。

后台的实际派工也可出现在工作群。网关每 15 秒核对一次新派工及已发布派工的状态变化，使用对应员工的应用发到任务话题；首次启用时从现有记录之后开始。群里显示任务号、派工号及执行状态，专业人士之间的咨询原文保存在本机。已从飞书派发的任务沿用原话题及交付回执。

## 嘟嘟的主动建议

在配置中设置 `"proactive_suggestions": true`，使用管理权限的设备令牌或本机所有者身份。嘟嘟每分钟查看一次待选建议，每次最多向老板私聊推送三条。同一建议的同一版本只登记一次。

直接引用建议消息，回复 `1` 或 `2`，按消息里的选项生成摘要或建立后续任务。也可回复「稍后」或「忽略」。嘟嘟等后端保存选择后才回执；有后续任务时，回执附任务号。选择绑定建议编号、版本与老板原回复，重投时使用同一选择。

## 临时提示与媒体验收

老板的新消息写入本机后，会唤醒处理线程。嘟嘟先发临时提示，再处理需要较长时间的附件或任务判断。等飞书确认最终答复已发送，网关才用嘟嘟的身份撤回对应消息的「思考中」和「已排队」。新版本只跟踪启用后登记的提示。请在嘟嘟应用中开通消息撤回权限（im:message:recall）；平台限时和权限会影响能否撤回。[飞书撤回消息接口](https://open.feishu.cn/document/server-docs/im-v1/message/delete)

飞书收发使用消息资源接口。图片、语音、视频和普通文件可以保存原件，发送端按各应用身份分别上传。管家聊天或任务执行能否识图、转写或解析文档，取决于所选后端与模型。Windows 管家用本机 CPU 转写语音。网关显式选择 local，PyAV 在独立进程中检查并解码音频，最长 20 分钟、最大 25 MiB。转写文字随聊天使用，音频不会送到云端转写服务。缺少本机模型时，网关保留原件并说明原因。对于支持图像的 Codex 会话，网关可使用本机 PyAV 从 MP4 抽取最多三张画面，每张不超过 512 × 384，并附上距开头的时间。抽帧优先保留原始附件，总附件数上限为八。解码超时、文件损坏或模型不接收图像时仍保留原件和文件信息。抽样画面只能说明所选时刻。专业机器人只读私聊目前返回文字和已公开的工作记录。已有 Opus 音频和 MP4 视频可按原生消息发送，系统不会自动把文字转成语音。音视频能否在目标租户的手机和电脑上直接播放，需要使用真实设备验收。[飞书消息内容与文件格式](https://open.feishu.cn/document/server-docs/im-v1/message-content-description/create_json?lang=zh-CN)

### 给已有嘟嘟配置加一位员工

先在本地团队设置里保存这位员工，记下数字角色 ID。准备好该员工应用的 App ID、App Secret、老板在此应用下的 open_id 和授权工作群 chat_id。在使用网关的 Windows 账户下打开 PowerShell，从 backend 目录执行以下命令。

```powershell
.venv/Scripts/python.exe -m app.feishu.setup_specialist --config "$env:LOCALAPPDATA\SuparagentSteward\feishu.json"
```

按提示填写角色与应用信息。最后一次输入是隐藏的密钥提示。配置文件保存角色、身份和密钥变量名；App Secret 默认使用当前 Windows 账户的 DPAPI 加密，放在同目录的 .specialist-credentials 子目录。工具不会重启服务或发送测试消息。下次启动时，网关会逐个加载已登记的应用。已有嘟嘟对话继续使用原会话；明确指定的专业任务按角色派单。尚未启用任务执行时，普通对话仍由嘟嘟接收。

如果已有服务账户凭据管理工具，可加 `--env-only --secret-env FEISHU_WELD_SECRET`，由其向网关进程提供密钥。每个应用使用独立变量。备份时保留配置及凭据目录；DPAPI 凭据换 Windows 账户后需要重新登记。

### 查看一条消息慢在哪里

`python -m app.feishu --timings` 显示最近 20 条入站事件的耗时（秒）。local_wait_and_prep_s 包含本机排队和附件准备，submit_until_ack_s 是提交本机后端到收到回执的时间，backend_turn_s 是后端记录的整轮运行时间，finish_until_delivery_s 是后端结束到飞书确认最终回复送达的时间。后端运行时间可能含模型调用、工具和审批，与提交等待也可能重叠。旧记录、纯查询和没有回执的阶段会留空。

## 按次查看与指定同事的私聊

在老板的管理设置中连接自己的飞书账号，授权管家按次查阅所选联系人。应用需申请用户权限 `im:chat:read`、`im:message:readonly`、`im:message.p2p_msg:get_as_user`，更新并发布版本。选择联系人时，粘贴这段单聊的链接或会话 ID。系统用老板的用户身份核对会话及对方身份。一对一私聊才可登记。群、话题群、机器人会话均不能作为来源。[飞书历史消息](https://open.feishu.cn/document/uAjLw4CM/ukTMukTMukTM/reference/im-v1/message/list) · [飞书官方工具的会话身份字段](https://github.com/larksuite/cli/blob/main/skills/lark-im/references/lark-im-chat-list.md)

可选一位团队员工作为该联系人的授权经办人。也可在 backend 目录通过命令登记或移除联系人。登记时只核对这一个单聊的身份信息，移除后，下一次查询就会使用新设置。

```powershell
.venv/Scripts/python.exe -m app.feishu.history --config $env:FEISHU_CONFIG --add 小周 oc_REPLACE
.venv/Scripts/python.exe -m app.feishu.history --config $env:FEISHU_CONFIG --remove 小周
```

在管家私聊发送「查看聊天记录 列表」可看到已选联系人。「查看飞书聊天记录 小周 最近6小时 10条」会返回带发言人和时间的原文摘录；末尾加「关键词：验收」可以从本批记录中查找相关发言。默认最近 24 小时、10 条，每次最多查 72 小时、30 条和 3 页。每条摘录最多 220 个字符，只标识非文字消息的类型，不下载附件。

发送「总结我和小周最近6小时的聊天记录」，管家会按问题、约定和后续安排在本机整理原文。要让已授权的员工经办，发送「请让焊接总结我和小周最近6小时的聊天记录」，其中“焊接”须与团队中的员工名一致。管家会回复私密任务号、经办人和可核对的摘录。默认使用本机规则归类摘录。在老板管理设置中启用 OpenAI Codex 摘要后，只有明确要求整理或总结时，系统才会把本次选中的摘录和岗位名称、职称送交 OpenAI。普通查看与关键词查询继续在本机完成。Codex 使用具名角色配置的模型，在临时会话中处理；发出摘录前会用虚构输入检查该版本和模型的实际请求是否为空工具列表。核对失败会退回本机整理并标明。结果由管家私聊发给老板，员工机器人及项目群不会收到。私密任务登记经办人、来源、时间范围和完成情况。本地投递记录保留回复内容，用于重试和核对，请保护网关状态目录。系统不在后台采集聊天记录。实际租户的权限和可见范围需在启用时确认。
