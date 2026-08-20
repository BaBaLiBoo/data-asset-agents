import type { Metadata } from "next";
import LineageDemo from "./LineageDemo";
import "./lineage-demo.css";

export const metadata: Metadata = {
  title: "算子级血缘解析｜数研智枢",
  description: "监管报送脚本算子级血缘解析工作台样例",
  openGraph: {
    title: "监管报送脚本算子级血缘解析",
    description: "穿透复杂 SQL，看清字段从哪里来、如何加工、到哪里去",
    images: ["/og-lineage.png"],
  },
};

export default function LineageDemoPage() {
  return <LineageDemo />;
}
