import { EmptyState } from "@/components/empty-state";
import { ListLimitNotice } from "@/components/list-limit-notice";
import { PageHeader } from "@/components/page-header";
import { requireCurrentUser, dashboardDemoMode } from "@/lib/server/auth";
import { loadAdminUsers } from "@/lib/server/admin-user-data";
import { UserAdminPanel } from "./user-admin-panel";

export const metadata = { title: "사용자 관리" };

export default async function UsersPage() {
  const currentUser = await requireCurrentUser();
  if (currentUser.role !== "admin") {
    return (
      <>
        <PageHeader
          eyebrow="ACCESS / USERS"
          title="사용자 관리"
          description="계정과 관리자 권한은 관리자 역할에서만 관리할 수 있습니다."
        />
        <section className="panel">
          <EmptyState
            title="관리자 권한이 필요합니다"
            description="관리자에게 계정 또는 권한 변경을 요청해 주세요."
          />
        </section>
      </>
    );
  }

  const users = await loadAdminUsers();
  return (
    <>
      <PageHeader
        eyebrow="ACCESS / USERS"
        title="사용자 관리"
        description="계정 생성, 역할·활성 상태 변경과 비밀번호 재설정을 행 버전으로 안전하게 관리합니다."
      />
      {users.limitation ? <ListLimitNotice limitation={users.limitation} /> : null}
      <UserAdminPanel
        currentUserId={currentUser.id}
        demoMode={dashboardDemoMode()}
        initialUsers={users.items}
      />
    </>
  );
}
