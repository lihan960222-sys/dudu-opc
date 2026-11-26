export function stewardPreview(text: string): string {
  if (!["```", "~~~", "“", "「", '"', "\n>"].some(mark => text.includes(mark))) {
    text = text.replace(/^(?:你必须|你得)(?=(?:先)?(?:确认|选择|授权|决定|提供|指定|填写|回复|打开|点击))/gm, "请您");
  }
  const chars = Array.from(text);
  if (chars.length <= 200) return text;
  const suffix = "详见完整回复。";
  const opening = text.trim().split(/(?:^|\s|(?<=[：:。！？；]))(?:\d+[.)、](?=\s|[^\x00-\x7f])|[-*+]\s)|\n/u, 1)[0]
    .replace(/\*\*([^*]+)\*\*/g, "$1");
  const stops = Array.from(opening.matchAll(/[。！？!?]|(?<=[A-Za-z])\.(?=\s|$)/g));
  let first = "";
  for (const stop of stops.slice(0, 2)) {
    const candidate = opening.slice(0, stop.index + 1).trim();
    if (Array.from(candidate).length > 135 || ["`", "[", "]", "://", "|", "#"].some(mark => candidate.includes(mark))) break;
    const balanced = (left: string, right: string) => candidate.split(left).length === candidate.split(right).length;
    if (balanced("（", "）") && balanced("(", ")")) first = candidate;
  }
  return first ? first + "\n" + suffix : "具体内容见完整回复。";
}

export function downloadStewardReply(text: string, name: string) {
  const url = URL.createObjectURL(new Blob([text], { type: "text/markdown;charset=utf-8" }));
  const link = document.createElement("a");
  link.href = url;
  link.download = name + ".md";
  link.click();
  window.setTimeout(() => URL.revokeObjectURL(url), 1000);
}
