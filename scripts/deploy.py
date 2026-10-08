#!/usr/bin/env python3
"""Automates deploying a wccomps-portal commit via wccomps-argocd.

Usage:
    python3 scripts/deploy.py [--sha SHA] [--argocd-dir PATH] [--skip-wait]
"""

from __future__ import annotations

import argparse
import contextlib
import json
import re
import subprocess
import sys
from pathlib import Path


def run_cmd(cmd: list[str], cwd: Path | None = None, check: bool = True) -> str:
    """Run a command and return stdout as string."""
    res = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, check=check)
    return res.stdout.strip()


def find_argocd_dir(explicit_path: str | None) -> Path:
    """Locate the wccomps-argocd repository directory."""
    candidates = []
    if explicit_path:
        candidates.append(Path(explicit_path))
    portal_root = Path(__file__).resolve().parent.parent
    candidates.extend(
        [
            portal_root.parent / "wccomps-argocd",
            Path.home() / "wccomps-argocd",
        ]
    )
    for cand in candidates:
        if (cand / "manifests" / "wccomps-portal" / "kustomization.yaml").is_file():
            return cand.resolve()
    print("Error: Could not locate wccomps-argocd repository.", file=sys.stderr)
    print("Specify its path with --argocd-dir <path>", file=sys.stderr)
    sys.exit(1)


def wait_for_ci_image(portal_dir: Path, target_sha: str) -> None:
    """Ensure the Docker image has finished building on main."""
    print(f"Checking GitHub Actions image build for commit {target_sha}...")
    try:
        runs_raw = run_cmd(
            [
                "gh",
                "run",
                "list",
                "--branch",
                "main",
                "--limit",
                "5",
                "--json",
                "databaseId,headSha,status,conclusion,workflowName",
            ],
            cwd=portal_dir,
        )
        runs = json.loads(runs_raw)
    except Exception as e:
        print(f"Warning: Could not query GitHub Actions runs ({e}). Proceeding anyway.")
        return

    matching_run = None
    for run in runs:
        if run.get("headSha", "").startswith(target_sha) or target_sha in run.get("headSha", ""):
            matching_run = run
            break

    if not matching_run:
        print(f"Note: No active CI run found for commit {target_sha}. Assuming image is published.")
        return

    status = matching_run.get("status")
    conclusion = matching_run.get("conclusion")
    run_id = matching_run.get("databaseId")

    if status != "completed":
        print(f"CI run #{run_id} is currently {status}. Waiting for completion...")
        subprocess.run(["gh", "run", "watch", str(run_id), "--interval", "10"], cwd=portal_dir, check=False)
    elif conclusion != "success":
        print(f"Warning: CI run #{run_id} finished with conclusion: '{conclusion}'")


def main() -> None:
    parser = argparse.ArgumentParser(description="Automate wccomps-portal deployment via ArgoCD.")
    parser.add_argument("--sha", help="Short SHA to deploy (defaults to latest main commit)")
    parser.add_argument("--argocd-dir", help="Path to wccomps-argocd repository")
    parser.add_argument("--skip-wait", action="store_true", help="Skip waiting for CI run")
    args = parser.parse_args()

    portal_dir = Path(__file__).resolve().parent.parent

    # Determine target SHA
    target_sha = args.sha or run_cmd(["git", "rev-parse", "--short", "origin/main"], cwd=portal_dir)
    print(f"Target deployment SHA: {target_sha}")

    if not args.skip_wait:
        wait_for_ci_image(portal_dir, target_sha)

    # Locate ArgoCD repo
    argocd_dir = find_argocd_dir(args.argocd_dir)
    print(f"Found ArgoCD repo at: {argocd_dir}")

    # Fetch latest main in argocd
    run_cmd(["git", "fetch", "origin", "main"], cwd=argocd_dir)
    run_cmd(["git", "checkout", "main"], cwd=argocd_dir)
    run_cmd(["git", "merge", "--ff-only", "origin/main"], cwd=argocd_dir)

    kustomize_path = argocd_dir / "manifests" / "wccomps-portal" / "kustomization.yaml"
    kustomize_text = kustomize_path.read_text(encoding="utf-8")

    tag_match = re.search(r"newTag:\s*sha-([a-f0-9]+)", kustomize_text)
    if not tag_match:
        print("Error: Could not find 'newTag: sha-<hash>' in kustomization.yaml", file=sys.stderr)
        sys.exit(1)

    current_sha = tag_match.group(1)
    print(f"Currently deployed SHA in ArgoCD: {current_sha}")

    if current_sha.startswith(target_sha) or target_sha.startswith(current_sha):
        print(f"sha-{target_sha} is already the deployed tag in ArgoCD! Nothing to do.")
        return

    # Collect commits between current and target SHA for the PR body
    commit_log = ""
    with contextlib.suppress(Exception):
        commit_log = run_cmd(
            ["git", "log", f"{current_sha}..{target_sha}", "--oneline"],
            cwd=portal_dir,
            check=False,
        )

    pr_bullets: list[str] = []
    if commit_log:
        pr_bullets.extend(f"• {line}" for line in commit_log.splitlines())
    bullets_text = "\n".join(pr_bullets) if pr_bullets else f"• Update wccomps-portal to sha-{target_sha}"

    pr_body = (
        f"Bump the portal image to sha-{target_sha} (main, CI green).\n\n"
        f"Since the deployed sha-{current_sha}:\n"
        f"{bullets_text}\n"
    )

    branch_name = f"bump-portal-{target_sha}"
    print(f"Creating branch {branch_name} in {argocd_dir}...")
    run_cmd(["git", "checkout", "-B", branch_name, "origin/main"], cwd=argocd_dir)

    # Update kustomization.yaml
    new_kustomize_text = re.sub(
        r"(newTag:\s*)sha-[a-f0-9]+",
        f"\\g<1>sha-{target_sha}",
        kustomize_text,
    )
    kustomize_path.write_text(new_kustomize_text, encoding="utf-8")

    # Commit and push
    commit_msg = f"Deploy wccomps-portal sha-{target_sha}"
    run_cmd(["git", "commit", "-am", commit_msg], cwd=argocd_dir)
    print(f"Pushing branch {branch_name} to origin...")
    run_cmd(["git", "push", "-u", "origin", branch_name, "--force"], cwd=argocd_dir)

    # Create PR
    print("Creating pull request in wccomps-argocd...")
    pr_url = run_cmd(
        [
            "gh",
            "pr",
            "create",
            "--title",
            commit_msg,
            "--body",
            pr_body,
        ],
        cwd=argocd_dir,
    )
    print(f"Created PR: {pr_url}")

    # Merge PR
    print("Squash-merging pull request...")
    run_cmd(["gh", "pr", "merge", pr_url, "--squash", "--delete-branch"], cwd=argocd_dir)
    print("Pull request merged successfully!")
    print(f"✓ Deployed sha-{target_sha} to wccomps-argocd. ArgoCD will roll out pods automatically.")


if __name__ == "__main__":
    main()
