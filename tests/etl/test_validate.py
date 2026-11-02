import copy
import json
import unittest
from pathlib import Path

from etl.validate import validate_index

TEST_DATA = Path(__file__).parent / "test_data"
REAL_INDEX = json.loads((TEST_DATA / "2026-09-28_fidelis_index.json").read_text())


class ValidateIndexTest(unittest.TestCase):
    def test_real_index_is_valid(self):
        self.assertEqual(validate_index(REAL_INDEX), [])

    def test_missing_plan_id_is_reported_with_its_path(self):
        index = copy.deepcopy(REAL_INDEX)
        del index["reporting_structure"][0]["reporting_plans"][3]["plan_id"]
        self.assertEqual(
            validate_index(index),
            ["reporting_structure[0] → reporting_plans[3]: 'plan_id' is a required property"],
        )

    def test_reporting_structure_as_a_string(self):
        index = copy.deepcopy(REAL_INDEX)
        index["reporting_structure"] = "not a list"
        problems = validate_index(index)
        self.assertEqual(len(problems), 1)
        self.assertTrue(problems[0].startswith("reporting_structure: "), problems)
        self.assertIn("is not of type 'array'", problems[0])

    def test_problems_are_sorted_by_path_and_top_level_has_no_path(self):
        index = copy.deepcopy(REAL_INDEX)
        del index["version"]
        del index["reporting_structure"][1]["reporting_plans"][0]["plan_name"]
        del index["reporting_structure"][0]["reporting_plans"][10]["issuer_name"]
        self.assertEqual(
            validate_index(index),
            [
                "'version' is a required property",
                "reporting_structure[0] → reporting_plans[10]: 'issuer_name' is a required property",
                "reporting_structure[1] → reporting_plans[0]: 'plan_name' is a required property",
            ],
        )

    def test_a_long_value_in_a_message_is_shortened(self):
        index = copy.deepcopy(REAL_INDEX)
        plans = index["reporting_structure"][0]["reporting_plans"]
        plans.append(dict(plans[0]))
        (problem,) = validate_index(index)
        self.assertTrue(problem.startswith("reporting_structure[0] → reporting_plans: [{'plan_name'"), problem)
        self.assertTrue(problem.endswith("… has non-unique elements"), problem)
        self.assertLess(len(problem), 200)


if __name__ == "__main__":
    unittest.main()
