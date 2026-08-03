from pathlib import Path
import re
import unittest


class WorkflowPinTests(unittest.TestCase):
    def test_setup_node_is_immutable_and_node24_compatible(self):
        workflow = (Path(__file__).parents[1] / ".github" / "workflows" / "ci.yml").read_text(
            encoding="utf-8"
        )
        matches = re.findall(r"actions/setup-node@([^\s#]+)", workflow)
        self.assertEqual(matches, ["820762786026740c76f36085b0efc47a31fe5020"])
        self.assertIn(
            "actions/setup-node@820762786026740c76f36085b0efc47a31fe5020 # v7.0.0",
            workflow,
        )


if __name__ == "__main__":
    unittest.main()
