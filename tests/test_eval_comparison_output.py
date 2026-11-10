from cassis_cli.eval import _print_results_table


def test_passed_result_still_prints_explicit_unapplied_rules(capsys):
    _print_results_table(
        [
            {
                "question": "Count?",
                "status": "passed",
                "comparison": {"numeric": "exact"},
                "comparison_applied": False,
                "comparison_note": "Not applied: SQL results were not executed",
            }
        ]
    )
    output = capsys.readouterr().out
    assert "exact numbers" in output
    assert (
        "Result comparison not applied: SQL results were not executed (metadata-only source: the judge decided)"
        in output
    )


def test_unapplied_note_prints_for_a_default_rule_case(capsys):
    _print_results_table(
        [
            {
                "question": "Count?",
                "status": "passed",
                "comparison": {"numeric": "legacy", "ordered": False, "columns": "automatic", "allow_empty": False},
                "comparison_applied": False,
                "comparison_note": "Not applied: SQL results were not executed",
            }
        ]
    )
    output = capsys.readouterr().out
    assert (
        "Result comparison not applied: SQL results were not executed (metadata-only source: the judge decided)"
        in output
    )
    assert "default rules" not in output


def test_unapplied_note_names_what_was_not_applied_and_why(capsys):
    _print_results_table(
        [
            {
                "question": "A?",
                "status": "failed",
                "comparison_applied": False,
                "comparison_note": "Not applied: SQL results were not compared",
            },
            {
                "question": "B?",
                "status": "passed",
                "comparison_applied": None,
                "comparison_note": "Unknown: execution mode was not recorded",
            },
        ]
    )
    output = capsys.readouterr().out
    assert "Result comparison not applied: SQL results were not compared\n" in output
    assert "Not applied: SQL" not in output
    # Only an unexecuted result says the judge decided; an unknown mode prints as sent.
    assert "metadata-only" not in output
    assert "      Unknown: execution mode was not recorded" in output


def test_applied_default_rules_say_so_and_name_the_deciding_rule(capsys):
    _print_results_table(
        [
            {
                "question": "Count?",
                "status": "passed",
                "comparison": {"numeric": "legacy", "ordered": False, "columns": "automatic", "allow_empty": False},
                "comparison_applied": True,
                "comparison_note": None,
                "comparison_rule": "legacy_tolerance",
            }
        ]
    )
    output = capsys.readouterr().out
    assert (
        "Comparison: default rules (1% numeric tolerance, unordered rows, columns by value, empty rejected)" in output
    )
    assert "Decided by: legacy 1% tolerance" in output


def test_failed_and_empty_rules_read_in_words(capsys):
    _print_results_table(
        [
            {"question": "A?", "status": "failed", "comparison_rule": "row_order"},
            {"question": "B?", "status": "passed", "comparison_rule": "empty_allowed"},
        ]
    )
    output = capsys.readouterr().out
    assert "Failed on row order" in output
    assert "Empty result accepted" in output


def test_older_server_results_print_no_comparison_line(capsys):
    _print_results_table([{"question": "A?", "status": "passed"}])
    assert "Comparison" not in capsys.readouterr().out
