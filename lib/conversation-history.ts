import type {
  AgentResponse,
  ExecutionStep,
  PreferredScene,
} from "./mock-agent.ts";
import { normalizeSupervisorResponse } from "./supervisor-adapter.ts";

export type FeedbackDecision = "ACCEPT" | "EDIT_AND_ACCEPT" | "REJECT";

export interface ChatMessage {
  id: string;
  role: "user" | "assistant";
  content: string;
  createdAt: string;
  streaming?: boolean;
  taskId?: string;
  liveSteps?: ExecutionStep[];
  response?: AgentResponse;
  feedback?: FeedbackDecision;
}

export interface Conversation {
  id: string;
  title: string;
  createdAt: string;
  updatedAt: string;
  messages: ChatMessage[];
  persisted?: boolean;
}

export interface StoredConversationSummary {
  conversationId: string;
  userId: string;
  title: string;
  preferredScene: PreferredScene;
  messageCount: number;
  createdAt: string;
  updatedAt: string;
}

export interface StoredConversationMessage {
  messageId: string;
  conversationId: string;
  role: "USER" | "ASSISTANT" | "SYSTEM";
  content: string;
  sequenceNumber: number;
  taskId: string | null;
  resultId: string | null;
  metadata: Record<string, unknown>;
  createdAt: string;
}

function record(value: unknown): Record<string, unknown> | null {
  return value && typeof value === "object" && !Array.isArray(value)
    ? value as Record<string, unknown>
    : null;
}

function feedbackDecision(value: unknown): FeedbackDecision | undefined {
  return value === "ACCEPT"
    || value === "EDIT_AND_ACCEPT"
    || value === "REJECT"
    ? value
    : undefined;
}

export function restoreConversation(
  summary: StoredConversationSummary,
  storedMessages: StoredConversationMessage[],
): Conversation {
  const feedbackByResult = new Map<string, FeedbackDecision>();
  for (const stored of storedMessages) {
    const feedback = record(stored.metadata.feedback);
    const decision = feedbackDecision(feedback?.decision);
    if (stored.resultId && decision) {
      feedbackByResult.set(stored.resultId, decision);
    }
  }

  let latestUserRequirement = summary.title;
  const messages: ChatMessage[] = [];
  for (const stored of [...storedMessages].sort(
    (left, right) => left.sequenceNumber - right.sequenceNumber,
  )) {
    const feedback = record(stored.metadata.feedback);
    const decision = feedbackDecision(feedback?.decision);
    if (stored.role === "USER" && decision) {
      if (decision === "REJECT") {
        messages.push({
          id: stored.messageId,
          role: "user",
          content: `不满意反馈：${stored.content}`,
          createdAt: stored.createdAt,
          taskId: stored.taskId || undefined,
        });
      }
      continue;
    }

    if (stored.role === "USER") {
      latestUserRequirement = stored.content;
      messages.push({
        id: stored.messageId,
        role: "user",
        content: stored.content,
        createdAt: stored.createdAt,
        taskId: stored.taskId || undefined,
      });
      continue;
    }

    if (stored.role !== "ASSISTANT") continue;
    const rawResponse = record(stored.metadata.response);
    if (!rawResponse) continue;
    const response = normalizeSupervisorResponse(rawResponse, {
      conversationId: summary.conversationId,
      message: latestUserRequirement,
      preferredScene: summary.preferredScene,
    });
    messages.push({
      id: stored.messageId,
      role: "assistant",
      content: response.summary,
      createdAt: response.createdAt,
      taskId: response.taskId,
      liveSteps: response.executionTrace,
      response,
      feedback:
        stored.resultId
          ? feedbackByResult.get(stored.resultId)
          : undefined,
    });
  }

  return {
    id: summary.conversationId,
    title: summary.title,
    createdAt: summary.createdAt,
    updatedAt: summary.updatedAt,
    messages,
    persisted: true,
  };
}
