import { Suspense } from "react";
import { AuthGate } from "@/components/AuthGate";
import { Workbench } from "@/components/staff/Workbench";

export const metadata = { title: "Dudu · OPC 工作台" };
export default function HomePage() {
  return (
    <Suspense fallback={<p>正在打开工作台…</p>}>
      <AuthGate>
        <Workbench />
      </AuthGate>
    </Suspense>
  );
}
