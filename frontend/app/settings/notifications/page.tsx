import { AuthGate } from "@/components/AuthGate";
import { SettingsShell } from "@/components/settings/SettingsShell";
import { NotificationsSettings } from "@/components/settings/NotificationsSettings";

export const metadata = { title: "Notifications · Suparagent" };

export default function Page() {
  return (
    <AuthGate>
      <SettingsShell>
        <NotificationsSettings />
      </SettingsShell>
    </AuthGate>
  );
}

