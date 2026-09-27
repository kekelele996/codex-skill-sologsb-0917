# 0927 实测避坑：Next 静态导出与 tsbuildinfo

## 触发记录

- 项目：`sologsb-1003 · 开源文档本地化翻译工作台`
- 配置：`next.config.mjs` 使用 `output: 'export'`
- A/B 已满足候选竞速、映射、语义审核和原子发布前置条件。
- 远端 A/B commit 仍包含 `out/` 全部静态导出文件；A 还包含
  `tsconfig.tsbuildinfo`。发布结果却把 `out/` 记为业务代码并显示 `hardOk=true`。

## 原因

- `GENERATED_PATH_EXCLUDES` 只排除了 `.next/`，没有覆盖 Next 实际导出的 `out/`。
- TypeScript 的 `*.tsbuildinfo` 不是目录名，不能被按路径片段判定的旧逻辑识别。
- 提交预检的 `GENERATED_DIR_NAMES` 存在同样遗漏，因此已上传的脏内容也没有被阻断。
- A 的验证脚本产生后续文件时，门禁仍按普通源码统计，说明发布期需要同时检查暂存清单、
  commit tree 与远端 A/B tree，不能只信任单个布尔结果。

## 修复

- 发布排除清单增加 `out/` 和 `*.tsbuildinfo`。
- `_is_generated_or_lock_path` 对 `.tsbuildinfo` 后缀做独立判断。
- 提交预检的生成目录集合增加 `out`，并对 `*.tsbuildinfo` 做后缀判断。
- 回归测试同时覆盖 `out/index.html` 与 `tsconfig.tsbuildinfo` 不进入暂存、不被业务行数统计。

## 恢复顺序

1. 删除带脏 A/B commit 的远端仓库。
2. 保留候选工作区内容，把本地映射候选重置到初始快照；不要从远端脏 commit 继承。
3. 重新应用排除规则，分别生成本地产物 commit，父提交必须仍然是初始快照。
4. 重建 `main/A/B` 仓库并原子发布干净 A/B。
5. 重新执行语义审核、真实验证、GSB、录屏和提交，不能复用脏 commit 的验证结论。
