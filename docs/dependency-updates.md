# Dependency updates

GitHub Actions are pinned to full commit SHAs in `.github/workflows/validate.yml`. Dependabot checks them weekly and proposes updates as pull requests. Review the upstream release notes and the proposed commit before merging; retain the human-readable version comment beside each SHA.

Chocolatey packages used by CI are pinned to exact versions in the same workflow. Chocolatey packages are not covered by the GitHub Actions Dependabot ecosystem, so review the official `shellcheck` package page before each release and at least quarterly. Update one package at a time, then run the complete local validation sequence and confirm the Windows CI job passes.

After any dependency update:

```powershell
python -B tests/run_validation.py
```
