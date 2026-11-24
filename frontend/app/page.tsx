import { Suspense } from "react";
import { AuthGate } from "@/components/AuthGate";
import { WorkspaceEntry } from "@/components/staff/WorkspaceEntry";

export const metadata = { title: "工作区 · Suparagent" };
export default function HomePage() {
  return (
    <Suspense fallback={<p>正在打开工作台…</p>}>
      <AuthGate>
        <WorkspaceEntry />
      </AuthGate>
    </Suspense>
  );
}
