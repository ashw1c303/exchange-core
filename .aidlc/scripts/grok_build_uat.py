import os, sys, json, subprocess, urllib.request

XAI_API_KEY = os.environ.get("XAI_API_KEY")

def call_grok(system_prompt: str, user_prompt: str) -> str:
    url = "https://api.x.ai/v1/chat/completions"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {XAI_API_KEY}"
    }
    payload = {
        "model": "grok-3-latest",
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt}
        ],
        "temperature": 0.1
    }
    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), headers=headers)
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read().decode("utf-8"))["choices"][0]["message"]["content"]

def check_guardrails():
    with open(".aidlc/guardrails.json") as f:
        rules = json.load(f)

    base_ref = os.environ.get("GITHUB_BASE_REF", "master")
    diff_cmd = ["git", "diff", "--name-only", f"origin/{base_ref}...HEAD"]
    diff_files = subprocess.check_output(diff_cmd).decode().strip().splitlines()
    violations = []

    if len(diff_files) > rules["max_files_changed_per_pr"]:
        violations.append(
            f"**Blast Radius Exceeded:** `{len(diff_files)}` files modified (Max allowed: `{rules['max_files_changed_per_pr']}`)."
        )

    for file_path in diff_files:
        if file_path in rules["protected_paths"]:
            violations.append(f"**Protected Path Modified:** `{file_path}` cannot be altered without Architect Override.")
        if any(file_path.startswith(hp) for hp in rules["hot_path_packages"]) and os.path.exists(file_path):
            with open(file_path) as src:
                content = src.read()
                for forbidden in rules["forbidden_imports_in_hot_path"]:
                    if f"import {forbidden}" in content:
                        violations.append(
                            f"**Zero-GC Hot-Path Violation:** Forbidden import `{forbidden}` detected in `{file_path}`."
                        )

    return diff_files, violations

def main():
    base_ref = os.environ.get("GITHUB_BASE_REF", "master")
    diff_files, violations = check_guardrails()
    git_diff = subprocess.check_output(
        ["git", "diff", f"origin/{base_ref}...HEAD"]
    ).decode("utf-8", errors="ignore")[:12000]

    system_prompt = (
        "You are Grok Build & UAT Engine for exchange-core. Analyze the git diff and generate a concise Markdown report with:\n"
        "1. **Architectural & Zero-GC Assessment**: Verify LMAX Disruptor thread safety and deterministic replay.\n"
        "2. **Automated UAT Scenarios Verified**: A Markdown table of Given/When/Then scenarios for the code changes.\n"
        "3. **Human-Led QA Sign-Off Checklist**: Interactive Markdown checkboxes (`- [ ]`) for a human QA reviewer to validate edge cases before merging."
    )
    report = call_grok(
        system_prompt,
        f"Changed files: {diff_files}\nGuardrail Violations: {violations}\nGit Diff:\n{git_diff}"
    )

    with open("grok_uat_report.md", "w") as out:
        badge = "❌ BLOCKED BY OPERATIONAL GUARDRAILS" if violations else "✅ PASSED GUARDRAILS & UAT READY"
        out.write(f"## 🤖 Grok Build: Guardrail & UAT Gate ({badge})\n\n")
        if violations:
            out.write("### 🚨 Operational Guardrail Violations Detected\n")
            for v in violations:
                out.write(f"- {v}\n")
            out.write("\n> **Action Required:** Remove hot-path allocations or reduce PR blast radius to unblock CI.\n\n---\n")
        out.write(report)

    if violations:
        sys.exit(1)

if __name__ == "__main__":
    main()
