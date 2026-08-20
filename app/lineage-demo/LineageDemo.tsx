"use client";

import Link from "next/link";
import { useMemo, useState } from "react";

type OperatorKey = "source" | "filter" | "join" | "case" | "aggregate" | "target";

const operatorDetails: Record<OperatorKey, {
  eyebrow: string;
  title: string;
  type: string;
  confidence: number;
  description: string;
  expression: string;
  lines: string;
}> = {
  source: {
    eyebrow: "来源字段",
    title: "ODS_BNWY_HTXX.HTBH",
    type: "COLUMN",
    confidence: 100,
    description: "贷款合同主表中的合同编号，作为当前路径的原始输入字段。",
    expression: "T1.HTBH",
    lines: "第 18 行",
  },
  filter: {
    eyebrow: "行级裁剪",
    title: "有效合同过滤",
    type: "WHERE",
    confidence: 98,
    description: "仅保留未删除、合同状态有效且数据日期为报送日的记录。",
    expression: "T1.DEL_FLAG = '0'\nAND T1.HTZT IN ('1', '2')\nAND T1.DATA_DT = ${BATCH_DATE}",
    lines: "第 24–27 行",
  },
  join: {
    eyebrow: "横向关联",
    title: "合同与业务明细关联",
    type: "LEFT JOIN",
    confidence: 96,
    description: "通过借据编号关联业务明细表，用于补充合同类型与历史编号。",
    expression: "LEFT JOIN ODS_BNWY_YWXX T7\n  ON T1.JJBH = T7.JJBH\n AND T7.DATA_DT = ${BATCH_DATE}",
    lines: "第 31–34 行",
  },
  case: {
    eyebrow: "业务规则映射",
    title: "合同编号口径映射",
    type: "CASE WHEN",
    confidence: 93,
    description: "银团贷款优先取主合同号，其余业务沿用合同表编号；空值统一回退至借据编号。",
    expression: "CASE\n  WHEN T7.YWLX = 'YTDK' THEN T7.ZHTH\n  WHEN T1.HTBH IS NOT NULL THEN T1.HTBH\n  ELSE T1.JJBH\nEND",
    lines: "第 52–58 行",
  },
  aggregate: {
    eyebrow: "纵向归并",
    title: "合同编号去重归并",
    type: "GROUP BY",
    confidence: 91,
    description: "按照机构和合同编号归并多笔业务记录，保留唯一监管报送口径。",
    expression: "GROUP BY JGDM, DBHTH",
    lines: "第 73–74 行",
  },
  target: {
    eyebrow: "目标字段",
    title: "EAST.BD_ODS_BNWYWDBHTB.DBHTH",
    type: "OUTPUT COLUMN",
    confidence: 97,
    description: "EAST业务委托表中的担保合同号，当前分析识别到13条来源路径。",
    expression: "DBHTH",
    lines: "第 81 行",
  },
};

const paths = [
  { id: 1, source: "ODS_BNWY_HTXX.HTBH", operators: 5, confidence: 97, tone: "green" },
  { id: 2, source: "ODS_BNWY_YWXX.ZHTH", operators: 6, confidence: 94, tone: "blue" },
  { id: 3, source: "DWD_HTGX_LS.LSHTH", operators: 8, confidence: 88, tone: "amber" },
  { id: 4, source: "ODS_BNWY_DKXX.JJBH", operators: 5, confidence: 86, tone: "slate" },
];

const sqlLines = [
  "WITH BASE_HT AS (",
  "  SELECT T1.JGDM, T1.JJBH, T1.HTBH,",
  "         T7.YWLX, T7.ZHTH",
  "  FROM ODS.ODS_BNWY_HTXX T1",
  "  LEFT JOIN ODS.ODS_BNWY_YWXX T7",
  "    ON T1.JJBH = T7.JJBH",
  "   AND T7.DATA_DT = ${BATCH_DATE}",
  "  WHERE T1.DEL_FLAG = '0'",
  "    AND T1.HTZT IN ('1', '2')",
  "), REPORT_DATA AS (",
  "  SELECT JGDM,",
  "    CASE",
  "      WHEN YWLX = 'YTDK' THEN ZHTH",
  "      WHEN HTBH IS NOT NULL THEN HTBH",
  "      ELSE JJBH",
  "    END AS DBHTH",
  "  FROM BASE_HT",
  ")",
  "SELECT JGDM, DBHTH",
  "FROM REPORT_DATA",
  "GROUP BY JGDM, DBHTH;",
];

export default function LineageDemo() {
  const [selected, setSelected] = useState<OperatorKey>("case");
  const [activePath, setActivePath] = useState(1);
  const [target, setTarget] = useState("EAST.BD_ODS_BNWYWDBHTB::DBHTH");
  const [status, setStatus] = useState("解析完成");
  const detail = operatorDetails[selected];
  const activePathData = useMemo(() => paths.find((path) => path.id === activePath) ?? paths[0], [activePath]);

  function rerun() {
    setStatus("正在解析…");
    window.setTimeout(() => setStatus("解析完成"), 900);
  }

  return (
    <main className="lineage-page">
      <header className="lineage-topbar">
        <Link className="lineage-brand" href="/">
          <span className="brand-mark">枢</span>
          <span><strong>数研智枢</strong><small>DATA ASSET INTELLIGENCE</small></span>
        </Link>
        <nav aria-label="能力导航">
          <Link href="/">资产复用</Link>
          <Link href="/">SQL生成</Link>
          <Link className="active" href="/lineage-demo">血缘解析</Link>
        </nav>
        <div className="top-actions"><span className="environment">试运行环境</span><button aria-label="打开帮助" type="button">?</button><i>数</i></div>
      </header>

      <section className="lineage-hero">
        <div>
          <div className="breadcrumb"><span>监管报送</span><b>/</b><span>脚本血缘解析</span><b>/</b><strong>分析详情</strong></div>
          <h1>监管报送脚本算子级血缘解析</h1>
          <p>穿透复杂嵌套、横向关联与业务规则，定位字段“从哪里来、经过什么加工、最终到哪里去”。</p>
        </div>
        <div className="hero-actions"><button className="ghost" type="button">导出解析报告</button><button className="primary" onClick={rerun} type="button">重新解析</button></div>
      </section>

      <section className="query-panel">
        <div className="script-file"><span className="file-icon">SQL</span><div><small>当前报送脚本</small><strong>EAST_BD_ODS_BNWYWDBHTB.sql</strong><em>48.6 KB · Oracle SQL · 更新于 10:24</em></div><button type="button">更换脚本</button></div>
        <label><span>目标字段</span><div><input onChange={(event) => setTarget(event.target.value)} value={target} /><button onClick={rerun} type="button">定位血缘</button></div></label>
      </section>

      <section className="metric-grid">
        <article><span className="metric-icon source">源</span><div><strong>38</strong><small>来源表</small></div><em>涉及 62 个字段</em></article>
        <article><span className="metric-icon path">径</span><div><strong>13</strong><small>来源路径</small></div><em>当前展示主路径</em></article>
        <article><span className="metric-icon depth">层</span><div><strong>5</strong><small>最大嵌套深度</small></div><em>含 4 层以上子查询</em></article>
        <article><span className="metric-icon operator">算</span><div><strong>126</strong><small>识别算子</small></div><em>JOIN 21 · CASE 17</em></article>
        <article className="metric-status"><span className="pulse" /><div><strong>{status}</strong><small>整体置信度 94%</small></div><em>用时 38.2s</em></article>
      </section>

      <section className="workbench-grid">
        <article className="graph-card panel-card">
          <div className="panel-title"><div><span className="title-icon">⌘</span><div><h2>字段血缘图</h2><p>目标字段：{target.replace("::", ".")}</p></div></div><div className="graph-tools"><button className="active" type="button">主路径</button><button type="button">全部路径</button><button title="缩小" type="button">−</button><button title="放大" type="button">＋</button></div></div>
          <div className="graph-stage">
            <div className="stage-labels"><span>来源层</span><span>加工层</span><span>输出层</span></div>
            <div className="graph-flow" role="group" aria-label="算子级字段血缘路径">
              <GraphNode active={selected === "source"} badge="源字段" className="source-node" onClick={() => setSelected("source")} subtitle="ODS_BNWY_HTXX" title="HTBH" />
              <FlowLine label="输入" />
              <GraphNode active={selected === "filter"} badge="WHERE" className="filter-node" onClick={() => setSelected("filter")} subtitle="有效合同裁剪" title="DEL_FLAG = '0'" />
              <FlowLine label="过滤" />
              <GraphNode active={selected === "join"} badge="LEFT JOIN" className="join-node" onClick={() => setSelected("join")} subtitle="关联 ODS_BNWY_YWXX" title="T1.JJBH = T7.JJBH" />
              <FlowLine label="补充" />
              <GraphNode active={selected === "case"} badge="CASE WHEN" className="case-node" onClick={() => setSelected("case")} subtitle="合同编号口径映射" title="YTDK → ZHTH" />
              <FlowLine label="归并" />
              <GraphNode active={selected === "aggregate"} badge="GROUP BY" className="aggregate-node" onClick={() => setSelected("aggregate")} subtitle="机构 + 合同编号" title="去重归并" />
              <FlowLine label="输出" />
              <GraphNode active={selected === "target"} badge="目标字段" className="target-node" onClick={() => setSelected("target")} subtitle="BD_ODS_BNWYWDBHTB" title="DBHTH" />
            </div>
            <div className="graph-foot"><span><i className="legend-source" />字段</span><span><i className="legend-filter" />过滤</span><span><i className="legend-join" />关联</span><span><i className="legend-rule" />业务规则</span><span><i className="legend-output" />目标</span><em>点击节点查看加工口径</em></div>
          </div>
        </article>

        <aside className="detail-card panel-card">
          <div className="panel-title compact"><div><span className="title-icon">≡</span><div><h2>算子详情</h2><p>当前选择节点</p></div></div><button type="button">定位SQL</button></div>
          <div className="detail-head"><span>{detail.eyebrow}</span><h3>{detail.title}</h3><div><code>{detail.type}</code><em>置信度 {detail.confidence}%</em></div></div>
          <div className="detail-section"><strong>口径说明</strong><p>{detail.description}</p></div>
          <div className="detail-section"><strong>SQL 表达式</strong><pre>{detail.expression}</pre></div>
          <div className="detail-meta"><span>脚本位置</span><strong>{detail.lines}</strong></div>
          <div className="detail-alert"><i>!</i><p><strong>需人工关注</strong><span>存在硬编码业务类型 “YTDK”，建议确认与一表通口径是否一致。</span></p></div>
        </aside>

        <article className="path-card panel-card">
          <div className="panel-title compact"><div><span className="title-icon">↗</span><div><h2>来源路径</h2><p>13条路径 · 按置信度排序</p></div></div><button type="button">筛选</button></div>
          <div className="path-list">
            {paths.map((path) => <button className={activePath === path.id ? "active" : ""} key={path.id} onClick={() => setActivePath(path.id)} type="button"><span className={`path-rank ${path.tone}`}>{path.id}</span><div><strong>{path.source}</strong><small>{path.operators} 个加工算子 · 路径深度 {Math.min(path.operators, 5)}</small></div><em>{path.confidence}%</em></button>)}
          </div>
          <div className="path-summary"><span>当前路径</span><strong>{activePathData.source}</strong><p>源字段 → 有效记录过滤 → 业务表关联 → 合同号映射 → 去重归并 → DBHTH</p></div>
          <button className="show-more" type="button">查看其余 9 条路径 ↓</button>
        </article>

        <article className="sql-card panel-card">
          <div className="panel-title compact"><div><span className="title-icon">&lt;/&gt;</span><div><h2>SQL口径定位</h2><p>EAST_BD_ODS_BNWYWDBHTB.sql</p></div></div><div><button type="button">复制片段</button><button type="button">展开编辑器</button></div></div>
          <div className="code-window">
            <div className="code-tabs"><span className="active">主脚本</span><span>BASE_HT</span><span>REPORT_DATA</span><em>Oracle</em></div>
            <pre>{sqlLines.map((line, index) => <span className={index >= 11 && index <= 15 ? "highlight" : ""} key={`${line}-${index}`}><i>{index + 1}</i><code>{line}</code></span>)}</pre>
          </div>
        </article>
      </section>
    </main>
  );
}

function GraphNode({ active, badge, className, onClick, subtitle, title }: { active: boolean; badge: string; className: string; onClick: () => void; subtitle: string; title: string }) {
  return <button aria-pressed={active} className={`graph-node ${className} ${active ? "active" : ""}`} onClick={onClick} type="button"><span>{badge}</span><strong>{title}</strong><small>{subtitle}</small></button>;
}

function FlowLine({ label }: { label: string }) {
  return <div className="flow-line"><span>{label}</span><i>›</i></div>;
}
