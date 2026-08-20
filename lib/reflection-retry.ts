import type {
  ReflectionCategory,
  ResultReflection,
} from "./mock-agent";

export interface ReflectionRetryDraft {
  reasonCodes: string[];
  comment: string;
}

const reasonCodeByCategory: Record<ReflectionCategory, string> = {
  METRIC: "METRIC_MISMATCH",
  DIMENSION: "DIMENSION_MISSING",
  GRAIN: "GRAIN_MISMATCH",
  TIME_RANGE: "TIME_RANGE_MISMATCH",
  FILTER: "FILTER_MISMATCH",
  TABLE_FIELD: "TABLE_FIELD_MISMATCH",
  ASSET_EVIDENCE: "EVIDENCE_INCOMPLETE",
  RISK: "RISK_REVIEW",
};

export const reflectionReasonLabels: Record<string, string> = {
  METRIC_MISMATCH: "指标不一致",
  DIMENSION_MISSING: "维度缺失",
  GRAIN_MISMATCH: "统计粒度不一致",
  TIME_RANGE_MISMATCH: "时间范围不一致",
  FILTER_MISMATCH: "过滤条件不一致",
  TABLE_FIELD_MISMATCH: "表或字段不一致",
  EVIDENCE_INCOMPLETE: "候选证据不完整",
  RISK_REVIEW: "风险需要复核",
};

const categoryLabels: Record<ReflectionCategory, string> = {
  METRIC: "指标",
  DIMENSION: "维度",
  GRAIN: "统计粒度",
  TIME_RANGE: "时间范围",
  FILTER: "过滤条件",
  TABLE_FIELD: "表与字段",
  ASSET_EVIDENCE: "资产证据",
  RISK: "风险",
};

function boundedText(value: string, limit: number) {
  const normalized = value.replace(/\s+/g, " ").trim();
  return normalized.length <= limit
    ? normalized
    : `${normalized.slice(0, limit)}…`;
}

export function buildReflectionRetryDraft(
  reflection: ResultReflection,
): ReflectionRetryDraft | null {
  if (reflection.status !== "WARNING") return null;
  const actionableItems = reflection.items
    .filter((item) => item.status === "WARNING" || item.status === "UNKNOWN")
    .slice(0, 8);
  if (!actionableItems.length) return null;

  const reasonCodes = [
    ...new Set(
      actionableItems.map((item) => reasonCodeByCategory[item.category]),
    ),
  ];
  const details = actionableItems.map((item, index) => {
    const parts = [
      `${index + 1}. [${categoryLabels[item.category]}]`,
      `需求：${boundedText(item.requirement, 300)}`,
      `观察：${boundedText(item.observation, 500)}`,
    ];
    if (item.suggestion.trim()) {
      parts.push(`修正建议：${boundedText(item.suggestion, 300)}`);
    }
    return parts.join("\n");
  });
  const comment = `按智能检查意见重试：\n${details.join("\n")}`;
  return {
    reasonCodes,
    comment:
      comment.length <= 3000
        ? comment
        : `${comment.slice(0, 2999)}…`,
  };
}
