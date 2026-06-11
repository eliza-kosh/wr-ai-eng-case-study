import Dashboard from "../components/dashboard";
import { getDashboardData } from "../lib/db";

export const dynamic = "force-dynamic";

type PageProps = {
  searchParams?: {
    ticker?: string;
  };
};

export default async function Page({ searchParams }: PageProps) {
  const data = await getDashboardData(searchParams?.ticker);
  return <Dashboard data={data} />;
}
