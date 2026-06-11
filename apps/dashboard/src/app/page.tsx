import { getDashboardData } from "@/lib/db";
import Dashboard from "@/components/dashboard";
export const dynamic = "force-dynamic";
export default async function Page({ searchParams }: { searchParams?: { ticker?: string } }) { const data = await getDashboardData(searchParams?.ticker); return <Dashboard data={data} />; }
