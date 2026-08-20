"use client";

import {
  useEffect,
  useMemo,
  useRef,
  useState,
  type CSSProperties,
  type ChangeEvent,
} from "react";
import type {
  AgentResponse,
  AgentStatus,
  DuplicateResult,
  ExecutionStep,
  LineageNode,
  LineageResult,
  PreferredScene,
  ResultReflection,
  SqlResult,
} from "@/lib/mock-agent";
import LineageGraphView from "./LineageGraph";
import {
  normalizeSupervisorResponse,
  supervisorSteps,
} from "@/lib/supervisor-adapter";
import {
  buildReflectionRetryDraft,
  reflectionReasonLabels,
  type ReflectionRetryDraft,
} from "@/lib/reflection-retry";
import {
  restoreConversation,
  type ChatMessage,
  type Conversation,
  type FeedbackDecision,
  type StoredConversationMessage,
  type StoredConversationSummary,
} from "@/lib/conversation-history";

interface CorrectionContext {
  previousResultId: string;
  decision: "EDIT_AND_ACCEPT" | "REJECT";
  reasonCode: string;
  comment: string;
  editedSql?: string;
}

interface FeedbackApiResponse {
  feedbackId: string;
  accepted: boolean;
  retryTriggered: boolean;
  nextResultVersion: number | null;
  taskStatus: AgentResponse["status"];
  result: unknown;
  storedAt: string;
}

const STORAGE_KEY = "shuyan-zhishu-conversations-v1";
const DEFAULT_LEFT_PANEL_WIDTH = 272;
const DEFAULT_RIGHT_PANEL_WIDTH = 312;
const MIN_LEFT_PANEL_WIDTH = 236;
const MAX_LEFT_PANEL_WIDTH = 390;
const MIN_RIGHT_PANEL_WIDTH = 276;
const MAX_RIGHT_PANEL_WIDTH = 440;
const MIN_WORKSPACE_WIDTH = 620;
const COLLAPSED_PANEL_WIDTH = 56;

const sceneOptions: Array<{ value: PreferredScene; label: string; hint: string }> = [
  { value: "AUTO", label: "智能判断", hint: "Supervisor自动路由" },
  { value: "ASSET_DUPLICATE", label: "资产复用检索", hint: "语义·关键词·SQL增强" },
  { value: "SQL_GENERATION", label: "智能SQL查询", hint: "本体·映射·校验" },
  { value: "LINEAGE_PARSING", label: "血缘解析", hint: "算子·路径·口径" },
];

const starterPrompts = [
  { label: "检索可复用资产", text: "请检索“客户月度交易汇总表”可能复用的已有资产候选", scene: "ASSET_DUPLICATE" as PreferredScene },
  { label: "智能SQL查询", text: "查询近90天各分行新增客户数", scene: "SQL_GENERATION" as PreferredScene },
  { label: "解析字段血缘", text: "请解析 EAST.BD_ODS_BNWYWDBHTB::DBHTH 字段的算子级血缘", scene: "LINEAGE_PARSING" as PreferredScene },
];

function id(prefix: string) {
  return `${prefix}_${Date.now()}_${Math.random().toString(16).slice(2)}`;
}

function createConversation(): Conversation {
  const now = new Date().toISOString();
  return {
    id: id("CONV"),
    title: "新研发会话",
    createdAt: now,
    updatedAt: now,
    messages: [],
    persisted: false,
  };
}

function formatTime(value: string) {
  return new Date(value).toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" });
}

function clamp(value: number, minimum: number, maximum: number) {
  return Math.min(Math.max(value, minimum), Math.max(minimum, maximum));
}

function formatDate(value: string) {
  const date = new Date(value);
  const today = new Date();
  if (date.toDateString() === today.toDateString()) return formatTime(value);
  return date.toLocaleDateString("zh-CN", { month: "2-digit", day: "2-digit" });
}

function sceneLabel(scene?: string) {
  if (scene === "ASSET_DUPLICATE") return "资产复用检索";
  if (scene === "SQL_GENERATION") return "本体智能查询";
  if (scene === "LINEAGE_PARSING") return "算子级血缘解析";
  if (scene === "CLARIFICATION") return "需求澄清";
  return "智能判断";
}

function sceneAsPreferred(scene: string): PreferredScene {
  if (scene === "ASSET_DUPLICATE") return "ASSET_DUPLICATE";
  if (scene === "SQL_GENERATION") return "SQL_GENERATION";
  if (scene === "LINEAGE_PARSING") return "LINEAGE_PARSING";
  return "AUTO";
}

function statusLabel(status?: AgentStatus) {
  if (status === "PROCESSING") return "执行中";
  if (status === "NEED_MORE_INFORMATION") return "等待用户补充";
  if (status === "REVIEW_REQUIRED") return "等待人工确认";
  if (status === "FAILED") return "执行失败";
  if (status === "SUCCESS") return "已完成";
  return "待执行";
}

function stepClass(status: ExecutionStep["status"]) {
  return status === "SUCCESS" ? "done" : status.toLowerCase();
}

function stepIcon(status: ExecutionStep["status"], index?: number) {
  if (status === "SUCCESS") return "✓";
  if (status === "FAILED") return "×";
  if (status === "WAITING") return "!";
  if (status === "SKIPPED") return "–";
  return index === undefined ? "•" : index + 1;
}

function completeWithFeedback(
  response: AgentResponse,
  decision: Extract<FeedbackDecision, "ACCEPT" | "EDIT_AND_ACCEPT">,
): AgentResponse {
  if (response.executionPlan) {
    if (response.executionPlan.status === "COMPLETED") return response;
    const completedSteps = response.executionPlan.steps
      .filter((step) => step.id !== "complete")
      .map((step) => step.id === "await-review"
        ? {
            ...step,
            detail: "用户已确认并采纳当前专业结果",
            status: "SUCCESS" as const,
          }
        : step);
    completedSteps.push({
      id: "complete",
      label: "任务完成",
      detail: "专业结果已通过人工确认，任务闭环完成",
      status: "SUCCESS",
    });
    const executionPlan = {
      ...response.executionPlan,
      status: "COMPLETED" as const,
      steps: completedSteps,
    };
    return {
      ...response,
      status: "SUCCESS",
      executionPlan,
      executionTrace: executionPlan.steps,
    };
  }

  const traceWithoutCompletion = response.executionTrace.filter(
    (step) => step.id !== "complete" && step.id !== "await-review",
  );
  return {
    ...response,
    status: "SUCCESS",
    executionTrace: [
      ...traceWithoutCompletion,
      {
        id: "human-review",
        label:
          decision === "EDIT_AND_ACCEPT"
            ? "用户修改后采纳"
            : "人工确认并采纳",
        detail:
          decision === "EDIT_AND_ACCEPT"
            ? "用户已确认修改后的内容作为最终结果"
            : "用户已确认当前专业结果满足需求",
        status: "SUCCESS",
      },
      {
        id: "complete",
        label: "任务完成",
        detail: "反馈已保存，当前任务已完成",
        status: "SUCCESS",
      },
    ],
  };
}

function refreshStoredTraces(conversations: Conversation[]): Conversation[] {
  return conversations.map((conversation) => ({
    ...conversation,
    messages: conversation.messages.map((message) => {
      if (!message.response) return message;
      const executionTrace = message.response.executionPlan?.steps
        ?? supervisorSteps(
          message.response.scene,
          message.response.status,
          message.response.result,
          "AUTO",
          message.response.error?.message,
        );
      const refreshedResponse = {
        ...message.response,
        executionTrace,
      };
      const responseWithFeedback =
        message.feedback === "ACCEPT" || message.feedback === "EDIT_AND_ACCEPT"
          ? completeWithFeedback(refreshedResponse, message.feedback)
          : refreshedResponse;
      return {
        ...message,
        liveSteps: message.streaming
          ? message.liveSteps
          : responseWithFeedback.executionTrace,
        response: responseWithFeedback,
      };
    }),
  }));
}

async function synchronizeStoredTasks(
  conversations: Conversation[],
): Promise<Conversation[]> {
  const taskIds = [
    ...new Set(
      conversations.flatMap((conversation) =>
        conversation.messages.flatMap((message) =>
          message.response?.taskId ? [message.response.taskId] : []),
      ),
    ),
  ];
  const serverResults = new Map<string, unknown>();
  await Promise.all(taskIds.map(async (taskId) => {
    try {
      const response = await fetch(`/api/tasks/${encodeURIComponent(taskId)}`);
      if (response.ok) serverResults.set(taskId, await response.json());
    } catch {
      // 后端未启动时保留本地最近一次结果，避免影响离线查看。
    }
  }));
  if (!serverResults.size) return conversations;

  return conversations.map((conversation) => ({
    ...conversation,
    messages: conversation.messages.map((message) => {
      if (!message.response) return message;
      const raw = serverResults.get(message.response.taskId);
      if (!raw) return message;
      const serverResponse = normalizeSupervisorResponse(raw, {
        conversationId: conversation.id,
        message: message.content || "恢复任务状态",
        preferredScene: sceneAsPreferred(message.response.scene),
      });
      const effectiveResponse =
        message.feedback === "ACCEPT" || message.feedback === "EDIT_AND_ACCEPT"
          ? completeWithFeedback(serverResponse, message.feedback)
          : serverResponse;
      return {
        ...message,
        content: effectiveResponse.summary,
        liveSteps: effectiveResponse.executionTrace,
        response: effectiveResponse,
      };
    }),
  }));
}

async function loadDatabaseConversations(
  signal?: AbortSignal,
): Promise<Conversation[]> {
  const response = await fetch("/api/conversations", {
    headers: { Accept: "application/json" },
    cache: "no-store",
    signal,
  });
  if (!response.ok) throw new Error("history unavailable");
  const summaries = await response.json() as StoredConversationSummary[];
  const conversations = await Promise.all(summaries.map(async (summary) => {
    const messagesResponse = await fetch(
      `/api/conversations/${encodeURIComponent(summary.conversationId)}/messages`,
      {
        headers: { Accept: "application/json" },
        cache: "no-store",
        signal,
      },
    );
    if (!messagesResponse.ok) throw new Error("messages unavailable");
    const messages =
      await messagesResponse.json() as StoredConversationMessage[];
    return restoreConversation(summary, messages);
  }));
  return refreshStoredTraces(conversations);
}

export default function AgentWorkbench() {
  const [ready, setReady] = useState(false);
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [activeId, setActiveId] = useState("");
  const [draft, setDraft] = useState("");
  const [preferredScene, setPreferredScene] = useState<PreferredScene>("AUTO");
  const [lineageScript, setLineageScript] = useState("");
  const [lineageFileName, setLineageFileName] = useState("");
  const [lineageTargetField, setLineageTargetField] = useState("");
  const [composerCollapsed, setComposerCollapsed] = useState(false);
  const [running, setRunning] = useState(false);
  const [deletingHistory, setDeletingHistory] = useState<string | null>(null);
  const [toast, setToast] = useState("");
  const [railOpen, setRailOpen] = useState(false);
  const [leftPanelWidth, setLeftPanelWidth] = useState(DEFAULT_LEFT_PANEL_WIDTH);
  const [rightPanelWidth, setRightPanelWidth] = useState(DEFAULT_RIGHT_PANEL_WIDTH);
  const [leftPanelCollapsed, setLeftPanelCollapsed] = useState(false);
  const [rightPanelCollapsed, setRightPanelCollapsed] = useState(false);
  const [resizingPanel, setResizingPanel] = useState<"left" | "right" | null>(null);
  const endRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    let cancelled = false;
    const abortController = new AbortController();
    const timeout = setTimeout(() => abortController.abort(), 8_000);
    let cachedConversations: Conversation[];
    try {
      const stored = localStorage.getItem(STORAGE_KEY);
      cachedConversations = stored
        ? refreshStoredTraces(JSON.parse(stored) as Conversation[])
        : [];
    } catch {
      cachedConversations = [];
    }
    void (async () => {
      let next: Conversation[];
      try {
        const databaseConversations = await loadDatabaseConversations(
          abortController.signal,
        );
        next = databaseConversations.length
          ? databaseConversations
          : [createConversation()];
      } catch {
        const synchronized = cachedConversations.length
          ? await synchronizeStoredTasks(cachedConversations)
          : [];
        next = synchronized.length ? synchronized : [createConversation()];
      } finally {
        clearTimeout(timeout);
      }
      if (!cancelled) {
        setConversations(next);
        setActiveId(next[0].id);
        setReady(true);
      }
    })();
    return () => {
      cancelled = true;
      clearTimeout(timeout);
      abortController.abort();
    };
  }, []);

  useEffect(() => {
    if (ready) localStorage.setItem(STORAGE_KEY, JSON.stringify(conversations));
  }, [conversations, ready]);

  useEffect(() => {
    if (!toast) return;
    const timer = setTimeout(() => setToast(""), 2800);
    return () => clearTimeout(timer);
  }, [toast]);

  useEffect(() => {
    if (!resizingPanel) return;

    function resizePanel(event: PointerEvent) {
      const viewportWidth = window.innerWidth;
      if (resizingPanel === "left") {
        const reservedRight = viewportWidth > 1250 ? rightPanelWidth : 0;
        setLeftPanelWidth(clamp(
          event.clientX,
          MIN_LEFT_PANEL_WIDTH,
          Math.min(
            MAX_LEFT_PANEL_WIDTH,
            viewportWidth - reservedRight - MIN_WORKSPACE_WIDTH,
          ),
        ));
        return;
      }
      setRightPanelWidth(clamp(
        viewportWidth - event.clientX,
        MIN_RIGHT_PANEL_WIDTH,
        Math.min(
          MAX_RIGHT_PANEL_WIDTH,
          viewportWidth - leftPanelWidth - MIN_WORKSPACE_WIDTH,
        ),
      ));
    }

    function stopResizing() {
      setResizingPanel(null);
    }

    document.body.classList.add("resizing-panels");
    window.addEventListener("pointermove", resizePanel);
    window.addEventListener("pointerup", stopResizing);
    window.addEventListener("pointercancel", stopResizing);
    return () => {
      document.body.classList.remove("resizing-panels");
      window.removeEventListener("pointermove", resizePanel);
      window.removeEventListener("pointerup", stopResizing);
      window.removeEventListener("pointercancel", stopResizing);
    };
  }, [leftPanelWidth, resizingPanel, rightPanelWidth]);

  function resizePanelWithKeyboard(
    panel: "left" | "right",
    key: string,
  ) {
    if (key !== "ArrowLeft" && key !== "ArrowRight") return;
    const direction = key === "ArrowRight" ? 1 : -1;
    if (panel === "left") {
      setLeftPanelWidth((width) => clamp(
        width + direction * 16,
        MIN_LEFT_PANEL_WIDTH,
        MAX_LEFT_PANEL_WIDTH,
      ));
      return;
    }
    setRightPanelWidth((width) => clamp(
      width - direction * 16,
      MIN_RIGHT_PANEL_WIDTH,
      MAX_RIGHT_PANEL_WIDTH,
    ));
  }

  const activeConversation = conversations.find((item) => item.id === activeId);
  const latestAssistant = useMemo(
    () => [...(activeConversation?.messages ?? [])].reverse().find((item) => item.role === "assistant"),
    [activeConversation],
  );
  const isLineageMode = preferredScene === "LINEAGE_PARSING";
  const lineageReady = Boolean(lineageTargetField.trim() && lineageScript.trim());
  const canSend = !running && (isLineageMode ? lineageReady : Boolean(draft.trim()));

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [activeConversation?.messages.length, latestAssistant?.liveSteps?.length, running]);

  function updateConversation(conversationId: string, updater: (item: Conversation) => Conversation) {
    setConversations((current) => current.map((item) => item.id === conversationId ? updater(item) : item));
  }

  function updateMessage(conversationId: string, messageId: string, updater: (item: ChatMessage) => ChatMessage) {
    updateConversation(conversationId, (conversation) => ({
      ...conversation,
      updatedAt: new Date().toISOString(),
      messages: conversation.messages.map((message) => message.id === messageId ? updater(message) : message),
    }));
  }

  async function consumeSse(response: Response, onEvent: (eventName: string, payload: unknown) => void) {
    if (!response.body) throw new Error("stream unavailable");
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";

    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const blocks = buffer.split("\n\n");
      buffer = blocks.pop() ?? "";

      for (const block of blocks) {
        let eventName = "message";
        let data = "";
        for (const line of block.split("\n")) {
          if (line.startsWith("event:")) eventName = line.slice(6).trim();
          if (line.startsWith("data:")) data += line.slice(5).trim();
        }
        if (data) onEvent(eventName, JSON.parse(data) as unknown);
      }
    }
  }

  async function sendMessage(
    text = draft,
    scene = preferredScene,
    correctionContext: CorrectionContext[] = [],
  ) {
    let content = text.trim();
    if (scene === "LINEAGE_PARSING") {
      if (!lineageScript.trim() || !lineageTargetField.trim()) {
        setToast("请填写目标字段，并粘贴或上传 SQL 脚本");
        return;
      }
      if (!content) {
        content = `请解析目标字段 ${lineageTargetField.trim()} 的算子级血缘`;
      }
    }
    if (!content || running || !activeConversation) return;
    const conversationId = activeConversation.id;
    const userMessage: ChatMessage = {
      id: id("MSG_USER"),
      role: "user",
      content,
      createdAt: new Date().toISOString(),
    };
    const assistantId = id("MSG_AGENT");
    const assistantMessage: ChatMessage = {
      id: assistantId,
      role: "assistant",
      content: "正在理解需求并准备专业能力…",
      createdAt: new Date().toISOString(),
      streaming: true,
      liveSteps: [],
    };

    setDraft("");
    if (scene === "LINEAGE_PARSING") setComposerCollapsed(true);
    setRunning(true);
    updateConversation(conversationId, (conversation) => ({
      ...conversation,
      title: conversation.messages.length === 0 ? content.slice(0, 22) : conversation.title,
      updatedAt: new Date().toISOString(),
      messages: [...conversation.messages, userMessage, assistantMessage],
    }));

    try {
      let context: Record<string, unknown> = { sqlDialect: "PostgreSQL", schemaScope: [] };
      if (scene === "LINEAGE_PARSING") {
        const uploadResponse = await fetch("/api/lineage/scripts", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            fileName: lineageFileName || "regulatory_report.sql",
            content: lineageScript,
            dialect: "Oracle",
            metadataVersion: "meta_2026_07",
          }),
        });
        if (!uploadResponse.ok) throw new Error("脚本上传失败，请稍后重试");
        const uploaded = await uploadResponse.json() as { scriptId: string };
        context = {
          sqlDialect: "Oracle",
          schemaScope: [],
          scriptId: uploaded.scriptId,
          targetField: lineageTargetField.trim(),
        };
      }

      const response = await fetch("/api/chat/stream", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          conversationId,
          message: content,
          preferredScene: scene,
          context,
          correctionContext,
        }),
      });
      if (!response.ok) throw new Error("request failed");

      await consumeSse(response, (eventName, payload) => {
        if (eventName === "task") {
          const task = payload as { taskId: string };
          updateMessage(conversationId, assistantId, (message) => ({ ...message, taskId: task.taskId }));
        }
        if (eventName === "step") {
          const step = payload as ExecutionStep;
          updateMessage(conversationId, assistantId, (message) => {
            const steps = [...(message.liveSteps ?? [])];
            const index = steps.findIndex((item) => item.id === step.id);
            if (index >= 0) steps[index] = step;
            else steps.push(step);
            return { ...message, content: step.detail, liveSteps: steps };
          });
        }
        if (eventName === "result") {
          const result = payload as AgentResponse;
          updateConversation(conversationId, (conversation) => ({
            ...conversation,
            persisted: true,
          }));
          updateMessage(conversationId, assistantId, (message) => ({
            ...message,
            content: result.summary,
            response: result,
            liveSteps: message.liveSteps?.length
              ? message.liveSteps
              : result.executionTrace,
          }));
        }
        if (eventName === "error") {
          const error = payload as { message?: string };
          updateMessage(conversationId, assistantId, (message) => ({
            ...message,
            content: error.message || "总控服务调用失败。",
          }));
        }
        if (eventName === "done") {
          updateMessage(conversationId, assistantId, (message) => ({ ...message, streaming: false }));
        }
      });
    } catch (error) {
      updateMessage(conversationId, assistantId, (message) => ({
        ...message,
        streaming: false,
        content:
          error instanceof Error && error.message !== "request failed"
            ? error.message
            : "总控服务暂时不可用，请确认后端已启动。",
      }));
      setToast("本次调用失败，请稍后重试");
    } finally {
      setRunning(false);
    }
  }

  async function submitFeedback(
    messageId: string,
    response: AgentResponse,
    decision: FeedbackDecision,
    reasonCodes: string[] = [],
    comment = "",
    editedSql?: string,
  ): Promise<boolean> {
    if (!activeConversation) return false;
    const conversationId = activeConversation.id;
    const feedbackResponse = await fetch("/api/feedback", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        conversationId,
        taskId: response.taskId,
        resultId: response.resultId,
        scene: response.scene,
        decision,
        reasonCodes,
        comment,
        editedContent: editedSql,
        retry: decision === "REJECT",
      }),
    });
    if (!feedbackResponse.ok) {
      const error = await feedbackResponse.json().catch(() => null) as {
        message?: string;
      } | null;
      setToast(error?.message || "反馈提交失败，请检查反馈说明");
      return false;
    }

    const payload = await feedbackResponse.json() as FeedbackApiResponse;
    const normalizedResult = payload.result
      ? normalizeSupervisorResponse(payload.result, {
          conversationId,
          message: comment || "处理用户反馈",
          preferredScene: sceneAsPreferred(response.scene),
        })
      : null;

    if (decision === "ACCEPT" || decision === "EDIT_AND_ACCEPT") {
      const acceptedResponse = completeWithFeedback(
        normalizedResult || {
          ...response,
          status: "SUCCESS",
          summary:
            decision === "EDIT_AND_ACCEPT"
              ? "用户已修改并采纳当前结果"
              : "用户已确认并采纳当前结果",
          executionTrace: supervisorSteps(
            response.scene,
            "SUCCESS",
            response.result,
            sceneAsPreferred(response.scene),
          ),
        },
        decision,
      );
      updateMessage(conversationId, messageId, (message) => ({
        ...message,
        content: acceptedResponse.summary,
        feedback: decision,
        liveSteps: acceptedResponse.executionTrace,
        response: acceptedResponse,
      }));
      setToast(
        decision === "EDIT_AND_ACCEPT"
          ? "修改后的结果已采纳，任务已完成"
          : "结果已采纳，任务已完成",
      );
      return true;
    }

    if (payload.retryTriggered && normalizedResult) {
      const now = new Date().toISOString();
      setConversations((current) => current.map((conversation) => {
        if (conversation.id !== conversationId) return conversation;
        return {
          ...conversation,
          updatedAt: now,
          messages: [
            ...conversation.messages.map((message) =>
              message.id === messageId
                ? { ...message, feedback: decision }
                : message),
            {
              id: id("MSG"),
              role: "user",
              content: `不满意反馈：${comment}`,
              createdAt: now,
            },
            {
              id: id("MSG"),
              role: "assistant",
              content: normalizedResult.summary,
              createdAt: normalizedResult.createdAt,
              taskId: normalizedResult.taskId,
              liveSteps: normalizedResult.executionTrace,
              response: normalizedResult,
            },
          ],
        };
      }));
      setToast(`反馈已保存，已生成同任务 v${normalizedResult.version}`);
      return true;
    }

    updateMessage(conversationId, messageId, (message) => ({
      ...message,
      feedback: decision,
    }));
    setToast("不满意反馈已保存");
    return true;
  }

  function updateSql(messageId: string, sql: string) {
    if (!activeConversation) return;
    updateMessage(activeConversation.id, messageId, (message) => {
      if (!message.response?.result || message.response.result.kind !== "sql") return message;
      return {
        ...message,
        response: {
          ...message.response,
          result: { ...message.response.result, sql },
        },
      };
    });
  }

  function handleScriptFile(event: ChangeEvent<HTMLInputElement>) {
    const file = event.target.files?.[0];
    if (!file) return;
    setLineageFileName(file.name);
    const reader = new FileReader();
    reader.onload = () => {
      setLineageScript(String(reader.result ?? ""));
      setToast("脚本已读取，填写目标字段后发送即可解析");
    };
    reader.readAsText(file);
  }

  function newConversation() {
    const conversation = createConversation();
    setConversations((current) => [conversation, ...current]);
    setActiveId(conversation.id);
    setDraft("");
    setPreferredScene("AUTO");
  }

  async function deleteConversation(conversation: Conversation) {
    if (running || deletingHistory) return;
    const confirmed = window.confirm(
      `确认删除会话“${conversation.title}”吗？\n\n该会话将从您的历史列表隐藏，任务结果和审计记录仍会保留。`,
    );
    if (!confirmed) return;

    setDeletingHistory(conversation.id);
    try {
      if (conversation.persisted !== false) {
        const response = await fetch(
          `/api/conversations/${encodeURIComponent(conversation.id)}`,
          { method: "DELETE" },
        );
        if (!response.ok) throw new Error("delete failed");
      }

      const remaining = conversations.filter(
        (item) => item.id !== conversation.id,
      );
      const next = remaining.length ? remaining : [createConversation()];
      setConversations(next);
      if (activeId === conversation.id) setActiveId(next[0].id);
      setToast("会话已从历史列表删除，审计记录仍保留");
    } catch {
      setToast("删除会话失败，请确认总控服务已启动");
    } finally {
      setDeletingHistory(null);
    }
  }

  async function clearConversationHistory() {
    if (running || deletingHistory) return;
    const confirmed = window.confirm(
      "确认清空全部会话历史吗？\n\n会话将从您的历史列表隐藏，任务结果和审计记录仍会保留。",
    );
    if (!confirmed) return;

    setDeletingHistory("all");
    try {
      const response = await fetch("/api/conversations", {
        method: "DELETE",
      });
      if (!response.ok) throw new Error("clear failed");
      const next = createConversation();
      setConversations([next]);
      setActiveId(next.id);
      setDraft("");
      setToast("会话历史已清空，审计记录仍保留");
    } catch {
      setToast("清空会话历史失败，请确认总控服务已启动");
    } finally {
      setDeletingHistory(null);
    }
  }

  if (!ready) return <div className="boot-screen"><img src="/shuyan-logo.png" alt="" /><span>正在加载数研智枢…</span></div>;

  const panelStyle = {
    "--sidebar-width": `${leftPanelCollapsed ? COLLAPSED_PANEL_WIDTH : leftPanelWidth}px`,
    "--rail-width": `${rightPanelCollapsed ? COLLAPSED_PANEL_WIDTH : rightPanelWidth}px`,
  } as CSSProperties;

  return (
    <div className={`app-shell${leftPanelCollapsed ? " left-panel-collapsed" : ""}${rightPanelCollapsed ? " right-panel-collapsed" : ""}`} style={panelStyle}>
      <aside className={`sidebar${leftPanelCollapsed ? " collapsed" : ""}`}>
        <button
          aria-label={leftPanelCollapsed ? "展开左侧栏" : "收起左侧栏"}
          className="panel-collapse-button sidebar-panel-toggle"
          onClick={() => setLeftPanelCollapsed((current) => !current)}
          title={leftPanelCollapsed ? "展开左侧栏" : "收起左侧栏"}
          type="button"
        >
          <span className="panel-collapse-icon" />
        </button>
        <div className="brand">
          <img src="/shuyan-logo.png" alt="数研智枢标志" />
          <div><strong>数研智枢</strong><span>DATA ASSET COPILOT</span></div>
        </div>
        <button className="new-chat" onClick={newConversation} type="button"><span>＋</span>新建研发会话<kbd>Ctrl K</kbd></button>

        <div className="side-label">专业能力</div>
        <nav className="capability-list" aria-label="专业能力">
          {sceneOptions.map((option) => (
            <button
              className={preferredScene === option.value ? "active" : ""}
              key={option.value}
              onClick={() => setPreferredScene(option.value)}
              type="button"
            >
              <span>{option.value === "AUTO" ? "AI" : option.value === "ASSET_DUPLICATE" ? "重" : option.value === "LINEAGE_PARSING" ? "源" : "SQL"}</span>
              <div><strong>{option.label}</strong><small>{option.hint}</small></div><i />
            </button>
          ))}
        </nav>

        <div className="side-label history-heading">
          <span>完整会话历史 <em>{conversations.length}</em></span>
          <button
            aria-label="清空会话历史"
            className="clear-history"
            disabled={running || deletingHistory !== null}
            onClick={() => void clearConversationHistory()}
            title="清空会话历史"
            type="button"
          >
            清空
          </button>
        </div>
        <div className="history-list">
          {conversations.map((conversation) => (
            <div
              className={`history-item${conversation.id === activeId ? " active" : ""}`}
              key={conversation.id}
            >
              <button
                aria-current={conversation.id === activeId ? "page" : undefined}
                className="history-open"
                onClick={() => setActiveId(conversation.id)}
                type="button"
              >
                <span>{conversation.messages.some((message) => message.response?.scene === "SQL_GENERATION") ? "S" : conversation.messages.some((message) => message.response?.scene === "LINEAGE_PARSING") ? "源" : conversation.messages.length ? "D" : "+"}</span>
                <div><strong>{conversation.title}</strong><small>{conversation.messages.length ? `${conversation.messages.length}条消息` : "等待输入需求"}</small></div>
                <time>{formatDate(conversation.updatedAt)}</time>
              </button>
              <button
                aria-label={`删除会话：${conversation.title}`}
                className="history-delete"
                disabled={running || deletingHistory !== null}
                onClick={() => void deleteConversation(conversation)}
                title="删除会话"
                type="button"
              >
                ×
              </button>
            </div>
          ))}
        </div>

        <div className="sidebar-bottom">
          <div className="user-profile">
            <span className="profile-avatar">研</span>
            <div>
              <strong>研发体验用户</strong>
              <small>数据研发工程师</small>
            </div>
            <em>演示账号</em>
          </div>
          <div className="profile-meta"><span>数据中台 · 资产研发组</span><i>在线</i></div>
        </div>
      </aside>

      {!leftPanelCollapsed && (
        <button
          aria-label="调整左侧栏宽度"
          aria-orientation="vertical"
          aria-valuemax={MAX_LEFT_PANEL_WIDTH}
          aria-valuemin={MIN_LEFT_PANEL_WIDTH}
          aria-valuenow={leftPanelWidth}
          className={`panel-resizer left-resizer${resizingPanel === "left" ? " active" : ""}`}
          onKeyDown={(event) => resizePanelWithKeyboard("left", event.key)}
          onPointerDown={(event) => {
            event.preventDefault();
            setResizingPanel("left");
          }}
          role="separator"
          title="拖动调整会话侧栏宽度"
          type="button"
        />
      )}

      <main className="workspace">
        <section className="conversation-area">
          <div className="conversation-inner">
            {!activeConversation?.messages.length && (
              <WelcomeCard onStart={(text, scene) => void sendMessage(text, scene)} />
            )}
            {activeConversation?.messages.map((message) => (
              <MessageRow
                key={message.id}
                message={message}
                onFeedback={(decision, reasonCodes, comment, editedSql) => submitFeedback(message.id, message.response!, decision, reasonCodes, comment, editedSql)}
                onSqlChange={(sql) => updateSql(message.id, sql)}
                onSuggestion={(text, scene) => void sendMessage(text, scene)}
                onToast={setToast}
              />
            ))}
            <div ref={endRef} />
          </div>
        </section>

        <footer className="composer-zone">
          <div className="composer-wrap">
            {(!isLineageMode || !composerCollapsed) && (
              <div className="suggestion-chips">
                <button onClick={() => setDraft("检索这张资产可能复用的已有资产候选")} type="button">检索可复用资产</button>
                <button onClick={() => setDraft("查询近90天各分行新增客户数")} type="button">智能SQL查询</button>
                <button onClick={() => setDraft("这个SQL应该使用哪些表和字段？")} type="button">推荐表字段</button>
              </div>
            )}
            <div className={`composer${isLineageMode ? " lineage-mode" : ""}${composerCollapsed && isLineageMode ? " collapsed" : ""}`}>
              {isLineageMode && (
                <div className="lineage-composer">
                  <div className="lineage-composer-head">
                    <div>
                      <strong>血缘解析输入</strong>
                      <span>{composerCollapsed ? lineageTargetField.trim() || "尚未填写目标字段" : "填写目标字段和 SQL 脚本后即可直接提交"}</span>
                    </div>
                    <div className="lineage-head-actions">
                      <span className={lineageReady ? "ready" : ""}>{running ? "解析中" : lineageReady ? "✓ 已就绪" : "必填项"}</span>
                      <button
                        aria-expanded={!composerCollapsed}
                        aria-label={composerCollapsed ? "展开血缘输入框" : "收起血缘输入框"}
                        onClick={() => setComposerCollapsed((current) => !current)}
                        type="button"
                      >
                        <b>{composerCollapsed ? "⌄" : "⌃"}</b>
                        {composerCollapsed ? "展开" : "收起"}
                      </button>
                    </div>
                  </div>
                  {!composerCollapsed && (
                    <>
                      <div className="lineage-composer-row">
                        <label className="lineage-target">
                          <span>目标字段 <b>*</b></span>
                          <input
                            aria-label="目标字段"
                            onChange={(event) => setLineageTargetField(event.target.value)}
                            placeholder="例如 EAST.BD_ODS_BNWYWDBHTB::DBHTH"
                            value={lineageTargetField}
                          />
                        </label>
                        <label className="lineage-upload">
                          <input accept=".sql,.txt" hidden onChange={handleScriptFile} type="file" />
                          <span aria-hidden="true">⇧</span>
                          <em>{lineageFileName || "上传 SQL/TXT"}</em>
                        </label>
                      </div>
                      <label className="lineage-script-field">
                        <span>SQL 脚本 <b>*</b></span>
                        <textarea
                          aria-label="监管报送脚本"
                          onChange={(event) => setLineageScript(event.target.value)}
                          placeholder="在此粘贴监管报送 SQL/TXT 脚本，支持超过 50K 的长文本…"
                          rows={5}
                          value={lineageScript}
                        />
                      </label>
                    </>
                  )}
                </div>
              )}
              {(!isLineageMode || !composerCollapsed) && (
                <>
                  <textarea
                    aria-label="研发需求"
                    className={isLineageMode ? "lineage-request" : ""}
                    maxLength={1200}
                    onChange={(event) => setDraft(event.target.value)}
                    onKeyDown={(event) => {
                      if (event.key === "Enter" && !event.shiftKey) {
                        event.preventDefault();
                        void sendMessage();
                      }
                    }}
                    placeholder={isLineageMode ? "补充解析要求（可选），例如：标注 CASE WHEN 条件并展开全部来源…" : "直接描述您的资产或SQL研发需求…"}
                    rows={2}
                    value={draft}
                  />
                  <div className="composer-toolbar">
                    <div className="mode-switch">
                      {sceneOptions.map((option) => (
                        <button className={preferredScene === option.value ? "active" : ""} key={option.value} onClick={() => setPreferredScene(option.value)} type="button">{option.label}</button>
                      ))}
                    </div>
                    <div className="send-area">
                      <span>{isLineageMode ? (lineageReady ? "字段与脚本将随请求发送" : "请补全目标字段与 SQL 脚本") : `${draft.length}/1200`}</span>
                      {!isLineageMode && <button className="attach" title="添加上下文（Demo）" type="button">＋</button>}
                      <button
                        aria-label={isLineageMode ? "开始血缘解析" : "发送消息"}
                        className={`send${isLineageMode ? " lineage-send" : ""}`}
                        disabled={!canSend}
                        onClick={() => void sendMessage()}
                        type="button"
                      >
                        {running ? <i /> : isLineageMode ? <><span>开始解析</span><b>↑</b></> : "↑"}
                      </button>
                    </div>
                  </div>
                </>
              )}
            </div>
            <p>AI结果仅作为研发辅助，资产复用建议与SQL上线前均需人工确认。</p>
          </div>
        </footer>
      </main>

      {!rightPanelCollapsed && (
        <button
          aria-label="调整右侧栏宽度"
          aria-orientation="vertical"
          aria-valuemax={MAX_RIGHT_PANEL_WIDTH}
          aria-valuemin={MIN_RIGHT_PANEL_WIDTH}
          aria-valuenow={rightPanelWidth}
          className={`panel-resizer right-resizer${resizingPanel === "right" ? " active" : ""}`}
          onKeyDown={(event) => resizePanelWithKeyboard("right", event.key)}
          onPointerDown={(event) => {
            event.preventDefault();
            setResizingPanel("right");
          }}
          role="separator"
          title="拖动调整任务上下文栏宽度"
          type="button"
        />
      )}

      <ContextRail
        collapsed={rightPanelCollapsed}
        latest={latestAssistant}
        onClose={() => setRailOpen(false)}
        onToggle={() => setRightPanelCollapsed((current) => !current)}
        open={railOpen}
      />
      {toast && <div className="toast"><span>✓</span>{toast}</div>}
    </div>
  );
}

function WelcomeCard({ onStart }: { onStart: (text: string, scene: PreferredScene) => void }) {
  return (
    <div className="assistant-row welcome-row">
      <AgentAvatar />
      <div className="welcome-card">
        <div className="online-label"><span /> SUPERVISOR ONLINE</div>
        <h2>您好，我是数研智枢</h2>
        <p>请直接描述您的研发需求。我会理解意图，调用资产复用检索或本体智能查询能力，并在同一段会话中呈现执行依据、专业结果和反馈闭环。</p>
        <div className="welcome-actions">
          {starterPrompts.map((item) => (
            <button key={item.label} onClick={() => onStart(item.text, item.scene)} type="button"><span>{item.scene === "SQL_GENERATION" ? "SQL" : "检"}</span><div><strong>{item.label}</strong><small>{item.scene === "SQL_GENERATION" ? "基于已发布本体生成PostgreSQL查询" : "语义与可选SQL增强召回，候选需复核"}</small></div><em>→</em></button>
          ))}
        </div>
        <div className="welcome-tip"><span>i</span>需求不明确时，我会先请您确认，不会随意调用专业服务。</div>
      </div>
    </div>
  );
}

function AgentAvatar() {
  return <div className="agent-avatar"><img src="/shuyan-logo.png" alt="" /></div>;
}

function MessageRow({
  message,
  onFeedback,
  onSqlChange,
  onSuggestion,
  onToast,
}: {
  message: ChatMessage;
  onFeedback: (decision: FeedbackDecision, reasonCodes?: string[], comment?: string, editedSql?: string) => Promise<boolean>;
  onSqlChange: (sql: string) => void;
  onSuggestion: (text: string, scene: PreferredScene) => void;
  onToast: (message: string) => void;
}) {
  if (message.role === "user") {
    async function copyUserMessage() {
      try {
        await navigator.clipboard.writeText(message.content);
        onToast("用户输入已复制，可粘贴后修改");
      } catch {
        onToast("复制失败，请手动选择文本");
      }
    }

    return (
      <div className="user-row">
        <div className="user-bubble">
          <p>{message.content}</p>
          <div className="user-message-meta">
            <button
              aria-label="复制这条用户输入"
              onClick={() => void copyUserMessage()}
              title="复制用户输入"
              type="button"
            >
              <span>⧉</span>复制
            </button>
            <time>{formatTime(message.createdAt)}</time>
          </div>
        </div>
        <div className="user-avatar">研</div>
      </div>
    );
  }
  return (
    <div className="assistant-row">
      <AgentAvatar />
      <div className="assistant-content">
        {message.streaming && (
          <StreamingCard message={message} />
        )}
        {message.response ? (
          <ResultCard message={message} onFeedback={onFeedback} onSqlChange={onSqlChange} onSuggestion={onSuggestion} onToast={onToast} />
        ) : !message.streaming ? <div className="error-message">{message.content}</div> : null}
      </div>
    </div>
  );
}

function StreamingCard({ message }: { message: ChatMessage }) {
  return (
    <div className="execution-progress">
      {message.streaming && (
        <div className="streaming-card">
          <div className="streaming-title"><span><i /><i /><i /></span>正在流式执行任务</div>
          <p>{message.content}</p>
        </div>
      )}
      <div className="streaming-step-list">
        {(message.liveSteps ?? []).map((step, index) => (
          <article className={`streaming-step-card ${stepClass(step.status)}`} key={step.id}>
            <i>{stepIcon(step.status)}</i>
            <div>
              <small>步骤 {index + 1}</small>
              <strong>{step.label}</strong>
              <p>{step.detail}</p>
            </div>
            <span>{step.status === "RUNNING" ? "执行中" : step.status === "SUCCESS" ? "已完成" : step.status === "WAITING" ? "等待中" : step.status === "FAILED" ? "失败" : "已跳过"}</span>
          </article>
        ))}
      </div>
    </div>
  );
}

function ResultCard({
  message,
  onFeedback,
  onSqlChange,
  onSuggestion,
  onToast,
}: {
  message: ChatMessage;
  onFeedback: (decision: FeedbackDecision, reasonCodes?: string[], comment?: string, editedSql?: string) => Promise<boolean>;
  onSqlChange: (sql: string) => void;
  onSuggestion: (text: string, scene: PreferredScene) => void;
  onToast: (message: string) => void;
}) {
  const response = message.response!;
  const [reflectionRetryRequest, setReflectionRetryRequest] =
    useState<{
      draft: ReflectionRetryDraft;
      requestId: number;
    } | null>(null);
  const availableRetryDraft = response.reflection
    ? buildReflectionRetryDraft(response.reflection)
    : null;
  const tool = response.executionPlan?.selectedTool
    ?? (response.scene === "SQL_GENERATION"
      ? "generate_sql"
      : response.scene === "ASSET_DUPLICATE"
        ? "check_asset_duplicate"
        : response.scene === "LINEAGE_PARSING"
          ? "parse_regulatory_lineage"
          : "clarify_intent");
  return (
    <article className="result-card">
      <div className="result-heading">
        <div className="route"><span className={`scene scene-${response.scene}`}>{sceneLabel(response.scene)}</span><small>Supervisor</small><em>→</em><code>{tool}</code></div>
        <div className="version">v{response.version}</div>
        <h3>{response.summary}</h3>
        <div className="result-meta"><span>{response.taskId}</span><span>{formatTime(response.createdAt)}</span></div>
      </div>
      {!response.result && <div className="error-message">{response.error?.message || response.summary}</div>}
      {response.result?.kind === "sql" && <SqlResultCard result={response.result} onChange={onSqlChange} onToast={onToast} />}
      {response.result?.kind === "duplicate" && <DuplicateResultCard result={response.result} />}
      {response.result?.kind === "lineage" && <LineageResultCard result={response.result} />}
      {response.result?.kind === "clarification" && <ClarificationCard result={response.result} onSuggestion={onSuggestion} />}
      {response.reflection && response.result && response.result.kind !== "clarification" && (
        <ReflectionCard
          onRetry={
            availableRetryDraft
            && response.status === "REVIEW_REQUIRED"
            && !message.feedback
              ? () => {
                  setReflectionRetryRequest({
                    draft: availableRetryDraft,
                    requestId: Date.now(),
                  });
                  onToast("已生成检查意见反馈草稿，确认后才会重新调用原专业智能体");
                }
              : undefined
          }
          reflection={response.reflection}
        />
      )}
      {response.result && response.result.kind !== "clarification" && (
        <FeedbackBar
          feedback={message.feedback}
          key={
            reflectionRetryRequest
              ? `${response.resultId}-${reflectionRetryRequest.requestId}`
              : response.resultId
          }
          onSubmit={onFeedback}
          response={response}
          retryDraft={reflectionRetryRequest?.draft ?? null}
        />
      )}
    </article>
  );
}

const reflectionCategoryLabels: Record<
  ResultReflection["items"][number]["category"],
  string
> = {
  METRIC: "指标",
  DIMENSION: "维度",
  GRAIN: "统计粒度",
  TIME_RANGE: "时间范围",
  FILTER: "过滤条件",
  TABLE_FIELD: "表与字段",
  ASSET_EVIDENCE: "资产证据",
  RISK: "风险",
};

function ReflectionCard({
  reflection,
  onRetry,
}: {
  reflection: ResultReflection;
  onRetry?: () => void;
}) {
  const statusLabel = reflection.status === "PASSED"
    ? "检查通过"
    : reflection.status === "WARNING"
      ? "需要确认"
      : "暂不可用";
  const riskLabel = {
    LOW: "低风险",
    MEDIUM: "中风险",
    HIGH: "高风险",
  }[reflection.riskLevel];
  return (
    <section className={`reflection-card reflection-${reflection.status.toLowerCase()}`}>
      <div className="reflection-head">
        <div><span>◇</span><div><strong>智能检查</strong><small>只读反思 · 不修改结果 · 不自动重试</small></div></div>
        <div><em>{statusLabel}</em><b>{riskLabel}</b></div>
      </div>
      <p className="reflection-summary">{reflection.summary}</p>
      {reflection.items.length > 0 && (
        <div className="reflection-items">
          {reflection.items.map((item, index) => (
            <article key={`${item.category}-${index}`}>
              <i className={item.status.toLowerCase()}>
                {item.status === "PASSED" ? "✓" : item.status === "WARNING" ? "!" : "?"}
              </i>
              <div>
                <strong>{reflectionCategoryLabels[item.category]}</strong>
                <p><span>需求</span>{item.requirement}</p>
                <p><span>观察</span>{item.observation}</p>
                {item.suggestion && <p><span>建议</span>{item.suggestion}</p>}
              </div>
            </article>
          ))}
        </div>
      )}
      {onRetry && (
        <div className="reflection-retry-action">
          <div><strong>检查意见可以作为纠错上下文</strong><small>先生成可编辑反馈草稿，不会立即调用专业Tool</small></div>
          <button onClick={onRetry} type="button">按检查意见重试</button>
        </div>
      )}
    </section>
  );
}

function SqlResultCard({ result, onChange, onToast }: { result: SqlResult; onChange: (sql: string) => void; onToast: (message: string) => void }) {
  async function copySql() {
    try { await navigator.clipboard.writeText(result.sql); onToast("SQL已复制到剪贴板"); }
    catch { onToast("未获得剪贴板权限"); }
  }
  return (
    <div className="sql-result">
      <div className="code-editor">
        <div className="editor-head"><div><i /><i /><i /><strong>generated_query.sql</strong><span>{result.sqlDialect}</span></div><button onClick={() => void copySql()} type="button">复制SQL</button></div>
        <div className="editor-body"><div className="line-numbers">{Array.from({ length: Math.max(9, result.sql.split("\n").length) }, (_, index) => <span key={index}>{index + 1}</span>)}</div><textarea aria-label="可编辑SQL" onChange={(event) => onChange(event.target.value)} spellCheck={false} value={result.sql} /></div>
      </div>
      <div className="sql-info-grid">
        <div className="sql-explanation"><strong>生成说明</strong><p>{result.explanation}</p><div>{result.recommendedFields.map((field) => <code key={field}>{field}</code>)}</div></div>
        <div className="validation"><div><strong>校验结果</strong><span>{{ LOW: "低风险", MEDIUM: "中风险", HIGH: "高风险" }[result.validation.riskLevel]}</span></div>{result.validation.items.map((item) => <p key={item.label}><i className={item.status === "PASSED" ? "passed" : "warning"}>{item.status === "PASSED" ? "✓" : "!"}</i><span><strong>{item.label}</strong><small>{item.detail}</small></span></p>)}</div>
      </div>
    </div>
  );
}

function DuplicateResultCard({ result }: { result: DuplicateResult }) {
  const hasCandidates = result.candidates.length > 0;
  return (
    <div className="duplicate-result">
      <div className="reuse-banner"><span>!</span><div><strong>{hasCandidates ? "发现可复用资产候选" : "未发现达到阈值的候选"}</strong><p>{hasCandidates ? "以下结果仅为召回候选，需补充完整资产画像并人工复核，不代表正式重复结论。" : "本次检索未命中候选，不代表已经完成全量正式判重。"}</p></div><em>召回阈值 ≥ {Math.round(result.threshold * 100)}%</em></div>
      <div className="draft-asset"><span>待检索资产</span><strong>{result.draftAsset?.assetName || "当前输入资产"}</strong><code>{result.draftAsset?.assetId || "由总控请求生成"}</code><em>{result.draftAsset?.grain || "粒度待确认"}</em></div>
      <div className="candidate-header"><strong>可复用资产候选</strong><span>按召回分排序 · {result.candidates.length}项</span></div>
      <div className="candidate-list">
        {result.candidates.map((candidate, index) => (
          <div className="candidate" key={candidate.assetId}><span className="rank">{index + 1}</span><div className="candidate-main"><div><strong>{candidate.assetName}</strong><code>{candidate.assetId}</code><em className={`rec rec-${candidate.recommendation}`}>{candidate.recommendation === "NO_DUPLICATE" ? "低相关候选" : "人工复核"}</em></div><div className="scores"><Score label="语义召回" value={candidate.scores.semantic} /><Score label="SQL增强" value={candidate.scores.logic} /></div><p>{candidate.evidence[0] || "Task1返回资产检索候选，尚未执行正式三层重复判定。"}</p></div><div className={`total ${candidate.totalScore >= result.threshold ? "high" : ""}`}><strong>{Math.round(candidate.totalScore * 100)}</strong><span>%</span><small>召回</small></div></div>
        ))}
      </div>
    </div>
  );
}

function Score({ label, value }: { label: string; value: number }) {
  return <div className="score"><div><span>{label}</span><strong>{Math.round(value * 100)}</strong></div><i><b style={{ width: `${value * 100}%` }} /></i></div>;
}

function LineageMetric({ label, value, sub }: { label: string; value: number; sub: string }) {
  return (
    <div className="lineage-metric">
      <strong>{value}</strong>
      <span>{label}</span>
      <small>{sub}</small>
    </div>
  );
}

function LineageResultCard({ result }: { result: LineageResult }) {
  const [selectedNode, setSelectedNode] = useState<LineageNode | null>(null);
  const [pathsCollapsed, setPathsCollapsed] = useState(false);
  const metrics = result.metrics;
  return (
    <div className="lineage-result">
      <div className="lineage-target-strip">
        <span>血缘</span>
        <div><small>目标字段</small><strong>{result.targetField}</strong><code>{result.scriptId}</code></div>
      </div>

      {metrics && (
        <div className="lineage-metrics">
          <LineageMetric label="来源表" sub={`${metrics.sourceFields} 个字段`} value={metrics.sourceTables} />
          <LineageMetric label="来源路径" sub="脚本内加工路径" value={metrics.sourcePaths} />
          <LineageMetric label="最大嵌套" sub="层子查询" value={metrics.maxNestingDepth} />
          <LineageMetric label="识别算子" sub={`JOIN ${metrics.joinCount} · CASE ${metrics.caseCount}`} value={metrics.operatorCount} />
        </div>
      )}

      {result.graph && result.graph.nodes.length > 0 && (
        <div className="lineage-graph-card">
          <div className="lineage-graph-head"><strong>字段血缘图</strong><span>点击节点查看加工口径</span></div>
          <LineageGraphView fileName={result.targetField} graph={result.graph} onSelectNode={setSelectedNode} />
        </div>
      )}

      {selectedNode && (
        <div className="lineage-node-detail">
          <div className="lineage-node-detail-head">
            <code>{selectedNode.type}</code>
            <strong>{selectedNode.label}</strong>
          </div>
          {selectedNode.description && <p>{selectedNode.description}</p>}
          {selectedNode.expression && <pre>{selectedNode.expression}</pre>}
          <div className="lineage-node-meta">
            {selectedNode.table && <span>表 {selectedNode.table}</span>}
            {selectedNode.sqlLineStart != null && (
              <span>第 {selectedNode.sqlLineStart}{selectedNode.sqlLineEnd != null && selectedNode.sqlLineEnd !== selectedNode.sqlLineStart ? `–${selectedNode.sqlLineEnd}` : ""} 行</span>
            )}
          </div>
        </div>
      )}

      {result.paths.length > 0 && (
        <div className={`lineage-paths${pathsCollapsed ? " collapsed" : ""}`}>
          <div className="lineage-paths-head">
            <strong>来源路径</strong>
            <div>
              <span>{result.paths.length} 条</span>
              <button
                aria-expanded={!pathsCollapsed}
                aria-label={pathsCollapsed ? "展开来源路径" : "收起来源路径"}
                onClick={() => setPathsCollapsed((current) => !current)}
                type="button"
              >
                {pathsCollapsed ? "展开" : "收起"}<b>{pathsCollapsed ? "⌄" : "⌃"}</b>
              </button>
            </div>
          </div>
          {!pathsCollapsed && result.paths.map((path, index) => (
            <div className="lineage-path" key={path.pathId}>
              <span className="path-rank">{String(index + 1).padStart(2, "0")}</span>
              <div><strong>{path.keyRules.join(" → ")}</strong><small>{path.nodeIds.length} 个血缘节点</small></div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

function ClarificationCard({ result, onSuggestion }: { result: Extract<NonNullable<AgentResponse["result"]>, { kind: "clarification" }>; onSuggestion: (text: string, scene: PreferredScene) => void }) {
  return <div className="clarification"><span>?</span><div><strong>{result.question}</strong><p>请选择更接近您需求的场景。</p>{result.suggestions.map((item) => <button key={item.label} onClick={() => onSuggestion(item.message, item.scene)} type="button">{item.label}<span>→</span></button>)}</div></div>;
}

function FeedbackBar({
  feedback,
  response,
  retryDraft,
  onSubmit,
}: {
  feedback?: FeedbackDecision;
  response: AgentResponse;
  retryDraft: ReflectionRetryDraft | null;
  onSubmit: (
    decision: FeedbackDecision,
    reasonCodes?: string[],
    comment?: string,
    editedSql?: string,
  ) => Promise<boolean>;
}) {
  const defaultReasonCode =
    response.scene === "SQL_GENERATION"
      ? "WRONG_FIELD"
      : response.scene === "LINEAGE_PARSING"
        ? "WRONG_PATH"
        : "WRONG_CANDIDATE";
  const [mode, setMode] = useState<"EDIT_AND_ACCEPT" | "REJECT" | null>(
    retryDraft ? "REJECT" : null,
  );
  const [reasonCodes, setReasonCodes] = useState<string[]>([
    ...(retryDraft?.reasonCodes.length
      ? retryDraft.reasonCodes
      : [defaultReasonCode]),
  ]);
  const [comment, setComment] = useState(retryDraft?.comment ?? "");
  const [submitting, setSubmitting] = useState(false);
  const editedSql = response.result?.kind === "sql" ? response.result.sql : undefined;

  function openMode(nextMode: "EDIT_AND_ACCEPT" | "REJECT") {
    setMode(nextMode);
    setReasonCodes([defaultReasonCode]);
    setComment("");
  }

  async function submit() {
    if (!mode || !comment.trim()) return;
    setSubmitting(true);
    try {
      const succeeded = await onSubmit(
        mode,
        reasonCodes,
        comment.trim(),
        editedSql,
      );
      if (succeeded) setMode(null);
    } finally {
      setSubmitting(false);
    }
  }
  async function accept() {
    setSubmitting(true);
    try {
      await onSubmit("ACCEPT");
    } finally {
      setSubmitting(false);
    }
  }
  if (feedback) {
    const label =
      feedback === "ACCEPT"
        ? "当前结果已人工确认并采纳"
        : feedback === "EDIT_AND_ACCEPT"
          ? "用户修改后的结果已采纳"
          : "不满意反馈已保存，已转入新版本";
    return <div className={`feedback-complete feedback-${feedback.toLowerCase()}`}><span>{feedback === "REJECT" ? "!" : "✓"}</span><strong>{label}</strong></div>;
  }
  return (
    <div className="feedback-bar">
      <div className="feedback-row"><div><strong>这个结果是否满足您的需求？</strong><span>反馈将关联当前任务与结果版本</span></div><div><button disabled={submitting} onClick={() => void accept()} type="button">✓ 采纳</button><button disabled={submitting} onClick={() => openMode("EDIT_AND_ACCEPT")} type="button">✎ 修改后采纳</button><button disabled={submitting} onClick={() => openMode("REJECT")} type="button">× 不满意</button></div></div>
      {mode && <div className="feedback-form"><div><strong>{mode === "EDIT_AND_ACCEPT" ? "说明需要调整的内容" : retryDraft && comment === retryDraft.comment ? "确认智能检查生成的反馈草稿" : "告诉我们结果哪里不符合预期"}</strong><span>{mode === "EDIT_AND_ACCEPT" ? "保存当前编辑内容并完成任务，不会重新生成" : "确认提交后才由同一个专业智能体生成新版本"}</span></div><div className="feedback-reasons"><select aria-label="反馈原因" disabled={submitting} onChange={(event) => setReasonCodes([event.target.value])} value={reasonCodes[0] || defaultReasonCode}>{response.scene === "SQL_GENERATION" ? <><option value="WRONG_FIELD">字段选择不正确</option><option value="WRONG_LOGIC">计算逻辑不正确</option><option value="MISSING_FILTER">缺少过滤条件</option><option value="SQL_INVALID">SQL无法执行</option></> : response.scene === "LINEAGE_PARSING" ? <><option value="WRONG_PATH">来源路径不完整或错误</option><option value="WRONG_OPERATOR">算子识别错误</option><option value="WRONG_EXPLANATION">业务解释偏差</option><option value="MISSING_SOURCE">遗漏来源</option></> : <><option value="WRONG_CANDIDATE">候选资产不相关</option><option value="WRONG_SCORE">召回分数不合理</option><option value="MISSING_ASSET">遗漏可复用资产</option></>}<option value="METRIC_MISMATCH">指标不一致</option><option value="DIMENSION_MISSING">维度缺失</option><option value="GRAIN_MISMATCH">统计粒度不一致</option><option value="TIME_RANGE_MISMATCH">时间范围不一致</option><option value="FILTER_MISMATCH">过滤条件不一致</option><option value="TABLE_FIELD_MISMATCH">表或字段不一致</option><option value="EVIDENCE_INCOMPLETE">候选证据不完整</option><option value="RISK_REVIEW">风险需要复核</option></select>{reasonCodes.length > 1 && <div className="reason-code-list">{reasonCodes.map((code) => <span key={code}>{reflectionReasonLabels[code] || code}</span>)}</div>}</div><textarea aria-label="反馈说明" disabled={submitting} maxLength={3000} onChange={(event) => setComment(event.target.value)} placeholder={response.scene === "ASSET_DUPLICATE" ? "请输入具体原因，例如：候选资产与当前业务口径无关…" : "请输入具体原因，例如：应使用first_open_date字段统计新增客户…"} rows={4} value={comment} /><button disabled={!comment.trim() || submitting} onClick={() => void submit()} type="button">{submitting ? "正在提交…" : mode === "EDIT_AND_ACCEPT" ? "保存并完成任务" : "确认重试"}</button></div>}
    </div>
  );
}

function ContextRail({
  latest,
  open,
  collapsed,
  onClose,
  onToggle,
}: {
  latest?: ChatMessage;
  open: boolean;
  collapsed: boolean;
  onClose: () => void;
  onToggle: () => void;
}) {
  const response = latest?.response;
  const sqlResult = response?.result?.kind === "sql" ? response.result : null;
  const schemaScope = sqlResult?.recommendedTables[0]?.split(".")[0] || "由本体解析";
  const steps = latest?.liveSteps ?? response?.executionTrace ?? [];
  const currentStatus = latest?.streaming && !response ? "PROCESSING" : response?.status;
  return (
    <aside className={`context-rail${open ? " open" : ""}${collapsed ? " collapsed" : ""}`}>
      <div className="rail-top">
        <div className="rail-heading"><strong>任务上下文</strong><span>{statusLabel(currentStatus)}</span></div>
        <div className="rail-head-actions">
          <button
            aria-label={collapsed ? "展开右侧栏" : "收起右侧栏"}
            className="panel-collapse-button rail-panel-toggle"
            onClick={onToggle}
            title={collapsed ? "展开右侧栏" : "收起右侧栏"}
            type="button"
          >
            <span className="panel-collapse-icon" />
          </button>
          <button aria-label="关闭任务上下文" className="rail-mobile-close" onClick={onClose} type="button">×</button>
        </div>
      </div>
      <section className="rail-section"><div className="rail-title"><strong>执行轨迹</strong><span>{statusLabel(currentStatus)}</span></div><div className="trace-list">{steps.length ? steps.map((step, index) => <div className="trace" key={step.id}><span className={stepClass(step.status)}>{stepIcon(step.status, index)}</span><div><strong>{step.label}</strong><p>{step.detail}</p>{step.tool && <code>{step.tool}</code>}</div></div>) : <div className="empty-trace">输入需求后显示实时执行步骤</div>}</div></section>
      <section className="rail-section"><div className="rail-title"><strong>会话上下文</strong><button type="button">编辑</button></div><dl><div><dt>任务模式</dt><dd>{sceneLabel(response?.scene)}</dd></div><div><dt>SQL方言</dt><dd>{sqlResult?.sqlDialect || "PostgreSQL"}</dd></div><div><dt>数据范围</dt><dd>{schemaScope}</dd></div><div><dt>安全策略</dt><dd className="readonly"><i />只读校验</dd></div></dl></section>
      <section className="rail-section"><div className="rail-title"><strong>当前任务</strong></div>{response ? <div className="task-info">{response.executionPlan && <><label>计划编号</label><code>{response.executionPlan.planId}</code><label>计划状态</label><span>{response.executionPlan.status}</span><label>专业调用</label><span>{response.executionPlan.constraints.professionalToolCalls} / {response.executionPlan.constraints.maxProfessionalToolCalls}</span></>}<label>任务编号</label><code>{response.taskId}</code><label>结果编号</label><code>{response.resultId}</code><label>结果状态</label><span className={`task-status status-${response.status.toLowerCase()}`}>{statusLabel(response.status)}</span><label>结果版本</label><span>v{response.version} · Supervisor</span></div> : <div className="task-empty"><span>◎</span><p>提交需求后显示任务编号、结果状态和版本信息。</p></div>}</section>
      <div className="security-note"><span>盾</span><p><strong>数据安全提示</strong>当前Demo不连接生产库，仅允许在受控演示库执行只读查询。</p></div>
    </aside>
  );
}
