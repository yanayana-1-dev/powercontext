# Copyright (c) 2026 OceanBase.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from __future__ import annotations

from typing import Any, cast

import pytest
from pydantic import ValidationError

from powercontext.builtin.runtime.artifact_processing import ArtifactProcessingBinding
from powercontext.builtin.runtime.config import RuntimeConfig

THRESHOLDS = ("memory_source_count_threshold", "topic_memory_source_count_threshold")


@pytest.mark.parametrize("field", THRESHOLDS)
def test_source_count_thresholds_default_off_and_accept_positive_integers(field: str) -> None:
    assert field in RuntimeConfig.model_fields
    assert getattr(RuntimeConfig(), field) is None
    for value in (None, 1, 3, 100):
        assert getattr(RuntimeConfig(**{field: value}), field) == value


@pytest.mark.parametrize("field", THRESHOLDS)
@pytest.mark.parametrize("value", [True, False, 0, -1, 1.0, "3"])
def test_source_count_thresholds_reject_non_positive_or_non_integer_values(field: str, value: object) -> None:
    with pytest.raises(ValidationError):
        RuntimeConfig(**{field: value})  # type: ignore[arg-type]


def _binding(**kwargs: Any) -> ArtifactProcessingBinding:
    return ArtifactProcessingBinding(
        binding_name="memory-source-window",
        artifact_family="memory",
        launcher=cast(Any, None),
        **kwargs,
    )


def test_binding_source_count_threshold_is_optional_positive_integer() -> None:
    assert _binding().automatic_source_count_threshold is None
    assert _binding(automatic_source_count_threshold=2).automatic_source_count_threshold == 2
    for value in (0, -1, True, 1.0):
        with pytest.raises(ValueError):
            _binding(automatic_source_count_threshold=value)
