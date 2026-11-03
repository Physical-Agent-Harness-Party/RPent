# Copyright 2026 The RPent Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Task prompts for the Unitree command-delivery prototype."""

from __future__ import annotations

from collections.abc import Mapping

from rpent.prompt.utils import PromptNode


def system_prompt(variables: Mapping[str, object] | None = None) -> PromptNode:
    """Describe the movement tools and the receipt-based completion criterion."""
    return """You send Unitree commands through the available tools.
Use move_forward for forward motion (前进) and move_backward for backward motion
(后退). Each call sends one command. Follow the requested order and call each
requested action once unless the task explicitly asks for repetition.
Completion means the server acknowledged the requested command strings.
A receipt does not verify physical movement; the local mock server records only.
After receiving all requested acknowledgements, call finish with status=success
and summarize the delivered commands. If a call fails, report the error and
finish with status=failure without automatically resending an uncertain command.
For tasks requiring other robot capabilities, finish with status=stuck and explain
the missing capability. No reference memory or camera images are needed."""


def user_prompt(variables: Mapping[str, object] | None = None) -> PromptNode:
    """Pass the CLI task text to the planner."""
    return {"TASK": "{{task}}", "COMMAND SERVER": "{{env_endpoint}}"}
