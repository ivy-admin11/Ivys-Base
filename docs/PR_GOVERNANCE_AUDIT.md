# Open pull-request governance audit

Audit date: 2026-08-03  
Repository: `ivy-admin11/Ivys-Base`  
Audited production base: `main` at `63ced0ee4be1163e5aba9b83c83dad669bfb03f4`

This is a read-only classification. No pull request was closed, merged, labelled,
or otherwise mutated during the audit.

## Recommended closures

| PR | Recommendation | Evidence |
|---|---|---|
| #17 | Close as superseded | Draft targets `copilot/implement-step-3-applescript-invocation`, not `main`, and removes database retry imports now used by the production retry path. |
| #18 | Close as superseded | Draft is based on `main` at `d9336d2`, is currently non-mergeable, and predates the reviewed agent-delivery implementation. |
| #19 | Close as superseded | Draft targets the obsolete PR #18 branch and removes retry imports required by current `main`. |
| #20 | Close as superseded | Duplicate of #19 on the obsolete PR #18 branch; its stated assumption that retry logic is unaffected is no longer true. |
| #21 | Close as superseded | Duplicate retry-import removal based on the obsolete PR #18 branch; current production code deliberately consumes those constants. |
| #34 | Close as superseded/conflicting | Draft targets `copilot/investigate-and-resolve-issues`, not `main`, and proposes a PDF-first Sharp Picks behavior plus removal of the quality filter that conflicts with the subsequently reviewed production design. |

## Safe closure procedure

Before closing, obtain explicit owner approval. Then close each PR with a short
comment that it was superseded by current `main`; do not merge or rebase the
stale branches. Delete remote branches only after separately confirming that no
unmerged work remains useful.

After the cleanup, enable branch protection for `main`, require the Linux and
macOS CI jobs, require review, disallow direct pushes, and automatically delete
branches after a successful merge.
