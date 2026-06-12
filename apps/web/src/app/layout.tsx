import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: {
    default: "RVC Training Orchestrator",
    template: "%s · RVC Orchestrator",
  },
  description: "다중 GPU RVC 학습 작업을 관리하고 비교하는 중앙 대시보드",
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="ko">
      <body>{children}</body>
    </html>
  );
}
