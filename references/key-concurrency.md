# 单 Key 并发与 429 门禁

`max_parallel_requests` 按虚拟 Key 全局计数，不按任务、项目、机器或 Docker 容器计数。

## 默认策略

- 单 Key 默认每个任务运行 2 个候选。
- 全局默认最多同时占用 4 个候选槽位；槽位包括已经运行的容器和已经预占但尚未启动的容器。
- 多个任务共享这 4 个槽位，推荐同时运行 2 个任务、每个任务 2 个候选。
- 每个容器在 `docker run` 前都必须调用 `_CONTAINER_LIMITER.acquire`。
  `acquire` 使用主机级独占锁，在同一个临界区内重新统计运行中容器和存活预占位，
  只有总数小于上限时才写入新的预占位；锁文件固定为
  `~/.codex/sologsb-0917/container-slots/limit.lock`。
- 总数达到上限时不启动当前容器，输出“容器名额已满，排队等待”提示，
  等待其他候选释放槽位后重新检查；一直等到配置的等待时间耗尽才报错。
- 只有 Key 独立分配且容量经实际探测确认后，才允许通过
  `SOLOSB_MAX_CONTAINERS` 提高本地上限。

## 什么算“任务容器”

只统计本技能自己创建的候选执行容器：

- 带 `sologsb-0917=true` 标签的容器；
- 或名字符合 `sologsb-<任务名>-candidate-<N>-...` 的历史容器。

以下容器一律不占名额：数据库与中间件容器、验证 clone 容器、监控台辅助容器、
任何非候选执行用途的 `sologsb-` 前缀容器。

## 生效上限怎么算

优先级从高到低，最后再受绝对硬顶 6 约束：

1. 环境变量 `SOLOSB_MAX_CONTAINERS`
2. 设备配置 `~/.codex/sologsb/config.json` 的 `claude.maxContainers`
3. 兼容配置 `~/.codex/sologsb-0917/container-limit.json` 的 `maxContainers`
4. 默认值 4

普通设备配置向导也会写入 `claude.maxContainers`，默认值为 4；由于代码存在绝对硬顶，
配置成 7 或更大时实际生效值仍然是 6。设备配置不存在或没有该字段时，默认 4 仍然生效。

查询当前生效值和占用情况（只读，不加锁、不清理标记）：

```python
from side_runner import _CONTAINER_LIMITER
status = _CONTAINER_LIMITER.status()
# {"limit": 4, "runningContainers": 2, "reservedSlots": 1, "used": 3,
#  "available": 1, "reservations": [...], "runningNames": [...], "deadMarkers": [...]}
```

`reservedSlots` 是“已占槽但容器还没出现”的预占位数量，`used` 是 `runningContainers + reservedSlots`，
`available` 是 `limit - used`。`advisoryReservedSlots` 为兼容旧监控展示保留，值与
`reservedSlots` 相同。预占位标记文件位于
`~/.codex/sologsb-0917/container-slots/reservations/*.json`，所有执行器共用同一批文件，
因此并发启动时不会各自看到一个过期的空余容量。

## 两种调度模式

监控台的 `automation.scheduleMode` 决定哪种容量模型生效，执行器通过提示词里的
`{{schedule_mode}}` 得知自己该怎么做。

| 模式 | 保持恒定的量 | 允许浮动 | 适用场景 |
|---|---|---|---|
| `容器优先`（默认） | 运行中的候选容器数 | 并行任务数 | Key 并发是瓶颈，想让容器始终跑满 |
| `任务数量优先` | 并行任务数 | 运行中的候选容器数 | 想控制同时进行的题目数 |

两种模式共用同一条进门规则：**运行中容器数与存活预占位数合计 `>=` 硬上限**时才等待。
预占位必须计入上限，因为 `docker run` 从发起请求到容器出现在 `docker ps` 之间存在时间差；
如果只看运行数，多个执行器会在这个时间差内同时越过上限。当前实现把计数和写预占位放在
同一把主机级独占锁内，超出上限的调用不会启动容器，而是等待槽位释放后重试。

## 预占位与超时

- `docker run` 前，执行器在独占锁内写入一个容器槽位标记
  （`reservations/<uuid>.json`）；该标记从写入时起就占用并发名额。
- 当容器出现在 `docker ps` 后，同一个标记会从“预占位”转为“运行中”，不会重复计数。
- `docker run` 失败或容器退出时，调用方立即删除标记；下次等待者获得锁后即可使用空出的槽位。
- 标记里的 `pid` 已死时，`acquire` 会清掉它，不会因为进程崩溃永久占满名额。
- `waitSeconds` 或 `SOLOSB_CONTAINER_WAIT_SECONDS` 控制排队等待时长，超时后抛出
  “等待容器名额超时”错误；不会在超限时偷偷启动第 5 个容器。

## 重复启动保护

监控台或人工可能对同一任务根再次触发 `run --side both`。第二个执行器过去会先清空
`monitor/state.json`、再重建候选工作区，最后才在候选任务锁上失败，把正在跑的候选现场破坏掉。
现在有两道保护：

- `run_candidates` 在改动任何状态之前先只读探测每个候选的任务锁，发现已被占用就直接退出，
  不写状态、不克隆、不删目录。
- `_clone_candidate` 需要删除已有工作区时，会先确认没有正在运行的容器挂载该目录；
  仍在挂载时直接报错，不再 `rmtree`。

因此重复启动只会返回一条明确错误，不会影响正在执行的候选。

## 429 处理

1. 遇到 `max_parallel_requests` 429 时，先停止启动新候选。
2. 使用真实的 `POST /v1/messages` 最小请求探测，不使用 `/v1/models` 判断模型并发槽。
3. 探测仍为 429 时保持等待，不得立即销毁并重启候选。
4. Key 恢复后继续按既有尝试计数规则运行。需要重开时仍使用新的容器、Claude home 和 SessionID。
5. 429 属于网关准入失败，不得把重复准入失败包装成代码完成或发布成功。

## Base URL

- 地址取自设备配置 `claude.baseUrl`，技能包里不预设域名。
- 单个任务可通过 `run --base-url URL` 覆盖，也可使用环境变量
  `SOLOSB_ANTHROPIC_BASE_URL`。
- 容器启动和 `docker exec` 都会显式注入 `ANTHROPIC_BASE_URL`，并校验实际值。
- 不同 Base URL 可能拥有独立并发槽池，但共享同一上游模型时仍会共同受到上游吞吐影响。

## 安全注入

设备专属凭据统一放在设备配置文件 `~/.codex/sologsb/config.json`（权限 0600），
技能启动时由 `scripts/device_config.py` 注入环境变量。监控台提示词和任务文件都不得写入明文凭据。

新设备接入或更换凭据时执行一次向导（会联网验证）：

```bash
python3 ~/.codex/skills/sologsb-0917/scripts/configure.py wizard
```

没有配置文件时，运行器仍按旧方式回退到 macOS 钥匙串（Claude Key）与环境变量，
因此未接入配置的设备不会因为本次改动而失效。日志和审计文件同样不写入凭据原文。
