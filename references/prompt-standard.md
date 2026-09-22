# 提示词标准

## 允许值

- 任务类型：`0-1代码生成`、`feature迭代`、`Bug修复`、`代码重构`、`工程化`、`代码测试`。
- 难度：`困难`、`地狱`。本期不接收其他值。
- 暂时排除 `代码理解`。

## 设计口径

沿用 `solo2-auto/references/fast-hard-first-round.md` 与
`solo2-annotation-loop/references/prompt-task-type-design.md`：

1. 首轮只保留一个小而真实的冲突闭环。
2. 只选一个最快可核对的证明面。
3. 至少两条约束互相牵制，并写清一条失败对另一条的影响。
4. 不靠新增页面、角色、服务、依赖、测试框架或部署面制造难度。
5. 优先控制在 170 字以内，Skill 绝对上限为 240 字。
6. 非代码测试题不把测试、自测、回归、验收标准当主交付物。
7. 决策点必须写成业务默认、优先级、失败后果或历史兼容规则，不能要求执行者反问。

## 改动量设计要求

- 困难、地狱题不能设计成只改一个字段、一个按钮、一个映射或一段文案。
- 每个 A/B 产物建议至少产生 `30` 行非测试业务代码改动，并跨至少 `3` 个业务文件。
- 每个 A/B 的本地硬底线是 `10` 行非测试业务代码改动；达不到时不得进入发布。
- 改动应自然跨数据模型、持久化或事务、业务 Store/Service、页面或入口中的多个层次。
- 测试、文档、锁文件、依赖目录、构建产物、缓存和纯配置不计入改动量。
- 禁止为凑行数增加无关重构、格式化、注释或重复代码；改动仍须服务同一条业务闭环。
- 详细平台背景、计算口径和门禁见 `references/change-volume-gate.md`。

## 设计开始前的历史 GSB 去重

历史提交页为设备配置 `solo2.baseUrl` 下的 `/app/gsb/submissions`。在设计提示词之前，先运行：

```bash
python3 "$CODEX_HOME/skills/gsb-submit-preflight/scripts/prompt_dedup.py" --task-root ROOT
```

读取脚本抽取的完整 `user_prompt` 列表；列表页的 `prompt_excerpt` 不能作为去重依据。候选提示词写好后，再运行：

```bash
python3 "$CODEX_HOME/skills/gsb-submit-preflight/scripts/prompt_dedup.py" --task-root ROOT --candidate CANDIDATE.md
```

只有 `UNIQUE` 才能继续。`EXACT`、`SIMILAR`、`REVIEW_REQUIRED` 都必须重写候选提示词。

## 文案门禁

- 唯一提示词写到 `workspace/评审文件/提示词.md`。
- `prompt.sha256` 记录最终文件哈希，A/B 只能读取该文件。
- 必须实际读取并执行固定版本 `ra-人话`，再运行
  `solo-annotation-loop/scripts/validate-prompt.py <prompt> --review <review>`。
- 查重范围至少覆盖本地历史、当前项目缓存和 SOLO2 当前 GSB 只读列表。
- 命中精确重复、长连续片段复用或语义重复时重写；最多三轮，仍不通过则停止。
