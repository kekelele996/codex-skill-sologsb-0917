# 单轮轨迹校验

干净运行必须同时满足：

1. `.jsonl` 有效，且全部事件的 `sessionId` 唯一并与记录一致。
2. 恰好一个真人 user prompt，忽略 `tool_result` 和 `isMeta` 事件。
3. 该 prompt 文本与唯一提示词逐字一致。
4. 最终 assistant 的 `stop_reason` 为 `end_turn`。
5. 最终 `end_turn` 之后没有 assistant、user 或错误事件。
6. 不出现 `AskUserQuestion`、权限询问、追问、超时、API/网络错误、会议话恢复输入或第二次真人输入。
7. 进程退出码为 0，容器正常终止。

失败尝试允许保留在 `workspace/轨迹文件/candidates/<candidate>/rejected/` 作为审计材料，
但不得进入有效 A/B 证据。每次实际重跑只处理失败候选，必须删除该候选原 clone 和容器，
从本地初始快照重新 clone，再创建新容器、新 Claude home 和新 SessionID，并从同一提示词
重新开始；每个候选最多六次实际尝试（含首次）。Claude Code 内部的自动重连默认最多十次，
属于同一个 attempt，不算一次新尝试，也不能替代上述全新启动。已经通过结构校验的候选保持 staged。
候选竞速得到最先完成的两个 staged 结果后，立即按完成顺序映射为 A/B，未进前两名的候选
可以主动停止；候选目录名保持不变。

## 结束后的语义完成审核

结构和单 attempt 默认超时为 7200 秒。`end_turn` 校验只能证明回合结束，不能证明需求已完成。
前两名候选映射 A/B 后，分别读取 `monitor/semantic/<side>.packet.json`，逐条核对提示词要求对应的
候选轨迹事件和候选产物文件，写入 `<side>.review.json`。只有两侧都满足：

- `completed=true`
- `interrupted=false`
- `unfinished=[]`
- 每条 requirement 都是 `satisfied`
- 每条 requirement 至少有 trace 或 artifact 证据

才允许运行 `publish`。任一侧未完成或证据不足时，使用 `run --side A|B --force` 丢弃该侧
现场并重跑；在两侧都通过前不得提交或推送 A/B 产物。
