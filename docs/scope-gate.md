# Scope gate: reject a scope whose screens cannot be reached

## Problem
At the scope gate the operator could approve a set of screens that the navigation cannot reach.
`apply_scope` drops `navigation.map` edges and `navigates_to` links to excluded screens, so a kept
screen whose only path ran through an excluded screen becomes an orphan. Code generation then failed
on the Mac worker, after approval, in `swiftui_navigation.tab_owners`
(`NavigationSpecError: screens reachable from no tab: ['0002']`). This happened on job Dogo.

## Decision
Approval is rejected, and the operator gets a clear message. The scope is never "repaired" silently:
we neither auto-include screens nor invent navigation edges. When a fix exists, the message suggests
the excluded screens that would make the orphan reachable. A suggestion is shown only after the same
check, run again with those screens included, passes.

The check runs exactly what code generation runs. It resolves the screen ids the way the build does,
prunes the full app spec the way `scope_to` does, then calls `swiftui_scaffold.build_plan`
(`derive_tabs` + `tab_owners`). A scope the gate accepts therefore cannot fail navigation planning
in codegen, and a scope the gate rejects would fail there.

What blocks approval:
- **unreachable**: a pushed screen is reachable from no tab root. In apps without a tab bar, the
  root is the implicit home screen (`route == "/"`, else the first pushed screen).
- **tabs**: the tab structure cannot be derived, for example a tab root screen was excluded
  while the tabs are inferred from `tab_bar` components (with an explicit `navigation.tabs` an
  excluded root just drops its tab). The excluded tab roots are named; "Include them" is added
  only when including them passes the check.
- **spec**: the pruned spec is invalid, the app spec cannot be loaded (or the check itself
  fails), or no home screen is left.

## UI
`POST /jobs/{id}/scope` returns **422** and renders the job page again. The Scope tab keeps the
operator's selection and lists the problems, for example: "Screen 'Settings' (0002) is not reachable
from any tab ('Home' 0011)… Include 'Profile' (0007) (path 0011 → 0007 → 0002) or exclude 0002."
Nothing is saved, and the job stays in `NEEDS_INPUT`. While a scope is still proposed, the job page
shows the same problems in advance.

## Where in the code
- `iosforge/mvp/scope_check.py` — `navigation_problems(full_spec, scope) -> list[ScopeProblem]`
- `iosforge/mvp/feasibility.py` — `resolve_scope_ids`, `scoped_spec`, both shared with the build path
- `iosforge/mvp/swiftui_navigation.py` — `UnreachableScreensError(orphans, roots)` raised by `tab_owners`
- `iosforge/admin/routes_jobs.py` — `jobs_scope_approve` (block via `_approval_problems`),
  `job_detail` (advance warning via `_proposal_warnings`; errors are logged, never shown as a failure)
- `iosforge/admin/templates/job_detail.html` — scope panel alert
