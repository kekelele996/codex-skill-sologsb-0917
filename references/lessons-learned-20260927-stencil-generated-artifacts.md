# 0927 实测避坑：Stencil 的 `.stencil` 缓存与 `www` 构建目录发布污染

## 触发记录

- 项目：`sologsb-1012 · 手语课程编排工具`
- 技术栈：Stencil 4、TypeScript、Ionic。
- A/B 候选在容器内完成依赖安装、生产构建和模型逻辑断言，语义审核通过后执行 `publish`。
- 发布后的产物 commit 包含 `.stencil/.build/*.log` 和 `www/index.html`、`www/assets/...`、`www/host.config.json`。
- 这些目录都是 Stencil 生成物，不应进入 A/B 产物；`www/index.html` 还被错误计入业务源码文件。

## 原因

- `GENERATED_PATH_EXCLUDES` 覆盖了 `dist/`、`out/`、`.next/`、`.nuxt/`、`.angular/` 等常见目录，但没有 Stencil 的 `.stencil/` 和 `www/`。
- 提交预检的 `GENERATED_DIR_NAMES` 有同样遗漏，发布后审计才在真实产物树上发现。
- Stencil 的 `www` 是 `README.md` 明确说明的生产静态目录，属于构建结果；`.stencil` 是编译缓存，两者都不能进入候选产物。

## 修复

- 发布排除清单增加 `.stencil/` 和 `www/`。
- 提交预检的生成目录集合增加 `.stencil` 和 `www`。
- 新增回归测试，覆盖 `.stencil` 缓存、`www` 构建文件的排除与预检识别。
- 对已发布错误仓库，不放行继续验证；按用户确认后的重建流程重新初始化仓库并生成父提交等于初始快照的干净 A/B commit。

## 恢复顺序

1. 保留原生轨迹、候选业务补丁和语义审核，记录旧仓库错误 commit。
2. 更新技能排除规则并跑回归测试。
3. 将候选 HEAD 重置到初始快照，只应用业务补丁，清除生成目录和依赖目录。
4. 重新执行 `github-init` 创建新的 `main/A/B`，再执行 `publish` 生成干净 A/B commit。
5. 从新 commit 重新执行真实验证、GSB、录屏和提交，不复用基于错误 commit 的任何产物结论。

## 同一批任务暴露的查重误判

- 同结论理由必须写“因此选择Same”，但旧的 `REASON_REQUIRED_BOILERPLATE_RE` 只剥离
  “这个任务最重要的是”和两侧称谓，没有剥离这个强制结论。
- 因此任意两条 Same 理由都至少共享 8 个规范化字符，触发 `REVIEW_REQUIRED`，即使业务内容完全不同。
- 修复方式是在进入历史相似度计算前，连同强制结论“因此选择Same”一起剥离；
  回归测试验证剥离后只剩真实业务文本。

## 恢复顺序补充

6. 重跑 G10；如果唯一命中仍是强制结论，先修复查重器的模板剥离规则，不能改写业务事实去迎合误判。
