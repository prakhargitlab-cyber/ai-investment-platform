import type { Metadata } from "next";
import "./styles.css";

export const metadata: Metadata = {
  title: "AI Investment Intelligence Platform",
  description: "Production-grade investment intelligence workspace"
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="en" data-theme="system">
      <body>{children}</body>
    </html>
  );
}
