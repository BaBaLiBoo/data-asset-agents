# Text-to-SQL Live Evaluation Fairness Checklist

最终实验日期：2026-07-23
实验 Git SHA：`705b017f00b0526cd244ab117ebe8cd02dfc03e7`

- [x] Git SHA 完全一致且为合法 40 位 SHA
- [x] Benchmark Version 一致
- [x] Benchmark Hash 一致：`e3045a0b12c567078aed8f80670e059062ef883583868e2e6387ea75a31ee779`
- [x] Database Snapshot Hash 一致：`8fdbc08b6f19006be80513e7d7a91882e87570dd9593ad35064cdf44ea1cb04f`
- [x] Provider 一致：DeepSeek
- [x] Model 一致：`deepseek-chat`
- [x] Temperature 一致：0
- [x] Max Output Tokens 一致：2048
- [x] Random Seed 一致：`20260716`
- [x] Concurrency 一致：1
- [x] Timeout 一致：30 s
- [x] Ontology Version 已记录且不是 `yaml-seed-*`
- [x] Bundle Hash 已记录且非空
- [x] SQLAsset Build 已记录并绑定当前 Ontology Version
- [x] Physical RAG Build 已记录于 Run Manifest
- [x] Prompt Version 与 Strategy Version 已记录
- [x] 四组各 80 条，Case ID 集合一致
- [x] Smoke 与 Live 未混用
- [x] Gold 文件在实验期间未变化
- [x] Benchmark generator 重跑后文件哈希不变
- [x] API Key 未进入命令、日志、导出或 Git
- [x] 原始 Provider Response 与完整 Prompt 未提交
- [x] 四个 Run ID 已写入结果报告和 Manifest
- [x] `compare` 返回 `warnings=[]`

正式 Run ID：

- Schema：`898801a9-686c-494c-a378-80d1d7b59f27`
- Physical RAG：`140ac26c-948e-4977-a61c-f3b3ba94c174`
- Ontology No SQLAsset：`4c917697-575c-4c03-b3ff-0917e95e6d14`
- Ontology Full：`1772b89e-0d41-4d97-aa9d-3674b3cb5aec`

预检先运行四组各 5 Case，公平性比较通过后冻结代码、Prompt、Benchmark 和配置，再
顺序完成四组 80 Case。Full Run 中的 1 个 Provider Error 保留在正式结果中，没有
选择性重跑、修改 Gold 或删除失败 Case。
