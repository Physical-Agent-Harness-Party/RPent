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

"""Keep rendered navigation guidance aligned with configured capabilities."""

import pytest

from robots.unitree.robot_spec import get_robot_spec


@pytest.mark.parametrize("visual", [False, True])
@pytest.mark.parametrize("frontend", [False, True])
def test_prompt_exposes_only_configured_navigation_tools(visual, frontend):
    variables = {"unitree_mode": "mujoco"}
    if visual:
        variables["vln_endpoint"] = "ws://vln.test"
    if frontend:
        variables["frontend_endpoint"] = "http://frontend.test"
    prompt = get_robot_spec().prompts.render("system", variables=variables)
    assert "plan_navigation" in prompt and "follow_navigation" in prompt
    for name in ("run_vln_navigation", "get_vln_status", "stop_vln_navigation"):
        assert (name in prompt) is visual
    assert ("register_navigation_subtasks" in prompt) is frontend
    assert "dis_move" not in prompt
    assert "Every task must end with the finish tool" in prompt


@pytest.mark.parametrize("limit,local", [(12.5, "12.5"), (120.0, "30")])
def test_prompt_search_budget_respects_the_configured_vln_limit(limit, local):
    prompt = get_robot_spec().prompts.render(
        "system",
        variables={
            "unitree_mode": "mujoco",
            "vln_endpoint": "ws://vln.test",
            "navigation_timeout_s": 87.5,
            "vln_timeout_s": limit,
        },
    )
    assert "configured timeout of 87.5 seconds" in prompt
    assert f"normally {local} seconds" in prompt
    assert f"configured VLN maximum is {limit} seconds" in prompt
