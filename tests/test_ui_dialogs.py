"""Playwright UI tests: dialog close handlers drive real POST APIs."""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
API = "http://127.0.0.1:3847"


def http_json(method: str, path: str, body: dict | None = None) -> tuple[int, dict | list]:
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(
        API + path,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"} if body is not None else {},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read().decode()
            return resp.status, json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            return e.code, json.loads(raw) if raw else {"error": raw}
        except Exception:
            return e.code, {"error": raw}


def wait_api(timeout: float = 20) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            code, _ = http_json("GET", "/health")
            if code == 200:
                return
        except Exception:
            pass
        time.sleep(0.5)
    raise RuntimeError("product API not up on :3847")


class DialogCreateProjectPlaywright(unittest.TestCase):
    """Drives shipped UI: New Project dialog must POST /projects via dialog close."""

    @classmethod
    def setUpClass(cls):
        wait_api()
        if not shutil.which("node"):
            raise unittest.SkipTest("node required")
        # Prefer local playwright from prior install or product tree
        cls.scratch = Path("/tmp/grok-goal-3efc8a93fe23/implementer")
        cls.scratch.mkdir(parents=True, exist_ok=True)

    def test_new_project_dialog_creates_via_api(self):
        code, before = http_json("GET", "/projects")
        self.assertEqual(code, 200)
        n0 = len(before.get("projects") or [])
        name = f"UIDialogProject_{int(time.time())}"

        script = self.scratch / "playwright_create_project.mjs"
        script.write_text(
            f"""
import {{ chromium }} from 'playwright';

const name = {json.dumps(name)};
const browser = await chromium.launch({{ headless: true }});
const page = await browser.newPage();
const errors = [];
page.on('pageerror', (e) => errors.push(String(e)));

await page.goto('http://127.0.0.1:3847/', {{ waitUntil: 'networkidle', timeout: 30000 }});
await page.getByRole('button', {{ name: '+ New Project' }}).click();
await page.locator('#dlg-project').waitFor({{ state: 'visible' }});
await page.locator('#form-project input[name="name"]').fill(name);
// method=dialog: submit via Create button value=ok
await page.locator('#form-project button[value="ok"]').click();
// Wait for dialog to close and project to appear in sidebar
await page.waitForFunction(
  (n) => document.body.innerText.includes(n),
  name,
  {{ timeout: 15000 }}
);
await page.screenshot({{ path: {json.dumps(str(self.scratch / "ui-create-project.png"))}, fullPage: true }});
console.log(JSON.stringify({{ errors, bodyHas: await page.locator('body').innerText().then(t => t.includes(name)) }}));
await browser.close();
""",
            encoding="utf-8",
        )

        # Run from scratch where node_modules/playwright was installed
        env = {**dict(**{k: v for k, v in __import__("os").environ.items()}), "NODE_PATH": str(self.scratch / "node_modules")}
        r = subprocess.run(
            ["node", str(script)],
            cwd=str(self.scratch),
            capture_output=True,
            text=True,
            timeout=90,
            env=env,
        )
        out = (r.stdout or "") + (r.stderr or "")
        (self.scratch / "playwright_create_project.log").write_text(out, encoding="utf-8")
        self.assertEqual(r.returncode, 0, out)

        code, after = http_json("GET", "/projects")
        self.assertEqual(code, 200)
        names = [p["name"] for p in (after.get("projects") or [])]
        self.assertIn(name, names, f"project not created via UI dialog; before={n0} names={names}\n{out}")
        self.assertGreater(len(names), n0)
        # cleanup demo project from live catalog
        for p in after.get("projects") or []:
            if p["name"] == name:
                http_json("DELETE", f"/projects/{p['id']}")

    def test_add_repo_dialog_indexes_seed(self):
        """Add Repository dialog posts Add & Index and reaches ready states."""
        # unique project — do not use Skeptic* (purged/hidden by product policy)
        name = f"UIAddRepo_{int(time.time())}"
        code, proj = http_json("POST", "/projects", {"name": name})
        self.assertEqual(code, 201, proj)
        pid = proj["id"]

        seed = self.scratch / f"seed-ui-{int(time.time())}"
        seed.mkdir(parents=True, exist_ok=True)
        subprocess.check_call(["git", "init"], cwd=seed, stdout=subprocess.DEVNULL)
        subprocess.check_call(["git", "config", "user.email", "t@example.com"], cwd=seed)
        subprocess.check_call(["git", "config", "user.name", "t"], cwd=seed)
        (seed / "note.txt").write_text("UI_DIALOG_SEED_TOKEN_ZZZ\n", encoding="utf-8")
        subprocess.check_call(["git", "add", "note.txt"], cwd=seed)
        subprocess.check_call(["git", "commit", "-m", "ui seed UI_DIALOG_SEED_TOKEN_ZZZ"], cwd=seed, stdout=subprocess.DEVNULL)

        script = self.scratch / "playwright_add_repo.mjs"
        script.write_text(
            f"""
import {{ chromium }} from 'playwright';
const projectName = {json.dumps(name)};
const url = {json.dumps(str(seed))};
const browser = await chromium.launch({{ headless: true }});
const page = await browser.newPage();
await page.goto('http://127.0.0.1:3847/', {{ waitUntil: 'networkidle', timeout: 30000 }});
// open project
await page.getByText(projectName, {{ exact: true }}).first().click();
await page.getByRole('button', {{ name: '+ Add Repository' }}).click();
await page.locator('#dlg-repo').waitFor({{ state: 'visible' }});
await page.locator('#form-repo input[name="url"]').fill(url);
await page.locator('#form-repo button[value="ok"]').click();
// wait for repo name to show as ready (poll UI text)
await page.waitForFunction(
  () => document.body.innerText.includes('search:ready') || document.body.innerText.includes('graph:ready'),
  null,
  {{ timeout: 60000 }}
);
// allow a bit more for full dual-ready after fixed poll
for (let i = 0; i < 20; i++) {{
  const t = await page.locator('body').innerText();
  if (t.includes('search:ready') && (t.includes('graph:ready') || t.includes('graph:ready_local'))) break;
  await page.waitForTimeout(1500);
}}
await page.screenshot({{ path: {json.dumps(str(self.scratch / "ui-add-repo.png"))}, fullPage: true }});
const text = await page.locator('body').innerText();
console.log(JSON.stringify({{ hasReady: text.includes('search:ready'), hasRepo: text.includes(url.split('/').pop()) }}));
await browser.close();
""",
            encoding="utf-8",
        )
        env = {**dict(**{k: v for k, v in __import__("os").environ.items()}), "NODE_PATH": str(self.scratch / "node_modules")}
        r = subprocess.run(
            ["node", str(script)],
            cwd=str(self.scratch),
            capture_output=True,
            text=True,
            timeout=120,
            env=env,
        )
        out = (r.stdout or "") + (r.stderr or "")
        (self.scratch / "playwright_add_repo.log").write_text(out, encoding="utf-8")
        self.assertEqual(r.returncode, 0, out)

        code, ov = http_json("GET", f"/projects/{pid}/overview")
        self.assertEqual(code, 200)
        self.assertGreaterEqual(ov["stats"]["repos"], 1)
        repos = ov.get("repos") or []
        self.assertTrue(repos, ov)
        r0 = repos[0]
        self.assertIn(r0["search_state"], ("ready",))
        self.assertIn(r0["graph_state"], ("ready", "ready_local"))
        # cleanup
        http_json("DELETE", f"/projects/{pid}")


class ExpandCollapsePlaywright(unittest.TestCase):
    """Caret toggles expand without being force-reopened; selected can collapse."""

    @classmethod
    def setUpClass(cls):
        wait_api()
        if not shutil.which("node"):
            raise unittest.SkipTest("node required")
        cls.scratch = Path("/tmp/grok-goal-3efc8a93fe23/implementer")
        cls.scratch.mkdir(parents=True, exist_ok=True)

    def test_caret_collapse_stays_collapsed_while_selected(self):
        # Ensure HugeGraph exists with repos
        code, data = http_json("GET", "/projects")
        self.assertEqual(code, 200)
        projects = data.get("projects") or []
        target = next((p for p in projects if p.get("name") == "HugeGraph"), None)
        if not target:
            code, target = http_json("POST", "/projects", {"name": "HugeGraph"})
            self.assertEqual(code, 201)

        script = self.scratch / "playwright_expand_collapse.mjs"
        script.write_text(
            """
import { chromium } from 'playwright';

const browser = await chromium.launch({ headless: true });
const page = await browser.newPage();
await page.goto('http://127.0.0.1:3847/', { waitUntil: 'networkidle', timeout: 30000 });

// Select HugeGraph by name (expands + selects)
const name = page.locator('.tree-project-head .name', { hasText: 'HugeGraph' }).first();
await name.click();
await page.waitForTimeout(400);

const project = page.locator('.tree-project').filter({ has: page.locator('.name', { hasText: 'HugeGraph' }) }).first();
// After select, should be open with repos visible
await page.waitForFunction(() => {
  const el = [...document.querySelectorAll('.tree-project')].find(p =>
    p.querySelector('.name')?.textContent === 'HugeGraph');
  return el && el.classList.contains('open');
}, null, { timeout: 10000 });

// Collapse via caret only
await project.locator('.caret').click();
await page.waitForTimeout(300);

const collapsed = await page.evaluate(() => {
  const el = [...document.querySelectorAll('.tree-project')].find(p =>
    p.querySelector('.name')?.textContent === 'HugeGraph');
  if (!el) return { ok: false, reason: 'missing' };
  const open = el.classList.contains('open');
  const caret = el.querySelector('.caret')?.textContent?.trim();
  const reposVisible = getComputedStyle(el.querySelector('.tree-repos')).display !== 'none';
  // selected head may still be active
  const active = el.querySelector('.tree-project-head')?.classList.contains('active');
  return { ok: !open && caret === '▸' && !reposVisible, open, caret, reposVisible, active };
});

// Stay collapsed after a short wait (no re-open from force logic)
await page.waitForTimeout(500);
const still = await page.evaluate(() => {
  const el = [...document.querySelectorAll('.tree-project')].find(p =>
    p.querySelector('.name')?.textContent === 'HugeGraph');
  return el && !el.classList.contains('open');
});

// Re-expand via caret
await project.locator('.caret').click();
await page.waitForTimeout(300);
const reopened = await page.evaluate(() => {
  const el = [...document.querySelectorAll('.tree-project')].find(p =>
    p.querySelector('.name')?.textContent === 'HugeGraph');
  return el && el.classList.contains('open');
});

await page.screenshot({ path: '/tmp/grok-goal-3efc8a93fe23/implementer/ui-expand-collapse.png', fullPage: true });
console.log(JSON.stringify({ collapsed, still, reopened }));
if (!collapsed.ok || !still || !reopened) {
  process.exit(1);
}
await browser.close();
""",
            encoding="utf-8",
        )
        env = {
            **{k: v for k, v in __import__("os").environ.items()},
            "NODE_PATH": str(self.scratch / "node_modules"),
        }
        r = subprocess.run(
            ["node", str(script)],
            cwd=str(self.scratch),
            capture_output=True,
            text=True,
            timeout=60,
            env=env,
        )
        out = (r.stdout or "") + (r.stderr or "")
        (self.scratch / "playwright_expand_collapse.log").write_text(out, encoding="utf-8")
        self.assertEqual(r.returncode, 0, out)
        payload = json.loads([ln for ln in out.splitlines() if ln.strip().startswith("{")][-1])
        self.assertTrue(payload["collapsed"]["ok"], payload)
        self.assertTrue(payload["still"], payload)
        self.assertTrue(payload["reopened"], payload)


if __name__ == "__main__":
    unittest.main()
