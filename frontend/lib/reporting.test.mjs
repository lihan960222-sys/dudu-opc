import assert from "node:assert/strict";
import test from "node:test";
import { reportingPath } from "./reporting.ts";

test("legacy team members report to the lead, hires follow their actual manager", () => {
  const people = [{id:1}, {id:2}, {id:3,manager_agent_id:2}, {id:4,manager_agent_id:3}];
  assert.deepEqual(reportingPath(people,1,4).map(a=>a.id),[1,2,3,4]);
  assert.deepEqual(reportingPath(people,2,1).map(a=>a.id),[2,1]);
});

test("an invalid or outside manager does not invent a reporting line", () => {
  const people = [{id:1}, {id:2,manager_agent_id:3}, {id:3,manager_agent_id:2}, {id:4,manager_agent_id:99}];
  for (const id of [2,3,4]) assert.equal(reportingPath(people,1,id),null);
  assert.equal(reportingPath(people,null,1),null);
});
