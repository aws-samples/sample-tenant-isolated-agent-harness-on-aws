# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The answer shown to the user drops the model's inline <thinking> blocks."""

from worker.handler import strip_reasoning


def test_inline_reasoning_is_removed_from_the_answer():
    text = "<thinking>The tool returned two jobs.</thinking>  Here are the open jobs: J7 and J9."
    assert strip_reasoning(text) == "Here are the open jobs: J7 and J9."


def test_text_without_reasoning_is_unchanged_and_reasoning_only_text_is_kept():
    assert strip_reasoning("  Two jobs are open. ") == "Two jobs are open."
    assert strip_reasoning("<thinking>only this</thinking>") == "<thinking>only this</thinking>"
