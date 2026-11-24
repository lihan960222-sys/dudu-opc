import test from "node:test";
import assert from "node:assert/strict";
import { employeeModelName, teamActivityLabel } from "./team-employee.ts";
import { translate } from "./i18n.ts";

test("employee summaries describe the configured model, including the role default", () => {
  for (const [model, label] of [[null, "岗位默认"], ["default", "岗位默认"], ["deepseek-flash", "DeepSeek Flash"], ["deepseek-v4-pro", "DeepSeek V4 Pro"], ["gpt-5.4", "GPT-5.4"], ["gpt-5.3-codex", "GPT-5.3 Codex"], ["claude-sonnet-4-6", "Claude Sonnet 4.6"], ["private/model-v7", "自定义模型"]]) {
    assert.equal(employeeModelName(model, "岗位默认", "自定义模型"), label);
  }
});

test("the roster and directory use the same human-readable audit activity", () => {
  const t = key => translate("zh", key);
  assert.equal(teamActivityLabel("job_enqueued", t), "协作任务已排队");
  assert.equal(teamActivityLabel("agent_updated", t), "已更新员工档案");
  assert.equal(teamActivityLabel("future_code", t), t("team.activity"));
});
