import assert from "node:assert/strict";
import { test } from "node:test";
import { spawnSync } from "node:child_process";

test("Next embeds the correct API address for each serving mode", () => {
  const config = new URL("../next.config.ts", import.meta.url).href;
  for (const [mode, api, prefix, expected] of [
    ["development", null, "", "http://localhost:8000/api/v1"],
    ["production", null, "", "/api/v1"],
    ["production", null, "/app", "/api/v1"],
    ["production", "https://backend.example/api/v1", "/app", "https://backend.example/api/v1"],
  ]) {
    const env = { ...process.env, NODE_ENV: mode, OD_BASE_PATH: prefix, OD_STATIC_EXPORT: "1" };
    if (api === null) delete env.NEXT_PUBLIC_API_BASE_URL;
    else env.NEXT_PUBLIC_API_BASE_URL = api;
    const child = spawnSync(process.execPath, ["--experimental-strip-types", "--input-type=module", "-e",
      `import config from ${JSON.stringify(config)}; console.log(JSON.stringify(config.env));`],
      { env, encoding: "utf8" });
    assert.equal(child.status, 0, child.stderr);
    const built = JSON.parse(child.stdout);
    assert.equal(built.NEXT_PUBLIC_API_BASE_URL, expected);
    assert.equal(built.NEXT_PUBLIC_BASE_PATH, prefix);
  }
});
