import type { Metadata } from "next";
import "./globals.css";
import "./ux-overrides.css";

export const metadata: Metadata = {
  title: "数研智枢 · 数据中台资产智能研发智能体",
  description: "统一承接资产复用检索、PostgreSQL本体查询与血缘解析的智能研发前端",
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
