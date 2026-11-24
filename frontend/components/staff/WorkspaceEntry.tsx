"use client";

import { useEffect } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { useSession } from "../AuthGate";
import { canOpenWorkbench } from "@/lib/session";
import { homeDestination } from "@/lib/workspace-navigation";
import { useT } from "@/lib/use-i18n";

export function WorkspaceEntry() {
  const { session } = useSession();
  const router = useRouter();
  const search = useSearchParams().toString();
  const t = useT();
  useEffect(() => {
    router.replace(homeDestination(search, window.location.hash, canOpenWorkbench(session)), { scroll: false });
  }, [router, search, session]);
  return <p role="status" className="p-6 text-center text-sm text-muted">{t("common.loading")}</p>;
}
