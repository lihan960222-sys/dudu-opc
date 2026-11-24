import { Suspense } from "react";
import { AuthGate } from "@/components/AuthGate";
import { Workspace } from "@/components/staff/Workspace";
import "@/components/chat/chat-shell.css";

export const metadata = {
  title: "工作区 · Suparagent",
};

// The open conversation is a query string (/chat?thread=3), not a route
// segment, so the page also works as a static export. useSearchParams needs
// the Suspense boundary for that.
export default function ChatPage() {
  return (
    <Suspense fallback={null}>
      <AuthGate>
        <Workspace />
      </AuthGate>
    </Suspense>
  );
}
