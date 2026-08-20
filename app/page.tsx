import type { Metadata } from "next";
import AgentWorkbench from "./AgentWorkbench";

export const metadata: Metadata = {
  title: "统一智能研发对话｜数研智枢",
  description: "数研智枢数据中台资产智能研发助手：资产复用检索、PostgreSQL本体查询与用户反馈闭环Demo。",
};

export default function Home() {
  return <AgentWorkbench />;
}
