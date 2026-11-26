import { presentStewardReply } from "./steward/format.ts";

export { presentStewardReply, stewardTurnStatus } from "./steward/format.ts";

export function stewardPreview(text: string): string {
  return presentStewardReply(text).text;
}

export function downloadStewardReply(text: string, name: string) {
  const url = URL.createObjectURL(new Blob([text], { type: "text/markdown;charset=utf-8" }));
  const link = document.createElement("a");
  link.href = url;
  link.download = name + ".md";
  link.click();
  window.setTimeout(() => URL.revokeObjectURL(url), 1000);
}
