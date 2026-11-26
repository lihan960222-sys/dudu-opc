import assert from "node:assert/strict";
import test from "node:test";
import { employeeToolChanges } from "./employee-tools.ts";

test("a tool save cannot overwrite the employee's role, model, workspace, or concurrent unchanged permissions", () => {
  const input = {name:"Original", provider:"codex", model:"deepseek-flash", approval_mode:"ask", connector_scopes:{calendar:"read"}};
  assert.deepEqual(employeeToolChanges(input, {...input, name:"Stale role", model:null, workspace_path:"stale", make_lead:true, approval_mode:"plan"}), {});
  assert.deepEqual(employeeToolChanges(input, {...input, connector_scopes:{}}), {connector_scopes:{}});
});

test("the advanced panel cannot change other employees folder access", () => {
  assert.deepEqual(employeeToolChanges({}, {team_workspaces_readable:true}), {});
  assert.deepEqual(employeeToolChanges({team_workspaces_readable:true}, {team_workspaces_readable:false}), {});
  assert.deepEqual(employeeToolChanges({team_workspaces_readable:true}, {team_workspaces_readable:true}), {});
});

