import type { Metadata } from "next";
import "./globals.css";
import "./ux-overrides.css";

export const metadata: Metadata = {
  title: "数研智枢 · 数据中台资产智能研发智能体",
  description: "统一承接重复资产识别与 SQL 智能生成的本地 Mock 演示前端",
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="zh-CN">
      <body>{children}</body>
    </html>
  );
}
