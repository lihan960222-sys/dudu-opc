"use client";
import { useT } from "@/lib/use-i18n";

import { useState } from "react";
import { Banner } from "@/components/ui/Banner";
import { Button } from "@/components/ui/Button";
import { useToast } from "@/components/ui/Toast";
import { API_BASE } from "@/lib/api-base";

export function HistoryExport() {
  const t = useT();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const toast = useToast();

  async function exportHistory() {
    setBusy(true); setError(null);
    try {
      const res = await fetch(`${API_BASE}/history/export`, { credentials: "include" });
      if (!res.ok) {
        let message = t("settingsPage.couldNotExportYourHistoryTryAgain");
        try {
          const detail = (await res.json())?.detail;
          if (typeof detail?.message === "string") message = detail.message;
          else if (typeof detail === "string") message = detail;
        } catch { /* not JSON */ }
        throw new Error(message);
      }
      const url = URL.createObjectURL(await res.blob());
      const link = document.createElement("a");
      link.href = url;
      link.download = `suparagent-history-${new Date().toISOString().slice(0, 10)}.zip`;
      link.click();
      setTimeout(() => URL.revokeObjectURL(url), 0);
      toast(t("settingsPage.exportSavedToYourDownloads"));
    } catch (err) {
      setError(err instanceof Error ? `${t("settingsPage.couldNotExportYourHistoryTryAgain")} ${err.message}` : t("settingsPage.couldNotExportYourHistoryTryAgain"));
    } finally {
      setBusy(false);
    }
  }

  return <section className="space-y-3 rounded-lg border border-line p-5">
    <h3 className="font-semibold">{t("settingsPage.yourHistory")}</h3>
    <p className="text-xs text-muted">{t("settingsPage.saveACopyOfYourAgentsConversationsAnd")}</p>
    {error && <Banner tone="danger">{error}</Banner>}
        <Button loading={busy} onClick={() => void exportHistory()}>{busy ? t("settingsPage.preparing") : t("settingsPage.exportMyHistory")}</Button>
  </section>;
}
