# 监控台提示词模板

以下内容可直接复制到监控台，只需替换 `{{selected_project}}`。监控工作目录和唯一任务名由监控台队列自动注入。该模板是操作指令，不属于提交到 GSB 的题目提示词。

```text
使用 $sologsb-0917，在监控队列提供的监控工作目录下执行一道完整的 Pair-wise GSB。仅本地交付：严禁调用 `submit --execute`，严禁调用 SOLO2 写接口。

环境：
- Solo Manager 必须使用 {{manager_username}} 的有效登录态，不是就立即停止。
- 凭据只从 `~/.codex/sologsb/config.json` 读取、由技能注入环境变量；禁止写进任务目录、状态文件、轨迹或日志。
- 容器名额由调度台动态管理，限流器启动容器前自动读取排队；不要自行设定、记录或核对上限。

项目：
- 已选定项目：`{{selected_project}}`。非空时必须用它接入，不得静默换题。
- 为空时从 Solo Manager 正式选择 `{{task_type}}`、`{{difficulty}}`，记录 taskId、taskNo、variant 与选择前后配额变化；平台未返回的字段写“平台未返回”。
- `init` 必须显式传 `--task-root` 指向本任务的唯一目录；只下载源码不消耗配额。

执行门禁：
- A/B 共用同一份 UTF-8 题目提示词，字节完全一致；任务类型不得选“代码理解”。
- A/B 模型始终以调度台「A / B 模型」的配置为准：本任务 A 侧 `{{model_a}}`、B 侧 `{{model_b}}`，必须按原样使用，不要自行替换或凭记忆改模型。
- 必须执行 `run --side both --candidates 2 --attempts 6 --base-url {{base_url}} --expect-model-a {{model_a}} --expect-model-b {{model_b}}`；candidate-1 固定映射 A、candidate-2 固定映射 B，除 modelname 外所有参数一致。
- {{b_alternate_clause}}
- 表单、Excel 与字段说明里的两侧模型名称写容器真实调用的模型，并写明交付的是第几次尝试；轨迹上报的模型与配置不符时技能会中断任务，此时停下如实上报，不要自行换模型重跑。
- A/B 映射完成后才可 `github-init`；两侧语义审核都通过后才可 `publish`。
- 遇到 429 先等 Key 恢复，恢复后仍用新容器、新 Claude home 和新 SessionID，尝试次数照记。
- `end_turn` 不能单独证明完成，必须结合轨迹、diff 与产物语义判断；每个 A/B 产物都要完成真实依赖准备、测试、生产构建或启动验证，结论绑定轨迹、commit、命令输出或退出码。
- A/B 各用 Terminal.app（Web 题另加独立 Chrome）做窗口级真实录屏，统一 1280x720；失败也保留真实过程，禁止 headless、伪造或只录成功片段。
- 基础设施或技能门禁失败立即停止；单侧产物自身失败可保留失败证据，但不得跳过该侧录屏。

最终交付：
- 最终回复严格使用 `references/final-delivery-format.md` 的八个二级标题，不得增删标题或添加额外说明。
- 本地交付时“SOLO2 推送结果”固定写“未执行（仅本地交付）”。
- Excel（含 2 个模型的完成情况打分与评价）、字段说明、轨迹与视频都用绝对路径，视频用 Markdown 图片语法内嵌。
- GSB 文案里的每个点都必须来自轨迹文件和真实代码，严禁猜测，遵守技能的文案规则。

其中 `{{b_alternate_clause}}` 由调度台按开关渲染：未勾选时是“未勾选交替：B 侧 6 次尝试都只跑 `{{model_b}}`，不换模型。”，勾选后是“已勾选交替：B 侧第 1、2 次先用 A 侧模型 `{{model_a}}`，从第 3 次起两个模型交替……如此往复；每一次尝试照样算实际尝试次数。”
```
