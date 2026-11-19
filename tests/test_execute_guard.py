# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Scripts that name a directory variable without its $ are refused with the fix (seen live on Nova Lite)."""

from worker.execute_process import bare_directory_names

LIVE_SCRIPT = "awk -F',' '{print $1, $2, $3}' INPUTS_DIR/prices.csv | column -t | awk 'NR>1 {total+=$3} END {print total}'"


def test_the_script_from_the_live_demo_is_caught():
    assert bare_directory_names(LIVE_SCRIPT) == ["INPUTS_DIR"]


def test_correct_variable_use_passes():
    assert bare_directory_names('awk -F, \'NR>1 {t+=$3} END {print t}\' "$INPUTS_DIR/prices.csv"') == []
    assert bare_directory_names('cp out.csv "${WORKSPACE_DIR}/out.csv"; ls $TRANSIENT_DIR/tmp') == []


def test_several_bare_names_are_all_reported():
    assert bare_directory_names("cat INPUTS_DIR/a > TRANSIENT_DIR/b; cp TRANSIENT_DIR/b WORKSPACE_DIR/c") == [
        "INPUTS_DIR", "TRANSIENT_DIR", "WORKSPACE_DIR"]


def test_the_model_is_told_transient_files_persist_and_home_is_new():
    from worker import agent, execute_process
    for text in (agent.SYSTEM_PROMPT, execute_process.DESCRIPTION):
        assert "persists between scripts" in text and "new subdirectory" in text
        assert "HOME and" in text and "TMPDIR are new" in text
        assert "internet" not in text.lower()  # the old "no internet access" claim is gone
        assert not bare_directory_names(text)  # the example it gives passes the guard
