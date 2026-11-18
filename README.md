# 学生问题归纳台

学生问题归纳台用于处理本地合成教研文本，保存来源版本、文本片段和处理记录，供校内复核使用。

在此之上，归纳台把学生的课后问题归纳为主题，方便教师决定哪些概念需要补讲：问题原文、主题归属、教师处置和汇总口径都按轮次留痕，主题可以合并、拆分，历史随时可回放。

## 环境

需要 Python 3.11 或更新版本。应用运行不依赖第三方包；运行测试和构建前，安装固定版本的开发工具：`python -m pip install -r requirements-dev.txt`。

## 运行

执行 `PYTHONPATH=src python -m nlp_student_question_cluster.cli ingest fixtures/records.json` 导入演练记录，再执行 `PYTHONPATH=src python -m nlp_student_question_cluster.cli list` 查看当前文本处理结果。运行数据保存在 `var/records.json`。

## 归纳台

归纳台命令统一为 `PYTHONPATH=src python -m nlp_student_question_cluster.cli board <操作>`，状态保存在 `var/board.json`（可用 `--board` 指定其他路径）。所有变更都必须发生在开放的轮次内：

```bash
# 第一轮：登记问题、归纳主题、教师处置、汇总
board round-open --note 第一轮复盘
board theme t-mix --label 单位与公式 --keywords 单位 公式
board question q1 --text "千米和米这些单位怎么换算？"
board autocluster                      # 按主题名称与关键词自动聚类
board assign q1 t-mix                  # 教师手工指定，优先于自动聚类
board followup q1 --theme t-mix --note 安排补讲
board done f1 --note 已补讲
board aggregate                        # 按当前口径汇总并保存快照
board round-close

# 新一轮复盘：拆分主题，旧跟进仍能找到原问题
board round-open
board split t-mix --new t-unit:单位换算:单位,换算 --new t-formula:公式选择:公式
board trace q1                         # 文本修订、归属变迁、跟进与历次汇总命中
board theme-trace t-mix                # 主题血缘与逐轮统计
board aggregate                        # 新一轮按当前口径统计，不重复计数
```

其他常用操作：

- `board merge 目标 来源... --label 名称`：合并主题。同一问题在多个来源都有归属时只保留一条（手工优先），进行中的跟进改指目标主题并记录轨迹，已完成的跟进保持原样。
- `board question q1 --text "……（补充）"`：补充问题文本会提升修订号，相关归属自动进入待复核；`board autocluster` 复核自动归属，`board confirm <归属编号>` 由教师确认手工归属。已完成的跟进不随文本补充迁移，`board report` 会单独提示“完成后文本又有更新”的跟进。
- `board report`：待复核归属、进行中跟进等当前待办总览。
- `board events [--round N]`：按轮次回放事件日志。

汇总口径：只统计当前有效归属（含待复核，单独标注），主题内按问题去重，总数跨主题去重；每轮快照连同口径说明一并保存，历史轮次的汇总结果不受后续合并、拆分影响。

## 测试

执行 `python -m pytest -q` 检查文本规范化、记录处理、状态读取、本地持久化，以及归纳台的轮次追溯、归属优先级、合并拆分与汇总去重。
