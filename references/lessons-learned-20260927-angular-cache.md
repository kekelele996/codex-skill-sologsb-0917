# 0927 实测避坑：Angular 构建缓存发布污染

## 触发记录

- 项目：`sologsb-1005 · 专利权利要求映射工作台`
- 技术栈：Angular 19、TypeScript、RxJS、PrimeNG
- A/B 已在宿主执行 `npm ci` 和生产构建，依赖安装、构建、启动探活和录屏均通过。
- 远端 `origin/A` 包含 `.angular/cache/...`，`origin/B` 也包含 `.angular/cache/...`。
- 发布结果把 `.angular/cache` 当成业务代码，A 的改动量因此从正常业务代码膨胀到八万多行；`status` 也没有拦截。

## 原因

- `GENERATED_PATH_EXCLUDES` 覆盖了 `node_modules/`、`dist/`、`.next/`、`out/`、`.output/` 和
  `*.tsbuildinfo`，但没有覆盖 Angular 的 `.angular/` 缓存目录。
- 提交预检的 `GENERATED_DIR_NAMES` 有同样遗漏，所以远端脏树没有在预检阶段被阻断。
- 候选容器只修改了源码，宿主复核和录屏期间生成的 `.angular/cache` 属于运行环境噪声，不能进入产物 commit。

## 修复

- 发布排除清单增加 `.angular/`。
- 提交预检的生成目录集合增加 `.angular`。
- `_commit_local` 在复用既有 commit、生成新 commit 和原子推送前，都读取
  `git ls-tree -r --name-only` 复核产物树；发现生成目录或构建缓存时直接阻断，不能只依赖暂存清单。
- 回归测试覆盖 `.angular/cache/vite/deps/chunk.js` 的排除、预检识别和 commit tree 拦截。

## 恢复顺序

1. 删除带 `.angular` 缓存的远端仓库。
2. 保留候选工作区，把本地映射候选的 HEAD 重置到初始快照；`.angular` 留在工作区但不进入新 commit。
3. 重新安装生成物排除规则，重新生成父提交等于初始快照的 A/B 产物 commit。
4. 重建 `main/A/B` 仓库并原子发布干净 A/B。
5. 重新执行真实验证、证据刷新、GSB 文案校验和录屏收尾；已有录屏只有在源码 commit 未变化且重新验证通过时才能复用。
