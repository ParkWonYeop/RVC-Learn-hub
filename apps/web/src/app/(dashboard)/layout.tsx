import { AppShell } from "@/components/app-shell";
import { dashboardDemoMode, requireCurrentUser } from "@/lib/server/auth";

export default async function DashboardLayout({ children }: { children: React.ReactNode }) {
  const user = await requireCurrentUser();
  return (
    <AppShell demoMode={dashboardDemoMode()} user={user}>
      {children}
    </AppShell>
  );
}
