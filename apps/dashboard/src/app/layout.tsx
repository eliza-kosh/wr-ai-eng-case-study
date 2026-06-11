import type { Metadata } from "next";
import "./globals.css";
export const metadata: Metadata = { title: "Signal Research", description: "Alternative-data dashboard for overview, sources, and connections." };
export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) { return <html lang="en"><body>{children}</body></html>; }
