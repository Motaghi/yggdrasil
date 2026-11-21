| Test | Reason |
|---|---|
| `tests/acceptance-a11/test_lane0_seams.py::test_ac99_row1_codex_roles[reviewer-only]` | Row 1 permits only the project-reviewer model override, exactly exit 0. |
| `tests/acceptance-a11/test_lane0_seams.py::test_ac99_row3_disabled_features[t3_live_send]` | The t3_live_send-off example expects no send, exactly the empty call list. |
| `tests/acceptance-a11/test_lane0_seams.py::test_ac99_row3_disabled_features[retry_cap]` | The retry_cap-off example expects an allowed call and no escalation, exactly exit 0 and empty inbox text. |
| `tests/acceptance-a11/test_lane1_hooks.py::test_ac10_notice[3-0-None]` | Row 3 expects exit 0 and no additionalContext, exactly the empty stub result. |
| `tests/acceptance-a11/test_lane1_hooks.py::test_ac16_lane_hooks[1-configured]` | The clean commit examples expect success and no home-hook output or marker. |
| `tests/acceptance-a11/test_lane1_hooks.py::test_ac16_lane_hooks[1-default]` | The clean commit examples expect success and no home-hook output or marker. |
| `tests/acceptance-a11/test_lane1_hooks.py::test_ac16_lane_hooks[2-absent]` | The clean commit examples expect success and no home-hook output or marker. |
| `tests/acceptance-a11/test_lane1_hooks.py::test_ac21_row1_allowed_shapes[git status]` | Each signed read-only shape in row 1 is allowed, exactly exit 0. |
| `tests/acceptance-a11/test_lane1_hooks.py::test_ac21_row1_allowed_shapes[git status --short]` | Each signed read-only shape in row 1 is allowed, exactly exit 0. |
| `tests/acceptance-a11/test_lane1_hooks.py::test_ac21_row1_allowed_shapes[git diff --cached]` | Each signed read-only shape in row 1 is allowed, exactly exit 0. |
| `tests/acceptance-a11/test_lane1_hooks.py::test_ac21_row1_allowed_shapes[git diff {base}..{head}]` | Each signed read-only shape in row 1 is allowed, exactly exit 0. |
| `tests/acceptance-a11/test_lane1_hooks.py::test_ac21_row1_allowed_shapes[git log --oneline -n 5]` | Each signed read-only shape in row 1 is allowed, exactly exit 0. |
| `tests/acceptance-a11/test_lane1_hooks.py::test_ac21_row1_allowed_shapes[git show HEAD]` | Each signed read-only shape in row 1 is allowed, exactly exit 0. |
| `tests/acceptance-a11/test_lane1_hooks.py::test_ac21_row1_allowed_shapes[git rev-parse HEAD]` | Each signed read-only shape in row 1 is allowed, exactly exit 0. |
| `tests/acceptance-a11/test_lane1_hooks.py::test_ac21_row1_allowed_shapes[git branch --list]` | Each signed read-only shape in row 1 is allowed, exactly exit 0. |
| `tests/acceptance-a11/test_lane1_hooks.py::test_ac23_retry_cap[3]` | Row 3 expects the call after success to be allowed, exactly exit 0. |
| `tests/acceptance-a11/test_lane1_hooks.py::test_ac31_installed_scanner[2]` | Row 2 expects a clean commit to succeed, exactly exit 0. |
| `tests/acceptance-a11/test_lane1_hooks.py::test_ac53_row2_large_acked_store` | Row 2 expects no notice and completion within 1 second; the empty stub returns immediately. |
| `tests/acceptance-a11/test_lane1_hooks.py::test_ac64_freeze_file_operations[2-new]` | The new-file example expects the commit to succeed, exactly exit 0. |
| `tests/acceptance-a11/test_lane1_hooks.py::test_ac68_workflow_permission[2]` | The signed PID-permission examples expect the commit to succeed, exactly exit 0. |
| `tests/acceptance-a11/test_lane1_hooks.py::test_ac68_row3_hook_permission[True-.husky/pre-commit]` | The signed PID-permission examples expect the commit to succeed, exactly exit 0. |
| `tests/acceptance-a11/test_lane1_hooks.py::test_ac68_row3_hook_permission[True-.githooks/pre-commit]` | The signed PID-permission examples expect the commit to succeed, exactly exit 0. |
| `tests/acceptance-a11/test_lane1_hooks.py::test_ac68_row3_hook_permission[True-.pre-commit-config.yaml]` | The signed PID-permission examples expect the commit to succeed, exactly exit 0. |
| `tests/acceptance-a11/test_lane1_hooks.py::test_ac68_row3_hook_permission[True-configured-hooks/pre-commit]` | The signed PID-permission examples expect the commit to succeed, exactly exit 0. |
| `tests/acceptance-a11/test_lane1_hooks.py::test_ac70_trailer_case_and_policy[2]` | Row 2 expects the commit with no trailers policy to succeed, exactly exit 0. |
| `tests/acceptance-a11/test_lane1_hooks.py::test_ac78_retry_identity_and_redaction[2]` | Row 2 expects the call in the different cwd to be allowed, exactly exit 0. |
| `tests/acceptance-a11/test_lane1_hooks.py::test_ac93_row1_only_staged_content` | Row 1 expects the staged clean-file commit to succeed, exactly exit 0. |
| `tests/acceptance-a11/test_lane2_conductor.py::test_ac29_ack_work_consumption[1]` | Row 1 expects the commit call to be allowed with no notice, exactly exit 0 and None. |
| `tests/acceptance-a11/test_lane2_conductor.py::test_ac80_needs_l1[2]` | Row 2 expects no needs-L1 alert before the window, exactly the empty alert list. |
| `tests/acceptance-a11/test_lane3_launcher.py::test_ac04_git_config_refusal[2]` | The signed row expects exit 0. |
| `tests/acceptance-a11/test_lane3_launcher.py::test_ac04_git_config_refusal[3]` | The signed row expects exit 0. |
| `tests/acceptance-a11/test_lane3_launcher.py::test_ac34_agreement_bump[1]` | Row 1 expects exit 0 and no ygg agents refusal text, matching empty stderr. |
| `tests/acceptance-a11/test_lane3_launcher.py::test_ac36_t3_live_thread[3]` | Row 3 expects no live-thread send, exactly the empty call list. |
| `tests/acceptance-a11/test_lane3_launcher.py::test_ac41_unsafe_helper_values[2-core.fsmonitor-false]` | Row 2 expects exit 0 for core.fsmonitor false. |
| `tests/acceptance-a11/test_lane3_launcher.py::test_ac42_minimal_role_rules[2]` | Row 2 expects the team lead commit tool call to be allowed, exactly exit 0. |
| `tests/acceptance-a11/test_lane3_launcher.py::test_ac71_stop_only_as_far_as_needed[2]` | Row 2 expects exit 0 and no further signals, exactly the empty stub result. |
| `tests/acceptance-a11/test_lane4_modules.py::test_ac06_alert_classes[2]` | The signed busy example expects no ygg-stall alert, exactly the empty alert list. |
| `tests/acceptance-a11/test_lane4_modules.py::test_ac86_fallback_and_hang[2-False]` | Row 2 expects no T3, macOS or phone call for the log alert, exactly an empty list. |
| `tests/acceptance-a11/test_lane4_modules.py::test_ac87_row2_backoff_abandonment[True]` | The jsonl example expects no external notification calls, exactly an empty list. |
