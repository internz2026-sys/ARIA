import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "ARIA — AI Business Automation Platform",
  description: "AI Agents That Run Your Business — You Just Describe It",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body className="antialiased">{children}</body>
    </html>
  );
}
