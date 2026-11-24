import assert from "node:assert/strict";
import test from "node:test";
import { teamActionsFor } from "./staff-team.ts";

test("team controls follow the saved employee's membership, administrator scope, and creator", () => {
  const a={id:4,archived_at:null};
  const t={team:{lead_agent_id:1},agents:[]};
  assert.deepEqual(teamActionsFor(false,a,t),[]);
  assert.deepEqual(teamActionsFor(true,a,null),[]);
  assert.deepEqual(teamActionsFor(true,{...a,archived_at:"today"},t),[]);
  assert.deepEqual(teamActionsFor(true,a,{team:null,agents:[]}),["lead"]);
  assert.deepEqual(teamActionsFor(true,a,{team:{lead_agent_id:null},agents:[]}),["lead","join"]);
  assert.deepEqual(teamActionsFor(true,a,t),["join"]);
  t.agents=[{...a,state:"member",is_lead:false,created_by_agent_id:1}];
  assert.deepEqual(teamActionsFor(true,a,t),["delegate","lead","remove","undo"]);
  t.agents[0].is_lead=true;
  t.agents[0].created_by_agent_id=null;
  assert.deepEqual(teamActionsFor(true,a,t),["delegate"]);
});
