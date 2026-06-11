import type { Metadata } from "next";
import "./globals.css";
export const metadata: Metadata = { title: "Whale Rock Signal Research", description: "Alternative-data dashboard for overview, sources, connections, and sentiment." };
export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) { return <html lang="en"><body>{children}</body></html>; }
