# 单 Key 并发与 429 门禁

`max_parallel_requests` 按虚拟 Key 全局计数，不按任务、项目、机器或 Docker 容器计数。

## 默认策略

- 单 Key 默认每个任务运行 2 个候选。
- 全局默认最多同时运行 4 个 `sologsb-*` 容器。
- 多个任务共享这 4 个容器名额，推荐同时运行 2 个任务、每个任务 2 个候选。
- 候选启动前检查同镜像容器；预计当前数量加本批候选数超过 4 时等待。
- 只有 Key 独立分配且容量经实际探测确认后，才允许通过
  `SOLOSB_MAX_CONTAINERS` 提高本地上限。

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
