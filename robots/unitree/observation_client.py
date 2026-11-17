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

"""Read capture-aligned Go2 camera observations from the existing stream service."""

from __future__ import annotations

import base64
import binascii
from typing import Any

from robots.unitree.http_client import (
    JsonServiceClient,
    ServiceError,
    finite_number,
    map_identity,
)


class ObservationClient(JsonServiceClient):
    """Borrow JPEG frames and their capture-time body poses, without controlling Go2."""

    def observe(self) -> dict[str, Any]:
        """Return one fresh JPEG and the timestamp/pose captured with that frame."""
        result = self.request("GET", "/observation")
        map_identity(result)
        if type(result.get("stamp_ns")) is not int or result["stamp_ns"] <= 0:
            raise ServiceError("observation.stamp_ns must be a positive Unix timestamp")
        pose = result.get("base_pose")
        if not isinstance(pose, dict):
            raise ServiceError("observation.base_pose is required")
        for key in ("x", "y", "yaw"):
            finite_number(pose.get(key), f"base_pose.{key}")
        image = result.get("image")
        if not isinstance(image, dict) or image.get("encoding") != "jpeg":
            raise ServiceError("observation must contain a JPEG image")
        try:
            if not isinstance(image.get("data"), str) or not base64.b64decode(
                image["data"], validate=True
            ):
                raise ValueError("empty image")
        except (ValueError, binascii.Error) as exc:
            raise ServiceError(
                "observation.image.data must contain base64 JPEG bytes"
            ) from exc
        return result
