export type FeishuContact = { chat_id: string; peer_open_id: string; agent_id: number | null };

export function assignedContact(contact: FeishuContact, employee: number, isLead: boolean) {
  return contact.agent_id === employee || (contact.agent_id === null && isLead);
}

// Carries only the employee number across the OAuth redirect in this browser tab.
export const FEISHU_RETURN_EMPLOYEE = "suparagent.feishu.return-employee";

export function feishuEntry(params: URLSearchParams, hash: string, returningEmployee: string | null) {
  const callback = params.has("feishu_oauth");
  const requested = params.get("capability") === "feishu" || hash === "#feishu-access" || callback;
  const employee = params.get("employee") ?? (callback ? returningEmployee : null);
  return { requested, employee: requested && employee && /^[1-9]\d*$/.test(employee) ? employee : "" };
}
