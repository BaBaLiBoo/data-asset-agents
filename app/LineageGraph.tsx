"use client";

import {
  useCallback,
  useEffect,
  useState,
  type CSSProperties,
} from "react";
import {
  Background,
  Controls,
  Handle,
  Position,
  ReactFlow,
  type Edge,
  type Node,
  type NodeProps,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";
// 这里用主线程版 ELK 布局；规模达到数千节点时再评估 Web Worker 或 Canvas/WebGPU 型 G6。
// 注意：不能在模块顶层 `new ELK()` —— SSR/RSC 求值时 workerd 里没有 Worker 全局，
// elkjs 的 worker 工厂会触发 "_Worker is not a constructor"。改为组件内按需创建。
import type {
  LineageEdgeRelation,
  LineageGraph,
  LineageNode,
} from "@/lib/mock-agent";

const NODE_WIDTH = 176;
const NODE_HEIGHT = 60;

const BADGES: Record<LineageNode["type"], string> = {
  TABLE: "表",
  COLUMN: "源字段",
  WHERE: "WHERE",
  JOIN: "JOIN",
  CASE: "CASE",
  AGG: "AGG",
  UNION: "UNION",
  SUBQUERY: "子查询",
  TEMP_TABLE: "临时表",
  TARGET: "目标字段",
};

const EDGE_LABELS: Record<LineageEdgeRelation, string> = {
  SOURCE: "来源",
  FILTER: "过滤",
  JOIN: "关联",
  TRANSFORM: "转换",
  AGGREGATE: "归并",
  OUTPUT: "输出",
  SUBQUERY_REF: "子查询",
};

type LineageFlowData = LineageNode & { badge: string };

const NODE_COLORS: Partial<Record<LineageNode["type"], string>> = {
  COLUMN: "#0f766e",
  WHERE: "#b07829",
  JOIN: "#386c9f",
  CASE: "#7f1d2a",
  AGG: "#6d4c8d",
  TARGET: "#7f1d2a",
};

function escapeXml(value: unknown) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&apos;");
}

function downloadFileName(value: string) {
  const normalized = value.replace(/[\\/:*?"<>|]+/g, "-").replace(/\s+/g, "-");
  return `字段血缘图-${normalized || "target"}.svg`;
}

function LineageNodeView({ data }: NodeProps) {
  const node = data as unknown as LineageFlowData;
  return (
    <div className={`lineage-flow-node lineage-flow-node-${node.type.toLowerCase()}`}>
      <Handle type="target" position={Position.Left} />
      <span className="lfn-badge">{node.badge}</span>
      <div className="lfn-body">
        <strong>{node.label}</strong>
        {node.table && <small>{node.table}</small>}
      </div>
      <Handle type="source" position={Position.Right} />
    </div>
  );
}

const nodeTypes = { lineage: LineageNodeView };

export default function LineageGraphView({
  graph,
  fileName,
  onSelectNode,
}: {
  graph: LineageGraph;
  fileName: string;
  onSelectNode?: (node: LineageNode) => void;
}) {
  const [nodes, setNodes] = useState<Node[]>([]);
  const [edges, setEdges] = useState<Edge[]>([]);
  const [layouting, setLayouting] = useState(true);

  useEffect(() => {
    let cancelled = false;
    const flowNodes: Node[] = graph.nodes.map((node) => ({
      id: node.nodeId,
      type: "lineage",
      position: { x: 0, y: 0 },
      data: {
        ...node,
        badge: BADGES[node.type] ?? node.type,
      } satisfies LineageFlowData,
      style: { width: NODE_WIDTH, height: NODE_HEIGHT } as CSSProperties,
    }));
    const flowEdges: Edge[] = graph.edges.map((edge) => ({
      id: edge.edgeId,
      source: edge.source,
      target: edge.target,
      label: EDGE_LABELS[edge.relation] ?? edge.relation,
      animated: true,
    }));

    async function layout() {
      try {
        // 延迟加载 elkjs 并只在客户端创建实例：SSR/RSC 求值阶段不会执行 useEffect，
        // 因此 workerd 缺少 Worker 全局的问题不会触发。
        const { default: ELK } = await import("elkjs/lib/elk.bundled.js");
        const instance = new ELK();
        const result = await instance.layout({
          id: "lineage-root",
          layoutOptions: {
            "elk.algorithm": "layered",
            "elk.direction": "RIGHT",
            "elk.spacing.nodeNode": "20",
            "elk.layered.spacing.nodeNodeBetweenLayers": "40",
          },
          children: flowNodes.map((node) => ({
            id: node.id,
            width: NODE_WIDTH,
            height: NODE_HEIGHT,
          })),
          edges: flowEdges.map((edge) => ({
            id: edge.id,
            sources: [edge.source],
            targets: [edge.target],
          })),
        });
        if (cancelled) return;
        const positioned = flowNodes.map((node) => {
          const child = result.children?.find((item) => item.id === node.id);
          return {
            ...node,
            position: { x: child?.x ?? 0, y: child?.y ?? 0 },
          };
        });
        setNodes(positioned);
        setEdges(flowEdges);
      } finally {
        if (!cancelled) setLayouting(false);
      }
    }
    void layout();
    return () => {
      cancelled = true;
    };
  }, [graph]);

  const handleNodeClick = useCallback(
    (_event: unknown, node: Node) => {
      const original = graph.nodes.find((item) => item.nodeId === node.id);
      if (original) onSelectNode?.(original);
    },
    [graph.nodes, onSelectNode],
  );

  const downloadSvg = useCallback(() => {
    if (!nodes.length) return;
    const padding = 48;
    const minX = Math.min(...nodes.map((node) => node.position.x));
    const minY = Math.min(...nodes.map((node) => node.position.y));
    const maxX = Math.max(...nodes.map((node) => node.position.x + NODE_WIDTH));
    const maxY = Math.max(...nodes.map((node) => node.position.y + NODE_HEIGHT));
    const width = Math.ceil(maxX - minX + padding * 2);
    const height = Math.ceil(maxY - minY + padding * 2);
    const positioned = new Map(nodes.map((node) => [node.id, node]));
    const originalNodes = new Map(graph.nodes.map((node) => [node.nodeId, node]));

    const edgeMarkup = graph.edges.map((edge) => {
      const source = positioned.get(edge.source);
      const target = positioned.get(edge.target);
      if (!source || !target) return "";
      const sourceX = source.position.x - minX + padding + NODE_WIDTH;
      const sourceY = source.position.y - minY + padding + NODE_HEIGHT / 2;
      const targetX = target.position.x - minX + padding;
      const targetY = target.position.y - minY + padding + NODE_HEIGHT / 2;
      const controlOffset = Math.max(28, Math.abs(targetX - sourceX) / 2);
      const label = EDGE_LABELS[edge.relation] ?? edge.relation;
      return `<g><path d="M ${sourceX} ${sourceY} C ${sourceX + controlOffset} ${sourceY}, ${targetX - controlOffset} ${targetY}, ${targetX} ${targetY}" fill="none" stroke="#a9b2bd" stroke-width="1.4" marker-end="url(#arrow)"/><text x="${(sourceX + targetX) / 2}" y="${(sourceY + targetY) / 2 - 5}" text-anchor="middle" fill="#64748b" font-size="10">${escapeXml(label)}</text></g>`;
    }).join("");

    const nodeMarkup = nodes.map((positionedNode) => {
      const node = originalNodes.get(positionedNode.id);
      if (!node) return "";
      const x = positionedNode.position.x - minX + padding;
      const y = positionedNode.position.y - minY + padding;
      const color = NODE_COLORS[node.type] ?? "#475569";
      const badge = BADGES[node.type] ?? node.type;
      const table = node.table ? `<text x="${x + 48}" y="${y + 43}" fill="#7b8794" font-size="9">${escapeXml(node.table)}</text>` : "";
      return `<g><rect x="${x}" y="${y}" width="${NODE_WIDTH}" height="${NODE_HEIGHT}" rx="10" fill="#ffffff" stroke="${color}" stroke-width="1.4"/><rect x="${x + 9}" y="${y + 17}" width="31" height="26" rx="6" fill="${color}"/><text x="${x + 24.5}" y="${y + 34}" text-anchor="middle" fill="#ffffff" font-size="9" font-weight="700">${escapeXml(badge)}</text><text x="${x + 48}" y="${y + 27}" fill="#334155" font-size="11" font-weight="700">${escapeXml(node.label)}</text>${table}</g>`;
    }).join("");

    const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="${width}" height="${height}" viewBox="0 0 ${width} ${height}"><defs><marker id="arrow" markerWidth="8" markerHeight="8" refX="7" refY="4" orient="auto"><path d="M0,0 L8,4 L0,8 Z" fill="#a9b2bd"/></marker><pattern id="grid" width="16" height="16" patternUnits="userSpaceOnUse"><circle cx="1" cy="1" r="1" fill="#e2e8f0"/></pattern></defs><rect width="100%" height="100%" fill="#f8fafc"/><rect width="100%" height="100%" fill="url(#grid)"/>${edgeMarkup}${nodeMarkup}</svg>`;
    const url = URL.createObjectURL(new Blob([svg], { type: "image/svg+xml;charset=utf-8" }));
    const link = document.createElement("a");
    link.href = url;
    link.download = downloadFileName(fileName);
    document.body.appendChild(link);
    link.click();
    link.remove();
    window.setTimeout(() => URL.revokeObjectURL(url), 0);
  }, [fileName, graph.edges, graph.nodes, nodes]);

  return (
    <div className="lineage-flow">
      <button
        className="lineage-download"
        disabled={layouting || !nodes.length}
        onClick={downloadSvg}
        title="下载完整字段血缘图"
        type="button"
      >
        <span>↓</span>下载 SVG
      </button>
      <ReactFlow
        edges={edges}
        fitView
        nodeTypes={nodeTypes}
        nodes={nodes}
        onNodeClick={handleNodeClick}
      >
        <Background gap={16} />
        <Controls showInteractive={false} />
      </ReactFlow>
      {layouting && <div className="lineage-flow-loading">正在计算分层布局…</div>}
    </div>
  );
}
