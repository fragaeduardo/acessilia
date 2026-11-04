"""Stage 1 module-boundary and legacy-entrypoint compatibility checks.

Existing callers import these functions from pddl_orchestrator. Moving code
must not silently change their function identity or handler registration.
"""

from backend.agents import pddl_orchestrator as legacy
from backend.core.execution import recovery
from backend.core.execution.handlers import common, deterministic, mathematics
from backend.core.manifest import element_geometry, payload, payload_lists, payload_text, table_reading
from backend.core.manifest.preprocessing import media_triage, page_limits, visual_signals
from backend.core.planning import coordinator


def test_manifest_compatibility_exports_are_actual_extracted_implementations():
    assert legacy.build_pddl_structured_payload is payload.build_pddl_structured_payload
    assert legacy.records_visual_failures is payload.records_visual_failures
    assert legacy._element_to_block is payload._element_to_block
    assert legacy._recover_empty_lettered_list_items is payload_lists._recover_empty_lettered_list_items
    assert legacy._merge_list_blocks is payload_lists._merge_list_blocks
    assert legacy._render_text_from_blocks is payload_text._render_text_from_blocks
    assert legacy._normalize_metadata_links is payload_text._normalize_metadata_links
    assert legacy._rows_from_visual_table_text is table_reading._rows_from_visual_table_text
    assert legacy._table_ast_from_metadata is table_reading._table_ast_from_metadata
    assert legacy._element_bbox is element_geometry._element_bbox
    assert legacy._element_role is element_geometry._element_role


def test_recovery_exports_are_still_usable_by_orchestrator_callers():
    assert legacy._blocks_delivery is recovery._blocks_delivery
    assert legacy._replannable_obligation_ids is recovery._replannable_obligation_ids
    assert legacy._unsatisfied_summary is recovery._unsatisfied_summary
    assert legacy.NON_CRITICAL_OBLIGATION_KINDS is recovery.NON_CRITICAL_OBLIGATION_KINDS


def test_handlers_share_one_implementation_with_original_registry_entrypoints():
    assert legacy._target_elements is common._target_elements
    assert legacy._missing_obligation is common._missing_obligation
    assert legacy._handle_mathml_method is mathematics._handle_mathml_method
    assert legacy._handle_latex_verbalizer_method is mathematics._handle_latex_verbalizer_method
    assert legacy._handle_pandoc_table_method is deterministic._handle_pandoc_table_method
    assert legacy._handle_norm_fix_method is deterministic._handle_norm_fix_method
    assert legacy._handle_human_review_method is common._handle_human_review_method
    assert legacy._handle_deterministic_heading_repair_method is deterministic._handle_deterministic_heading_repair_method


def test_preprocessing_exports_keep_original_contracts():
    assert legacy._inspect_media is media_triage._inspect_media
    assert legacy._is_presentation is media_triage._is_presentation
    assert legacy._inject_full_page_picture is media_triage._inject_full_page_picture
    assert legacy._has_meaningful_text is visual_signals._has_meaningful_text


def test_media_triage_handles_missing_genre_without_speculative_classification():
    assert media_triage._is_presentation(None) is False
    assert media_triage._is_presentation({}) is False
    assert media_triage._is_presentation({"document_genre": "document"}) is False
    assert media_triage._is_presentation({"document_genre": "presentation"}) is True


def test_coordinator_is_the_single_orchestration_planning_entrypoint():
    assert legacy.build_nominal_plan is coordinator.build_nominal_plan
    assert legacy.unavailable_methods is coordinator.unavailable_methods


def test_page_limit_failures_remain_the_same_exception_class():
    assert legacy.DocumentTooLargeError is page_limits.DocumentTooLargeError
    assert legacy.StructuralExtractionError is page_limits.StructuralExtractionError
    assert legacy._check_page_limit_before_extraction is page_limits._check_page_limit_before_extraction


def test_unverified_preflight_page_count_does_not_reject_document():
    # Actual extractors remain authoritative for non-verified PDF counts.
    page_limits._check_page_limit_before_extraction({
        "is_pdf": True, "page_count": 999999,
        "page_count_source": "unverified",
    })
    page_limits._check_page_limit_before_extraction(None)
