from pathlib import Path

from django.test import SimpleTestCase

from .services.schedule_generator.configuration import load_scheduler_settings
from .services.schedule_generator.input_models import load_school_input_from_yaml
from .services.schedule_generator.sanpin_validator import SanPinValidator


class HybridSupportTests(SimpleTestCase):
    def test_yaml_input_loader_validates_school_payload(self):
        payload_path = Path(__file__).resolve().parent / 'testdata' / 'school_input.yaml'
        model = load_school_input_from_yaml(payload_path)
        self.assertEqual(model.school.name, 'Demo School')
        self.assertEqual(model.classes[0].weekly_subject_hours['Mathematics'], 5)

    def test_sanpin_validator_uses_difficulty_tables(self):
        settings = load_scheduler_settings()
        validator = SanPinValidator(settings.school, settings.sanpin)
        self.assertEqual(validator.difficulty_score('Physics', 9), 13)
        self.assertEqual(validator.difficulty_score('Mathematics', 5), 10)
