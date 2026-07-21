"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import type {
  AgentResponse,
  DuplicateResult,
  ExecutionStep,
  PreferredScene,
  SqlResult,
} from "@/lib/mock-agent";

type FeedbackDecision = "ACCEPT" | "EDIT_AND_ACCEPT" | "REJECT";

interface CorrectionContext {
  previousResultId: string;
  decision: "EDIT_AND_ACCEPT" | "REJECT";
  reasonCode: string;
  comment: string;
  editedSql?: string;
}

interface ChatMessage {
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

interface Conversation {
  id: string;
  title: string;
  createdAt: string;
  updatedAt: string;
  messages: ChatMessage[];
}

const STORAGE_KEY = "shuyan-zhishu-conversations-v1";

const sceneOptions: Array<{ value: PreferredScene; label: string; hint: string }> = [
  { value: "AUTO", label: "智能判断", hint: "Supervisor自动路由" },
  { value: "ASSET_DUPLICATE", label: "资产查重", hint: "语义·逻辑·血缘" },
  { value: "SQL_GENERATION", label: "SQL生成", hint: "检索·改写·校验" },
];

const starterPrompts = [
  { label: "检查重复资产", text: "请检查“客户月度交易汇总表”是否存在重复资产，并给出复用建议", scene: "ASSET_DUPLICATE" as PreferredScene },
  { label: "生成MySQL", text: "请生成近90天各分行新增客户数SQL", scene: "SQL_GENERATION" as PreferredScene },
];

function id(prefix: string) {
  return `${prefix}_${Date.now()}_${Math.random().toString(16).slice(2)}`;
}

function createConversation(): Conversation {
  const now = new Date().toISOString();
  return { id: id("CONV"), title: "新研发会话", createdAt: now, updatedAt: now, messages: [] };
}

function formatTime(value: string) {
  return new Date(value).toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" });
}

function formatDate(value: string) {
  const date = new Date(value);
  const today = new Date();
  if (date.toDateString() === today.toDateString()) return formatTime(value);
  return date.toLocaleDateString("zh-CN", { month: "2-digit", day: "2-digit" });
}

function sceneLabel(scene?: string) {
  if (scene === "ASSET_DUPLICATE") return "重复资产识别";
  if (scene === "SQL_GENERATION") return "SQL智能生成";
  if (scene === "CLARIFICATION") return "需求澄清";
  return "智能判断";
}

function sceneAsPreferred(scene: string): PreferredScene {
  if (scene === "ASSET_DUPLICATE") return "ASSET_DUPLICATE";
  if (scene === "SQL_GENERATION") return "SQL_GENERATION";
  return "AUTO";
}

export default function AgentWorkbench() {
  const [ready, setReady] = useState(false);
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [activeId, setActiveId] = useState("");
  const [draft, setDraft] = useState("");
  const [preferredScene, setPreferredScene] = useState<PreferredScene>("AUTO");
  const [running, setRunning] = useState(false);
  const [toast, setToast] = useState("");
  const [railOpen, setRailOpen] = useState(false);
  const endRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    try {
      const stored = localStorage.getItem(STORAGE_KEY);
      const parsed = stored ? JSON.parse(stored) as Conversation[] : [];
      const next = parsed.length ? parsed : [createConversation()];
      setConversations(next);
      setActiveId(next[0].id);
    } catch {
      const next = createConversation();
      setConversations([next]);
      setActiveId(next.id);
    } finally {
      setReady(true);
    }
  }, []);

  useEffect(() => {
    if (ready) localStorage.setItem(STORAGE_KEY, JSON.stringify(conversations));
  }, [conversations, ready]);

  useEffect(() => {
    if (!toast) return;
    const timer = setTimeout(() => setToast(""), 2800);
    return () => clearTimeout(timer);
  }, [toast]);

  const activeConversation = conversations.find((item) => item.id === activeId);
  const latestAssistant = useMemo(
    () => [...(activeConversation?.messages ?? [])].reverse().find((item) => item.role === "assistant"),
    [activeConversation],
  );

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
    const content = text.trim();
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
    setRunning(true);
    updateConversation(conversationId, (conversation) => ({
      ...conversation,
      title: conversation.messages.length === 0 ? content.slice(0, 22) : conversation.title,
      updatedAt: new Date().toISOString(),
      messages: [...conversation.messages, userMessage, assistantMessage],
    }));

    try {
      const response = await fetch("/api/chat/stream", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          conversationId,
          message: content,
          preferredScene: scene,
          context: { sqlDialect: "MySQL", schemaScope: ["retail_dm"] },
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
          updateMessage(conversationId, assistantId, (message) => ({
            ...message,
            content: result.summary,
            response: result,
            liveSteps: result.executionTrace,
          }));
        }
        if (eventName === "done") {
          updateMessage(conversationId, assistantId, (message) => ({ ...message, streaming: false }));
        }
      });
    } catch {
      updateMessage(conversationId, assistantId, (message) => ({
        ...message,
        streaming: false,
        content: "Mock服务暂时不可用，请稍后重试。",
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
    reasonCode = "",
    comment = "",
    editedSql?: string,
  ) {
    if (!activeConversation) return;
    const feedbackResponse = await fetch("/api/feedback", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        conversationId: activeConversation.id,
        taskId: response.taskId,
        resultId: response.resultId,
        scene: response.scene,
        decision,
        reasonCode,
        comment,
        editedSql,
        retry: decision !== "ACCEPT",
      }),
    });
    if (!feedbackResponse.ok) {
      setToast("反馈提交失败，请检查反馈说明");
      return;
    }

    updateMessage(activeConversation.id, messageId, (message) => ({ ...message, feedback: decision }));
    if (decision === "ACCEPT") {
      setToast("结果已采纳，反馈已保存");
      return;
    }

    const correction: CorrectionContext = {
      previousResultId: response.resultId,
      decision,
      reasonCode,
      comment,
      editedSql,
    };
    setToast("修改意见已回传，正在生成新版本");
    await sendMessage(
      `${decision === "EDIT_AND_ACCEPT" ? "修改意见" : "拒绝原因"}：${comment}`,
      sceneAsPreferred(response.scene),
      [correction],
    );
  }

  function updateSql(messageId: string, sql: string) {
    if (!activeConversation) return;
    updateMessage(activeConversation.id, messageId, (message) => {
      if (!message.response || message.response.result.kind !== "sql") return message;
      return {
        ...message,
        response: {
          ...message.response,
          result: { ...message.response.result, sql },
        },
      };
    });
  }

  function newConversation() {
    const conversation = createConversation();
    setConversations((current) => [conversation, ...current]);
    setActiveId(conversation.id);
    setDraft("");
    setPreferredScene("AUTO");
  }

  if (!ready) return <div className="boot-screen"><img src="/shuyan-logo.png" alt="" /><span>正在加载数研智枢…</span></div>;

  return (
    <div className="app-shell">
      <aside className="sidebar">
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
              <span>{option.value === "AUTO" ? "AI" : option.value === "ASSET_DUPLICATE" ? "重" : "SQL"}</span>
              <div><strong>{option.label}</strong><small>{option.hint}</small></div><i />
            </button>
          ))}
        </nav>

        <div className="side-label history-heading"><span>完整会话历史</span><em>{conversations.length}</em></div>
        <div className="history-list">
          {conversations.map((conversation) => (
            <button
              className={conversation.id === activeId ? "active" : ""}
              key={conversation.id}
              onClick={() => setActiveId(conversation.id)}
              type="button"
            >
              <span>{conversation.messages.some((message) => message.response?.scene === "SQL_GENERATION") ? "S" : conversation.messages.length ? "D" : "+"}</span>
              <div><strong>{conversation.title}</strong><small>{conversation.messages.length ? `${conversation.messages.length}条消息` : "等待输入需求"}</small></div>
              <time>{formatDate(conversation.updatedAt)}</time>
            </button>
          ))}
        </div>

        <div className="sidebar-bottom">
          <div className="environment"><span /><div><strong>本地演示环境</strong><small>Mock服务运行正常</small></div><em>LOCAL</em></div>
          <p>不连接生产数据库，不执行真实SQL。</p>
        </div>
      </aside>

      <main className="workspace">
        <header className="topbar">
          <div><h1>统一智能研发对话</h1><p>数据中台资产智能研发助手</p></div>
          <div className="top-actions"><span className="mock-badge">MOCK AGENT</span><span className="safe-badge">✓ 安全受控</span><button onClick={() => setRailOpen((current) => !current)} type="button">任务上下文</button></div>
        </header>

        <section className="conversation-area">
          <div className="conversation-inner">
            {!activeConversation?.messages.length && (
              <WelcomeCard onStart={(text, scene) => void sendMessage(text, scene)} />
            )}
            {activeConversation?.messages.map((message) => (
              <MessageRow
                key={message.id}
                message={message}
                onFeedback={(decision, reasonCode, comment, editedSql) => void submitFeedback(message.id, message.response!, decision, reasonCode, comment, editedSql)}
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
            <div className="suggestion-chips">
              <button onClick={() => setDraft("检查这张资产是否已经存在")} type="button">检查重复资产</button>
              <button onClick={() => setDraft("生成近90天各分行新增客户数SQL")} type="button">生成MySQL</button>
              <button onClick={() => setDraft("这个SQL应该使用哪些表和字段？")} type="button">推荐表字段</button>
            </div>
            <div className="composer">
              <textarea
                aria-label="研发需求"
                maxLength={1200}
                onChange={(event) => setDraft(event.target.value)}
                onKeyDown={(event) => {
                  if (event.key === "Enter" && !event.shiftKey) {
                    event.preventDefault();
                    void sendMessage();
                  }
                }}
                placeholder="直接描述您的资产或SQL研发需求…"
                rows={2}
                value={draft}
              />
              <div className="composer-toolbar">
                <div className="mode-switch">
                  {sceneOptions.map((option) => (
                    <button className={preferredScene === option.value ? "active" : ""} key={option.value} onClick={() => setPreferredScene(option.value)} type="button">{option.label}</button>
                  ))}
                </div>
                <div className="send-area"><span>{draft.length}/1200</span><button className="attach" title="添加上下文（Demo）" type="button">＋</button><button className="send" disabled={!draft.trim() || running} onClick={() => void sendMessage()} type="button">{running ? <i /> : "↑"}</button></div>
              </div>
            </div>
            <p>AI结果仅作为研发辅助，资产复用建议与SQL上线前均需人工确认。</p>
          </div>
        </footer>
      </main>

      <ContextRail latest={latestAssistant} open={railOpen} onClose={() => setRailOpen(false)} />
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
        <p>请直接描述您的研发需求。我会理解意图，调用重复资产识别或SQL生成能力，并在同一段会话中呈现执行依据、专业结果和反馈闭环。</p>
        <div className="welcome-actions">
          {starterPrompts.map((item) => (
            <button key={item.label} onClick={() => onStart(item.text, item.scene)} type="button"><span>{item.scene === "SQL_GENERATION" ? "SQL" : "重"}</span><div><strong>{item.label}</strong><small>{item.scene === "SQL_GENERATION" ? "检索存量脚本并生成MySQL" : "比较语义、逻辑与数据血缘"}</small></div><em>→</em></button>
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
  onFeedback: (decision: FeedbackDecision, reasonCode?: string, comment?: string, editedSql?: string) => void;
  onSqlChange: (sql: string) => void;
  onSuggestion: (text: string, scene: PreferredScene) => void;
  onToast: (message: string) => void;
}) {
  if (message.role === "user") {
    return <div className="user-row"><div className="user-bubble"><p>{message.content}</p><time>{formatTime(message.createdAt)}</time></div><div className="user-avatar">研</div></div>;
  }
  return (
    <div className="assistant-row">
      <AgentAvatar />
      <div className="assistant-content">
        {message.streaming && !message.response ? <StreamingCard message={message} /> : message.response ? (
          <ResultCard message={message} onFeedback={onFeedback} onSqlChange={onSqlChange} onSuggestion={onSuggestion} onToast={onToast} />
        ) : <div className="error-message">{message.content}</div>}
      </div>
    </div>
  );
}

function StreamingCard({ message }: { message: ChatMessage }) {
  return (
    <div className="streaming-card">
      <div className="streaming-title"><span><i /><i /><i /></span>正在流式执行任务</div>
      <p>{message.content}</p>
      <div className="streaming-steps">
        {(message.liveSteps ?? []).map((step) => <span className={step.status === "SUCCESS" ? "done" : "running"} key={step.id}>{step.status === "SUCCESS" ? "✓" : "•"} {step.label}</span>)}
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
  onFeedback: (decision: FeedbackDecision, reasonCode?: string, comment?: string, editedSql?: string) => void;
  onSqlChange: (sql: string) => void;
  onSuggestion: (text: string, scene: PreferredScene) => void;
  onToast: (message: string) => void;
}) {
  const response = message.response!;
  const tool = response.scene === "SQL_GENERATION" ? "generate_sql" : response.scene === "ASSET_DUPLICATE" ? "check_asset_duplicate" : "clarify_intent";
  return (
    <article className="result-card">
      <div className="result-heading">
        <div className="route"><span className={`scene scene-${response.scene}`}>{sceneLabel(response.scene)}</span><small>Supervisor</small><em>→</em><code>{tool}</code></div>
        <div className="version">v{response.version}</div>
        <h3>{response.summary}</h3>
        <div className="result-meta"><span>{response.taskId}</span><span>{formatTime(response.createdAt)}</span></div>
      </div>
      {response.result.kind === "sql" && <SqlResultCard result={response.result} onChange={onSqlChange} onToast={onToast} />}
      {response.result.kind === "duplicate" && <DuplicateResultCard result={response.result} />}
      {response.result.kind === "clarification" && <ClarificationCard result={response.result} onSuggestion={onSuggestion} />}
      {response.result.kind !== "clarification" && <FeedbackBar feedback={message.feedback} response={response} onSubmit={onFeedback} />}
    </article>
  );
}

function SqlResultCard({ result, onChange, onToast }: { result: SqlResult; onChange: (sql: string) => void; onToast: (message: string) => void }) {
  async function copySql() {
    try { await navigator.clipboard.writeText(result.sql); onToast("SQL已复制到剪贴板"); }
    catch { onToast("未获得剪贴板权限"); }
  }
  return (
    <div className="sql-result">
      <div className="template-strip"><span>SQL</span><div><small>推荐存量模板</small><strong>{result.template.templateName}</strong><code>{result.template.templateId}</code></div><div><strong>{Math.round(result.template.similarity * 100)}%</strong><small>模板相似度</small></div></div>
      <div className="code-editor">
        <div className="editor-head"><div><i /><i /><i /><strong>generated_query.sql</strong><span>{result.sqlDialect}</span></div><button onClick={() => void copySql()} type="button">复制SQL</button></div>
        <div className="editor-body"><div className="line-numbers">{Array.from({ length: Math.max(9, result.sql.split("\n").length) }, (_, index) => <span key={index}>{index + 1}</span>)}</div><textarea aria-label="可编辑SQL" onChange={(event) => onChange(event.target.value)} spellCheck={false} value={result.sql} /></div>
      </div>
      <div className="sql-info-grid">
        <div className="sql-explanation"><strong>生成说明</strong><p>{result.explanation}</p><div>{result.recommendedFields.map((field) => <code key={field}>{field}</code>)}</div></div>
        <div className="validation"><div><strong>校验结果</strong><span>低风险</span></div>{result.validation.items.map((item) => <p key={item.label}><i className={item.status === "PASSED" ? "passed" : "warning"}>{item.status === "PASSED" ? "✓" : "!"}</i><span><strong>{item.label}</strong><small>{item.detail}</small></span></p>)}</div>
      </div>
    </div>
  );
}

function DuplicateResultCard({ result }: { result: DuplicateResult }) {
  return (
    <div className="duplicate-result">
      <div className="reuse-banner"><span>!</span><div><strong>建议优先复用存量资产</strong><p>{result.recommendation}</p></div><em>阈值 ≥ {Math.round(result.threshold * 100)}%</em></div>
      <div className="draft-asset"><span>待检查资产</span><strong>{result.draftAsset.assetName}</strong><code>{result.draftAsset.assetId}</code><em>{result.draftAsset.grain}</em></div>
      <div className="candidate-header"><strong>相似资产候选</strong><span>按综合相似度排序 · {result.candidates.length}项</span></div>
      <div className="candidate-list">
        {result.candidates.map((candidate, index) => (
          <div className="candidate" key={candidate.assetId}><span className="rank">{index + 1}</span><div className="candidate-main"><div><strong>{candidate.assetName}</strong><code>{candidate.assetId}</code><em className={`rec rec-${candidate.recommendation}`}>{candidate.recommendation === "REUSE" ? "建议复用" : candidate.recommendation === "REVIEW" ? "人工复核" : "不构成重复"}</em></div><div className="scores"><Score label="语义" value={candidate.scores.semantic} /><Score label="逻辑" value={candidate.scores.logic} /><Score label="血缘" value={candidate.scores.lineage} /></div><p>{candidate.evidence[0]}</p></div><div className={`total ${candidate.totalScore >= result.threshold ? "high" : ""}`}><strong>{Math.round(candidate.totalScore * 100)}</strong><span>%</span><small>综合</small></div></div>
        ))}
      </div>
    </div>
  );
}

function Score({ label, value }: { label: string; value: number }) {
  return <div className="score"><div><span>{label}</span><strong>{Math.round(value * 100)}</strong></div><i><b style={{ width: `${value * 100}%` }} /></i></div>;
}

function ClarificationCard({ result, onSuggestion }: { result: Extract<AgentResponse["result"], { kind: "clarification" }>; onSuggestion: (text: string, scene: PreferredScene) => void }) {
  return <div className="clarification"><span>?</span><div><strong>{result.question}</strong><p>请选择更接近您需求的场景。</p>{result.suggestions.map((item) => <button key={item.label} onClick={() => onSuggestion(item.message, item.scene)} type="button">{item.label}<span>→</span></button>)}</div></div>;
}

function FeedbackBar({ feedback, response, onSubmit }: { feedback?: FeedbackDecision; response: AgentResponse; onSubmit: (decision: FeedbackDecision, reasonCode?: string, comment?: string, editedSql?: string) => void }) {
  const [mode, setMode] = useState<"EDIT_AND_ACCEPT" | "REJECT" | null>(null);
  const [reasonCode, setReasonCode] = useState(response.scene === "SQL_GENERATION" ? "WRONG_FIELD" : "WRONG_CONCLUSION");
  const [comment, setComment] = useState("");
  const editedSql = response.result.kind === "sql" ? response.result.sql : undefined;
  function submit() {
    if (!mode || !comment.trim()) return;
    onSubmit(mode, reasonCode, comment.trim(), editedSql);
    setMode(null);
  }
  return (
    <div className="feedback-bar">
      <div className="feedback-row"><div><strong>这个结果是否满足您的需求？</strong><span>反馈将关联当前任务与结果版本</span></div><div><button className={feedback === "ACCEPT" ? "active" : ""} onClick={() => onSubmit("ACCEPT")} type="button">✓ 采纳</button><button className={feedback === "EDIT_AND_ACCEPT" ? "active" : ""} onClick={() => setMode("EDIT_AND_ACCEPT")} type="button">✎ 修改后采纳</button><button className={feedback === "REJECT" ? "active reject" : ""} onClick={() => setMode("REJECT")} type="button">× 不满意</button></div></div>
      {mode && <div className="feedback-form"><div><strong>{mode === "EDIT_AND_ACCEPT" ? "说明需要调整的内容" : "告诉我们结果哪里不符合预期"}</strong><span>提交后将返回同一个专业智能体生成新版本</span></div><select aria-label="反馈原因" onChange={(event) => setReasonCode(event.target.value)} value={reasonCode}>{response.scene === "SQL_GENERATION" ? <><option value="WRONG_FIELD">字段选择不正确</option><option value="WRONG_LOGIC">计算逻辑不正确</option><option value="MISSING_FILTER">缺少过滤条件</option><option value="SQL_INVALID">SQL无法执行</option></> : <><option value="WRONG_CONCLUSION">复用结论不正确</option><option value="WRONG_CANDIDATE">候选资产不相关</option><option value="WRONG_SCORE">相似度评分不合理</option><option value="MISSING_ASSET">遗漏相似资产</option></>}</select><textarea aria-label="反馈说明" onChange={(event) => setComment(event.target.value)} placeholder="请输入具体原因，例如：应使用first_open_date字段统计新增客户…" rows={2} value={comment} /><button disabled={!comment.trim()} onClick={submit} type="button">提交并重新生成</button></div>}
    </div>
  );
}

function ContextRail({ latest, open, onClose }: { latest?: ChatMessage; open: boolean; onClose: () => void }) {
  const response = latest?.response;
  const steps = latest?.liveSteps ?? response?.executionTrace ?? [];
  return (
    <aside className={`context-rail ${open ? "open" : ""}`}>
      <div className="rail-top"><div><strong>任务上下文</strong><span>{latest?.streaming ? "流式执行中" : response ? "等待用户确认" : "待命"}</span></div><button onClick={onClose} type="button">×</button></div>
      <section className="rail-section"><div className="rail-title"><strong>执行轨迹</strong><span>{response ? "已完成" : latest?.streaming ? "运行中" : "待执行"}</span></div><div className="trace-list">{steps.length ? steps.map((step, index) => <div className="trace" key={step.id}><span className={step.status === "SUCCESS" ? "done" : "running"}>{step.status === "SUCCESS" ? "✓" : index + 1}</span><div><strong>{step.label}</strong><p>{step.detail}</p>{step.tool && <code>{step.tool}</code>}</div></div>) : <div className="empty-trace">输入需求后显示实时执行步骤</div>}</div></section>
      <section className="rail-section"><div className="rail-title"><strong>会话上下文</strong><button type="button">编辑</button></div><dl><div><dt>任务模式</dt><dd>{sceneLabel(response?.scene)}</dd></div><div><dt>SQL方言</dt><dd>MySQL</dd></div><div><dt>Schema范围</dt><dd>retail_dm</dd></div><div><dt>安全策略</dt><dd className="readonly"><i />只读校验</dd></div></dl></section>
      <section className="rail-section"><div className="rail-title"><strong>当前任务</strong></div>{response ? <div className="task-info"><label>任务编号</label><code>{response.taskId}</code><label>结果编号</label><code>{response.resultId}</code><label>结果状态</label><span className="review">{response.status}</span><label>结果版本</label><span>v{response.version} · Mock</span></div> : <div className="task-empty"><span>◎</span><p>提交需求后显示任务编号、结果状态和版本信息。</p></div>}</section>
      <div className="security-note"><span>盾</span><p><strong>数据安全提示</strong>当前Demo不会连接生产库，也不会执行生成的SQL。</p></div>
    </aside>
  );
}
