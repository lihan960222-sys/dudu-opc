const LIMIT = 1000;
const SUFFIX = "详见完整回复。";

export function stewardTurnStatus(status: string, detail?: string | null): string {
  const message = status === "failed" ? "老板，这次我没办完。"
    : status === "cancelled" ? "老板，这次办理已经停止。"
    : status === "interrupted" ? "老板，这次回复中断了，执行结果尚未核实。"
    : "老板，这一轮已经结束，我还没有可交付的结果。";
  return detail ? message + "\n\n运行记录：\n" + detail : message;
}

export function presentStewardReply(original: string): { text: string; markdown: string | null } {
  let text = original;
  if (!["`", "~~~", "“", "「", '"', "\n\n运行记录：\n"].some(mark => text.includes(mark)) && !/^(?:>| {4}|\t)/m.test(text)) {
    text = text
      .replace(/^(老板[，,][ \t]*)?你(?:必须|得|应该|赶快|赶紧|快点)(?=(?:先)?(?:确认|选择|授权|决定|提供|指定|填写|回复|打开|点击))/gm, "$1请您")
      .replace(/^(老板[，,][ \t]*)?你(?=希望|想|愿意|方便|可以|能否|是否)/gm, "$1您");
  }
  if (Array.from(original).length <= LIMIT) return { text, markdown: null };
  const opening = text.trim().split(/(?:^|\s|(?<=[：:。！？；]))(?:\d+[.)、](?=\s|[^\x00-\x7f])|[-*+]\s)|\n/u, 1)[0]
    .replace(/\*\*([^*]+)\*\*/g, "$1");
  const stops = Array.from(opening.matchAll(/[。！？!?]|(?<=[A-Za-z])\.(?=\s|$)/g));
  let summary = "";
  for (const stop of stops.slice(0, 2)) {
    const candidate = opening.slice(0, stop.index + 1).trim();
    if (Array.from(candidate).length > 135 || ["`", "[", "]", "://", "|", "#"].some(mark => candidate.includes(mark))) break;
    const balanced = (left: string, right: string) => candidate.split(left).length === candidate.split(right).length;
    if (balanced("（", "）") && balanced("(", ")")) summary = candidate;
  }
  if (!summary && opening && !/[`[\]#|<>]|:\/\//.test(opening)) {
    const chars = Array.from(opening);
    summary = "原文开头：" + chars.slice(0, 80).join("") + (chars.length > 80 ? "…" : "");
  }
  return { text: summary ? summary + "\n" + SUFFIX : "具体内容见完整回复。", markdown: original };
}
