const LIMIT = 1000;
const SUFFIX = "详见完整回复。";
// Preserve Markdown literals, quoted source text and the original diagnostic tail.
const VERBATIM = new RegExp([
  /^运行记录：[ \t]*\r?\n[\s\S]*/,
  /^(?:(?:报错|错误)(?:原文|信息)?|原话|工具回执|命令输出)[：:][ \t]*\r?\n[^\r\n]+(?:\r?\n(?![ \t]*\r?$)[^\r\n]+)*/,
  /^ {0,3}(`{3,}|~{3,})[^\r\n]*(?:\r?\n|(?![\s\S]))[\s\S]*?(?:^ {0,3}\1[`~]*[ \t]*\r?$|(?![\s\S]))|(`+)(?!`)[\s\S]*?\2(?!`)/,
  /^ {0,3}>[^\r\n]*(?:\r?\n(?![ \t]*\r?$| {0,3}(?:>|`{3,}|~{3,}))[^\r\n]+)*|^(?: {4}|\t)[^\r\n]*/,
  /“[^”]*”|‘[^’]*’|「[^」]*」|『[^』]*』|《[^》]*》|"(?:\\.|[^"\\])*"|(?<![A-Za-z0-9_])'(?:\\.|[^'\\\r\n])*'/,
  /\[(?:\\.|[^\]\\\r\n])*\]\((?:\\.|[^)\\\r\n])*\)/,
].map(literal => literal.source).join("|"), "gm");
const SALUTATION = /(?<![^\r\n。！？!?，,])([ \t]*(?:老板[，,][ \t]*)?)/;
const REQUESTS = [
  /(你(?:必须|得|应该|赶快|赶紧|快点))(?=(?:先)?(?:确认|选择|授权|决定|提供|指定|填写|回复|打开|点击))/,
  /你(?=希望|想|愿意|方便|可以|能否|是否)/,
  /(?:(?:请|麻烦)?[你您](?:这边|自己)?|麻烦)?(?:先|直接)?(?:给我?(?:一?个)|给我)((?:任务|工单|项目|派工)?(?:编号|号)|单号|文件名|文档名|标题|日期)(?:就(?:行|可以)了?)?([，,。.!！?？]|(?=\r?$))/,
];
const DIRECT_REQUEST = new RegExp(SALUTATION.source + "(?:" + REQUESTS.map(request => request.source).join("|") + ")", "gm");

function polish(source: string, compact = false): string {
  if (!source.includes("你") && !source.includes("给")) return source;
  const literals = source.matchAll(VERBATIM);
  let literal = literals.next().value;
  return source.replace(DIRECT_REQUEST, (whole, lead, urgent, field, stop, offset: number) => {
    while (literal && literal.index + literal[0].length <= offset) literal = literals.next().value;
    if (literal && literal.index < offset + whole.length) return whole;
    if (field) {
      return lead + (compact ? "请问" + field + (stop ? "？" : "") : "您方便告诉我" + field + "吗？");
    }
    return lead + (urgent ? "请您" : "您");
  });
}

export function stewardTurnStatus(status: string, detail?: string | null): string {
  const message = status === "failed" ? "老板，这次我没办完。"
    : status === "cancelled" ? "老板，这次办理已经停止。"
    : status === "interrupted" ? "老板，这次回复中断了，执行结果尚未核实。"
    : "老板，这一轮已经结束，我还没有可交付的结果。";
  return detail ? message + "\n\n运行记录：\n" + detail : message;
}

export function presentStewardReply(original: string): { text: string; markdown: string | null } {
  const text = polish(original);
  if (Array.from(original).length <= LIMIT) {
    return { text: Array.from(text).length > LIMIT ? polish(original, true) : text, markdown: null };
  }
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
