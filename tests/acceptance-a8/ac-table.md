| AC-ID | test name | status |
|---|---|---|
| AC-01 | test_every_supervisor_write_fault_preserves_check_h_and_one_safe_outcome | covered |
| AC-02 | test_unrecorded_exit_is_held_until_user_release | covered |
| AC-02 | test_unrecorded_live_agent_is_adopted_and_stopped | covered |
| AC-03 | test_recovery_aborts_before_gate_and_launches_one_successor | covered |
| AC-03 | test_recovery_keeps_spawned_reservation_before_final_check | covered |
| AC-04 | test_pause_before_final_check_blocks_until_resume | covered |
| AC-04 | test_stop_before_final_check_aborts_without_starting_agent | covered |
| AC-04 | test_stop_returns_while_journal_locked_then_reconciles_intent | covered |
| AC-05 | test_reused_pid_is_held_and_unrelated_sleep_survives | covered |
| AC-06 | test_unknown_probe_holds_then_live_evidence_adopts | covered |
| AC-07 | test_clock_jump_keeps_dead_spawned_activation_uncertain_and_not_aborted | covered |
| AC-08 | test_capacity_deadline_expires_without_another_journal_event | covered |
| AC-08 | test_checkpoint_batch_deadline_wakes_pm_without_all_lanes | covered |
| AC-09 | test_offline_pause_blocks_pending_work | covered |
| AC-09 | test_offline_stop_kills_agent_and_blocks_relaunch | covered |
| AC-09 | test_offline_stop_survives_full_state_wipe_and_is_journalled | covered |
| AC-10 | test_tick_completes_stop_that_crashed_before_signalling | covered |
| AC-11 | test_interrupted_resume_blocks_until_tick_then_launches_once | covered |
| AC-11 | test_old_resume_cannot_clear_new_offline_pause | covered |
| AC-12 | test_every_launch_write_fault_has_idempotent_rehydrate | covered |
