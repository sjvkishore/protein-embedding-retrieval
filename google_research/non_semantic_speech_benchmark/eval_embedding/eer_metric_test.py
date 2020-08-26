# coding=utf-8
# Copyright 2020 The Google Research Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# Lint as: python3
"""Tests for utilities defined in eer_metric.py."""

from absl.testing import absltest
from absl.testing import parameterized

from non_semantic_speech_benchmark.eval_embedding import eer_metric


class EERMetric(parameterized.TestCase):

  def testCalculateEer(self):
    self.assertEqual(
        eer_metric.calculate_eer([0.1, 0.2, 0.8, 0.9], [0, 0, 1, 1]), 0)
    self.assertEqual(
        eer_metric.calculate_eer([0.9, 0.8, 0.2, 0.1], [0, 0, 1, 1]), 1)
    self.assertEqual(
        eer_metric.calculate_eer([0.1, 0.8, 0.2, 0.9], [0, 0, 1, 1]), 0.5)

  @parameterized.named_parameters(
      ('Perfect scores', [0.1, 0.2, 0.8, 0.9], [0, 0, 1, 1], [1, 0.5, 0, 0, 0],
       [0, 0, 0, 0.5, 1.0]),
      ('Perfectly wrong', [0.9, 0.8, 0.2, 0.1], [0, 0, 1, 1], [1, 1, 1, 0.5, 0],
       [0, 0.5, 1, 1, 1]),
      ('Fifty-fifty', [0.1, 0.8, 0.2, 0.9], [0, 0, 1, 1], [1, 0.5, 0.5, 0, 0],
       [0, 0, 0.5, 0.5, 1]),
  )
  def testCalculateDetCurve(self, scores, labels, expected_fpr, expected_fnr):
    fpr, fnr = eer_metric.calculate_det_curve(scores, labels)
    fpr = list(fpr)
    fnr = list(fnr)
    self.assertEqual(fpr, expected_fpr)
    self.assertEqual(fnr, expected_fnr)


if __name__ == '__main__':
  absltest.main()
