import { loadDatasetsData } from "@/lib/server/dashboard-data";
import { dashboardDemoMode } from "@/lib/server/auth";
import { DatasetDashboard } from "./dataset-dashboard";

export const metadata = { title: "데이터셋" };

export default async function DatasetsPage() {
  const datasets = await loadDatasetsData();
  return (
    <DatasetDashboard
      demo={dashboardDemoMode()}
      initialDatasets={datasets.items}
      initialTotal={datasets.total}
      initialLimitation={datasets.limitation}
    />
  );
}
