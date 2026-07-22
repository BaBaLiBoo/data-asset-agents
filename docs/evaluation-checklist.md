# Text-to-SQL Live Evaluation Fairness Checklist

本清单用于正式四组 live 运行前后的人工确认。程序事实以 `EvaluationRun` 和 CLI
`compare` 的强制公平性校验为准；清单不复制另一套判定逻辑。

- [ ] Git SHA 完全一致
- [ ] Benchmark Version 一致
- [ ] Benchmark Hash 一致
- [ ] Database Snapshot Hash 一致
- [ ] Provider 一致
- [ ] Model 一致
- [ ] Temperature 一致
- [ ] Max Output Tokens 一致
- [ ] Random Seed 一致
- [ ] Concurrency 一致
- [ ] Timeout 一致
- [ ] Ontology Version 已记录
- [ ] Bundle Hash 已记录
- [ ] SQLAsset Build 已记录
- [ ] Physical RAG Build 已记录
- [ ] Prompt Version 与 Strategy Version 已记录
- [ ] 四组各 80 条，且 Case ID 集合一致
- [ ] Smoke 与 Live 未混合
- [ ] Gold 文件在实验期间未变化
- [ ] 工作区 clean
- [ ] API Key 未进入命令、日志、导出或 Git
- [ ] 原始 Provider Response 与完整 Prompt 未提交
- [ ] 四个 Run ID 已写入结果报告和 Manifest

固定执行顺序：先用完全相同配置分别做 5-case live 预检；四组均正常后，不修改 Prompt、
模型参数、Benchmark 或 Gold，依次运行完整 80-case；最后显式传入四个 Run ID 执行
`compare`。任一关键字段不一致时停止实验并重跑受影响的全部组，不能使用
`allow_mismatch` 生成效果结论。
