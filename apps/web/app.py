import os

import httpx
import streamlit as st

API_BASE_URL = os.getenv("API_BASE_URL", "http://localhost:8000")
EXAMPLES = [
    "查询近30天各分行信用卡交易金额和交易笔数。",
    "查询最近30天各机构消费金额和流水笔数。",
]

st.set_page_config(page_title="Data Asset Agents", page_icon="🧭", layout="wide")
st.title("Data Asset Agents")
st.caption("基于本体语义层与 LangGraph 的 MiniBank Text-to-SQL 演示")

with st.sidebar:
    st.header("查询设置")
    example = st.selectbox("示例问题", EXAMPLES)
    mode = st.radio("检索模式", ["ontology", "rag", "schema"], horizontal=True)
    st.info("MVP 中 ontology 为正式链路；rag/schema 保留相同接口用于后续对照实验。")

question = st.text_area("自然语言问题", value=example, height=90)
run = st.button("执行 Text-to-SQL", type="primary", use_container_width=True)

if run:
    try:
        with st.spinner("LangGraph 正在解析、选表、生成并验证 SQL…"):
            response = httpx.post(
                f"{API_BASE_URL}/api/v1/query",
                json={"question": question, "query_mode": mode},
                timeout=30,
            )
            response.raise_for_status()
            data = response.json()
    except httpx.HTTPStatusError as exc:
        try:
            error = exc.response.json()
        except ValueError:
            error = {"detail": str(exc)}
        if error.get("status") == "unsupported":
            st.warning(f"暂不支持：{error.get('detail')}")
        else:
            st.error(f"API 请求失败：{error.get('detail', exc)}")
        st.stop()
    except httpx.HTTPError as exc:
        st.error(f"无法连接 API：{exc}")
        st.stop()

    st.subheader("执行概览")
    col1, col2, col3 = st.columns(3)
    col1.metric("置信度", f"{data['confidence']:.0%}")
    col2.metric("选择表", len(data["selected_tables"]))
    col3.metric("返回行数", (data.get("execution_result") or {}).get("row_count", 0))

    with st.expander("LangGraph 执行步骤", expanded=True):
        for index, step in enumerate(data["trace_steps"], 1):
            icon = "✅" if step["status"] == "completed" else "❌"
            st.markdown(f"{index}. {icon} **{step['node']}** — {step['summary']}")

    left, right = st.columns(2)
    with left:
        st.subheader("Semantic Query")
        st.json(data["semantic_query"])
        st.subheader("匹配业务概念")
        st.dataframe(data["matched_concepts"], use_container_width=True, hide_index=True)
        st.subheader("候选与排除")
        st.write("候选表：", data["candidate_tables"])
        st.dataframe(data["rejected_tables"], use_container_width=True, hide_index=True)
    with right:
        st.subheader("物理资产与 Join")
        st.write("最终表：", data["selected_tables"])
        st.json(data["join_plan"])
        st.subheader("认证历史 SQL")
        st.dataframe(data["historical_sql_examples"], use_container_width=True, hide_index=True)

    st.subheader("生成 SQL")
    st.code(data["generated_sql"], language="sql")
    st.subheader("校验报告")
    st.json(data["validation_report"])
    st.subheader("查询结果")
    execution = data.get("execution_result") or {}
    st.dataframe(execution.get("rows", []), use_container_width=True, hide_index=True)
    st.subheader("自然语言解释")
    st.success(data["explanation"])
